"""Settings for the Claude Code bridge. It runs on the Mac, like the VS Code plugin, so Claude gets the user's own
login, `gh`, permission rules and memories. This package never imports `fern` or `fern_web`.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def read_env_value(env_file: Path, name: str) -> str:
    """One value from .env, without loading the rest: Claude inherits this process's environment."""
    if not env_file.exists():
        return ""
    for line in env_file.read_text().splitlines():
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip().strip("\"'")
    return ""


@dataclass(frozen=True)
class BuilderSettings:
    project_dir: Path = Path(os.getenv("FERN_PROJECT_DIR", str(PROJECT_ROOT)))  # Claude works here, like VS Code
    approval_timeout_seconds: float = float(os.getenv("FERN_APPROVAL_TIMEOUT_SECONDS", "600"))  # unanswered = no
    bridge_token: str = field(default_factory=lambda: read_env_value(PROJECT_ROOT / ".env", "FERN_BRIDGE_TOKEN"))


builder_settings = BuilderSettings()
