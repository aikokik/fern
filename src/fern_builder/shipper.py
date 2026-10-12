"""Shipper: the only service holding the GitHub token. It owns the real git clones (in /repos, which the builder
can't see) and only copies plain files to and from Claude's working tree (in /workspace). It also serves Claude's
`fern-github` command: read PRs, issues and CI, create issues, close or reopen. Never merge or push.

Why: anything Claude writes (code, tests, git hooks, git config filters) could run next to whatever secrets live
where git runs. Here git never reads Claude-controlled config or hooks, and only `claude/*` branches are pushed.
"""

import argparse
import asyncio
import base64
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from fern_builder.config import BuilderSettings, builder_settings, topic_slug

GITHUB_BODY_CHARS = 4000
CREATED_BY_CLAUDE = "\n\n_Created by Claude Code via fern._"
# Never copied between Claude's tree and the git clone.
SYNC_EXCLUDES = (".git", ".venv", "__pycache__", ".uv-cache", ".pytest_cache", ".ruff_cache", "data", ".env*")


class GitError(Exception):
    pass


class Shipper:
    def __init__(self, settings: BuilderSettings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.github_http = httpx.AsyncClient(base_url="https://api.github.com", timeout=30, transport=transport)
        self._locks: dict[str, asyncio.Lock] = {}

    def lock(self, slug: str) -> asyncio.Lock:
        return self._locks.setdefault(slug, asyncio.Lock())

    async def _run(self, *command: str, cwd: Path | None = None) -> str:
        process = await asyncio.create_subprocess_exec(
            *command, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise GitError(f"{command[0]} {command[1] if len(command) > 1 else ''} failed: {stderr.decode()[-300:]}")
        return stdout.decode()

    async def _git(self, *arguments: str, cwd: Path | None = None) -> str:
        safety = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]
        if self.settings.github_token:  # per-command header: the token is never written into .git/config
            basic = base64.b64encode(f"x-access-token:{self.settings.github_token}".encode()).decode()
            safety += ["-c", f"http.extraheader=AUTHORIZATION: basic {basic}"]
        return await self._run("git", *safety, *arguments, cwd=cwd)

    async def _sync(self, source: Path, destination: Path) -> None:
        excludes = [f"--exclude={pattern}" for pattern in SYNC_EXCLUDES]
        # -a keeps symlinks as symlinks (never follows them), --delete mirrors removals; excluded paths survive.
        await self._run("rsync", "-a", "--delete", *excludes, f"{source}/", f"{destination}/")

    async def prepare(self, slug: str) -> dict:
        """Clone into /repos/<slug> on branch claude/<slug>, then copy the plain files into /workspace/<slug>."""
        repo_dir, work_dir, branch = self.settings.repos / slug, self.settings.workspace / slug, f"claude/{slug}"
        if not repo_dir.exists():
            await self._git("clone", "--quiet", self.settings.remote_url, str(repo_dir))
            await self._git("checkout", "--quiet", "-B", branch, cwd=repo_dir)
        if not work_dir.exists():
            work_dir.mkdir(parents=True)
            await self._sync(repo_dir, work_dir)
        return {"branch": branch}

    async def ship(self, slug: str, title: str, body: str) -> dict:
        repo_dir, work_dir, branch = self.settings.repos / slug, self.settings.workspace / slug, f"claude/{slug}"
        if not repo_dir.exists() or not work_dir.exists():
            raise GitError("topic was never prepared")
        current = (await self._git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo_dir)).strip()
        if current != branch or not branch.startswith("claude/"):
            raise GitError(f"refusing to ship from {current!r}")
        await self._sync(work_dir, repo_dir)
        await self._git("add", "-A", cwd=repo_dir)
        if not (await self._git("status", "--porcelain", cwd=repo_dir)).strip():
            raise GitError("nothing changed")
        await self._git(
            "-c", "user.name=fern builder", "-c", "user.email=builder@fern.local",
            "commit", "--quiet", "-m", title, cwd=repo_dir,
        )  # fmt: skip
        await self._git("push", "--quiet", "origin", f"HEAD:refs/heads/{branch}", cwd=repo_dir)
        commit = (await self._git("rev-parse", "HEAD", cwd=repo_dir)).strip()
        return {"commit": commit, "pr_url": await self._open_pull_request(branch, title, body)}

    def _github_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.github_token}", "Accept": "application/vnd.github+json"}

    async def github(self, method: str, path: str, **request_options: Any) -> Any:
        """One call to this repo's GitHub API (`path` is relative to /repos/<owner>/<name>)."""
        response = await self.github_http.request(
            method, f"/repos/{self.settings.github_repo}{path}", headers=self._github_headers(), **request_options
        )
        response.raise_for_status()
        return response.json() if response.content else {}

    async def read_item(self, number: int) -> dict:
        item = await self.github("GET", f"/issues/{number}")
        comments = await self.github("GET", f"/issues/{number}/comments") if item.get("comments") else []
        result = {
            "number": number,
            "title": item["title"],
            "state": item["state"],
            "author": item["user"]["login"],
            "body": (item.get("body") or "")[:GITHUB_BODY_CHARS],
            "comments": [{"author": comment["user"]["login"], "body": comment["body"][:1000]} for comment in comments],
        }
        if "pull_request" in item:
            pull = await self.github("GET", f"/pulls/{number}")
            check_runs = (await self.github("GET", f"/commits/{pull['head']['sha']}/check-runs"))["check_runs"]
            result["pull_request"] = {
                "head": pull["head"]["ref"],
                "base": pull["base"]["ref"],
                "merged": bool(pull.get("merged_at")),
                "checks": {check["name"]: check["conclusion"] or check["status"] for check in check_runs},
            }
        return result

    async def _open_pull_request(self, branch: str, title: str, body: str) -> str | None:
        if not self.settings.github_token:
            return None
        headers = self._github_headers()
        repo = self.settings.github_repo
        payload = {"title": title, "head": branch, "base": "main", "body": body}
        response = await self.github_http.post(f"/repos/{repo}/pulls", json=payload, headers=headers)
        if response.status_code == 422:  # already open for this branch: the push updated it
            owner = repo.split("/")[0]
            existing = await self.github_http.get(
                f"/repos/{repo}/pulls", params={"head": f"{owner}:{branch}", "state": "open"}, headers=headers
            )
            pulls = existing.json() if existing.status_code == 200 else []
            return pulls[0]["html_url"] if pulls else None
        response.raise_for_status()
        return response.json()["html_url"]


