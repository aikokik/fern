"""Offline tests: ScriptedModel stands in for the LLM, httpx.MockTransport for the network."""

import json
import time
from contextlib import AsyncExitStack
from dataclasses import replace

import httpx
import pytest
from agents.testing import ScriptedModel, assistant_message, function_call

from fern.brain import Brain, trim_history
from fern.config import Settings
from fern.core import EgressBlockedError, http_client
from fern.notify import push
from fern.store import GLOBAL

BUILDER = {"builder_url": "https://api.anthropic.com/v1/fire", "builder_token": "rt"}
PLAN_TV = {"kind": "plan", "spec": "Plan TV control."}


def offline(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(503)


def claude_code_accepts(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"claude_code_session_url": "https://claude.ai/code/session_1"})


@pytest.fixture
async def make_brain(tmp_path):
    """Build a Brain whose network is `handler` (default: offline), with optional settings overrides."""
    base_settings = Settings(data_dir=tmp_path, skills_dir=Settings().skills_dir)
    async with AsyncExitStack() as cleanup:

        async def build(handler=offline, **overrides) -> Brain:
            brain = Brain(replace(base_settings, **overrides), httpx.MockTransport(handler))
            return await cleanup.enter_async_context(brain)

        yield build


@pytest.fixture
async def brain(make_brain):
    return await make_brain()


def script(brain: Brain, steps: list, model_alias: str = "main") -> ScriptedModel:
    """Replace the orchestrator's LLM with a scripted one that returns `steps` in order."""
    scripted_model = ScriptedModel(steps)
    brain.agent(model_alias).model = scripted_model
    return scripted_model


def visible_tools(model_call) -> set[str]:
    return {function.name for function in model_call.tools}


async def test_action_finishes_in_one_model_call(brain):
    model = script(brain, [[function_call("set_alarm", {"time": "06:30", "label": "gym"}, call_id="c1")]])
    response = await brain.run("wake me 6:30 for gym", device="phone", session="s1")
    assert len(model.calls) == 1  # no second call just to restate the result
    assert response["reply"] == "Alarm set for 06:30."
    assert response["commands"] == [{"type": "alarm.set", "device": "phone", "time": "06:30", "label": "gym"}]
    assert response["tools"] == ["set_alarm"]
    assert not response["pending"]


async def test_errors_and_lookups_go_back_to_the_model(brain):
    model = script(
        brain,
        [
            [function_call("set_alarm", {"time": "7am", "label": ""}, call_id="c1")],  # error -> model retries
            [
                function_call("set_alarm", {"time": "07:00", "label": ""}, call_id="c2"),
                function_call("recall", {"query": "wake"}, call_id="c3"),  # lookup in the same step -> continue
            ],
            [assistant_message("Alarm set for 7. Nothing saved about waking.")],
        ],
    )
    response = await brain.run("alarm at 7am", session="s2")
    assert len(model.calls) == 3
    assert "time must be HH:MM" in json.dumps(model.calls[1].input)
    assert response["reply"] == "Alarm set for 7. Nothing saved about waking."
    assert response["commands"][0]["time"] == "07:00"


async def test_built_in_tools_need_no_load_step(brain):
    model = script(brain, [[function_call("set_alarm", {"time": "07:00", "label": ""}, call_id="c1")]])
    await brain.run("alarm at 7", session="s3")
    tools_shown = visible_tools(model.calls[0])
    assert {"set_alarm", "remember", "recall", "request_change", "build_status"} <= tools_shown
    assert "load_tools" not in tools_shown  # no plugin groups registered
    assert "read_skill" not in tools_shown  # no skills in skills/


async def test_risky_action_waits_for_approval_and_resumes_without_a_model_call(make_brain):
    sent_requests = []

    def claude_code(request: httpx.Request) -> httpx.Response:
        sent_requests.append(request.url.host)
        return claude_code_accepts(request)

    brain = await make_brain(claude_code, **BUILDER)
    model = script(brain, [[function_call("request_change", PLAN_TV, call_id="c1")]])
    response = await brain.run("ask Claude Code to plan TV control", session="s4")
    assert response["pending"][0]["tool"] == "request_change"
    assert not sent_requests  # nothing ran yet

    response = await brain.resolve(response["pending"][0]["id"], approve=True)
    assert sent_requests == ["api.anthropic.com"]
    assert response["reply"].startswith("Claude Code started (plan)")
    assert len(model.calls) == 1  # approval resumed straight into the tool
    assert brain.store.stats("s4")[0]["requests"] >= 1


