"""Offline tests: ScriptedModel stands in for the LLM, httpx.MockTransport for the network."""

import json
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from agents.testing import ScriptedModel, assistant_message, function_call

from fern.brain import Brain, _trim
from fern.config import Settings
from fern.core import EgressBlocked, http_client
from fern.fakes import world
from fern import reader


@pytest.fixture
async def brain(tmp_path):
    s = Settings(data_dir=tmp_path, skills_dir=Settings().skills_dir, home_url="http://ha.local:8123", home_token="t")
    async with Brain(s) as b:
        yield b


def script(brain: Brain, steps: list, alias: str = "main") -> ScriptedModel:
    m = ScriptedModel(steps)
    brain.agent(alias).model = m
    return m


async def test_alarm_queues_device_command(brain):
    script(brain, [[function_call("set_alarm", {"time": "06:30", "label": "gym"}, call_id="c1")],
                   [assistant_message("Alarm set for 6:30.")]])
    out = await brain.run("wake me 6:30 for gym", device="phone", session="s1")
    assert out["commands"] == [{"type": "alarm.set", "device": "phone", "time": "06:30", "label": "gym"}]
    assert out["tools"] == ["set_alarm"]
    assert out["reply"] == "Alarm set for 6:30." and not out["pending"]


async def test_news_subagent_isolated_and_untrusted(brain, monkeypatch):
    brain.http._transport = httpx.MockTransport(world)
    monkeypatch.setattr(reader, "_client", httpx.AsyncClient(transport=httpx.MockTransport(world)))
    main = script(brain, [[function_call("news_desk", {"input": "top 2"}, call_id="c1")],
                          [assistant_message("Two stories.")]])
    sub = brain.news_desk.model = ScriptedModel(
        [[function_call("hn_top", {"n": 2}, call_id="n1")],
         [function_call("read_story", {"item_id": 2}, call_id="n2")],
         [assistant_message("Story 2; Story 1.")]]
    )
    out = await brain.run("news?", session="s2")
    assert out["reply"] == "Two stories."
    hn_output = json.dumps(sub.last_call.input)
    assert "<untrusted source='hackernews'>" in hn_output and "[2] Story 2 (20 pts, ex2.com)" in hn_output
    assert "<untrusted source='article'>" in hn_output and "Story 2 Article 2 body." in hn_output
    assert "menu" not in hn_output and "x()" not in hn_output  # nav/script stripped
    assert "Story 1" not in json.dumps(main.calls[0].input)  # orchestrator never saw raw HN data first
    assert "<untrusted source='news_desk'>" in json.dumps(main.calls[1].input)  # subagent output marked as data


async def test_deferred_group_and_risky_approval(brain):
    calls = []

    def ha(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path, req.headers["authorization"]))
        return httpx.Response(200, json=[])

    brain.http._transport = httpx.MockTransport(ha)
    m = script(brain, [
        [function_call("load_tools", {"group": "home"}, call_id="c1")],
        [function_call("home_secure", {"domain": "lock", "service": "unlock", "entity_id": "lock.front"}, call_id="c2")],
        [assistant_message("Front door unlocked.")],
    ])
    out = await brain.run("unlock the front door", session="s3")
    names = lambda call: {t.name for t in call.tools}
    assert "home_secure" not in names(m.calls[0]) and "home_secure" in names(m.calls[1])
    assert "Tool group 'home' loaded" in json.dumps(m.calls[1].input)  # mid-conversation system note
    assert out["pending"][0]["tool"] == "home_secure" and not calls  # nothing executed before approval

    out = await brain.resolve(out["pending"][0]["id"], approve=True)
    assert calls == [("POST", "/api/services/lock/unlock", "Bearer t")]
    assert out["reply"] == "Front door unlocked."
    assert brain.store.get("s3", "loaded") == ["home"]  # group stays loaded for this session
    assert brain.store.stats("s3")[0]["requests"] >= 3


async def test_rejection_and_never_mode(brain):
    script(brain, [
        [function_call("load_tools", {"group": "home"}, call_id="c1")],
        [function_call("home_secure", {"domain": "lock", "service": "unlock", "entity_id": "lock.front"}, call_id="c2")],
        [assistant_message("Okay, I won't.")],
    ])
    pid = (await brain.run("unlock", session="s4"))["pending"][0]["id"]
    out = await brain.resolve(pid, approve=False)
    assert out["reply"] == "Okay, I won't."
    with pytest.raises(KeyError):  # single use: can't flip a rejection into an approval
        await brain.resolve(pid, approve=True)

    never = Brain(replace(brain.s, approval="never"))
    m = ScriptedModel([[function_call("load_tools", {"group": "home"}, call_id="c1")], [assistant_message("ok")]])
    never.agent().model = m
    await never.run("unlock", session="s5")
    assert "home_secure" not in {t.name for t in m.calls[1].tools}


async def test_domain_allowlist(brain):
    brain.http._transport = httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
    m = script(brain, [
        [function_call("load_tools", {"group": "home"}, call_id="c1")],
        [function_call("home_control", {"domain": "lock", "service": "unlock", "entity_id": "lock.front"}, call_id="c2")],
        [assistant_message("no")],
    ])
    await brain.run("unlock via the wrong tool", session="s6")
    assert "not allowed" in json.dumps(m.calls[2].input)


async def test_egress_blocked():
    c = http_client(Settings(allowed_hosts=frozenset({"hacker-news.firebaseio.com"})))
    with pytest.raises(EgressBlocked):
        await c.get("https://evil.example.com/x")


