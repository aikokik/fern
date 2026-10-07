"""fern_web offline: fake brain (httpx.MockTransport), fake OpenAI handshake, fake Realtime connection."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS

import httpx
import pytest

from fern_web import app as web
from fern_web.brain_client import BrainClient
from fern_web.sideband import VoiceSession

TOKEN = "t" * 32


def brain_transport(calls: list, voice_seconds: float = 0):
    def handle(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path, json.loads(req.content or b"null")))
        if req.headers["authorization"] != f"Bearer {TOKEN}":
            return httpx.Response(401)
        match req.url.path:
            case "/v1/profile":
                return httpx.Response(200, json={"device": "phone", "facts": ["Sam wakes at 7"],
                                                 "voice_seconds_today": voice_seconds})
            case "/v1/chat":
                return httpx.Response(200, json={
                    "reply": "Need approval", "tools": ["home_secure"],
                    "commands": [{"type": "alarm.set", "time": "07:00", "label": ""}],
                    "pending": [{"id": "a" * 32, "tool": "home_secure", "args": "{}"}],
                })
            case "/v1/notices":
                return httpx.Response(200, json=[])
            case "/v1/voice-log":
                return httpx.Response(204)
        return httpx.Response(404)
    return httpx.MockTransport(handle)


class FakeConn:
    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []

    async def send(self, ev):
        self.sent.append(ev)

    async def close(self):
        await self.q.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if (ev := await self.q.get()) is None:
            raise StopAsyncIteration
        return ev


def session(conn: FakeConn, calls: list, **kw) -> VoiceSession:
    @asynccontextmanager
    async def connect(_cid):
        yield conn

    async def hangup(_cid):
        pass

    return VoiceSession("sid", TOKEN, "rtc_1", BrainClient("http://brain", brain_transport(calls)),
                        connect, hangup, max_seconds=600, idle_seconds=600, notice_poll=600, **kw)


async def until(cond, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


async def test_tool_call_runs_on_server_channel():
    conn, calls = FakeConn(), []
    vs = session(conn, calls)
    task = asyncio.create_task(vs.run())
    await conn.q.put(NS(type="conversation.item.input_audio_transcription.completed", transcript="unlock the door"))
    await conn.q.put(NS(type="response.function_call_arguments.done", name="ask_fern", call_id="c1",
                        arguments='{"request": "unlock the door"}'))
    await until(lambda: len(conn.sent) >= 2)

    out = conn.sent[0]["item"]
    assert out["type"] == "function_call_output" and out["call_id"] == "c1"
    assert "tap Approve" in out["output"]  # spoken yes can't approve
    assert conn.sent[1] == {"type": "response.create"}
    assert ("POST", "/v1/chat", {"text": "unlock the door", "session": "voice"}) in calls
    assert await vs.events.get() == {"type": "command", "command": {"type": "alarm.set", "time": "07:00", "label": ""}}
    assert (await vs.events.get())["type"] == "pending"

    await vs.close()
    await task
    assert (await vs.events.get()) == {"type": "end"}
    log = next(c for c in calls if c[1] == "/v1/voice-log")[2]
    assert log["transcript"] == [{"role": "user", "text": "unlock the door"}]


async def test_say_injects_system_message():
    conn, calls = FakeConn(), []
    vs = session(conn, calls)
    task = asyncio.create_task(vs.run())
    await until(lambda: vs.conn is not None)
    await vs.say("Notice: build ready")
    assert conn.sent[0]["item"]["role"] == "system" and conn.sent[0]["item"]["content"][0]["text"] == "Notice: build ready"
    await vs.close()
    await task


async def test_idle_timeout_ends_session():
    conn, calls = FakeConn(), []
    vs = session(conn, calls)
    vs.idle_seconds = 0.5
    await asyncio.wait_for(vs.run(), 5)
    assert vs.closed.is_set()


@pytest.fixture
def client(monkeypatch):
    calls, sent = [], {}

    async def create(sdp, session, extra_headers):
        sent.update(sdp=sdp, session=session)
        return NS(http_response=NS(text="answer-sdp"), headers={"location": "/v1/realtime/calls/rtc_abc"})

    fake_openai = NS(realtime=NS(calls=NS(with_raw_response=NS(create=create), hangup=None), connect=None))
    monkeypatch.setattr(web.state, "openai", fake_openai, raising=False)
    monkeypatch.setattr(web.state, "brain", BrainClient("http://brain", brain_transport(calls)), raising=False)
    started = []
    monkeypatch.setattr(web.VoiceSession, "run", lambda self: started.append(self.call_id) or asyncio.sleep(0))
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url="http://fern")
    return c, sent, started, calls


async def test_call_handshake_server_side(client):
    c, sent, started, _ = client
    r = await c.post("/realtime/call", json={"sdp": "v=0 offer-sdp"}, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.json()["sdp"] == "answer-sdp" and "sid" in r.json()
    assert "rtc_abc" not in r.text  # call_id never leaves the server
    assert started == ["rtc_abc"]
    cfg = sent["session"]
    assert cfg["model"] and {t["name"] for t in cfg["tools"]} == {"ask_fern", "end_conversation"}
    assert "Sam wakes at 7" in cfg["instructions"]  # personalization
    assert "connect-src 'self';" in r.headers["content-security-policy"]


async def test_call_rejects_bad_token_and_cap(client, monkeypatch):
    c, _, started, _ = client
    r = await c.post("/realtime/call", json={"sdp": "v=0 offer-sdp"}, headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401 and not started
    monkeypatch.setattr(web.state, "brain", BrainClient("http://brain", brain_transport([], voice_seconds=10**6)))
    r = await c.post("/realtime/call", json={"sdp": "v=0 offer-sdp"}, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 429 and not started


async def test_events_scoped_to_owner(client):
    c, *_ = client
    r = await c.post("/realtime/call", json={"sdp": "v=0 offer-sdp"}, headers={"Authorization": f"Bearer {TOKEN}"})
    sid = r.json()["sid"]
    r = await c.post("/realtime/end", json={"sid": sid}, headers={"Authorization": "Bearer other-device-token"})
    assert r.status_code == 404
