"""Offline tests: ScriptedModel stands in for the LLM, httpx.MockTransport for the network."""

import json
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from agents.testing import ScriptedModel, assistant_message, function_call

from fern import reader
from fern.brain import Brain, trim_history
from fern.config import Settings
from fern.core import EgressBlockedError, http_client
from fern.fakes import world
from fern.store import GLOBAL

UNLOCK_FRONT_DOOR = {"domain": "lock", "service": "unlock", "entity_id": "lock.front"}


@pytest.fixture
async def make_brain(tmp_path):
    """Build a Brain whose network is `handler` (default: the fake world), with optional settings overrides."""
    base_settings = Settings(
        data_dir=tmp_path, skills_dir=Settings().skills_dir, home_url="http://ha.local:8123", home_token="t"
    )
    async with AsyncExitStack() as cleanup:

        async def build(handler=world, **overrides) -> Brain:
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


async def test_alarm_queues_device_command(brain):
    script(
        brain,
        [
            [function_call("set_alarm", {"time": "06:30", "label": "gym"}, call_id="c1")],
            [assistant_message("Alarm set for 6:30.")],
        ],
    )
    response = await brain.run("wake me 6:30 for gym", device="phone", session="s1")
    assert response["commands"] == [{"type": "alarm.set", "device": "phone", "time": "06:30", "label": "gym"}]
    assert response["tools"] == ["set_alarm"]
    assert response["reply"] == "Alarm set for 6:30."
    assert not response["pending"]


async def test_news_subagent_isolated_and_untrusted(brain, monkeypatch):
    monkeypatch.setattr(reader, "_client", httpx.AsyncClient(transport=httpx.MockTransport(world)))
    orchestrator = script(
        brain,
        [
            [function_call("news_desk", {"input": "top 2"}, call_id="c1")],
            [assistant_message("Two stories.")],
        ],
    )
    subagent = brain.news_desk.model = ScriptedModel(
        [
            [function_call("hn_top", {"count": 2}, call_id="n1")],
            [function_call("read_story", {"item_id": 2}, call_id="n2")],
            [assistant_message("Story 2; Story 1.")],
        ]
    )
    response = await brain.run("news?", session="s2")
    assert response["reply"] == "Two stories."

    subagent_input = json.dumps(subagent.last_call.input)
    assert "<untrusted source='hackernews'>" in subagent_input
    assert "[2] Story 2 (20 pts, ex2.com)" in subagent_input
    assert "<untrusted source='article'>" in subagent_input
    assert "Story 2 Article 2 body." in subagent_input
    assert "menu" not in subagent_input
    assert "x()" not in subagent_input
    assert "Story 1" not in json.dumps(orchestrator.calls[0].input)  # orchestrator never saw raw HN data
    assert "<untrusted source='news_desk'>" in json.dumps(orchestrator.calls[1].input)  # subagent output = data


async def test_deferred_group_and_risky_approval(make_brain):
    home_assistant_calls = []

    def home_assistant(request: httpx.Request) -> httpx.Response:
        home_assistant_calls.append((request.method, request.url.path, request.headers["authorization"]))
        return httpx.Response(200, json=[])

    brain = await make_brain(home_assistant)
    model = script(
        brain,
        [
            [function_call("load_tools", {"group": "home"}, call_id="c1")],
            [function_call("home_secure", UNLOCK_FRONT_DOOR, call_id="c2")],
            [assistant_message("Front door unlocked.")],
        ],
    )
    response = await brain.run("unlock the front door", session="s3")
    assert "home_secure" not in visible_tools(model.calls[0])
    assert "home_secure" in visible_tools(model.calls[1])
    assert "Tool group 'home' loaded" in json.dumps(model.calls[1].input)  # mid-conversation system note
    assert response["pending"][0]["tool"] == "home_secure"
    assert not home_assistant_calls

    response = await brain.resolve(response["pending"][0]["id"], approve=True)
    assert home_assistant_calls == [("POST", "/api/services/lock/unlock", "Bearer t")]
    assert response["reply"] == "Front door unlocked."
    assert brain.store.get("s3", "loaded_groups") == ["home"]  # group stays loaded for this session
    assert brain.store.stats("s3")[0]["requests"] >= 3


async def test_rejection_and_never_mode(brain, make_brain):
    script(
        brain,
        [
            [function_call("load_tools", {"group": "home"}, call_id="c1")],
            [function_call("home_secure", UNLOCK_FRONT_DOOR, call_id="c2")],
            [assistant_message("Okay, I won't.")],
        ],
    )
    pending_id = (await brain.run("unlock", session="s4"))["pending"][0]["id"]
    response = await brain.resolve(pending_id, approve=False)
    assert response["reply"] == "Okay, I won't."
    with pytest.raises(KeyError):  # single use: can't flip a rejection into an approval
        await brain.resolve(pending_id, approve=True)

    never_brain = await make_brain(approval="never")
    model = ScriptedModel(
        [
            [function_call("load_tools", {"group": "home"}, call_id="c1")],
            [assistant_message("ok")],
        ]
    )
    never_brain.agent().model = model
    await never_brain.run("unlock", session="s5")
    assert "home_secure" not in visible_tools(model.calls[1])


