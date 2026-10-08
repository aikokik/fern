"""fern_web offline: fake brain (httpx.MockTransport), fake OpenAI handshake, fake Realtime connection."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from fern_web import app as web
from fern_web.brain_client import BrainClient
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


def voice_session(connection: FakeRealtimeConnection, recorded_calls: list, chat_status: int = 200) -> VoiceSession:
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
    )


def response_done(status: str, arguments: str, call_id: str = "c1") -> SimpleNamespace:
    """A Realtime `response.done` event whose output is one ask_fern call."""
    call = SimpleNamespace(type="function_call", name="ask_fern", call_id=call_id, arguments=arguments)
    return SimpleNamespace(type="response.done", response=SimpleNamespace(status=status, output=[call]))


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
    assert "tap Approve" in tool_output["output"]  # spoken yes can't approve
    assert connection.sent[1] == {"type": "response.create"}
    assert ("POST", "/v1/chat", {"text": "unlock the door", "session": "voice"}) in recorded_calls
    alarm = {"type": "alarm.set", "time": "07:00", "label": ""}
    assert await session.browser_events.get() == {"type": "command", "command": alarm}
    assert (await session.browser_events.get())["type"] == "pending"

    await session.close()
    await run_task
    assert (await session.browser_events.get()) == {"type": "end"}
    voice_log = next(call for call in recorded_calls if call[1] == "/v1/voice-log")[2]
    assert voice_log["transcript"] == [{"role": "user", "text": "unlock the door"}]


async def test_interrupted_or_malformed_tool_calls_never_reach_the_brain():
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
    error_card = await session.browser_events.get()
    assert error_card["type"] == "error"
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
    assert {function["name"] for function in session_config["tools"]} == {"ask_fern", "end_conversation"}
    assert "Sam wakes at 7" in session_config["instructions"]  # personalization
    assert session_config["max_output_tokens"] > 0  # spoken replies are capped
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
