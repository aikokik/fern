"""fern_builder offline: a fake Claude Agent SDK client plays scripted turns (replies, permissions, questions)."""

import asyncio
import json
import tempfile
from pathlib import Path

import httpx
import pytest
from claude_agent_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    PermissionUpdate,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import PermissionRuleValue, SDKSessionInfo

from fern_builder import bridge as bridge_module
from fern_builder.bridge import VOICE_NOTE, ChatBusyError, ClaudeChat
from fern_builder.config import BuilderSettings, read_env_value

PROJECT = Path("/Users/me/fern")
TRANSCRIPT = "/Users/me/.claude/projects/-Users-me-fern/s.jsonl"
REMEMBER_PUSH = PermissionUpdate(
    type="addRules",
    rules=[PermissionRuleValue(tool_name="Bash", rule_content="git push:*")],
    behavior="allow",
    destination="localSettings",
)
QUESTION = {"question": "Which source?", "header": "Source", "multiSelect": False, "options": [
    {"label": "Open-Meteo", "description": "free"}, {"label": "Weather.gov", "description": "US only"},
]}  # fmt: skip


def result(text: str) -> ResultMessage:
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s",
        result=text, total_cost_usd=0.02,
    )  # fmt: skip


class FakeClaude:
    """Stands in for ClaudeSDKClient: records options and prompts, plays one scripted turn per message."""

    def __init__(self, options, turns: list, opened: list):
        self.options, self.turns, self.prompts, self.interrupted = options, turns, [], False
        opened.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exception_info):
        return False

    async def query(self, text: str) -> None:
        self.prompts.append(text)
        for matcher in (self.options.hooks or {}).get("UserPromptSubmit", []):  # what Claude Code's hook reports
            for hook in matcher.hooks:
                hook_input = {"session_id": "s", "transcript_path": TRANSCRIPT, "cwd": str(PROJECT), "prompt": text}
                await hook(hook_input | {"permission_mode": "default", "hook_event_name": "UserPromptSubmit"}, None, {})

    async def receive_response(self):
        async for message in self.turns.pop(0)(self):
            yield message

    async def interrupt(self) -> None:
        self.interrupted = True


def make_chat(turns: list, **settings) -> tuple[ClaudeChat, list[FakeClaude]]:
    opened: list[FakeClaude] = []
    settings.setdefault("runs_file", Path(tempfile.mkdtemp()) / "runs.jsonl")  # never the user's real run log
    chat_settings = BuilderSettings(project_dir=PROJECT, bridge_token="bridge-secret", **settings)
    return ClaudeChat(chat_settings, lambda options: FakeClaude(options, turns, opened)), opened


async def events_of(chat: ClaudeChat, text: str) -> list[dict]:
    """One whole turn, read like the HTTP stream reads it (until `done`)."""
    await chat.start(text)
    return [event async for event in chat.stream()]


async def reads_and_answers(_client):
    read = ToolUseBlock(id="t1", name="Read", input={"file_path": f"{PROJECT}/src/app.py"})
    yield AssistantMessage(content=[read, TextBlock(text="Two ways to do it.")], model="claude-opus-5-5")
    yield result("Two ways to do it. Details on screen.")


def test_options_continue_the_latest_conversation_with_the_users_settings():
    chat, _opened = make_chat([])
    options = chat.options()
    assert options.cwd == PROJECT
    assert options.setting_sources == ["user", "project", "local"]  # the user's own rules, like VS Code
    assert (options.continue_conversation, options.fork_session, options.resume) == (True, False, None)  # itself
    assert options.system_prompt == {"type": "preset", "preset": "claude_code", "append": VOICE_NOTE}
    chat.fresh = True
    assert (chat.options().continue_conversation, chat.options().resume) == (False, None)
    chat.fresh, chat.chosen_session = False, "session-2"
    assert (chat.options().continue_conversation, chat.options().resume) == (False, "session-2")


async def test_sessions_are_listed_and_one_can_be_opened(monkeypatch):
    infos = [
        SDKSessionInfo(session_id="s1", summary="Weather tool", last_modified=1_791_800_000_000),
        SDKSessionInfo(session_id="s2", summary="Fix flaky tests", last_modified=1_791_700_000_000, custom_title="CI"),
    ]
    monkeypatch.setattr(bridge_module, "list_sessions", lambda directory, limit: infos[:limit])
    chat, opened = make_chat([reads_and_answers, reads_and_answers])
    await events_of(chat, "hi")  # in session "s" (from the fake result)
    assert chat.sessions() == [
        {"id": "s1", "title": "Weather tool", "updated": 1_791_800_000, "current": False},
        {"id": "s2", "title": "CI", "updated": 1_791_700_000, "current": False},
    ]
    with pytest.raises(KeyError):
        await chat.open_session("not-in-this-project")
    await chat.open_session("s2")
    await events_of(chat, "where were we?")
    assert opened[1].options.resume == "s2"  # the next message continues the chosen conversation
    assert chat.chosen_session is None


