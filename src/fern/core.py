"""Run context, tool policy (tiers, approvals, deferred groups) and egress-guarded HTTP."""

import functools
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx
from agents import RunContextWrapper, function_tool

from fern.config import Settings
from fern.memory import Memory


class Tier(StrEnum):
    READ = "read"  # no side effects: runs freely
    ACT = "act"  # reversible side effects: runs, logged; asks under "untrusted"
    RISKY = "risky"  # locks, heat, purchases, messages: always needs human approval


@dataclass
class FernContext:
    """Per-request state shared by the orchestrator and every subagent (agents-as-tools share it)."""

    settings: Settings
    memory: Memory
    http: httpx.AsyncClient
    store: Any = None  # fern.brain.Store (typed loosely to avoid an import cycle)
    device: str = "cli"
    session: str = "default"
    loaded_groups: set[str] = field(default_factory=set)  # deferred tool groups loaded this run
    device_commands: list[dict] = field(default_factory=list)  # device-side actions (e.g. phone alarm)
    notes: list[str] = field(default_factory=list)  # mid-conversation system notes for next model call


class EgressBlockedError(Exception):
    pass


def http_client(settings: Settings) -> httpx.AsyncClient:
    extra_hosts = {httpx.URL(url).host for url in (settings.home_url, settings.reader_url) if url}
    allowed_hosts = set(settings.allowed_hosts) | extra_hosts

    async def block_unlisted_hosts(request: httpx.Request) -> None:
        if request.url.host not in allowed_hosts:
            raise EgressBlockedError(f"egress to {request.url.host} not allowed")

    return httpx.AsyncClient(timeout=10, follow_redirects=False, event_hooks={"request": [block_unlisted_hosts]})


def untrusted(source: str, text: str) -> str:
    """Mark external content as data. Instructions tell the model never to follow text inside these tags."""
    return f"<untrusted source={source!r}>\n{text}\n</untrusted>"


def tool(tier: Tier, group: str | None = None, timeout: float = 20) -> Callable[[Callable], Any]:
    """function_tool + approval policy by tier + optional deferred group + output truncation."""

    def decorate(tool_function: Callable) -> Any:
        @functools.wraps(tool_function)
        async def run_and_truncate(*args, **kwargs):
            output = await tool_function(*args, **kwargs)
            has_context = args and isinstance(args[0], RunContextWrapper)
            limit = args[0].context.settings.tool_output_chars if has_context else 4000
            if not isinstance(output, str) or len(output) <= limit:
                return output
            return output[:limit] + "…[truncated]"

        async def needs_approval(run: RunContextWrapper[FernContext], _arguments: dict, _call_id: str) -> bool:
            policy = run.context.settings.approval
            return tier is Tier.RISKY or (tier is Tier.ACT and policy == "untrusted")

        def is_visible(run: RunContextWrapper[FernContext], _agent: Any) -> bool:
            if tier is Tier.RISKY and run.context.settings.approval == "never":
                return False
            return group is None or group in run.context.loaded_groups

        function = function_tool(
            run_and_truncate, needs_approval=needs_approval, is_enabled=is_visible, timeout=timeout
        )
        function.tier, function.group = tier, group
        return function

    return decorate
