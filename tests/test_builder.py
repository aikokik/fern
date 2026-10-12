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
from fern_builder import github_cli
from fern_builder import shipper as shipper_module
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
    assert plan[plan.index("--tools") + 1] == "Read,Grep,Glob,WebSearch,Bash"
    assert plan[plan.index("--allowedTools") + 1 :][:2] == ["WebSearch", "Bash(fern-github *)"]  # no other shell
    assert plan[plan.index("--permission-mode") + 1] == "dontAsk"
    assert "--session-id" in plan
    assert "--resume" not in plan

    build = build_command(settings, topic | {"started": True}, "go", "build")
    assert build[build.index("--permission-mode") + 1] == "acceptEdits"
    allowed = build[build.index("--allowedTools") + 1 :]
    assert all(rule.startswith(("Bash(uv ", "Bash(fern-github ")) for rule in allowed)  # no git or arbitrary shell
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


GITHUB_TOOLS = {"Authorization": "Bearer tools-secret"}


def fake_github_api(sent_requests: list, status: int = 200):
    def handle(request: httpx.Request) -> httpx.Response:
        sent_requests.append((request.method, request.url.path, json.loads(request.content or b"null")))
        if status != 200:
            return httpx.Response(status, json={"message": "echo of the request"})
        match request.method, request.url.path.removeprefix("/repos/me/fern"):
            case "GET", "/issues":
                return httpx.Response(200, json=[
                    {"number": 3, "title": "Bug", "state": "open", "user": {"login": "aikokik"}},
                    {"number": 4, "title": "A PR", "state": "open", "user": {"login": "x"}, "pull_request": {}},
                ])  # fmt: skip
            case "GET", "/issues/4":
                return httpx.Response(200, json={
                    "number": 4, "title": "A PR", "state": "open", "user": {"login": "x"}, "body": "hi", "comments": 0,
                    "pull_request": {},
                })  # fmt: skip
            case "GET", "/pulls/4":
                return httpx.Response(200, json={"head": {"ref": "claude/a", "sha": "s1"}, "base": {"ref": "main"}})
            case "GET", "/commits/s1/check-runs":
                return httpx.Response(
                    200, json={"check_runs": [{"name": "test", "status": "in_progress", "conclusion": None}]}
                )
            case "POST", "/issues":
                return httpx.Response(201, json={"number": 5, "html_url": "https://github.com/me/fern/issues/5"})
            case "PATCH", "/issues/4":
                return httpx.Response(200, json={})
        return httpx.Response(404)

    return handle


@pytest.fixture
def shipper_app(monkeypatch):
    sent_requests: list = []
    settings = BuilderSettings(
        github_repo="me/fern", github_token="gh", shipper_token="ship", github_tools_token="tools-secret"
    )
    monkeypatch.setattr(
        shipper_module, "shipper", Shipper(settings, httpx.MockTransport(fake_github_api(sent_requests)))
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=shipper_module.app), base_url="http://shipper")
    return client, sent_requests


async def test_github_tools_token_reaches_only_the_github_calls(shipper_app):
    client, sent_requests = shipper_app
    assert (await client.get("/github/list?kind=issues", headers={"Authorization": "Bearer ship"})).status_code == 401
    shipping = await client.post("/topics/weather/ship", json={"title": "Sneaky"}, headers=GITHUB_TOOLS)
    assert shipping.status_code == 401  # Claude's token can't ship (or prepare)

    issues = (await client.get("/github/list?kind=issues", headers=GITHUB_TOOLS)).json()
    assert [issue["number"] for issue in issues] == [3]  # PRs filtered out of the issues list
    pull = (await client.get("/github/item/4", headers=GITHUB_TOOLS)).json()
    assert pull["pull_request"] == {
        "head": "claude/a",
        "base": "main",
        "merged": False,
        "checks": {"test": "in_progress"},
    }

    created = await client.post(
        "/github/issues", json={"title": "Flaky test", "body": "Seen twice."}, headers=GITHUB_TOOLS
    )
    assert created.json() == {"number": 5, "url": "https://github.com/me/fern/issues/5"}
    assert sent_requests[-1][2]["body"].endswith("_Created by Claude Code via fern._")
    closed = await client.post("/github/item/4/state", json={"state": "closed"}, headers=GITHUB_TOOLS)
    assert closed.json() == {"number": 4, "state": "closed"}
    assert sent_requests[-1] == ("PATCH", "/repos/me/fern/issues/4", {"state": "closed"})
    assert not any(method == "PUT" or path.endswith("/merge") for method, path, _ in sent_requests)


async def test_github_failures_return_status_only(monkeypatch):
    settings = BuilderSettings(
        github_repo="me/fern", github_token="gh", shipper_token="ship", github_tools_token="tools-secret"
    )
    monkeypatch.setattr(
        shipper_module, "shipper", Shipper(settings, httpx.MockTransport(fake_github_api([], status=403)))
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=shipper_module.app), base_url="http://shipper")
    response = await client.get("/github/list?kind=prs", headers=GITHUB_TOOLS)
    assert response.status_code == 502
    assert response.json()["detail"] == "GitHub returned 403"


def test_fern_github_command_maps_to_shipper_calls(monkeypatch, capsys):
    sent_requests = []

    def shipper(request: httpx.Request) -> httpx.Response:
        sent_requests.append(
            (request.method, str(request.url), json.loads(request.content or b"null"), request.headers["authorization"])
        )
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        github_cli, "builder_settings", BuilderSettings(shipper_url="http://shipper", github_tools_token="tools-secret")
    )
    real_client = httpx.Client
    monkeypatch.setattr(
        github_cli.httpx, "Client", lambda **options: real_client(transport=httpx.MockTransport(shipper), **options)
    )

    assert github_cli.main(["list", "prs", "--state", "all"]) == 0
    assert github_cli.main(["create-issue", "--title", "Flaky test", "--body", "Seen twice."]) == 0
    assert github_cli.main(["reopen", "4"]) == 0
    assert [request[:3] for request in sent_requests] == [
        ("GET", "http://shipper/github/list?kind=prs&state=all", None),
        ("POST", "http://shipper/github/issues", {"title": "Flaky test", "body": "Seen twice."}),
        ("POST", "http://shipper/github/item/4/state", {"state": "open"}),
    ]
    assert all(request[3] == "Bearer tools-secret" for request in sent_requests)
    assert '"ok": true' in capsys.readouterr().out
