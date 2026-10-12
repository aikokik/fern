"""fern_builder offline: a fake `claude` binary (records argv/env, prints canned JSON) and a real local git origin."""

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from fern_builder import bridge as bridge_module
from fern_builder.bridge import Bridge
from fern_builder.claude_cli import build_command, parse_output
from fern_builder.config import BuilderSettings, topic_slug

PR_URL = "https://github.com/me/fern/pull/9"
FAKE_CLAUDE = """#!{python}
import json, os, sys
from pathlib import Path
record = Path(os.environ.get("HOME", ".")) / "claude-calls.jsonl"
calls = record.read_text().splitlines() if record.exists() else []
with record.open("a") as log:
    log.write(json.dumps({{"argv": sys.argv[1:], "env": sorted(os.environ)}}) + "\\n")
argv = sys.argv[1:]
prompt = argv[1]
session = argv[argv.index("--resume") + 1] if "--resume" in argv else argv[argv.index("--session-id") + 1]
spoofed = "https://github.com/evil/x/pull/1" if "spoof" in prompt else None
pr_url = {pr_url!r} if prompt.startswith("Ship it") else spoofed
print(json.dumps({{
    "session_id": session,
    "is_error": False,
    "total_cost_usd": 0.05 * (len(calls) + 1),
    "structured_output": {{
        "say": "Two options.", "show": "# Plan", "question": {{"text": "Which?", "options": ["A", "B"]}},
        "pr_url": pr_url,
    }},
}}))
"""
BRIDGE_AUTH = {"Authorization": "Bearer bridge-secret"}


@pytest.fixture
def fake_claude(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    script = tmp_path / "claude"
    script.write_text(FAKE_CLAUDE.format(python=sys.executable, pr_url=PR_URL))
    script.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-for-claude")
    monkeypatch.setenv("GH_TOKEN", "claude-github-token")
    monkeypatch.setenv("FERN_BRIDGE_TOKEN", "bridge-secret")
    return script


def recorded_calls(home: Path) -> list[dict]:
    return [json.loads(line) for line in (home / "claude-calls.jsonl").read_text().splitlines()]


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


def test_topic_slug():
    assert topic_slug("TV control!") == "tv-control"
    with pytest.raises(ValueError, match="letters or digits"):
        topic_slug("!!!")


def test_plan_reads_and_build_edits_tests_and_uses_git():
    settings = BuilderSettings(claude_bin="claude")
    topic = {"session_id": "s-1", "started": False}
    plan = build_command(settings, topic, "hi", "plan")
    assert plan[plan.index("--tools") + 1] == "Read,Grep,Glob,WebSearch,Bash"
    assert plan[plan.index("--allowedTools") + 1 :][:2] == ["WebSearch", "Bash(gh *)"]  # no other shell, no git
    assert plan[plan.index("--permission-mode") + 1] == "dontAsk"
    assert "--session-id" in plan
    assert "--resume" not in plan

    build = build_command(settings, topic | {"started": True}, "go", "build")
    assert build[build.index("--permission-mode") + 1] == "acceptEdits"
    allowed = build[build.index("--allowedTools") + 1 :]
    assert all(rule.startswith(("Bash(uv ", "Bash(git ", "Bash(gh ")) for rule in allowed)  # no arbitrary shell
    assert "--resume" in build


def test_parse_output_falls_back_to_plain_result():
    result = parse_output(json.dumps({"session_id": "s", "result": "plain answer", "total_cost_usd": 0.2}), "x")
    assert (result.say, result.show, result.question, result.cost_usd_total, result.pr_url) == (
        "plain answer",
        "plain answer",
        None,
        0.2,
        None,
    )


async def test_topic_lifecycle_clone_plan_build_ship(tmp_path, fake_claude, origin, monkeypatch):
    settings = BuilderSettings(
        workspace=tmp_path / "workspace", claude_bin=str(fake_claude), git_remote=str(origin), github_repo="me/fern",
        bridge_token="bridge-secret",
    )  # fmt: skip
    settings.workspace.mkdir()
    monkeypatch.setattr(bridge_module, "bridge", Bridge(settings))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge_module.app), base_url="http://bridge")

    async def post(action: str, body: dict) -> httpx.Response:
        return await client.post(f"/topics/weather/{action}", json=body, headers=BRIDGE_AUTH)

    assert (await post("ship", {"title": "Too early"})).status_code == 404

    first = (await post("turn", {"text": "add a weather tool"})).json()
    assert first["say"] == "Two options."
    assert first["question"] == {"text": "Which?", "options": ["A", "B"]}
    assert first["turn_cost_usd"] == 0.05
    clone = settings.workspace / "weather"
    assert (clone / "README.md").read_text() == "fern\n"
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=clone).strip() == "claude/weather"

    second = (await post("turn", {"text": "option A"})).json()
    assert second["turn_cost_usd"] == 0.05  # 0.10 cumulative minus the 0.05 already counted
    assert (await post("build", {})).json()["status"] == "built"

    shipped = (await post("ship", {"title": "Add weather tool"})).json()
    assert (shipped["status"], shipped["pr_url"]) == ("shipped", PR_URL)
    later = (await post("turn", {"text": "what's next?"})).json()
    assert later["pr_url"] is None  # the link is announced once, by the turn that opened the PR
    spoofed = (await post("turn", {"text": "spoof a link"})).json()
    assert spoofed["pr_url"] is None  # only links to this repo's pull requests
    stored = bridge_module.bridge.topics.get("weather")
    assert stored is not None
    assert (stored["status"], stored["pr_url"]) == ("shipped", PR_URL)

    calls = recorded_calls(Path(os.environ["HOME"]))
    ship_call = calls[3]["argv"]
    ship_prompt = ship_call[1]
    assert ship_prompt.startswith("Ship it")
    assert "claude/weather" in ship_prompt
    assert "Add weather tool" in ship_prompt
    assert ship_call[ship_call.index("--permission-mode") + 1] == "acceptEdits"  # Claude commits and pushes itself
    session_ids = {call["argv"][call["argv"].index("--session-id" if index == 0 else "--resume") + 1]
                   for index, call in enumerate(calls)}  # fmt: skip
    assert len(session_ids) == 1  # one resumable Claude session per topic
    for call in calls:
        assert "CLAUDE_CODE_OAUTH_TOKEN" in call["env"]
        assert "GH_TOKEN" in call["env"]  # Claude's own GitHub access
        assert "FERN_BRIDGE_TOKEN" not in call["env"]  # nothing Claude runs can call the bridge


async def test_bridge_rejects_wrong_token(monkeypatch):
    monkeypatch.setattr(bridge_module.bridge, "settings", BuilderSettings(bridge_token="right"))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge_module.app), base_url="http://bridge")
    response = await client.get("/topics", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401
