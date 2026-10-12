"""fern_web offline: fake brain (httpx.MockTransport), fake OpenAI handshake, fake Realtime connection."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from fern_web import app as web
from fern_web.brain_client import BrainClient
from fern_web.claude_client import ClaudeClient
from fern_web.claude_mode import chosen_label, spoken, spoken_options
from fern_web.realtime import instructions_for
from fern_web.sideband import VoiceSession

DEVICE_TOKEN = "t" * 32
AUTHORIZED = {"Authorization": f"Bearer {DEVICE_TOKEN}"}
OFFER = {"sdp": "v=0 offer-sdp"}


def fake_brain(recorded_calls: list, voice_seconds_today: float = 0, chat_status: int = 200) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        recorded_calls.append((request.method, request.url.path, json.loads(request.content or b"null")))
        if request.headers["authorization"] != f"Bearer {DEVICE_TOKEN}":
            return httpx.Response(401)
        match request.url.path:
            case "/v1/profile":
                return httpx.Response(
                    200,
                    json={
                        "device": "phone",
                        "facts": ["Sam wakes at 7"],
                        "voice_seconds_today": voice_seconds_today,
                    },
                )
            case "/v1/chat" if chat_status != 200:
                return httpx.Response(chat_status)
            case "/v1/chat":
                return httpx.Response(
                    200,
                    json={
                        "reply": "Need approval",
                        "tools": ["home_secure"],
                        "commands": [{"type": "alarm.set", "time": "07:00", "label": ""}],
                        "pending": [{"id": "a" * 32, "tool": "home_secure", "args": "{}"}],
                    },
                )
            case path if path.startswith("/v1/approvals/"):
                return httpx.Response(200, json={"reply": "Claude Code started.", "commands": [], "pending": []})
            case "/v1/notices":
                return httpx.Response(200, json=[])
            case "/v1/voice-log":
                return httpx.Response(204)
        return httpx.Response(404)

    return httpx.MockTransport(handle)


class FakeRealtimeConnection:
    """Events put on `incoming` are what OpenAI would send; `sent` records what fern sends back."""

    def __init__(self):
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []

    async def send(self, event):
        self.sent.append(event)

    async def close(self):
        await self.incoming.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if (event := await self.incoming.get()) is None:
            raise StopAsyncIteration
        return event


def voice_session(
    connection: FakeRealtimeConnection,
    recorded_calls: list,
    chat_status: int = 200,
    claude: ClaudeClient | None = None,
    claude_devices: tuple[str, ...] = (),
) -> VoiceSession:
    @asynccontextmanager
    async def connect(_call_id):
        yield connection

    async def hangup(_call_id):
        pass

    return VoiceSession(
        "session-1",
        DEVICE_TOKEN,
        "rtc_1",
        BrainClient("http://brain", fake_brain(recorded_calls, chat_status=chat_status)),
        connect,
        hangup,
        max_seconds=600,
        idle_seconds=600,
        notice_poll_seconds=600,
        claude=claude,
        device_name="phone",
        claude_devices=claude_devices,
    )


def response_done(status: str, arguments: str, call_id: str = "c1", tool: str = "ask_fern") -> SimpleNamespace:
    """A Realtime `response.done` event whose output is one tool call (ask_fern by default), with token usage."""
    call = SimpleNamespace(type="function_call", name=tool, call_id=call_id, arguments=arguments)
    usage = SimpleNamespace(
        input_tokens=500,
        output_tokens=40,
        input_token_details=SimpleNamespace(
            cached_tokens=200, audio_tokens=300, cached_tokens_details=SimpleNamespace(audio_tokens=100)
        ),
        output_token_details=SimpleNamespace(audio_tokens=30),
    )
    return SimpleNamespace(type="response.done", response=SimpleNamespace(status=status, output=[call], usage=usage))


BRIDGE_TOKEN = "b" * 32
PR_URL = "https://github.com/aikokik/fern/pull/9"
DONE = {"type": "done", "reply": f"Tests pass and the PR is open.\n\nPR: {PR_URL}", "is_error": False, "cost_usd": 0.1}
STEPS = [{"type": "progress", "text": "Bash: uv run pytest"}, {"type": "text", "markdown": "All tests pass."}]
SESSIONS = [
    {"id": "s-latest", "title": "Household AI learning plan", "updated": 1_791_800_000, "current": True},
    {"id": "s-older", "title": "Weather tool", "updated": 1_791_700_000, "current": False},
]
PERMISSION_ID = "a1" * 16
PERMISSION = {"type": "permission", "id": PERMISSION_ID, "tool": "Bash", "detail": "Bash: git push"}
QUESTION = {
    "type": "question",
    "id": "q1",
    "questions": [{"question": "Which source?", "options": ["Open-Meteo", "Weather.gov"], "multi_select": False}],
}


class FakeBridge:
    """The Claude bridge on the Mac (fern_builder.bridge) as fern_web sees it: streams a scripted turn as NDJSON,
    pausing at each permission request or question until fern answers it."""

    def __init__(self, turn: list[dict] | None = None, status: int = 200):
        self.turn = turn or [DONE]
        self.status = status
        self.requests: list[tuple[str, str, object]] = []
        self.answered = asyncio.Event()
        self.transport = httpx.MockTransport(self.handle)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path, json.loads(request.content or b"null")))
        if request.headers["authorization"] != f"Bearer {BRIDGE_TOKEN}":
            return httpx.Response(401)
        if self.status != 200:
            return httpx.Response(self.status)
        if request.url.path == "/messages":
            return httpx.Response(200, content=self.stream(), headers={"content-type": "application/x-ndjson"})
        if request.url.path == "/sessions":
            return httpx.Response(200, json=SESSIONS)
        self.answered.set()
        return httpx.Response(200, json={"ok": True})

    async def stream(self):
        self.answered.clear()  # only answers given during this turn count
        for event in self.turn:
            yield (json.dumps(event) + "\n").encode()
            if event["type"] in ("permission", "question"):
                await self.answered.wait()
                self.answered.clear()


async def wait_until(condition, timeout_seconds=2.0):
    for _attempt in range(int(timeout_seconds / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


async def test_tool_call_runs_on_server_channel():
    connection, recorded_calls = FakeRealtimeConnection(), []
    session = voice_session(connection, recorded_calls)
    run_task = asyncio.create_task(session.run())
    await connection.incoming.put(
        SimpleNamespace(type="conversation.item.input_audio_transcription.completed", transcript="unlock the door")
    )
    await connection.incoming.put(response_done("completed", '{"request": "unlock the door"}'))
    await wait_until(lambda: len(connection.sent) >= 2)

    tool_output = connection.sent[0]["item"]
    assert tool_output["type"] == "function_call_output"
    assert tool_output["call_id"] == "c1"
    assert "Needs approval: home_secure" in tool_output["output"]
    assert "approve?" in tool_output["output"]
    assert connection.sent[1] == {"type": "response.create"}
    assert ("POST", "/v1/chat", {"text": "unlock the door", "session": "voice"}) in recorded_calls
    alarm = {"type": "alarm.set", "time": "07:00", "label": ""}
    events = browser_events(session)
    assert [event["type"] for event in events] == ["working", "working", "command", "pending"]
    assert events[0] == {"type": "working", "by": "fern"}  # the screen shows fern's brain is on it
    assert events[2] == {"type": "command", "command": alarm}

    await session.close()
    await run_task
    assert (await session.browser_events.get()) == {"type": "end"}
    voice_log = next(call for call in recorded_calls if call[1] == "/v1/voice-log")[2]
    assert voice_log["transcript"] == [{"role": "user", "text": "unlock the door"}]
    voice_usage, transcription_usage = voice_log["usage"]
    assert voice_usage == {
        "kind": "voice", "model": "realtime", "requests": 1, "input": 500, "cached": 200, "output": 40,
        "audio_input": 300, "audio_cached": 100, "audio_output": 30, "max_input": 500,
    }  # fmt: skip
    assert transcription_usage["kind"] == "transcription"


async def test_interrupted_or_malformed_tool_calls_never_reach_the_brain(caplog):
    connection, recorded_calls = FakeRealtimeConnection(), []
    session = voice_session(connection, recorded_calls)
    run_task = asyncio.create_task(session.run())
    # User talked over the model: the response was cancelled with a half-written call.
    await connection.incoming.put(response_done("cancelled", '{"request": "ask Claude Code to pla', call_id="c1"))
    # A completed response with broken JSON: tell the model to ask again instead of failing.
    await connection.incoming.put(response_done("completed", '{"request": "ask Cl', call_id="c2"))
    await wait_until(lambda: len(connection.sent) >= 2)

    assert connection.sent[0]["item"]["call_id"] == "c2"
    assert "repeat" in connection.sent[0]["item"]["output"]
    assert "ask Cl" not in caplog.text  # the user's words never reach the logs
    assert not any(call[1] == "/v1/chat" for call in recorded_calls)
    await session.close()
    await run_task


async def test_brain_failure_is_reported_with_a_log_reference():
    connection, recorded_calls = FakeRealtimeConnection(), []
    session = voice_session(connection, recorded_calls, chat_status=500)
    run_task = asyncio.create_task(session.run())
    await connection.incoming.put(response_done("completed", '{"request": "ask Claude Code to plan TV control"}'))
    await wait_until(lambda: len(connection.sent) >= 2)

    output = connection.sent[0]["item"]["output"]
    error_card = next(event for event in browser_events(session) if event["type"] == "error")
    assert error_card["reference"] in output  # same reference spoken, shown and logged
    assert "check the fern logs" in output
    await session.close()
    await run_task


async def test_tool_result_waits_until_the_model_pauses():
    connection, recorded_calls = FakeRealtimeConnection(), []
    session = voice_session(connection, recorded_calls)
    run_task = asyncio.create_task(session.run())
    await wait_until(lambda: session.connection is not None)
    session.model_is_responding = True  # the model is mid-sentence with the user
    tool_task = asyncio.create_task(session._run_tool("ask_fern", "c1", '{"request": "lights off"}'))
    await wait_until(lambda: any(call[1] == "/v1/chat" for call in recorded_calls))
    await asyncio.sleep(0.3)
    assert connection.sent == []  # result held back, nothing interrupts the model
    session.model_is_responding = False
    await tool_task
    assert connection.sent[0]["item"]["type"] == "function_call_output"
    await session.close()
    await run_task


async def ask_and_answer(spoken_answer: str, approve: bool) -> tuple[FakeRealtimeConnection, list]:
    """fern pauses on a risky action; the user answers out loud; the model calls decide_approval."""
    connection, recorded_calls = FakeRealtimeConnection(), []
    session = voice_session(connection, recorded_calls)
    run_task = asyncio.create_task(session.run())
    await connection.incoming.put(response_done("completed", '{"request": "plan TV control"}', call_id="c1"))
    await wait_until(lambda: len(connection.sent) >= 2)  # "Needs approval ... approve?"
    await connection.incoming.put(
        SimpleNamespace(type="conversation.item.input_audio_transcription.completed", transcript=spoken_answer)
    )
    decision = SimpleNamespace(
        type="function_call", name="decide_approval", call_id="c2", arguments=json.dumps({"approve": approve})
    )
    await connection.incoming.put(
        SimpleNamespace(type="response.done", response=SimpleNamespace(status="completed", output=[decision]))
    )
    await wait_until(lambda: len(connection.sent) >= 4)
    await session.close()
    await run_task
    return connection, recorded_calls


async def test_voice_approval_requires_a_spoken_yes():
    connection, recorded_calls = await ask_and_answer("yes, go ahead", approve=True)
    assert connection.sent[2]["item"]["output"].startswith("Approved.")
    assert any(call[1].startswith("/v1/approvals/") and call[2] == {"approve": True} for call in recorded_calls)

    connection, recorded_calls = await ask_and_answer("no, don't do it", approve=True)  # model misheard
    assert connection.sent[2]["item"]["output"].startswith("Not approved")
    assert not any(call[1].startswith("/v1/approvals/") for call in recorded_calls)

    connection, recorded_calls = await ask_and_answer("no thanks", approve=False)  # rejecting needs no proof
    assert connection.sent[2]["item"]["output"].startswith("Rejected.")


async def test_goodbye_is_spoken_once():
    connection, recorded_calls = FakeRealtimeConnection(), []
    session = voice_session(connection, recorded_calls)
    run_task = asyncio.create_task(session.run())
    goodbye = SimpleNamespace(type="function_call", name="end_conversation", call_id="c1", arguments="{}")
    await connection.incoming.put(
        SimpleNamespace(type="response.done", response=SimpleNamespace(status="completed", output=[goodbye]))
    )
    await asyncio.wait_for(run_task, 10)
    assert connection.sent[0]["item"]["type"] == "function_call_output"
    assert {"type": "response.create"} not in connection.sent  # no second goodbye


async def test_say_injects_system_message():
    connection, recorded_calls = FakeRealtimeConnection(), []
    session = voice_session(connection, recorded_calls)
    run_task = asyncio.create_task(session.run())
    await wait_until(lambda: session.connection is not None)
    await session.say("Notice: build ready")
    injected = connection.sent[0]["item"]
    assert injected["role"] == "system"
    assert injected["content"][0]["text"] == "Notice: build ready"
    await session.close()
    await run_task


async def test_idle_timeout_ends_session():
    session = voice_session(FakeRealtimeConnection(), [])
    session.idle_seconds = 0.5
    await asyncio.wait_for(session.run(), 5)
    assert session.closed.is_set()


@pytest.fixture
def web_client(monkeypatch):
    recorded_calls, handshake = [], {}

    async def create_call(sdp, session, extra_headers):
        handshake.update(sdp=sdp, session=session)
        return SimpleNamespace(
            http_response=SimpleNamespace(text="answer-sdp"), headers={"location": "/v1/realtime/calls/rtc_abc"}
        )

    fake_openai = SimpleNamespace(
        realtime=SimpleNamespace(
            calls=SimpleNamespace(with_raw_response=SimpleNamespace(create=create_call), hangup=None),
            connect=None,
        )
    )
    monkeypatch.setattr(web.state, "openai", fake_openai, raising=False)
    monkeypatch.setattr(web.state, "brain", BrainClient("http://brain", fake_brain(recorded_calls)), raising=False)
    claude = ClaudeClient("http://bridge", BRIDGE_TOKEN, FakeBridge().transport)
    monkeypatch.setattr(web.state, "claude", claude, raising=False)
    started_calls = []

    async def record_start(session):
        started_calls.append(session.call_id)

    monkeypatch.setattr(web.VoiceSession, "run", record_start)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url="http://fern")
    return client, handshake, started_calls


async def test_call_handshake_server_side(web_client):
    client, handshake, started_calls = web_client
    response = await client.post("/realtime/call", json=OFFER, headers=AUTHORIZED)
    assert response.status_code == 200
    assert response.json()["sdp"] == "answer-sdp"
    assert "session_id" in response.json()
    assert "rtc_abc" not in response.text  # call_id never leaves the server
    assert started_calls == ["rtc_abc"]
    session_config = handshake["session"]
    assert session_config["model"]
    tool_names = {function["name"] for function in session_config["tools"]}
    assert tool_names == {
        "ask_fern", "decide_approval", "end_conversation", "switch_backend", "ask_claude", "claude_action"
    }  # fmt: skip
    assert "Sam wakes at 7" in session_config["instructions"]  # personalization
    assert session_config["max_output_tokens"] > 0  # spoken replies are capped
    assert session_config["truncation"]["token_limits"]["post_instructions"] > 0  # voice context is capped
    assert "connect-src 'self';" in response.headers["content-security-policy"]


async def test_call_rejects_bad_token_and_cap(web_client, monkeypatch):
    client, _handshake, started_calls = web_client
    response = await client.post("/realtime/call", json=OFFER, headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401
    assert not started_calls

    over_budget_brain = BrainClient("http://brain", fake_brain([], voice_seconds_today=10**6))
    monkeypatch.setattr(web.state, "brain", over_budget_brain)
    response = await client.post("/realtime/call", json=OFFER, headers=AUTHORIZED)
    assert response.status_code == 429
    assert not started_calls


async def test_events_scoped_to_owner(web_client):
    client, *_ = web_client
    session_id = (await client.post("/realtime/call", json=OFFER, headers=AUTHORIZED)).json()["session_id"]
    response = await client.post(
        "/realtime/end", json={"session_id": session_id}, headers={"Authorization": "Bearer other-device-token"}
    )
    assert response.status_code == 404


async def start_claude_session(bridge: FakeBridge, claude_devices=(), configured=True):
    connection = FakeRealtimeConnection()
    claude = ClaudeClient("http://bridge" if configured else "", BRIDGE_TOKEN, bridge.transport)
    session = voice_session(connection, [], claude=claude, claude_devices=claude_devices)
    run_task = asyncio.create_task(session.run())
    return connection, session, run_task


async def start_tool(connection: FakeRealtimeConnection, tool: str, arguments: dict, call_id: str) -> None:
    await connection.incoming.put(response_done("completed", json.dumps(arguments), call_id=call_id, tool=tool))


async def output_for(connection: FakeRealtimeConnection, call_id: str) -> str:
    def answered():
        return any(event.get("item", {}).get("call_id") == call_id for event in connection.sent)

    await wait_until(answered)
    return next(event["item"]["output"] for event in connection.sent if event.get("item", {}).get("call_id") == call_id)


async def call_tool(connection: FakeRealtimeConnection, tool: str, arguments: dict, call_id: str) -> str:
    """Send one completed tool call; return what fern answered to it."""
    await start_tool(connection, tool, arguments, call_id)
    return await output_for(connection, call_id)


async def user_says(connection: FakeRealtimeConnection, words: str) -> None:
    await connection.incoming.put(
        SimpleNamespace(type="conversation.item.input_audio_transcription.completed", transcript=words)
    )


def notices(connection: FakeRealtimeConnection) -> list[str]:
    return [
        event["item"]["content"][0]["text"]
        for event in connection.sent
        if event.get("item", {}).get("role") == "system"
    ]


def browser_events(session: VoiceSession) -> list[dict]:
    events = []
    while not session.browser_events.empty():
        events.append(session.browser_events.get_nowait())
    return events


async def test_open_claude_and_back_swaps_the_voice_instructions():
    connection, session, run_task = await start_claude_session(FakeBridge())

    output = await call_tool(connection, "switch_backend", {"target": "claude"}, "c1")
    assert "Switched to Claude Code" in output
    instructions_update = next(event for event in connection.sent if event["type"] == "session.update")
    assert "same Claude as\nin their VS Code chat" in instructions_update["session"]["instructions"]
    assert {"type": "mode", "mode": "claude"} in browser_events(session)

    output = await call_tool(connection, "ask_fern", {"request": "set an alarm"}, "c2")
    assert "talking to Claude Code" in output  # fern's tools are off while Claude is listening

    output = await call_tool(connection, "switch_backend", {"target": "fern"}, "c3")
    assert "back to fern" in output
    assert (
        "You are fern" in [e for e in connection.sent if e["type"] == "session.update"][-1]["session"]["instructions"]
    )
    assert {"type": "mode", "mode": "fern"} in browser_events(session)
    await session.close()
    await run_task


async def test_a_claude_turn_streams_to_the_screen_and_the_voice_gets_the_gist():
    bridge = FakeBridge([*STEPS, DONE])
    connection, session, run_task = await start_claude_session(bridge)
    await call_tool(connection, "switch_backend", {"target": "claude"}, "c1")
    browser_events(session)

    output = await call_tool(connection, "ask_claude", {"request": "run the tests and push"}, "c2")
    assert output == "Claude's answer: Tests pass and the PR is open.\nMore detail is on screen."
    assert ("POST", "/messages", {"text": "run the tests and push"}) in bridge.requests
    assert browser_events(session) == [
        {"type": "working", "by": "claude"},
        {"type": "claude_turn", "text": "run the tests and push"},
        {"type": "claude_progress", "text": "Bash: uv run pytest"},  # live steps, like the VS Code chat
        {"type": "claude_text", "markdown": "All tests pass."},
        {"type": "working", "by": ""},
        {"type": "claude_link", "label": "Pull request", "url": PR_URL},
    ]
    await session.close()
    await run_task


async def test_claude_permission_needs_the_users_spoken_yes():
    bridge = FakeBridge([PERMISSION, DONE])
    connection, session, run_task = await start_claude_session(bridge)
    await call_tool(connection, "switch_backend", {"target": "claude"}, "c1")
    await start_tool(connection, "ask_claude", {"request": "push it"}, "c2")
    await wait_until(lambda: session.claude.permission is not None)
    assert "Notice: Claude wants permission for Bash: git push. Ask the user: approve?" in notices(connection)
    assert {"type": "claude_permission", "id": PERMISSION_ID, "detail": "Bash: git push"} in browser_events(session)

    await user_says(connection, "no wait")
    output = await call_tool(connection, "decide_approval", {"approve": True}, "c3")  # the model misheard
    assert output.startswith("Not approved")
    assert not any(path.startswith("/permissions") for _method, path, _body in bridge.requests)

    await user_says(connection, "yes, don't ask again")
    output = await call_tool(connection, "decide_approval", {"approve": True, "always": True}, "c4")
    assert output == "Approved. Claude continues."
    assert ("POST", f"/permissions/{PERMISSION_ID}", {"decision": "always"}) in bridge.requests
    assert (await output_for(connection, "c2")).startswith("Claude's answer:")  # the turn finished
    await session.close()
    await run_task


async def test_claude_questions_are_answered_by_voice():
    bridge = FakeBridge([QUESTION, DONE])
    connection, session, run_task = await start_claude_session(bridge)
    await call_tool(connection, "switch_backend", {"target": "claude"}, "c1")
    await start_tool(connection, "ask_claude", {"request": "add weather"}, "c2")
    await wait_until(lambda: session.claude.question is not None)
    assert "Notice: Claude asks: Which source? Option 1: Open-Meteo. Option 2: Weather.gov." in notices(connection)

    output = await call_tool(connection, "ask_claude", {"request": "option 2"}, "c3")
    assert output == "Answer sent to Claude."
    assert ("POST", "/questions/q1", {"answers": {"Which source?": "Weather.gov"}}) in bridge.requests
    assert (await output_for(connection, "c2")).startswith("Claude's answer:")
    await session.close()
    await run_task


async def test_stop_new_chat_and_hanging_up_mid_prompt():
    bridge = FakeBridge([PERMISSION, DONE])
    connection, session, run_task = await start_claude_session(bridge)
    await call_tool(connection, "switch_backend", {"target": "claude"}, "c1")
    assert await call_tool(connection, "claude_action", {"action": "stop"}, "c2") == "Stopping Claude."
    await call_tool(connection, "claude_action", {"action": "new_chat"}, "c3")
    assert [path for _method, path, _body in bridge.requests] == ["/interrupt", "/new"]

    await start_tool(connection, "ask_claude", {"request": "push it"}, "c4")
    await wait_until(lambda: session.claude.permission is not None)
    await session.close()  # nobody can answer anymore: stop that turn
    await run_task
    assert bridge.requests[-1][1] == "/interrupt"


async def test_sessions_can_be_listed_and_opened_by_voice():
    bridge = FakeBridge()
    connection, session, run_task = await start_claude_session(bridge)
    await call_tool(connection, "switch_backend", {"target": "claude"}, "c1")
    browser_events(session)

    output = await call_tool(connection, "claude_action", {"action": "open_session", "number": 2}, "c2")
    assert output.startswith("Which session?")  # nothing listed yet

    output = await call_tool(connection, "claude_action", {"action": "list_sessions"}, "c3")
    assert "2 sessions are on screen" in output
    assert "2: Weather tool." in output
    panel = next(event for event in browser_events(session) if event["type"] == "claude_text")
    assert panel["markdown"].startswith("**Claude sessions**\n1. Household AI learning plan")
    assert "(current)" in panel["markdown"]

    output = await call_tool(connection, "claude_action", {"action": "open_session", "number": 2}, "c4")
    assert output == "Opened 'Weather tool'. The next message continues it."
    assert ("POST", "/sessions/s-older/open", {}) in bridge.requests
    await session.close()
    await run_task


async def test_claude_unavailable_device_not_allowed_or_bridge_failing():
    connection, session, run_task = await start_claude_session(FakeBridge(), configured=False)
    assert "isn't set up" in await call_tool(connection, "switch_backend", {"target": "claude"}, "c1")
    assert not session.claude.active
    await session.close()
    await run_task

    bridge = FakeBridge()
    connection, session, run_task = await start_claude_session(bridge, claude_devices=("tablet",))
    assert "isn't allowed" in await call_tool(connection, "switch_backend", {"target": "claude"}, "c1")
    assert not bridge.requests
    await session.close()
    await run_task

    connection, session, run_task = await start_claude_session(FakeBridge(status=500))
    await call_tool(connection, "switch_backend", {"target": "claude"}, "c1")
    output = await call_tool(connection, "ask_claude", {"request": "hi"}, "c2")
    assert output.startswith("Claude Code failed")
    error = next(event for event in browser_events(session) if event["type"] == "error")
    assert error["reference"] in output
    await session.close()
    await run_task


async def test_waiting_on_a_tool_is_not_idle():
    session = voice_session(FakeRealtimeConnection(), [])
    session.idle_seconds = 0.3
    run_task = asyncio.create_task(session.run())
    session.run_in_background(asyncio.sleep(1.5))  # e.g. a Claude build
    await asyncio.sleep(1.2)
    assert not session.closed.is_set()
    await asyncio.wait_for(run_task, 5)
    assert session.closed.is_set()


async def test_tapped_answers_reach_claude(web_client, monkeypatch):
    client, *_ = web_client
    bridge = FakeBridge()
    monkeypatch.setattr(web.state, "claude", ClaudeClient("http://bridge", BRIDGE_TOKEN, bridge.transport))
    session_id = (await client.post("/realtime/call", json=OFFER, headers=AUTHORIZED)).json()["session_id"]
    _token_hash, session = web.state.voice_sessions[session_id]
    tap_allow = {"session_id": session_id, "permission_id": PERMISSION_ID, "decision": "allow"}
    tap_option = {"session_id": session_id, "text": "option 1"}

    assert (await client.post("/realtime/claude/permission", json=tap_allow, headers=AUTHORIZED)).status_code == 409
    assert (await client.post("/realtime/claude", json=tap_option, headers=AUTHORIZED)).status_code == 409
    session.claude.permission = PERMISSION
    session.claude.question = {"id": "q1", "questions": QUESTION["questions"], "answers": {}}
    other_device = {"Authorization": "Bearer other-device-token"}
    assert (await client.post("/realtime/claude/permission", json=tap_allow, headers=other_device)).status_code == 404

    assert (await client.post("/realtime/claude/permission", json=tap_allow, headers=AUTHORIZED)).status_code == 202
    assert (await client.post("/realtime/claude", json=tap_option, headers=AUTHORIZED)).status_code == 202
    await wait_until(lambda: len(bridge.requests) == 2)
    assert ("POST", f"/permissions/{PERMISSION_ID}", {"decision": "allow"}) in bridge.requests
    assert ("POST", "/questions/q1", {"answers": {"Which source?": "Open-Meteo"}}) in bridge.requests


def test_the_voice_gets_a_short_gist_and_speakable_options():
    long_reply = "First point is short. " + "Then a very long explanation. " * 20
    assert len(spoken(long_reply)) < 300
    assert spoken(long_reply).endswith("More detail is on screen.")
    long_options = ["Use a small local model that runs on the Mac", "Call the hosted forecast API every hour"]
    assert spoken_options(long_options) == "2 options are on screen; answer with a number."
    assert spoken_options(["Yes", "No"]) == "Option 1: Yes. Option 2: No."
    assert chosen_label("the second one", ["Open-Meteo", "Weather.gov"]) == "Weather.gov"
    assert "rephrase it for speech" in instructions_for("claude")  # paraphrased, not read verbatim
