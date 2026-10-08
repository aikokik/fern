"""Inspect eval of the real brain. The agent bridge routes the brain's model calls through Inspect,
so the model is chosen on the CLI and every call is logged:

    uv run inspect eval evals/household.py --model openai/gpt-5-mini
    uv run inspect view
"""

import tempfile
from dataclasses import replace
from pathlib import Path

import httpx
from inspect_ai import Task, task
from inspect_ai.agent import Agent, AgentState, agent, agent_bridge
from inspect_ai.dataset import json_dataset
from inspect_ai.scorer import Score, Target, accuracy, model_graded_qa, scorer
from inspect_ai.solver import TaskState
from inspect_ai.util import store

from fern.brain import Brain
from fern.config import settings
from fern.fakes import world


@agent
def household_brain() -> Agent:
    async def execute(state: AgentState) -> AgentState:
        eval_settings = replace(
            settings,
            data_dir=Path(tempfile.mkdtemp()),
            models={"main": "inspect", "fast": "inspect"},
            home_url="http://ha.local:8123",
            home_token="eval",
            tracing=False,
        )
        async with agent_bridge(state) as bridge, Brain(eval_settings) as brain:
            brain.http._transport = httpx.MockTransport(world)
            response = await brain.run(state.messages[-1].text, device="phone", session="eval")
            store().set("response", response)
        return bridge.state

    return execute


@scorer(metrics=[accuracy()])
def behavior():
    """Expected tools called, forbidden tools not called, approval gate respected."""

    async def score(state: TaskState, target: Target) -> Score:
        response, expected = store().get("response", {}), state.metadata
        called = set(response.get("tools", []))
        problems = [f"missing {name}" for name in expected.get("tools", []) if name not in called]
        problems += [f"forbidden {name}" for name in expected.get("forbid", []) if name in called]
        is_pending = bool(response.get("pending"))
        if expected.get("pending") is not None and is_pending != expected["pending"]:
            problems.append(f"pending={is_pending}")
        return Score(value=not problems, explanation="; ".join(problems) or "ok")

    return score


@task
def household() -> Task:
    return Task(
        dataset=json_dataset(str(Path(__file__).parent / "household.jsonl")),
        solver=household_brain(),
        scorer=[behavior(), model_graded_qa()],
    )
