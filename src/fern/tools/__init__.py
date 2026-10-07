"""Tool registry. Plugins (FERN_PLUGINS=pkg.mod,...) are modules exposing TOOLS and optional GROUPS."""

import importlib
from pathlib import Path

from agents import RunContextWrapper

from fern.core import Ctx, Tier, tool
from fern.tools import builder, device, home, news


def skills_index(skills_dir: Path) -> dict[str, str]:
    """name -> description, from SKILL.md frontmatter. Only this index sits in the prompt (progressive disclosure)."""
    out = {}
    for f in sorted(skills_dir.glob("*/SKILL.md")):
        meta = dict(
            line.split(":", 1) for line in f.read_text().split("---")[1].strip().splitlines() if ":" in line
        )
        out[meta.get("name", f.parent.name).strip()] = meta.get("description", "").strip()
    return out


@tool(Tier.READ)
async def read_skill(ctx: RunContextWrapper[Ctx], name: str) -> str:
    """Load a skill's full instructions before doing that task.

    Args:
        name: Skill name from the skills index.
    """
    f = ctx.context.settings.skills_dir / name / "SKILL.md"
    if name not in skills_index(ctx.context.settings.skills_dir):
        return f"error: unknown skill {name!r}"
    return f.read_text().split("---", 2)[2].strip()


@tool(Tier.READ)
async def load_tools(ctx: RunContextWrapper[Ctx], group: str) -> str:
    """Load a deferred tool group; its tools become callable on your next step.

    Args:
        group: Group name from the tool groups index.
    """
    if group not in GROUPS:
        return f"error: unknown group {group!r}"
    ctx.context.loaded.add(group)
    names = [t.name for t in ALL if t.group == group]
    ctx.context.notes.append(f"Tool group '{group}' loaded: {', '.join(names)}")
    return f"loaded {group}: {', '.join(names)}"


@tool(Tier.ACT)
async def remember(ctx: RunContextWrapper[Ctx], text: str) -> str:
    """Save a durable fact or preference about the household (only when the user asks or clearly implies it).

    Args:
        text: One self-contained fact, e.g. 'Sam wakes at 07:00 on weekdays'.
    """
    ctx.context.memory.remember(text[:500])
    return "saved"


@tool(Tier.READ)
async def recall(ctx: RunContextWrapper[Ctx], query: str) -> str:
    """Search saved household facts and preferences.

    Args:
        query: Keywords.
    """
    return "\n".join(ctx.context.memory.recall(query)) or "nothing found"


GROUPS: dict[str, str] = home.GROUPS | builder.GROUPS
ALL = [read_skill, load_tools, remember, recall, *device.TOOLS, *home.TOOLS, *builder.TOOLS]


def load_plugins(mods: tuple[str, ...]) -> None:
    for m in mods:
        mod = importlib.import_module(m)
        GROUPS.update(getattr(mod, "GROUPS", {}))
        ALL.extend(mod.TOOLS)