async def test_rejection_is_single_use_and_never_mode_hides_risky_tools(make_brain):
    brain = await make_brain(claude_code_accepts, **BUILDER)
    script(brain, [[function_call("request_change", PLAN_TV, call_id="c1")]])
    pending_id = (await brain.run("plan TV control", session="s5"))["pending"][0]["id"]
    response = await brain.resolve(pending_id, approve=False)
    assert response["reply"]  # the SDK's rejection message reaches the user
    assert brain.store.get(GLOBAL, "builds", []) == []  # rejected request never fired
    with pytest.raises(KeyError):  # single use: can't flip a rejection into an approval
        await brain.resolve(pending_id, approve=True)

    never_brain = await make_brain(approval="never")
    model = ScriptedModel([[assistant_message("I can't do that.")]])
    never_brain.agent().model = model
    await never_brain.run("plan TV control", session="s6")
    assert "request_change" not in visible_tools(model.calls[0])


async def test_egress_blocked():
    client = http_client(Settings(allowed_hosts=frozenset({"api.openai.com"})))
    with pytest.raises(EgressBlockedError):
        await client.get("https://evil.example.com/x")


async def test_memory_roundtrip(brain):
    brain.memory.remember("Sam wakes at 07:00 on weekdays")
    assert brain.memory.recall("when does Sam wake") == ["Sam wakes at 07:00 on weekdays"]


def test_trim_keeps_turn_boundaries():
    items = []
    for turn in range(10):
        items += [{"role": "user", "content": str(turn)}, {"type": "function_call"}, {"type": "function_call_output"}]
    assert trim_history(items, 4) == items[18:]  # 10 user turns > 2*4 -> keep last 4, starting at a user message
    assert trim_history(items[:24], 4) == items[:24]  # 8 turns: untouched (cache prefix stays stable)


async def test_notices_profile_voice_log_and_usage(brain):
    brain.notice("TV control is ready")
    assert brain.notices_for("phone") == ["TV control is ready"]
    assert brain.notices_for("phone") == []  # delivered once per device
    assert brain.notices_for("ipad") == ["TV control is ready"]

    brain.memory.remember("Sam wakes at 07:00")
    voice_usage = {"model": "gpt-realtime-2.1-mini", "requests": 3, "input": 900, "cached": 300, "output": 400}
    brain.voice_log("phone", 42.0, [{"role": "user", "text": "hi"}], voice_usage)
    profile = brain.profile("phone")
    assert profile["facts"] == ["Sam wakes at 07:00"]
    assert profile["voice_seconds_today"] == 42.0
    assert brain.profile("ipad")["voice_seconds_today"] == 0
    assert '"voice": true' in brain.store.trajectories_file.read_text()

    [voice_row] = brain.store.usage_since(time.time() - 60)
    assert voice_row["model"] == "gpt-realtime-2.1-mini"
    assert (voice_row["requests"], voice_row["input"], voice_row["output"]) == (3, 900, 400)
    assert brain.store.usage_since(time.time() + 60) == []  # time window respected


