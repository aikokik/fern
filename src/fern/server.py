"""HTTP API for devices (iOS Shortcuts, iPad, TV, laptop). Per-device bearer tokens; sessions are device-scoped."""

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from fern.brain import Brain
from fern.config import settings

brain: Brain
log = logging.getLogger("fern")


async def scheduler() -> None:
    """Proactive work: follow Claude Code jobs to their PRs and announce results (no model calls)."""
    while True:
        await asyncio.sleep(300)
        try:
            await brain.check_builds()
        except Exception:
            log.exception("check_builds failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    global brain
    if not settings.device_tokens or any(len(token) < 24 for token in settings.device_tokens.values()):
        raise RuntimeError("Set FERN_DEVICE_TOKENS=phone=<token>,... with tokens from `openssl rand -hex 24`")
    async with Brain(settings) as brain:
        scheduler_task = asyncio.create_task(scheduler())
        yield
        scheduler_task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def device(authorization: str = Header("")) -> str:
    """Bearer token -> device name. Constant-time comparison so tokens can't be guessed by timing."""
    presented = authorization.removeprefix("Bearer ")
    for device_name, expected in settings.device_tokens.items():
        if secrets.compare_digest(presented, expected):
            return device_name
    raise HTTPException(401)


class Chat(BaseModel):
    text: str = Field(max_length=4000)
    session: str | None = Field(None, pattern=r"^[\w-]{1,64}$")
    model: str | None = Field(None, max_length=64)  # alias from FERN_MODELS, switches this session


class Decision(BaseModel):
    approve: bool


@app.post("/v1/chat")
async def chat(body: Chat, device_name: str = Depends(device)) -> dict:
    session = f"{device_name}-{body.session or 'main'}"
    return await brain.run(body.text, device=device_name, session=session, model=body.model)


@app.post("/v1/approvals/{pending_id}")
async def approve(pending_id: str, body: Decision, _: str = Depends(device)) -> dict:
    try:
        return await brain.resolve(pending_id, body.approve)
    except KeyError:
        raise HTTPException(404) from None  # unknown or expired; don't leak which


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> str:
    return "ok"


@app.get("/v1/profile")
async def profile(device_name: str = Depends(device)) -> dict:
    return brain.profile(device_name)


@app.get("/v1/notices")
async def notices(device_name: str = Depends(device)) -> list[str]:
    return brain.notices_for(device_name)


class VoiceLog(BaseModel):
    seconds: float = Field(ge=0, le=86400)
    transcript: list[dict[str, str]] = Field(default_factory=list, max_length=2000)
    usage: dict[str, int | str] = Field(default_factory=dict)  # voice model tokens: model, requests, input, ...


@app.post("/v1/voice-log", status_code=204)
async def voice_log(body: VoiceLog, device_name: str = Depends(device)) -> None:
    brain.voice_log(device_name, body.seconds, body.transcript, body.usage)


@app.get("/v1/sessions/{session}/stats")
async def stats(session: str, device_name: str = Depends(device)) -> list[dict]:
    return brain.store.stats(f"{device_name}-{session}")