async def test_a_turn_streams_steps_text_and_the_final_reply():
    chat, opened = make_chat([reads_and_answers, reads_and_answers])
    events = await events_of(chat, "how should we add weather?")
    assert events == [
        {"type": "progress", "text": "Read: src/app.py"},  # project path trimmed for the panel
        {"type": "text", "markdown": "Two ways to do it."},
        {"type": "done", "reply": "Two ways to do it. Details on screen.", "is_error": False, "cost_usd": 0.02},
    ]
    await events_of(chat, "option one")
    assert len(opened) == 1  # one live conversation, like the VS Code chat
    assert opened[0].prompts == ["how should we add weather?", "option one"]
    chat.events = asyncio.Queue()  # a turn is running
    with pytest.raises(ChatBusyError):
        await chat.start("too soon")


async def test_permission_requests_wait_for_the_user():
    decisions = []

    async def wants_to_push(client):
        context = ToolPermissionContext(suggestions=[REMEMBER_PUSH])
        decisions.append(await client.options.can_use_tool("Bash", {"command": "git push"}, context))
        yield result("pushed")

    chat, _opened = make_chat([wants_to_push, wants_to_push, wants_to_push], approval_timeout_seconds=0.2)
    for decision in ("always", "deny"):
        await chat.start("push it")
        stream = chat.stream()
        permission = await anext(stream)
        assert {key: permission[key] for key in ("type", "tool", "detail")} == {
            "type": "permission", "tool": "Bash", "detail": "Bash: git push",
        }  # fmt: skip
        chat.decide(permission["id"], decision)
        assert (await anext(stream))["type"] == "done"
        await stream.aclose()
    allowed, denied = decisions
    assert isinstance(allowed, PermissionResultAllow)
    assert allowed.updated_permissions == [REMEMBER_PUSH]  # "always" saves the rule, like "don't ask again"
    assert isinstance(denied, PermissionResultDeny)

    await events_of(chat, "push it")  # nobody answers
    assert isinstance(decisions[2], PermissionResultDeny)
    assert "didn't answer" in decisions[2].message
    with pytest.raises(KeyError):
        chat.decide("unknown", "allow")


async def test_questions_come_back_with_the_chosen_labels():
    answers = []

    async def asks(client):
        answers.append(await client.options.can_use_tool("AskUserQuestion", {"questions": [QUESTION]}, None))
        yield result("ok")

    chat, _opened = make_chat([asks])
    await chat.start("add weather")
    stream = chat.stream()
    question = await anext(stream)
    assert question["questions"] == [
        {
            "question": "Which source?",
            "options": ["Open-Meteo", "Weather.gov"],
            "descriptions": ["free", "US only"],  # shown under each choice, like VS Code's question card
            "multi_select": False,
        }
    ]
    chat.answer_questions(question["id"], {"Which source?": "Weather.gov"})
    assert (await anext(stream))["type"] == "done"
    assert answers[0].updated_input == {"questions": [QUESTION], "answers": {"Which source?": "Weather.gov"}}


async def test_stop_denies_what_is_pending_and_new_chat_starts_fresh():
    decisions = []

    async def waits_for_permission(client):
        decisions.append(
            await client.options.can_use_tool("Bash", {"command": "rm -rf build"}, ToolPermissionContext())
        )
        yield result("stopped")

    chat, opened = make_chat([waits_for_permission, reads_and_answers])
    await chat.start("clean up")
    stream = chat.stream()
    assert (await anext(stream))["type"] == "permission"
    await chat.interrupt()
    assert (await anext(stream))["type"] == "done"
    await stream.aclose()
    assert opened[0].interrupted
    assert isinstance(decisions[0], PermissionResultDeny)
    assert decisions[0].interrupt

    await chat.new_conversation()
    await events_of(chat, "new topic")
    assert len(opened) == 2
    assert opened[1].options.continue_conversation is False  # a fresh chat, not the latest one


async def test_quiet_work_sends_pings():
    async def slow(_client):
        await asyncio.sleep(0.05)
        yield result("done")

    chat, _opened = make_chat([slow])
    original_ping_seconds = bridge_module.PING_SECONDS
    bridge_module.PING_SECONDS = 0.01
    try:
        events = await events_of(chat, "run the tests")
    finally:
        bridge_module.PING_SECONDS = original_ping_seconds
    assert events[0] == {"type": "ping"}  # keeps the proxy connection open during long steps
    assert events[-1]["type"] == "done"


