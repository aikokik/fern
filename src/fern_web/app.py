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
from fern_web.config import web_settings as S
from fern_web.realtime import start_call
from fern_web.sideband import VoiceSession

STATIC = Path(__file__).parent / "static"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


class State:
    brain: BrainClient
    openai: AsyncOpenAI
    sessions: dict[str, tuple[str, VoiceSession]] = {}  # sid -> (token hash, session)


state = State()


@asynccontextmanager
async def lifespan(_: FastAPI):
    state.brain = BrainClient(S.brain_url)
    state.openai = AsyncOpenAI()
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.update({
        "Content-Security-Policy": CSP, "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
        "Permissions-Policy": "microphone=(self), camera=(), geolocation=()",
    })
    return resp


def bearer(authorization: str = Header("")) -> str:
    if not (token := authorization.removeprefix("Bearer ")):
        raise HTTPException(401)
    return token


async def profile_of(token: str) -> dict:
    try:
        return await state.brain.profile(token)
    except BrainError as e:
        raise HTTPException(401 if e.status == 401 else 502)


def remaining_seconds(profile: dict) -> float:
    return S.daily_minutes * 60 - profile.get("voice_seconds_today", 0)


def owned(sid: str, token: str) -> VoiceSession:
    entry = state.sessions.get(sid)
    if not entry or not secrets.compare_digest(entry[0], _h(token)):
        raise HTTPException(404)
    return entry[1]


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
async def healthz() -> JSONResponse:
    ok = await state.brain.healthy()
    return JSONResponse({"web": True, "brain": ok}, status_code=200 if ok else 503)


class Offer(BaseModel):
    sdp: str = Field(min_length=10, max_length=20000)


@app.post("/realtime/call")
async def call(body: Offer, token: str = Depends(bearer)) -> dict:
    """WebRTC handshake via fern, then attach the server-side channel to the same call."""
    profile = await profile_of(token)
    if (left := remaining_seconds(profile)) < 30:
        raise HTTPException(429, "Daily voice limit reached")
    answer, call_id = await start_call(state.openai, S, profile, body.sdp)
    sid = secrets.token_urlsafe(16)
    vs = VoiceSession(
        sid, token, call_id, state.brain,
        connect=lambda cid: state.openai.realtime.connect(call_id=cid),
        hangup=lambda cid: state.openai.realtime.calls.hangup(cid),
        max_seconds=left, idle_seconds=S.idle_seconds,
        notice_poll=S.notice_poll_seconds,
    )
    state.sessions[sid] = (_h(token), vs)
    task = asyncio.create_task(vs.run())
    task.add_done_callback(lambda _: asyncio.get_running_loop().call_later(60, state.sessions.pop, sid, None))
    return {"sdp": answer, "sid": sid, "remaining_seconds": int(left)}


@app.get("/realtime/events")
async def events(sid: str, token: str = Depends(bearer)) -> StreamingResponse:
    vs = owned(sid, token)

    async def stream():
        while True:
            try:
                ev = await asyncio.wait_for(vs.events.get(), 15)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            yield f"data: {json.dumps(ev)}\n\n"
            if ev["type"] == "end":
                return

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})


class Decision(BaseModel):
    sid: str
    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    approve: bool


@app.post("/realtime/approve")
async def approve(body: Decision, token: str = Depends(bearer)) -> dict:
    vs = owned(body.sid, token)
    try:
        out = await state.brain.approve(token, body.id, body.approve)
    except BrainError as e:
        if e.status != 404:
            raise HTTPException(502)
        await vs.say("Notice: that approval expired; ask the user to repeat the request if they still want it.")
        raise HTTPException(404, "Approval expired")
    await vs.publish(out)
    verdict = "approved" if body.approve else "rejected"
    await vs.say(f"Notice: the user {verdict} the action. Result: {out.get('reply', '')}")
    return out


class End(BaseModel):
    sid: str


@app.post("/realtime/end", status_code=204)
async def end(body: End, token: str = Depends(bearer)) -> None:
    vs = owned(body.sid, token)
    vs.closed.set()
    await vs.close()


@app.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def brain_api(path: str, request: Request) -> Response:
    """Pass-through so Shortcuts and other clients reach the brain via the single entry point. The brain still
    authenticates every call; nothing here is trusted."""
    headers = {k: v for k in ("authorization", "content-type") if (v := request.headers.get(k))}
    r = await state.brain.http.request(
        request.method, f"/v1/{path}", params=request.query_params, content=await request.body(), headers=headers,
    )
    return Response(r.content, r.status_code, media_type=r.headers.get("content-type"))


def main() -> None:
    import uvicorn

    p = argparse.ArgumentParser(prog="fern-web")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8766)
    a = p.parse_args()
    uvicorn.run("fern_web.app:app", host=a.host, port=a.port)
