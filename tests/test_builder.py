"""fern_builder offline: a fake `claude` binary (records argv/env, prints canned JSON) and a real local git origin."""

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from fern_builder import bridge as bridge_module
from fern_builder.bridge import Bridge
from fern_builder.claude_cli import build_command, parse_output
from fern_builder.config import BuilderSettings, topic_slug
from fern_builder.shipper import GitError, Shipper

FAKE_CLAUDE = """#!{python}
import json, os, sys
from pathlib import Path
record = Path(os.environ.get("HOME", ".")) / "claude-calls.jsonl"
calls = record.read_text().splitlines() if record.exists() else []
with record.open("a") as log:
    log.write(json.dumps({{"argv": sys.argv[1:], "env": sorted(os.environ)}}) + "\\n")
argv = sys.argv[1:]
session = argv[argv.index("--resume") + 1] if "--resume" in argv else argv[argv.index("--session-id") + 1]
print(json.dumps({{
    "session_id": session,
    "is_error": False,
    "total_cost_usd": 0.05 * (len(calls) + 1),
    "structured_output": {{
        "say": "Two options.", "show": "# Plan", "question": {{"text": "Which?", "options": ["A", "B"]}},
    }},
}}))
"""


@pytest.fixture
def fake_claude(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    script = tmp_path / "claude"
    script.write_text(FAKE_CLAUDE.format(python=sys.executable))
    script.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-for-claude")
    monkeypatch.setenv("FERN_BRIDGE_TOKEN", "bridge-secret")
    monkeypatch.setenv("FERN_SHIPPER_TOKEN", "shipper-secret")
    monkeypatch.setenv("FERN_BUILDER_GITHUB_TOKEN", "github-secret")
    return script


def recorded_calls(home: Path) -> list[dict]:
    return [json.loads(line) for line in (home / "claude-calls.jsonl").read_text().splitlines()]


def test_topic_slug():
    assert topic_slug("TV control!") == "tv-control"
    with pytest.raises(ValueError, match="letters or digits"):
        topic_slug("!!!")


def test_plan_is_read_only_and_build_can_only_edit_and_test():
    settings = BuilderSettings(claude_bin="claude")
    topic = {"session_id": "s-1", "started": False}
    plan = build_command(settings, topic, "hi", "plan")
    assert plan[plan.index("--tools") + 1] == "Read,Grep,Glob,WebSearch"
    assert plan[plan.index("--permission-mode") + 1] == "dontAsk"
    assert "--session-id" in plan
    assert "--resume" not in plan

    build = build_command(settings, topic | {"started": True}, "go", "build")
    assert build[build.index("--permission-mode") + 1] == "acceptEdits"
    allowed = build[build.index("--allowedTools") + 1 :]
    assert all(rule.startswith("Bash(uv ") for rule in allowed)  # no git, no network, no arbitrary shell
    assert "--resume" in build


def test_parse_output_falls_back_to_plain_result():
    result = parse_output(json.dumps({"session_id": "s", "result": "plain answer", "total_cost_usd": 0.2}), "x")
    assert (result.say, result.show, result.question, result.cost_usd_total) == (
        "plain answer",
        "plain answer",
        None,
        0.2,
    )


async def test_bridge_topic_lifecycle_and_minimal_child_env(tmp_path, fake_claude):
    shipper_calls = []

    def shipper(request: httpx.Request) -> httpx.Response:
        shipper_calls.append((request.url.path, request.headers["authorization"]))
        if request.url.path.endswith("/prepare"):
            (tmp_path / "workspace" / "weather").mkdir(parents=True)
            return httpx.Response(200, json={"branch": "claude/weather"})
        return httpx.Response(200, json={"commit": "abc", "pr_url": "https://github.com/me/fern/pull/9"})

    settings = BuilderSettings(
        workspace=tmp_path / "workspace", claude_bin=str(fake_claude), shipper_url="http://shipper",
        shipper_token="shipper-secret",
    )  # fmt: skip
    (tmp_path / "workspace").mkdir()
    bridge = Bridge(settings, httpx.MockTransport(shipper))

    first = await bridge.turn("weather", "add a weather tool", "plan")
    assert first["say"] == "Two options."
    assert first["question"] == {"text": "Which?", "options": ["A", "B"]}
    assert first["turn_cost_usd"] == 0.05
    second = await bridge.turn("weather", "option A", "plan")
    assert second["turn_cost_usd"] == 0.05  # 0.10 cumulative minus the 0.05 already counted
    built = await bridge.turn("weather", "build it", "build")
    assert built["status"] == "built"
    shipped = await bridge.ship("weather", "Add weather tool")
    assert shipped["pr_url"] == "https://github.com/me/fern/pull/9"
    stored = bridge.topics.get("weather")
    assert stored is not None
    assert stored["status"] == "shipped"

    calls = recorded_calls(Path(os.environ["HOME"]))
    session_ids = {call["argv"][call["argv"].index("--session-id" if index == 0 else "--resume") + 1]
                   for index, call in enumerate(calls)}  # fmt: skip
    assert len(session_ids) == 1  # one resumable Claude session per topic
    for call in calls:
        assert "CLAUDE_CODE_OAUTH_TOKEN" in call["env"]
        for secret in ("FERN_BRIDGE_TOKEN", "FERN_SHIPPER_TOKEN", "FERN_BUILDER_GITHUB_TOKEN"):
            assert secret not in call["env"]  # nothing Claude runs can see the other services' tokens
    assert [path for path, _ in shipper_calls] == ["/topics/weather/prepare", "/topics/weather/ship"]
    assert all(auth == "Bearer shipper-secret" for _, auth in shipper_calls)


async def test_bridge_rejects_wrong_token(monkeypatch):
    monkeypatch.setattr(bridge_module.bridge, "settings", BuilderSettings(bridge_token="right", shipper_token="x"))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge_module.app), base_url="http://bridge")
    response = await client.get("/topics", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


def git(*arguments: str, cwd: Path) -> str:
    return subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def origin(tmp_path) -> Path:
    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "-q", "-b", "main", cwd=seed)
    (seed / "README.md").write_text("fern\n")
    git("add", ".", cwd=seed)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=seed)
    bare = tmp_path / "origin.git"
    git("clone", "-q", "--bare", str(seed), str(bare), cwd=tmp_path)
    return bare