async def test_http_api_auth_busy_and_unknown_requests(monkeypatch):
    chat, _opened = make_chat([reads_and_answers])
    monkeypatch.setattr(bridge_module, "chat", chat)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge_module.app), base_url="http://bridge")
    authorized = {"Authorization": "Bearer bridge-secret"}

    assert (
        await client.post("/messages", json={"text": "hi"}, headers={"Authorization": "Bearer x"})
    ).status_code == 401
    response = await client.post("/messages", json={"text": "hi"}, headers=authorized)
    lines = [json.loads(line) for line in response.text.splitlines()]
    assert response.headers["content-type"] == "application/x-ndjson"
    assert lines[-1]["type"] == "done"

    chat.events = asyncio.Queue()  # a turn is running
    assert (await client.post("/messages", json={"text": "more"}, headers=authorized)).status_code == 409
    missing = await client.post("/permissions/nope", json={"decision": "allow"}, headers=authorized)
    assert missing.status_code == 404


def test_only_the_bridge_token_is_read_from_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('OPENAI_API_KEY=sk-secret\nFERN_BRIDGE_TOKEN="abc123"\n')
    assert read_env_value(env_file, "FERN_BRIDGE_TOKEN") == "abc123"
    assert read_env_value(tmp_path / "missing.env", "FERN_BRIDGE_TOKEN") == ""


async def runs_tests(_client):
    yield SystemMessage(subtype="init", data={"session_id": "s", "model": "claude-opus-5-5", "cwd": str(PROJECT),
                                              "permissionMode": "default", "apiKeySource": "none"})  # fmt: skip
    yield AssistantMessage(
        content=[
            ThinkingBlock(thinking="Run the suite first.", signature="x"),
            TextBlock(text="Running the tests."),
            ToolUseBlock(id="t1", name="Bash", input={"command": "uv run pytest -q"}),
        ],
        model="m",
    )
    subagent_step = ToolUseBlock(id="t2", name="Read", input={"file_path": "a.py"})
    yield AssistantMessage(content=[subagent_step], model="m", parent_tool_use_id="t1")
    yield UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="53 passed", is_error=False)])
    yield AssistantMessage(content=[TextBlock(text="All tests pass.")], model="m")
    yield result("All tests pass.")


async def test_each_run_records_what_claude_code_reported(tmp_path):
    runs_file = tmp_path / "runs.jsonl"
    chat, _opened = make_chat([runs_tests], runs_file=runs_file)
    listener = chat.runs.subscribe()
    await events_of(chat, "run the tests")

    run = chat.runs.newest_first()[0]
    assert {key: run[key] for key in ("input", "status", "output", "model", "permission_mode", "api_key_source")} == {
        "input": "run the tests", "status": "done", "output": "All tests pass.", "model": "claude-opus-5-5",
        "permission_mode": "default", "api_key_source": "none",
    }  # fmt: skip
    assert run["transcript_path"] == TRANSCRIPT  # where Claude Code itself says it keeps the session
    assert (run["num_turns"], run["cost_usd"]) == (1, 0.02)  # Claude Code's own numbers, from the ResultMessage
    timeline = [(entry["kind"], entry.get("tool") or entry.get("text"), entry["parent"]) for entry in run["timeline"]]
    assert timeline == [  # in the order Claude produced it, as the IDE shows it
        ("thinking", "Run the suite first.", None),
        ("text", "Running the tests.", None),
        ("tool", "Bash", None),
        ("tool", "Read", "t1"),  # a subagent's step points at its parent call
        ("text", "All tests pass.", None),
    ]
    bash = run["timeline"][2]
    assert (bash["input"], bash["status"], bash["output"]) == ({"command": "uv run pytest -q"}, "done", "53 passed")
    assert run["timeline"][3]["status"] == "stopped"  # never got a result before the run ended
    assert listener.qsize() >= 4  # the panel saw every change live
    assert chat.runs.__class__(runs_file).newest_first()[0]["id"] == run["id"]  # history survives a restart


async def test_runs_endpoint_reports_source_and_limits(monkeypatch):
    chat, _opened = make_chat([reads_and_answers])
    monkeypatch.setattr(bridge_module, "chat", chat)
    await events_of(chat, "hi")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge_module.app), base_url="http://bridge")
    assert (await client.get("/runs")).status_code == 401
    report = (await client.get("/runs", headers={"Authorization": "Bearer bridge-secret"})).json()
    assert report["run_log_file"] == str(chat.settings.runs_file)
    assert any("no execution log" in limit for limit in report["limits"])
    assert [run["input"] for run in report["runs"]] == ["hi"]
