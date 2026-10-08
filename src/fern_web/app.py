"""Voice web interface: serves the page, does the WebRTC handshake with OpenAI, runs the server-side channel.
Auth = brain device tokens (verified by the brain on every call)."""

import argparse
import asyncio
import hashlib
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from fern_web.brain_client import BrainClient, BrainError
from fern_web.config import web_settings
from fern_web.realtime import start_call
from fern_web.sideband import VoiceSession

STATIC_DIR = Path(__file__).parent / "static"
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
MIN_SECONDS_TO_START = 30
SSE_KEEPALIVE_SECONDS = 15
FORGET_SESSION_AFTER_SECONDS = 60


class AppState:
    brain: BrainClient  # created in lifespan (needs the event loop / API key)
    openai: AsyncOpenAI

    def __init__(self):
        self.voice_sessions: dict[str, tuple[str, VoiceSession]] = {}  # session id -> (device token hash, session)


state = AppState()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    state.brain = BrainClient(web_settings.brain_url)
    state.openai = AsyncOpenAI()
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.update(
        {
            "Content-Security-Policy": CONTENT_SECURITY_POLICY,
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Permissions-Policy": "microphone=(self), camera=(), geolocation=()",
        }
    )
    return response


def bearer_token(authorization: str = Header("")) -> str:
    if not (token := authorization.removeprefix("Bearer ")):
        raise HTTPException(401)
    return token


async def fetch_profile(device_token: str) -> dict:
    """The brain's profile call doubles as authentication of the device token."""
    try:
        return await state.brain.profile(device_token)
    except BrainError as error:
        raise HTTPException(401 if error.status == 401 else 502) from error


def remaining_voice_seconds(profile: dict) -> float:
    return web_settings.daily_minutes * 60 - profile.get("voice_seconds_today", 0)


def _hash(device_token: str) -> str:
    return hashlib.sha256(device_token.encode()).hexdigest()


def owned_session(session_id: str, device_token: str) -> VoiceSession:
    """Only the device that started a voice session may watch or control it."""
    entry = state.voice_sessions.get(session_id)
    if not entry or not secrets.compare_digest(entry[0], _hash(device_token)):
        raise HTTPException(404)
    return entry[1]


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
async def healthz() -> JSONResponse:
    brain_ok = await state.brain.healthy()
    return JSONResponse({"web": True, "brain": brain_ok}, status_code=200 if brain_ok else 503)


class Offer(BaseModel):
    sdp: str = Field(min_length=10, max_length=20000)


@app.post("/realtime/call")
async def start_voice_call(body: Offer, device_token: str = Depends(bearer_token)) -> dict:
    """WebRTC handshake via fern, then attach the server-side channel to the same call."""
    profile = await fetch_profile(device_token)
    seconds_left = remaining_voice_seconds(profile)
    if seconds_left < MIN_SECONDS_TO_START:
        raise HTTPException(429, "Daily voice limit reached")
    answer_sdp, call_id = await start_call(state.openai, web_settings, profile, body.sdp)
    session_id = secrets.token_urlsafe(16)
    voice_session = VoiceSession(
        session_id,
        device_token,
        call_id,
        state.brain,
        connect=lambda call: state.openai.realtime.connect(call_id=call),
        hangup=lambda call: state.openai.realtime.calls.hangup(call),
        max_seconds=seconds_left,
        idle_seconds=web_settings.idle_seconds,
        notice_poll_seconds=web_settings.notice_poll_seconds,
    )
    state.voice_sessions[session_id] = (_hash(device_token), voice_session)

    def forget_later(_task: asyncio.Task) -> None:
        loop = asyncio.get_running_loop()
        loop.call_later(FORGET_SESSION_AFTER_SECONDS, state.voice_sessions.pop, session_id, None)

    voice_session.task = asyncio.create_task(voice_session.run())
    voice_session.task.add_done_callback(forget_later)
    return {"sdp": answer_sdp, "session_id": session_id, "remaining_seconds": int(seconds_left)}


@app.get("/realtime/events")
async def stream_events(session_id: str, device_token: str = Depends(bearer_token)) -> StreamingResponse:
    voice_session = owned_session(session_id, device_token)

    async def server_sent_events():
        while True:
            try:
                event = await asyncio.wait_for(voice_session.browser_events.get(), SSE_KEEPALIVE_SECONDS)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            yield f"data: {json.dumps(event)}\n\n"
            if event["type"] == "end":
                return

    return StreamingResponse(
        server_sent_events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"}
    )


class Decision(BaseModel):
    session_id: str
    pending_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    approve: bool


@app.post("/realtime/approve")
async def approve(body: Decision, device_token: str = Depends(bearer_token)) -> dict:
    voice_session = owned_session(body.session_id, device_token)
    try:
        brain_response = await state.brain.approve(device_token, body.pending_id, body.approve)
    except BrainError as error:
        if error.status != 404:
            raise HTTPException(502) from error
        await voice_session.say("Notice: that approval expired; ask the user to repeat the request if needed.")
        raise HTTPException(404, "Approval expired") from error
    await voice_session.publish(brain_response)
    verdict = "approved" if body.approve else "rejected"
    await voice_session.say(f"Notice: the user {verdict} the action. Result: {brain_response.get('reply', '')}")
    return brain_response


class EndCall(BaseModel):
    session_id: str


@app.post("/realtime/end", status_code=204)
async def end_voice_call(body: EndCall, device_token: str = Depends(bearer_token)) -> None:
    voice_session = owned_session(body.session_id, device_token)
    voice_session.closed.set()
    await voice_session.close()


@app.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def brain_api(path: str, request: Request) -> Response:
    """Pass-through so Shortcuts and other clients reach the brain via the single entry point. The brain still
    authenticates every call; nothing here is trusted."""
    forwarded_headers = {
        name: value for name in ("authorization", "content-type") if (value := request.headers.get(name))
    }
    brain_response = await state.brain.http.request(
        request.method,
        f"/v1/{path}",
        params=request.query_params,
        content=await request.body(),
        headers=forwarded_headers,
    )
    return Response(
        brain_response.content, brain_response.status_code, media_type=brain_response.headers.get("content-type")
    )


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(prog="fern-web")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    arguments = parser.parse_args()
    uvicorn.run("fern_web.app:app", host=arguments.host, port=arguments.port)