async def test_shipper_syncs_plain_files_and_pushes_only_claude_branch(tmp_path, origin):
    pulls = []

    def github(request: httpx.Request) -> httpx.Response:
        pulls.append(json.loads(request.content))
        return httpx.Response(201, json={"html_url": "https://github.com/me/fern/pull/3"})

    settings = BuilderSettings(
        workspace=tmp_path / "workspace", repos=tmp_path / "repos", git_remote=str(origin), github_token="gh"
    )
    shipper = Shipper(settings, httpx.MockTransport(github))
    assert await shipper.prepare("weather") == {"branch": "claude/weather"}
    work = settings.workspace / "weather"
    assert (work / "README.md").read_text() == "fern\n"
    assert not (work / ".git").exists()  # Claude's tree has no git metadata to tamper with

    (work / "weather.py").write_text("print('sunny')\n")
    (work / ".env").write_text("SECRET=1\n")  # excluded from sync
    (work / "passwd").symlink_to("/etc/passwd")  # copied as a link, never followed
    shipped = await shipper.ship("weather", "Add weather", "body")
    assert shipped["pr_url"] == "https://github.com/me/fern/pull/3"
    assert pulls == [{"title": "Add weather", "head": "claude/weather", "base": "main", "body": "body"}]

    files = git("ls-tree", "-r", "--name-only", "claude/weather", cwd=origin).split()
    assert sorted(files) == ["README.md", "passwd", "weather.py"]
    assert git("cat-file", "-p", "claude/weather:passwd", cwd=origin) == "/etc/passwd"  # the link text only
    assert "main" in git("branch", cwd=origin)
    assert git("rev-parse", "main", cwd=origin) != shipped["commit"]  # main untouched

    with pytest.raises(GitError, match="nothing changed"):
        await shipper.ship("weather", "Again", "")


async def test_shipper_refuses_unprepared_topics(tmp_path):
    shipper = Shipper(replace(BuilderSettings(), workspace=tmp_path / "w", repos=tmp_path / "r"))
    with pytest.raises(GitError, match="never prepared"):
        await shipper.ship("nope", "Title", "")
