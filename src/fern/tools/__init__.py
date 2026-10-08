"""Tool registry. Plugins (FERN_PLUGINS=pkg.mod,...) are modules exposing TOOLS and optional GROUPS."""

import importlib
from pathlib import Path

from agents import RunContextWrapper

from fern.core import TOOL_GROUPS, FernContext, Tier, tool
from fern.tools import builder, device, home


def skills_index(skills_dir: Path) -> dict[str, str]:
    """name -> description, from SKILL.md frontmatter. Only this index sits in the prompt (progressive disclosure)."""
    index = {}
    for skill_file in sorted(skills_dir.glob("*/SKILL.md")):
        frontmatter = skill_file.read_text().split("---")[1].strip().splitlines()
        fields = dict(line.split(":", 1) for line in frontmatter if ":" in line)
        index[fields.get("name", skill_file.parent.name).strip()] = fields.get("description", "").strip()
    return index


@tool(Tier.READ)
async def read_skill(run: RunContextWrapper[FernContext], name: str) -> str:
    """Load a skill's full instructions before doing that task.

    Args:
        name: Skill name from the skills index.
    """
    skills_dir = run.context.settings.skills_dir
    if name not in skills_index(skills_dir):
        return f"error: unknown skill {name!r}"
    return (skills_dir / name / "SKILL.md").read_text().split("---", 2)[2].strip()


@tool(Tier.READ)
async def load_tools(run: RunContextWrapper[FernContext], group: str) -> str:
    """Load a deferred tool group; its tools become callable on your next step.

    Args:
        group: Group name from the tool groups index.
    """
    if group not in GROUPS:
        return f"error: unknown group {group!r}"
    run.context.loaded_groups.add(group)
    tool_names = ", ".join(function.name for function in ALL if TOOL_GROUPS.get(function.name) == group)
    run.context.notes.append(f"Tool group '{group}' loaded: {tool_names}")
    return f"loaded {group}: {tool_names}"


@tool(Tier.ACT)
async def remember(run: RunContextWrapper[FernContext], text: str) -> str:
    """Save a durable fact or preference about the household (only when the user asks or clearly implies it).

    Args:
        text: One self-contained fact, e.g. 'Sam wakes at 07:00 on weekdays'.
    """
    run.context.memory.remember(text[:500])
    return "saved"


@tool(Tier.READ)
async def recall(run: RunContextWrapper[FernContext], query: str) -> str:
    """Search saved household facts and preferences.

    Args:
        query: Keywords.
    """
    return "\n".join(run.context.memory.recall(query)) or "nothing found"


GROUPS: dict[str, str] = home.GROUPS | builder.GROUPS
ALL = [read_skill, load_tools, remember, recall, *device.TOOLS, *home.TOOLS, *builder.TOOLS]


_loaded_plugins: set[str] = set()


def load_plugins(module_names: tuple[str, ...]) -> None:
    """Idempotent: creating several Brains (tests, evals) must not register a plugin's tools twice."""
    for module_name in set(module_names) - _loaded_plugins:
        _loaded_plugins.add(module_name)
        plugin = importlib.import_module(module_name)
        GROUPS.update(getattr(plugin, "GROUPS", {}))
        ALL.extend(plugin.TOOLS)
