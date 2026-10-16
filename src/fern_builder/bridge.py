"""Bridge: fern's voice talks to Claude Code on this Mac, the way the VS Code chat does.

Claude works in the project folder with the user's own login, `gh`, permission rules, memories and CLAUDE.md, and
continues their latest conversation itself (the same session file VS Code uses). Whatever their settings
would ask about is asked through fern instead: approval card on screen and "approve?" by voice. Built on the
Claude Agent SDK, the same engine IDE integrations use.

POST /messages {text}           one turn, streamed as NDJSON: progress, text, permission, question, done, ping
POST /permissions/{id}          {decision: allow | always | deny}
POST /questions/{id}            {answers: {question: label}}
POST /interrupt                 stop the current turn
POST /new                       the next message starts a fresh conversation
GET  /runs                      what Claude Code reported for recent runs (input, actions, output, status)
GET  /runs/stream               the same, live, as NDJSON: a snapshot, then every change, with pings
GET  /sessions                  recent conversations in this project, newest first
POST /sessions/{id}/open        the next message continues that conversation
GET  /healthz
"""

import argparse
import asyncio
import json
import logging
import secrets
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, Literal

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    list_sessions,
)
from claude_agent_sdk.types import HookJSONOutput
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from fern_builder.config import BuilderSettings, builder_settings
from fern_builder.runs import HISTORY, TEXT_LIMIT, RunLog, clip

log = logging.getLogger("fern_builder")
VOICE_NOTE = (
    "The user is talking to you by voice through fern, their household assistant. Your replies appear on a "
    "screen and a short summary is read aloud: start each final reply with one or two plain sentences that sum it "
    "up (no markdown), then any details. Ask for decisions with AskUserQuestion. Their words come from speech "
    "recognition, so read past small transcription errors."
)
PING_SECONDS = 15  # the egress proxy closes connections after 60 s of silence
IDLE_SECONDS = 20 * 60  # an idle conversation closes; the next message continues the latest one again
KEY_ARGUMENT = {
    "Bash": "command", "Read": "file_path", "Edit": "file_path", "Write": "file_path", "NotebookEdit": "notebook_path",
    "Grep": "pattern", "Glob": "pattern", "WebSearch": "query", "WebFetch": "url", "Agent": "description",
}  # fmt: skip
Decision = Literal["allow", "always", "deny"]


class ChatBusyError(Exception):
    pass


