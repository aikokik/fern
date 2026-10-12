"""Settings for the builder: the Claude Code bridge. Claude uses its own GitHub access (`gh` + `git` with GH_TOKEN,
a token scoped to this repo). This package never imports `fern` or `fern_web`.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BuilderSettings:
    workspace: Path = Path(os.getenv("FERN_WORKSPACE", "/workspace"))  # one git clone per topic
    github_repo: str = os.getenv("FERN_GITHUB_REPO", "aikokik/fern")
    git_remote: str = os.getenv("FERN_GIT_REMOTE", "")  # override for tests; default is the GitHub HTTPS URL
    claude_bin: str = os.getenv("FERN_CLAUDE_BIN", "claude")
    claude_model: str = os.getenv("FERN_CLAUDE_MODEL", "sonnet")
    bridge_token: str = os.getenv("FERN_BRIDGE_TOKEN", "")  # fern_web -> bridge
    plan_turn_seconds: float = float(os.getenv("FERN_PLAN_TURN_SECONDS", "600"))
    build_turn_seconds: float = float(os.getenv("FERN_BUILD_TURN_SECONDS", "1800"))

    @property
    def remote_url(self) -> str:
        return self.git_remote or f"https://github.com/{self.github_repo}.git"


def topic_slug(name: str) -> str:
    """'TV control!' -> 'tv-control'. Topic names become directory and branch names, so keep them tame."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")
    if not slug:
        raise ValueError("topic name needs letters or digits")
    return slug


builder_settings = BuilderSettings()
