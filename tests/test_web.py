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
from fern_web.claude_mode import ClaudeMode, summary
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
PLAN_REPLY = {
    "topic": "weather-tool",
    "status": "planning",
    "say": "Two ways to do it.",
    "show": "## Plan\n- fetch the forecast",
    "question": {"text": "Which source?", "options": ["Open-Meteo", "Weather.gov"]},
    "is_error": False,
}
PR_URL = "https://github.com/aikokik/fern/pull/9"


def fake_bridge(bridge_requests: list) -> httpx.MockTransport:
    """The builder bridge (fern_builder.bridge) as fern_web sees it."""

    def handle(request: httpx.Request) -> httpx.Response:
        bridge_requests.append((request.method, request.url.path, json.loads(request.content or b"null")))
        if request.headers["authorization"] != f"Bearer {BRIDGE_TOKEN}":
            return httpx.Response(401)
        match request.url.path.rsplit("/", 1)[-1]:
            case _ if "broken" in request.url.path:
                return httpx.Response(500, json={"detail": "claude exited 1"})
            case "turn":
                return httpx.Response(200, json=PLAN_REPLY)
            case "ship":
                return httpx.Response(200, json={"status": "shipped", "commit": "abc123", "pr_url": PR_URL})
            case "topics":
                return httpx.Response(200, json=[{"slug": "weather-tool", "status": "planning", "pr_url": ""}])
        return httpx.Response(404)

    return httpx.MockTransport(handle)


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
    claude = ClaudeClient("http://builder", BRIDGE_TOKEN, fake_bridge([]))
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


async def start_claude_session(claude_devices=(), configured=True):
    connection, bridge_requests = FakeRealtimeConnection(), []
    bridge_url = "http://builder" if configured else ""
    claude = ClaudeClient(bridge_url, BRIDGE_TOKEN, fake_bridge(bridge_requests))
    session = voice_session(connection, [], claude=claude, claude_devices=claude_devices)
    run_task = asyncio.create_task(session.run())
    return connection, session, run_task, bridge_requests


async def call_tool(connection: FakeRealtimeConnection, tool: str, arguments: dict, call_id: str) -> str:
    """Send one completed tool call; return what fern answered to it."""
    await connection.incoming.put(response_done("completed", json.dumps(arguments), call_id=call_id, tool=tool))

    def answered():
        return any(event.get("item", {}).get("call_id") == call_id for event in connection.sent)

    await wait_until(answered)
    return next(event["item"]["output"] for event in connection.sent if event.get("item", {}).get("call_id") == call_id)


def browser_events(session: VoiceSession) -> list[dict]:
    events = []
    while not session.browser_events.empty():
        events.append(session.browser_events.get_nowait())
    return events


async def test_switch_to_claude_and_back_swaps_the_voice_instructions():
    connection, session, run_task, _bridge_requests = await start_claude_session()

    output = await call_tool(connection, "switch_backend", {"target": "claude"}, "c1")
    assert "topic name" in output  # no topic yet: ask for one instead of switching
    assert not session.claude.active

    output = await call_tool(connection, "switch_backend", {"target": "claude", "topic": "weather tool"}, "c2")
    assert "Switched to Claude Code" in output
    instructions_update = next(event for event in connection.sent if event["type"] == "session.update")
    assert 'topic "weather tool"' in instructions_update["session"]["instructions"]
    assert {"type": "mode", "mode": "claude", "topic": "weather tool"} in browser_events(session)

    output = await call_tool(connection, "ask_fern", {"request": "set an alarm"}, "c3")
    assert "talking to Claude Code" in output  # fern's tools are off while Claude is listening

    output = await call_tool(connection, "switch_backend", {"target": "fern"}, "c4")
    assert "back to fern" in output
    assert (
        "You are fern" in [e for e in connection.sent if e["type"] == "session.update"][-1]["session"]["instructions"]
    )
    assert {"type": "mode", "mode": "fern", "topic": ""} in browser_events(session)
    assert not session.claude.active
    await session.close()
    await run_task