class ClaudeChat:
    """One live Claude Code conversation. The SDK client must connect and disconnect in the same task, so a single
    worker task owns it; HTTP handlers hand it messages and read the running turn's events."""

    def __init__(self, settings: BuilderSettings, client_factory: Callable[..., Any] = ClaudeSDKClient):
        self.settings = settings
        self.client_factory = client_factory
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.events: asyncio.Queue[dict] | None = None  # the running turn's stream; None when idle
        self.pending: dict[str, tuple[asyncio.Future, str, dict, ToolPermissionContext]] = {}
        self.client: Any = None
        self.worker: asyncio.Task | None = None
        self.fresh = False  # set by /new: don't continue the latest conversation
        self.chosen_session: str | None = None  # set by /sessions/{id}/open: continue that one instead
        self.session_id: str | None = None  # the conversation Claude is in
        self.runs = RunLog(settings.runs_file)
        self.run: dict | None = None  # the run in progress, as Claude Code reports it

    def options(self) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            cwd=self.settings.project_dir,
            setting_sources=["user", "project", "local"],  # the user's own rules, like VS Code
            resume=self.chosen_session,
            continue_conversation=not (self.fresh or self.chosen_session),  # the latest one itself, not a copy
            can_use_tool=self._ask_user,
            hooks={"UserPromptSubmit": [HookMatcher(hooks=[self._on_prompt])]},  # reports transcript_path
            system_prompt={"type": "preset", "preset": "claude_code", "append": VOICE_NOTE},
        )

    def describe(self, tool_name: str, tool_input: dict) -> str:
        detail = str(tool_input.get(KEY_ARGUMENT.get(tool_name, ""), "") or "")
        detail = detail.removeprefix(f"{self.settings.project_dir}/")
        return f"{tool_name}: {detail[:200]}" if detail else tool_name

    async def start(self, text: str) -> None:
        if self.events is not None:
            raise ChatBusyError
        self.events = asyncio.Queue()
        self.run = self.runs.start(text)
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._run())
        await self.inbox.put(text)

    async def stream(self) -> AsyncGenerator[dict]:
        """The running turn's events, with pings while Claude works quietly. Ends after `done` or `error`."""
        events = self.events
        if events is None:
            return
        try:
            while True:
                try:
                    event = await asyncio.wait_for(events.get(), PING_SECONDS)
                except TimeoutError:
                    yield {"type": "ping"}
                    continue
                yield event
                if event["type"] in ("done", "error"):
                    return
        finally:
            self.events = None

    async def _emit(self, event: dict) -> None:
        if self.events is not None:
            await self.events.put(event)

    async def _run(self) -> None:
        try:
            async with self.client_factory(self.options()) as client:
                self.client, self.fresh, self.chosen_session = client, False, None
                while (text := await self._next_message()) is not None:
                    await client.query(text)
                    async for message in client.receive_response():
                        await self._publish(message)
        except Exception as error:
            reference = secrets.token_hex(3)
            log.exception("claude session failed [ref %s]", reference)
            if self.run is not None:
                self.runs.finish(
                    self.run, "error", errors=[f"{type(error).__name__} (fern-bridge log ref {reference})"]
                )
            await self._emit({"type": "error", "message": type(error).__name__, "reference": reference})
        finally:
            self.client = None
            self._deny_pending("The conversation ended.")
            if not self.inbox.empty():  # a message arrived while this session was closing: serve it in a new one
                self.worker = asyncio.create_task(self._run())

    async def _next_message(self) -> str | None:
        try:
            return await asyncio.wait_for(self.inbox.get(), IDLE_SECONDS)
        except TimeoutError:
            return None

    async def _on_prompt(self, hook_input: Any, _tool_use_id: str | None, _context: Any) -> HookJSONOutput:
        """Claude Code's UserPromptSubmit hook: it reports where it keeps this session's transcript."""
        if self.run is not None:
            reported = {key: hook_input.get(key) for key in ("transcript_path", "session_id", "cwd", "permission_mode")}
            self.runs.update(self.run, **reported)
        return {}

    def _record(self, message: Any) -> None:
        """Add what this SDK message reports to the current run (for the activity panel)."""
        run = self.run
        if run is None:
            return
        if isinstance(message, SystemMessage) and message.subtype == "init":
            data = message.data
            self.runs.update(
                run, session_id=data.get("session_id"), model=data.get("model"), cwd=data.get("cwd"),
                permission_mode=data.get("permissionMode"), api_key_source=data.get("apiKeySource"),
            )  # fmt: skip
        elif isinstance(message, AssistantMessage):
            parent = message.parent_tool_use_id  # set when a subagent (Agent tool) produced this message
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    self.runs.add_text(run, "text", block.text, parent)
                elif isinstance(block, ThinkingBlock) and block.thinking.strip():
                    self.runs.add_text(run, "thinking", block.thinking, parent)
                elif isinstance(block, ToolUseBlock):
                    self.runs.add_tool(run, block.id, block.name, block.input, parent)
        elif isinstance(message, UserMessage) and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    self.runs.tool_result(run, block.tool_use_id, block.content, block.is_error)
        elif isinstance(message, ResultMessage):
            self.runs.finish(
                run, "error" if message.is_error else "done", output=clip(message.result or ""),
                result_subtype=message.subtype, session_id=message.session_id, num_turns=message.num_turns,
                duration_ms=message.duration_ms, duration_api_ms=message.duration_api_ms,
                cost_usd=message.total_cost_usd, usage=message.usage, errors=message.errors,
                permission_denials=message.permission_denials,
            )  # fmt: skip

    async def _publish(self, message: Any) -> None:
        self._record(message)
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    await self._emit({"type": "text", "markdown": block.text})
                elif isinstance(block, ToolUseBlock) and block.name != "AskUserQuestion":
                    await self._emit({"type": "progress", "text": self.describe(block.name, block.input)})
        elif isinstance(message, ResultMessage):
            self.session_id = message.session_id
            done = {"reply": message.result or "", "is_error": message.is_error, "cost_usd": message.total_cost_usd}
            await self._emit({"type": "done", **done})

    async def _ask_user(self, tool_name: str, tool_input: dict, context: ToolPermissionContext) -> Any:
        """Called by the SDK whenever the user's settings would ask: forward to fern and wait for the answer."""
        request_id = uuid.uuid4().hex
        answer = asyncio.get_running_loop().create_future()
        self.pending[request_id] = (answer, tool_name, tool_input, context)
        if tool_name == "AskUserQuestion":
            questions = [
                {
                    "question": question["question"],
                    "options": [option["label"] for option in question.get("options", [])],
                    "descriptions": [option.get("description", "") for option in question.get("options", [])],
                    "multi_select": bool(question.get("multiSelect")),
                }
                for question in tool_input.get("questions", [])
            ]
            await self._emit({"type": "question", "id": request_id, "questions": questions})
        else:
            detail = self.describe(tool_name, tool_input)
            await self._emit({"type": "permission", "id": request_id, "tool": tool_name, "detail": detail})
        run, tool_use_id = self.run, getattr(context, "tool_use_id", None)
        if run is not None:
            self.runs.tool_status(run, tool_use_id, "waiting_for_you")
            self.runs.update(run, status="waiting_for_you")
        try:
            decision = await asyncio.wait_for(answer, self.settings.approval_timeout_seconds)
        except TimeoutError:
            decision = PermissionResultDeny(message="The user didn't answer in time.")
        finally:
            self.pending.pop(request_id, None)
        if run is not None:
            self.runs.tool_status(run, tool_use_id, "denied" if decision.behavior == "deny" else "running")
            if run["status"] == "waiting_for_you":
                self.runs.update(run, status="running")
        return decision

    def _take(self, request_id: str) -> tuple[asyncio.Future, str, dict, ToolPermissionContext]:
        if (entry := self.pending.get(request_id)) is None or entry[0].done():
            raise KeyError(request_id)
        return entry

    def decide(self, request_id: str, decision: Decision) -> None:
        answer, _tool_name, tool_input, context = self._take(request_id)
        if decision == "deny":
            answer.set_result(PermissionResultDeny(message="The user said no."))
            return
        # "always" saves the CLI's suggested rule to .claude/settings.local.json, like VS Code's "don't ask again".
        remember = [rule for rule in context.suggestions if rule.destination == "localSettings"]
        answer.set_result(
            PermissionResultAllow(
                updated_input=tool_input, updated_permissions=remember if decision == "always" else None
            )
        )

    def answer_questions(self, request_id: str, answers: dict[str, str | list[str]]) -> None:
        answer, tool_name, tool_input, _context = self._take(request_id)
        if tool_name != "AskUserQuestion":
            raise KeyError(request_id)
        answer.set_result(
            PermissionResultAllow(updated_input={"questions": tool_input.get("questions", []), "answers": answers})
        )

    def _deny_pending(self, reason: str) -> None:
        for answer, *_rest in list(self.pending.values()):
            if not answer.done():
                answer.set_result(PermissionResultDeny(message=reason, interrupt=True))

    async def interrupt(self) -> None:
        if self.run is not None and self.run["status"] in ("running", "waiting_for_you"):
            self.runs.update(self.run, status="stopping")
        self._deny_pending("The user stopped this.")
        if self.client is not None and self.events is not None:  # only a running turn can be stopped
            await self.client.interrupt()

    async def new_conversation(self) -> None:
        self.fresh, self.chosen_session = True, None
        await self._close_after_turn()

    def sessions(self, limit: int = 10) -> list[dict]:
        return [
            {
                "id": info.session_id,
                "title": info.custom_title or info.summary or info.first_prompt or "Untitled",
                "updated": info.last_modified // 1000,
                "current": info.session_id == self.session_id,
            }
            for info in list_sessions(directory=str(self.settings.project_dir), limit=limit)
        ]

    async def open_session(self, session_id: str) -> None:
        if session_id not in {session["id"] for session in self.sessions(limit=100)}:
            raise KeyError(session_id)
        self.fresh, self.chosen_session = False, session_id
        await self._close_after_turn()

    async def _close_after_turn(self) -> None:
        if self.worker is not None and not self.worker.done():
            await self.inbox.put(None)  # the next message opens the new choice


