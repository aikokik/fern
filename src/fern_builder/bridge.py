"""Bridge: fern's voice interface talks to Claude Code through this service (sealed network, bearer token).

POST /topics/{name}/turn   {text}   one plan-mode turn; clones the repo on branch claude/<topic> on first use
POST /topics/{name}/build  {text?}  implement the agreed plan: edits + tests/lint
POST /topics/{name}/ship   {title}  Claude commits, pushes claude/<topic> and opens the PR with its own gh/git
GET  /topics                        resumable topics, newest first
"""

import argparse
import asyncio
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from fern_builder.claude_cli import Mode, run_turn
from fern_builder.config import BuilderSettings, builder_settings, topic_slug
from fern_builder.topics import TopicStore

BUILD_REQUEST = (
    "Implement the plan we agreed on now. Add or update tests, then run `uv run ruff check .`, "
    "`uv run ruff format .`, `uv run pyright src evals tests` and `uv run pytest -q` until they pass. "
    "In `show`, list what changed and the test results."
)


def ship_request(branch: str, title: str) -> str:
    return (
        f"Ship it: commit all changes on `{branch}`, push the branch, and open a pull request to main with "
        f"`gh pr create` titled {title!r} (what changed, how it was tested). If a PR for this branch is already "
        "open, just push. Set `pr_url` to the pull request's URL."
    )


class Bridge:
    def __init__(self, settings: BuilderSettings):
        self.settings = settings
        self.topics = TopicStore(settings.workspace / "topics.json")
        self._locks: dict[str, asyncio.Lock] = {}

    def lock(self, slug: str) -> asyncio.Lock:
        return self._locks.setdefault(slug, asyncio.Lock())  # one Claude turn per topic at a time

    async def _git(self, *arguments: str) -> None:
        process = await asyncio.create_subprocess_exec(
            "git", *arguments, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise HTTPException(502, f"git {arguments[0]} failed: {stderr.decode()[-200:]}")

    async def turn(self, slug: str, text: str, mode: Mode) -> dict:
        topic = self.topics.get(slug)
        workdir = self.settings.workspace / slug
        if topic is None:
            if not workdir.exists():
                await self._git("clone", "--quiet", self.settings.remote_url, str(workdir))
                await self._git("-C", str(workdir), "checkout", "--quiet", "-B", f"claude/{slug}")
            topic = self.topics.create(slug)
        result = await run_turn(self.settings, topic, workdir, text, mode)
        turn_cost = max(result.cost_usd_total - topic["cost_usd_total"], 0.0)  # resumed totals include past turns
        topic |= {"started": True, "session_id": result.session_id, "cost_usd_total": result.cost_usd_total}
        if mode == "build" and not result.is_error:
            topic["status"] = "built"
        pull_request_prefix = f"https://github.com/{self.settings.github_repo}/pull/"
        shipped = bool(result.pr_url and result.pr_url.startswith(pull_request_prefix))  # only this repo's PRs
        if shipped:
            topic |= {"status": "shipped", "pr_url": result.pr_url}
        self.topics.save(topic)
        return {
            "topic": slug,
            "status": topic["status"],
            "say": result.say,
            "show": result.show,
            "question": result.question,
            "is_error": result.is_error,
            "pr_url": result.pr_url if shipped else None,  # this turn's PR only, so the link shows once
            "turn_cost_usd": round(turn_cost, 4),
            "model_usage": result.model_usage,
        }


bridge = Bridge(builder_settings)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if not builder_settings.bridge_token:
        raise RuntimeError("Set FERN_BRIDGE_TOKEN")
    builder_settings.workspace.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def caller(authorization: str = Header("")) -> None:
    if not secrets.compare_digest(authorization.removeprefix("Bearer "), bridge.settings.bridge_token):
        raise HTTPException(401)


class Turn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


class Build(BaseModel):
    text: str = Field(default=BUILD_REQUEST, max_length=8000)


class Ship(BaseModel):
    title: str = Field(min_length=3, max_length=120)


def slug_of(name: str) -> str:
    try:
        return topic_slug(name)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


@app.post("/topics/{name}/turn", dependencies=[Depends(caller)])
async def plan_turn(name: str, body: Turn) -> dict:
    slug = slug_of(name)
    async with bridge.lock(slug):
        return await bridge.turn(slug, body.text, "plan")


@app.post("/topics/{name}/build", dependencies=[Depends(caller)])
async def build(name: str, body: Build) -> dict:
    slug = slug_of(name)
    async with bridge.lock(slug):
        if bridge.topics.get(slug) is None:
            raise HTTPException(404, "unknown topic: plan it first")
        return await bridge.turn(slug, body.text, "build")


@app.post("/topics/{name}/ship", dependencies=[Depends(caller)])
async def ship(name: str, body: Ship) -> dict:
    slug = slug_of(name)
    async with bridge.lock(slug):
        topic = bridge.topics.get(slug)
        if topic is None:
            raise HTTPException(404, "unknown topic: plan it first")
        return await bridge.turn(slug, ship_request(topic["branch"], body.title), "build")


@app.get("/topics", dependencies=[Depends(caller)])
async def topics() -> list[dict]:
    return bridge.topics.all()


@app.get("/healthz")
async def healthz() -> dict:
    return {"bridge": True}


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(prog="fern-bridge")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    arguments = parser.parse_args()
    uvicorn.run("fern_builder.bridge:app", host=arguments.host, port=arguments.port)