async def test_domain_allowlist(brain):
    model = script(
        brain,
        [
            [function_call("load_tools", {"group": "home"}, call_id="c1")],
            [function_call("home_control", UNLOCK_FRONT_DOOR, call_id="c2")],
            [assistant_message("no")],
        ],
    )
    await brain.run("unlock via the wrong tool", session="s6")
    assert "not allowed" in json.dumps(model.calls[2].input)


async def test_egress_blocked():
    client = http_client(Settings(allowed_hosts=frozenset({"hacker-news.firebaseio.com"})))
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


async def test_alarm_schedules_briefing_audio(brain, monkeypatch):
    spoken_texts = []

    async def fake_speak(_settings, text):
        spoken_texts.append(text)
        return b"mp3"

    monkeypatch.setattr("fern.brain.speak", fake_speak)
    in_ten_minutes = (brain.settings.now() + timedelta(minutes=10)).strftime("%H:%M")
    script(
        brain,
        [
            [function_call("set_alarm", {"time": in_ten_minutes, "label": ""}, call_id="c1")],
            [assistant_message("Set.")],
            [assistant_message("Good morning. Story 1 is about X.")],
        ],
    )
    await brain.run(f"alarm at {in_ten_minutes}", device="phone", session="s7")
    assert len(brain.store.get(GLOBAL, "alarms")) == 1

    assert await brain.prepare_briefings()  # alarm within lead time -> pre-generated
    assert not brain.store.get(GLOBAL, "alarms")
    assert spoken_texts == ["Good morning. Story 1 is about X."]
    audio_file = await brain.briefing_audio()  # alarm-stop request: served from cache, no new model or TTS call
    assert audio_file.read_bytes() == b"mp3"
    assert len(spoken_texts) == 1
    assert not await brain.prepare_briefings()  # nothing due anymore


async def test_reader_refuses_private_addresses():
    for url in ("http://127.0.0.1:8765/v1/chat", "http://localhost/", "http://192.168.1.1/"):
        with pytest.raises(httpx.ConnectError):
            await reader._public_addresses_only(httpx.Request("GET", url))


async def test_notices_profile_voice_log(brain):
    brain.notice("TV control is ready")
    assert brain.notices_for("phone") == ["TV control is ready"]
    assert brain.notices_for("phone") == []  # delivered once per device
    assert brain.notices_for("ipad") == ["TV control is ready"]

    brain.memory.remember("Sam wakes at 07:00")
    brain.voice_log("phone", 42.0, [{"role": "user", "text": "hi"}])
    profile = brain.profile("phone")
    assert profile["facts"] == ["Sam wakes at 07:00"]
    assert profile["voice_seconds_today"] == 42.0
    assert brain.profile("ipad")["voice_seconds_today"] == 0
    assert '"voice": true' in brain.store.trajectories_file.read_text()


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

    test_brain = await make_brain(
        outside_world,
        builder_url="https://api.anthropic.com/v1/fire",
        builder_token="rt",
        github_repo="me/fern",
        github_token="gh",
        ntfy_topic="topic123",
    )
    spec = "Add a tv plugin: media_player on/off/volume via Home Assistant, with tests."
    test_brain.agent().model = ScriptedModel(
        [
            [function_call("load_tools", {"group": "builder"}, call_id="c1")],
            [function_call("request_change", {"kind": "change", "spec": spec}, call_id="c2")],
            [assistant_message("Claude Code is on it.")],
        ]
    )
    response = await test_brain.run("tell claude code to add TV control", session="s9")
    assert response["pending"][0]["tool"] == "request_change"
    assert not sent_requests
    assert spec in response["pending"][0]["args"]  # user sees the exact spec

    await test_brain.resolve(response["pending"][0]["id"], approve=True)
    host, _path, body, headers = sent_requests[0]
    assert host == "api.anthropic.com"
    assert json.loads(body)["text"].endswith(spec)
    assert headers["authorization"] == "Bearer rt"
    assert "anthropic-beta" in headers

    await test_brain.check_builds()
    job = test_brain.store.get(GLOBAL, "builds")[0]
    assert job["status"] == "ready"
    assert job["pr"] == 7
    _host, push_path, push_body, push_headers = next(sent for sent in sent_requests if sent[0] == "ntfy.sh")
    assert push_path == "/topic123"
    assert "PR #7: Add TV control (security-review)" in push_body
    assert push_headers["click"] == "https://github.com/me/fern/pull/7"

    requests_before = len(sent_requests)
    await test_brain.check_builds()  # already announced: no duplicate push
    assert not any(sent[0] == "ntfy.sh" for sent in sent_requests[requests_before:])
    expected_notice = "Change ready for review: PR #7: Add TV control (security-review)"
    assert test_brain.notices_for("phone") == [expected_notice]