shipper = Shipper(builder_settings)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if not builder_settings.shipper_token:
        raise RuntimeError("Set FERN_SHIPPER_TOKEN")
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def caller(authorization: str = Header("")) -> None:
    if not secrets.compare_digest(authorization.removeprefix("Bearer "), shipper.settings.shipper_token):
        raise HTTPException(401)


class ShipRequest(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    body: str = Field(default="", max_length=20_000)


@app.post("/topics/{name}/prepare", dependencies=[Depends(caller)])
async def prepare(name: str) -> dict:
    slug = topic_slug(name)
    async with shipper.lock(slug):
        try:
            return await shipper.prepare(slug)
        except GitError as error:
            raise HTTPException(502, str(error)) from None


@app.post("/topics/{name}/ship", dependencies=[Depends(caller)])
async def ship(name: str, body: ShipRequest) -> dict:
    slug = topic_slug(name)
    async with shipper.lock(slug):
        try:
            return await shipper.ship(slug, body.title, body.body)
        except GitError as error:
            raise HTTPException(409, str(error)) from None


def github_tools_caller(authorization: str = Header("")) -> None:
    """Claude's token: only the /github routes below accept it (never prepare or ship)."""
    expected = shipper.settings.github_tools_token
    if not expected or not secrets.compare_digest(authorization.removeprefix("Bearer "), expected):
        raise HTTPException(401)


async def _github_or_502(method: str, path: str, **request_options: Any) -> Any:
    try:
        return await shipper.github(method, path, **request_options)
    except httpx.HTTPStatusError as error:  # the status only: GitHub's error text can echo the request
        raise HTTPException(502, f"GitHub returned {error.response.status_code}") from None


@app.get("/github/list", dependencies=[Depends(github_tools_caller)])
async def github_list(kind: Literal["prs", "issues"], state: Literal["open", "closed", "all"] = "open") -> list:
    items = await _github_or_502("GET", "/pulls" if kind == "prs" else "/issues", params={"state": state})
    if kind == "issues":
        items = [item for item in items if "pull_request" not in item]  # the issues API lists PRs too
    return [
        {"number": item["number"], "title": item["title"], "state": item["state"], "author": item["user"]["login"]}
        for item in items[:30]
    ]


@app.get("/github/item/{number}", dependencies=[Depends(github_tools_caller)])
async def github_read(number: int) -> dict:
    try:
        return await shipper.read_item(number)
    except httpx.HTTPStatusError as error:
        raise HTTPException(502, f"GitHub returned {error.response.status_code}") from None


class NewIssue(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    body: str = Field(default="", max_length=10_000)


@app.post("/github/issues", dependencies=[Depends(github_tools_caller)])
async def github_create_issue(issue: NewIssue) -> dict:
    payload = {"title": issue.title, "body": issue.body + CREATED_BY_CLAUDE}
    created = await _github_or_502("POST", "/issues", json=payload)
    return {"number": created["number"], "url": created["html_url"]}


class NewState(BaseModel):
    state: Literal["open", "closed"]


@app.post("/github/item/{number}/state", dependencies=[Depends(github_tools_caller)])
async def github_set_state(number: int, change: NewState) -> dict:
    await _github_or_502("PATCH", f"/issues/{number}", json={"state": change.state})  # works for PRs too
    return {"number": number, "state": change.state}


@app.get("/healthz")
async def healthz() -> dict:
    return {"shipper": True}


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(prog="fern-shipper")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8771)
    arguments = parser.parse_args()
    uvicorn.run("fern_builder.shipper:app", host=arguments.host, port=arguments.port)
