"""Bridge: fern's voice talks to Claude Code on this Mac, the way the VS Code chat does.

Claude works in the project folder with the user's own login, `gh`, permission rules, memories and CLAUDE.md, and
continues their latest conversation (as a fork, so an open VS Code chat isn't touched). Whatever their settings
would ask about is asked through fern instead: approval card on screen and "approve?" by voice. Built on the
Claude Agent SDK, the same engine IDE integrations use.

POST /messages {text}           one turn, streamed as NDJSON: progress, text, permission, question, done, ping
POST /permissions/{id}          {decision: allow | always | deny}
POST /questions/{id}            {answers: {question: label}}
POST /interrupt                 stop the current turn
POST /new                       the next message starts a fresh conversation
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
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
)
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from fern_builder.config import BuilderSettings, builder_settings

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

    def options(self) -> ClaudeAgentOptions:
        continue_latest = not self.fresh
        return ClaudeAgentOptions(
            cwd=self.settings.project_dir,
            setting_sources=["user", "project", "local"],  # the user's own rules, like VS Code
            continue_conversation=continue_latest,
            fork_session=continue_latest,
            can_use_tool=self._ask_user,
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
                self.client, self.fresh = client, False
                while (text := await self._next_message()) is not None:
                    await client.query(text)
                    async for message in client.receive_response():
                        await self._publish(message)
        except Exception as error:
            reference = secrets.token_hex(3)
            log.exception("claude session failed [ref %s]", reference)
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

    async def _publish(self, message: Any) -> None:
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    await self._emit({"type": "text", "markdown": block.text})
                elif isinstance(block, ToolUseBlock) and block.name != "AskUserQuestion":
                    await self._emit({"type": "progress", "text": self.describe(block.name, block.input)})
        elif isinstance(message, ResultMessage):
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
                    "multi_select": bool(question.get("multiSelect")),
                }
                for question in tool_input.get("questions", [])
            ]
            await self._emit({"type": "question", "id": request_id, "questions": questions})
        else:
            detail = self.describe(tool_name, tool_input)
            await self._emit({"type": "permission", "id": request_id, "tool": tool_name, "detail": detail})
        try:
            return await asyncio.wait_for(answer, self.settings.approval_timeout_seconds)
        except TimeoutError:
            return PermissionResultDeny(message="The user didn't answer in time.")
        finally:
            self.pending.pop(request_id, None)

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
        self._deny_pending("The user stopped this.")
        if self.client is not None and self.events is not None:  # only a running turn can be stopped
            await self.client.interrupt()

    async def new_conversation(self) -> None:
        self.fresh = True
        if self.worker is not None and not self.worker.done():
            await self.inbox.put(None)  # closes after the current turn; the next message starts fresh


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