async def test_request_change_needs_approval_then_fires_exact_spec(make_brain):
    sent_requests = []

    def outside_world(request: httpx.Request) -> httpx.Response:
        sent_requests.append((request.url.host, request.url.path, request.content.decode(), dict(request.headers)))
        if request.url.host == "api.anthropic.com":
            return httpx.Response(200, json={"claude_code_session_url": "https://claude.ai/code/session_1"})
        if request.url.path.endswith("/pulls"):
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 7,
                        "title": "Add TV control",
                        "html_url": "https://github.com/me/fern/pull/7",
                        "created_at": "2099-01-01T00:00:00Z",
                        "state": "open",
                        "merged_at": None,
                        "head": {"ref": "claude/tv", "sha": "abc"},
                        "labels": [{"name": "security-review"}],
                    }
                ],
            )
        if "check-runs" in request.url.path:
            return httpx.Response(200, json={"check_runs": [{"status": "completed", "conclusion": "success"}]})
        return httpx.Response(200)

    brain = await make_brain(
        outside_world,
        builder_url="https://api.anthropic.com/v1/fire",
        builder_token="rt",
        github_repo="me/fern",
        github_token="gh",
        ntfy_topic="topic123",
    )
    spec = "Add a tv plugin: media_player on/off/volume via Home Assistant, with tests."
    brain.agent().model = ScriptedModel(
        [[function_call("request_change", {"kind": "change", "spec": spec}, call_id="c1")]]
    )
    response = await brain.run("tell claude code to add TV control", session="s9")
    assert response["pending"][0]["tool"] == "request_change"
    assert not sent_requests  # nothing fired yet
    assert spec in response["pending"][0]["args"]  # user sees the exact spec

    response = await brain.resolve(response["pending"][0]["id"], approve=True)
    assert response["reply"].startswith("Claude Code started (change)")
    host, _path, body, headers = sent_requests[0]
    assert host == "api.anthropic.com"
    assert json.loads(body)["text"].endswith(spec)
    assert headers["authorization"] == "Bearer rt"
    assert "anthropic-beta" in headers

    await brain.check_builds()
    job = brain.store.get(GLOBAL, "builds")[0]
    assert job["status"] == "ready"
    assert job["pr"] == 7
    _host, push_path, push_body, push_headers = next(sent for sent in sent_requests if sent[0] == "ntfy.sh")
    assert push_path == "/topic123"
    assert "PR #7: Add TV control (security-review)" in push_body
    assert push_headers["click"] == "https://github.com/me/fern/pull/7"

    requests_before = len(sent_requests)
    await brain.check_builds()  # already announced: no duplicate push
    assert not any(sent[0] == "ntfy.sh" for sent in sent_requests[requests_before:])
    assert brain.notices_for("phone") == ["Change ready for review: PR #7: Add TV control (security-review)"]


async def test_request_change_failure_returns_log_reference(make_brain):
    brain = await make_brain(
        lambda request: httpx.Response(503), builder_url="https://api.anthropic.com/v1/fire", builder_token="rt"
    )
    model = script(
        brain,
        [
            [function_call("request_change", {"kind": "plan", "spec": "TV control"}, call_id="c1")],
            [assistant_message("Something went wrong; check the logs.")],
        ],
    )
    pending_id = (await brain.run("plan TV control", session="s10"))["pending"][0]["id"]
    response = await brain.resolve(pending_id, approve=True)
    tool_result = json.dumps(model.calls[-1].input)  # errors go back to the model
    assert "could not start Claude Code (HTTPStatusError)" in tool_result
    assert "check the fern logs for reference" in tool_result
    assert response["reply"] == "Something went wrong; check the logs."
    assert brain.store.get(GLOBAL, "builds", []) == []  # failed request is not tracked as a job


async def test_failure_logs_never_contain_urls_tokens_or_topics(make_brain, caplog):
    brain = await make_brain(
        lambda request: httpx.Response(503),
        builder_url="https://api.anthropic.com/v1/claude_code/routines/trig_secret/fire",
        builder_token="sk-ant-secret",
        ntfy_topic="fern-secret-topic",
    )
    script(
        brain,
        [
            [function_call("request_change", {"kind": "plan", "spec": "TV"}, call_id="c1")],
            [assistant_message("It failed.")],
        ],
    )
    pending_id = (await brain.run("plan TV", session="s14"))["pending"][0]["id"]
    await brain.resolve(pending_id, approve=True)
    await push(brain.http, brain.settings, "title", "message")

    logged = caplog.text
    assert "request_change failed" in logged
    assert "HTTP 503" in logged
    assert "push failed: HTTP 503" in logged
    for secret in ("trig_secret", "sk-ant-secret", "fern-secret-topic", "https://"):
        assert secret not in logged