async def test_memory_roundtrip(brain):
    brain.memory.remember("Sam wakes at 07:00 on weekdays")
    assert brain.memory.recall("when does Sam wake") == ["Sam wakes at 07:00 on weekdays"]


def test_trim_keeps_turn_boundaries():
    items = []
    for i in range(10):
        items += [{"role": "user", "content": str(i)}, {"type": "function_call"}, {"type": "function_call_output"}]
    assert _trim(items, 4) == items[18:]  # 10 user turns > 2*4 -> keep last 4, starting at a user message
    assert _trim(items[:24], 4) == items[:24]  # 8 turns: untouched (cache prefix stays stable)


async def test_alarm_schedules_briefing_audio(brain, monkeypatch):
    spoken = []

    async def fake_speak(_s, text):
        spoken.append(text)
        return b"mp3"

    monkeypatch.setattr("fern.brain.speak", fake_speak)
    soon = (brain.s.now() + timedelta(minutes=10)).strftime("%H:%M")
    script(brain, [[function_call("set_alarm", {"time": soon, "label": ""}, call_id="c1")], [assistant_message("Set.")],
                   [assistant_message("Good morning. Story 1 is about X.")]])
    await brain.run(f"alarm at {soon}", device="phone", session="s7")
    assert len(brain.store.get("_", "alarms")) == 1

    assert await brain.prepare_briefings()  # alarm within lead time -> pre-generated
    assert not brain.store.get("_", "alarms") and spoken == ["Good morning. Story 1 is about X."]
    f = await brain.briefing_audio()  # alarm-stop request: served from cache, no new model or TTS call
    assert f.read_bytes() == b"mp3" and len(spoken) == 1
    assert not await brain.prepare_briefings()  # nothing due anymore


async def test_reader_refuses_private_addresses():
    for url in ("http://127.0.0.1:8765/v1/chat", "http://localhost/", "http://192.168.1.1/"):
        with pytest.raises(httpx.ConnectError):
            await reader._public_only(httpx.Request("GET", url))


async def test_notices_profile_voice_log(brain):
    brain.notice("TV control is ready")
    assert brain.notices_for("phone") == ["TV control is ready"]
    assert brain.notices_for("phone") == []  # delivered once per device
    assert brain.notices_for("ipad") == ["TV control is ready"]

    brain.memory.remember("Sam wakes at 07:00")
    brain.voice_log("phone", 42.0, [{"role": "user", "text": "hi"}])
    p = brain.profile("phone")
    assert p["facts"] == ["Sam wakes at 07:00"] and p["voice_seconds_today"] == 42.0
    assert brain.profile("ipad")["voice_seconds_today"] == 0
    assert '"voice": true' in brain.store.traj.read_text()


async def test_request_change_needs_approval_then_fires_exact_spec(tmp_path):
    s = Settings(data_dir=tmp_path, skills_dir=Settings().skills_dir, builder_url="https://api.anthropic.com/v1/fire",
                 builder_token="rt", github_repo="me/fern", github_token="gh", ntfy_topic="topic123")
    sent = []

    def world2(req: httpx.Request) -> httpx.Response:
        sent.append((req.url.host, req.url.path, req.content.decode(), dict(req.headers)))
        if req.url.host == "api.anthropic.com":
            return httpx.Response(200, json={"claude_code_session_url": "https://claude.ai/code/session_1"})
        if req.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[{
                "number": 7, "title": "Add TV control", "html_url": "https://github.com/me/fern/pull/7",
                "created_at": "2099-01-01T00:00:00Z", "state": "open", "merged_at": None,
                "head": {"ref": "claude/tv", "sha": "abc"}, "labels": [{"name": "security-review"}]}])
        if "check-runs" in req.url.path:
            return httpx.Response(200, json={"check_runs": [{"status": "completed", "conclusion": "success"}]})
        return httpx.Response(200)

    async with Brain(s) as b:
        b.http._transport = httpx.MockTransport(world2)
        spec = "Add a tv plugin: media_player on/off/volume via Home Assistant, with tests."
        b.agent().model = ScriptedModel([
            [function_call("load_tools", {"group": "builder"}, call_id="c1")],
            [function_call("request_change", {"kind": "change", "spec": spec}, call_id="c2")],
            [assistant_message("Claude Code is on it.")],
        ])
        out = await b.run("tell claude code to add TV control", session="s9")
        assert out["pending"][0]["tool"] == "request_change" and not sent  # nothing fired before approval
        assert spec in out["pending"][0]["args"]  # user sees the exact spec

        await b.resolve(out["pending"][0]["id"], approve=True)
        host, _, body, headers = sent[0]
        assert host == "api.anthropic.com" and json.loads(body)["text"].endswith(spec)
        assert headers["authorization"] == "Bearer rt" and "anthropic-beta" in headers

        await b.check_builds()
        job = b.store.get("_", "builds")[0]
        assert job["status"] == "ready" and job["pr"] == 7
        push = next(x for x in sent if x[0] == "ntfy.sh")
        assert push[1] == "/topic123" and "PR #7: Add TV control (security-review)" in push[2]
        assert push[3]["click"] == "https://github.com/me/fern/pull/7"
        n_sent = len(sent)
        await b.check_builds()  # already announced: no duplicate push
        assert not any(x[0] == "ntfy.sh" for x in sent[n_sent:])
        assert b.notices_for("phone") == ["Change ready for review: PR #7: Add TV control (security-review)"]
