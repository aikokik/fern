"""HTTP API for devices (iOS Shortcuts, iPad, TV, laptop). Per-device bearer tokens; sessions are device-scoped."""

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from fern.brain import Brain
from fern.config import settings

brain: Brain
log = logging.getLogger("fern")


async def scheduler() -> None:
    """Proactive work: pre-generate briefings before wake-ups (every minute), follow Claude Code jobs (every 5)."""
    tick = 0
    while True:
        await asyncio.sleep(60)
        tick += 1
        for every, job in ((1, brain.prepare_briefings), (5, brain.check_builds)):
            if tick % every == 0:
                try:
                    await job()
                except Exception:
                    log.exception("%s failed", job.__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global brain
    if not settings.device_tokens or any(len(t) < 24 for t in settings.device_tokens.values()):
        raise RuntimeError("Set FERN_DEVICE_TOKENS=phone=<token>,... with tokens from `openssl rand -hex 24`")
    async with Brain(settings) as brain:
        task = asyncio.create_task(scheduler())
        yield
        task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def device(authorization: str = Header("")) -> str:
    token = authorization.removeprefix("Bearer ")
    for name, t in settings.device_tokens.items():
        if secrets.compare_digest(token, t):
            return name
    raise HTTPException(401)


class Chat(BaseModel):
    text: str = Field(max_length=4000)
    session: str | None = Field(None, pattern=r"^[\w-]{1,64}$")
    model: str | None = Field(None, max_length=64)  # alias from FERN_MODELS, switches this session


class Decision(BaseModel):
    approve: bool


@app.post("/v1/chat")
async def chat(body: Chat, dev: str = Depends(device)) -> dict:
    return await brain.run(body.text, device=dev, session=f"{dev}-{body.session or 'main'}", model=body.model)


@app.post("/v1/approvals/{pending_id}")
async def approve(pending_id: str, body: Decision, _: str = Depends(device)) -> dict:
    try:
        return await brain.resolve(pending_id, body.approve)
    except KeyError:
        raise HTTPException(404)


@app.get("/v1/briefing", response_class=PlainTextResponse)
async def briefing(dev: str = Depends(device)) -> str:
    return (await brain.briefing(dev))["text"]


@app.get("/v1/briefing/audio")
async def briefing_audio(dev: str = Depends(device)) -> FileResponse:
    return FileResponse(await brain.briefing_audio(dev), media_type="audio/mpeg")


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> str:
    return "ok"


@app.get("/v1/profile")
async def profile(dev: str = Depends(device)) -> dict:
    return brain.profile(dev)


@app.get("/v1/notices")
async def notices(dev: str = Depends(device)) -> list[str]:
    return brain.notices_for(dev)


class VoiceLog(BaseModel):
    seconds: float = Field(ge=0, le=86400)
    transcript: list[dict[str, str]] = Field(default_factory=list, max_length=2000)


@app.post("/v1/voice-log", status_code=204)
async def voice_log(body: VoiceLog, dev: str = Depends(device)) -> None:
    brain.voice_log(dev, body.seconds, body.transcript)


@app.get("/v1/sessions/{session}/stats")
async def stats(session: str, dev: str = Depends(device)) -> list[dict]:
    return brain.store.stats(f"{dev}-{session}")