chat = ClaudeChat(builder_settings)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if not builder_settings.bridge_token:
        raise RuntimeError("Set FERN_BRIDGE_TOKEN in .env")
    yield
    if chat.worker is not None:
        chat.worker.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def caller(authorization: str = Header("")) -> None:
    expected = chat.settings.bridge_token
    if not expected or not secrets.compare_digest(authorization.removeprefix("Bearer "), expected):
        raise HTTPException(401)


class Message(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


class PermissionAnswer(BaseModel):
    decision: Decision


class QuestionAnswers(BaseModel):
    answers: dict[str, str | list[str]]


async def ndjson(events: AsyncIterator[dict]) -> AsyncIterator[str]:
    async for event in events:
        yield json.dumps(event) + "\n"


@app.post("/messages", dependencies=[Depends(caller)])
async def send_message(body: Message) -> StreamingResponse:
    try:
        await chat.start(body.text)
    except ChatBusyError:
        raise HTTPException(409, "Claude is still working on the last message") from None
    return StreamingResponse(ndjson(chat.stream()), media_type="application/x-ndjson")


@app.post("/permissions/{request_id}", dependencies=[Depends(caller)])
async def decide_permission(request_id: str, body: PermissionAnswer) -> dict:
    try:
        chat.decide(request_id, body.decision)
    except KeyError:
        raise HTTPException(404, "No such pending request") from None
    return {"ok": True}


@app.post("/questions/{request_id}", dependencies=[Depends(caller)])
async def answer_questions(request_id: str, body: QuestionAnswers) -> dict:
    try:
        chat.answer_questions(request_id, body.answers)
    except KeyError:
        raise HTTPException(404, "No such pending question") from None
    return {"ok": True}


@app.post("/interrupt", dependencies=[Depends(caller)])
async def interrupt() -> dict:
    await chat.interrupt()
    return {"ok": True}


@app.post("/new", dependencies=[Depends(caller)])
async def new_conversation() -> dict:
    await chat.new_conversation()
    return {"ok": True}


def runs_report() -> dict:
    return {
        "runs_on": "this Mac: fern-bridge drives Claude Code through the Claude Agent SDK",
        "run_log_file": str(chat.settings.runs_file),
        "limits": [
            "Anthropic's API returns no execution log; this shows what the Agent SDK and Claude Code's hooks report.",
            f"Text longer than {TEXT_LIMIT} characters is cut.",
            f"History keeps the last {HISTORY} runs.",
        ],
        "runs": chat.runs.newest_first(),
    }


@app.get("/runs", dependencies=[Depends(caller)])
async def runs() -> dict:
    return runs_report()


@app.get("/runs/stream", dependencies=[Depends(caller)])
async def runs_stream() -> StreamingResponse:
    async def updates() -> AsyncIterator[dict]:
        listener = chat.runs.subscribe()
        try:
            yield {"type": "snapshot", **runs_report()}
            while True:
                try:
                    yield {"type": "run", "run": await asyncio.wait_for(listener.get(), PING_SECONDS)}
                except TimeoutError:
                    yield {"type": "ping"}
        finally:
            chat.runs.unsubscribe(listener)

    return StreamingResponse(ndjson(updates()), media_type="application/x-ndjson")


@app.get("/sessions", dependencies=[Depends(caller)])
async def sessions() -> list[dict]:
    return chat.sessions()


@app.post("/sessions/{session_id}/open", dependencies=[Depends(caller)])
async def open_session(session_id: str) -> dict:
    try:
        await chat.open_session(session_id)
    except KeyError:
        raise HTTPException(404, "No such conversation in this project") from None
    return {"ok": True}


@app.get("/healthz")
async def healthz() -> dict:
    return {"bridge": True, "working": chat.events is not None}


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="fern-bridge")
    parser.add_argument("--host", default="127.0.0.1")  # this Mac only; Docker reaches it via host.docker.internal
    parser.add_argument("--port", type=int, default=8770)
    arguments = parser.parse_args()
    uvicorn.run("fern_builder.bridge:app", host=arguments.host, port=arguments.port)
