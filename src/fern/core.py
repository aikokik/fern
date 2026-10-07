"""Run context, tool policy (tiers, approvals, deferred groups) and egress-guarded HTTP."""

import functools
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import httpx
from agents import RunContextWrapper, function_tool

from fern.config import Settings
from fern.memory import Memory


class Tier(str, Enum):
    READ = "read"    # no side effects: runs freely
    ACT = "act"      # reversible side effects: runs, logged; asks under "untrusted"
    RISKY = "risky"  # locks, heat, purchases, messages: always needs human approval


@dataclass
class Ctx:
    """Per-request state shared by the orchestrator and every subagent (agents-as-tools share it)."""

    settings: Settings
    memory: Memory
    http: httpx.AsyncClient
    store: Any = None  # fern.brain.Store (typed loosely to avoid an import cycle)
    device: str = "cli"
    session: str = "default"
    loaded: set[str] = field(default_factory=set)       # deferred tool groups loaded this run
    commands: list[dict] = field(default_factory=list)  # device-side actions (e.g. phone alarm)
    notes: list[str] = field(default_factory=list)      # mid-conversation system notes for next model call


class EgressBlocked(Exception):
    pass


def http_client(s: Settings) -> httpx.AsyncClient:
    allowed = set(s.allowed_hosts) | {httpx.URL(u).host for u in (s.home_url, s.reader_url) if u}

    async def guard(req: httpx.Request) -> None:
        if req.url.host not in allowed:
            raise EgressBlocked(f"egress to {req.url.host} not allowed")

    return httpx.AsyncClient(timeout=10, follow_redirects=False, event_hooks={"request": [guard]})


def untrusted(source: str, text: str) -> str:
    """Mark external content as data. Instructions tell the model never to follow text inside these tags."""
    return f"<untrusted source={source!r}>\n{text}\n</untrusted>"


def tool(tier: Tier, group: str | None = None, timeout: float = 20) -> Callable[[Callable], Any]:
    """function_tool + approval policy by tier + optional deferred group + output truncation."""

    def wrap(fn: Callable) -> Any:
        @functools.wraps(fn)
        async def run(*a, **kw):
            out = await fn(*a, **kw)
            limit = a[0].context.settings.tool_output_chars if a and isinstance(a[0], RunContextWrapper) else 4000
            return out if not isinstance(out, str) or len(out) <= limit else out[:limit] + "…[truncated]"

        async def needs_approval(w: RunContextWrapper[Ctx], _args: dict, _call_id: str) -> bool:
            return tier is Tier.RISKY or (tier is Tier.ACT and w.context.settings.approval == "untrusted")

        def enabled(w: RunContextWrapper[Ctx], _agent: Any) -> bool:
            if tier is Tier.RISKY and w.context.settings.approval == "never":
                return False
            return group is None or group in w.context.loaded

        t = function_tool(run, needs_approval=needs_approval, is_enabled=enabled, timeout=timeout)
        t.tier, t.group = tier, group
        return t

    return wrap