async def test_claude_reply_goes_to_screen_and_spoken_options_resolve():
    connection, session, run_task, bridge_requests = await start_claude_session()
    await call_tool(connection, "switch_backend", {"target": "claude", "topic": "weather tool"}, "c1")

    output = await call_tool(connection, "ask_claude", {"request": "add a weather tool"}, "c2")
    assert output == (
        "Say only this: Two ways to do it. Details are on screen. "
        "Then ask: Which source? Option 1: Open-Meteo. Option 2: Weather.gov."
    )
    assert ("POST", "/topics/weather tool/turn", {"text": "add a weather tool"}) in bridge_requests
    events = browser_events(session)
    assert [event for event in events if event["type"] == "working"] == [
        {"type": "working", "by": "claude"},
        {"type": "working", "by": ""},
    ]
    panel_update = next(event for event in events if event["type"] == "claude_reply")
    assert panel_update["say"] == "Two ways to do it."
    assert panel_update["markdown"].startswith("## Plan")
    assert panel_update["question"]["options"] == ["Open-Meteo", "Weather.gov"]

    await call_tool(connection, "ask_claude", {"request": "option 2"}, "c3")
    assert bridge_requests[-1][2] == {"text": 'My answer to "Which source?": Weather.gov'}
    await session.close()
    await run_task


async def test_ship_puts_the_pull_request_link_on_screen():
    connection, session, run_task, bridge_requests = await start_claude_session()
    await call_tool(connection, "switch_backend", {"target": "claude", "topic": "weather tool"}, "c1")

    output = await call_tool(connection, "claude_action", {"action": "ship", "title": "Add weather tool"}, "c2")
    assert "shipped" in output
    assert ("POST", "/topics/weather tool/ship", {"title": "Add weather tool"}) in bridge_requests
    assert {"type": "claude_link", "label": "Pull request", "url": PR_URL} in browser_events(session)
    await session.close()
    await run_task


async def test_claude_unavailable_device_not_allowed_or_bridge_failing():
    connection, session, run_task, _bridge_requests = await start_claude_session(configured=False)
    output = await call_tool(connection, "switch_backend", {"target": "claude", "topic": "weather"}, "c1")
    assert "isn't set up" in output
    assert not session.claude.active
    await session.close()
    await run_task

    connection, session, run_task, bridge_requests = await start_claude_session(claude_devices=("tablet",))
    output = await call_tool(connection, "switch_backend", {"target": "claude", "topic": "weather"}, "c1")
    assert "isn't allowed" in output
    assert not bridge_requests
    await session.close()
    await run_task

    connection, session, run_task, _bridge_requests = await start_claude_session()
    await call_tool(connection, "switch_backend", {"target": "claude", "topic": "broken"}, "c1")
    output = await call_tool(connection, "ask_claude", {"request": "hi"}, "c2")
    assert "failed" in output
    error = next(event for event in browser_events(session) if event["type"] == "error")
    assert error["reference"]
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


async def test_tapped_option_goes_to_claude(web_client):
    client, *_ = web_client
    session_id = (await client.post("/realtime/call", json=OFFER, headers=AUTHORIZED)).json()["session_id"]
    _token_hash, session = web.state.voice_sessions[session_id]
    answer = {"session_id": session_id, "text": "option 1"}

    assert (await client.post("/realtime/claude", json=answer, headers=AUTHORIZED)).status_code == 409
    session.claude.active, session.claude.topic = True, "weather tool"
    other_device = {"Authorization": "Bearer other-device-token"}
    assert (await client.post("/realtime/claude", json=answer, headers=other_device)).status_code == 404

    response = await client.post("/realtime/claude", json=answer, headers=AUTHORIZED)
    assert response.status_code == 202
    await wait_until(lambda: session.claude.last_question is not None)  # Claude's reply reached the session


def test_voice_speaks_a_summary_and_the_screen_gets_everything():
    long_say = "First point is short. " + "Then a very long explanation. " * 20
    long_options = ["Use a small local model that runs on the Mac", "Call the hosted forecast API every hour"]
    reply = {"say": long_say, "question": {"text": "Which one?", "options": long_options}}
    spoken = ClaudeMode.__new__(ClaudeMode)._spoken(reply)

    assert len(spoken) < 400
    assert spoken.endswith("Then ask: Which one? 2 options are on screen; answer with a number.")
    assert "Details are on screen." in spoken
    assert summary(long_say).endswith(".")
