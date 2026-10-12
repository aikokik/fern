"""Settings for the builder (Claude Code bridge) and shipper (git + GitHub) services.

Each container only receives the env vars it needs: the builder never gets the GitHub token, the shipper never
gets the Claude token. This package never imports `fern` or `fern_web`.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BuilderSettings:
    workspace: Path = Path(os.getenv("FERN_WORKSPACE", "/workspace"))  # plain file trees Claude edits, per topic
    repos: Path = Path(os.getenv("FERN_REPOS", "/repos"))  # shipper-only git clones, per topic
    github_repo: str = os.getenv("FERN_GITHUB_REPO", "aikokik/fern")
    git_remote: str = os.getenv("FERN_GIT_REMOTE", "")  # override for tests; default is the GitHub HTTPS URL
    github_token: str = os.getenv("FERN_BUILDER_GITHUB_TOKEN", "")  # shipper only
    claude_bin: str = os.getenv("FERN_CLAUDE_BIN", "claude")
    claude_model: str = os.getenv("FERN_CLAUDE_MODEL", "sonnet")
    bridge_token: str = os.getenv("FERN_BRIDGE_TOKEN", "")  # fern_web -> bridge
    shipper_url: str = os.getenv("FERN_SHIPPER_URL", "http://shipper:8771")
    shipper_token: str = os.getenv("FERN_SHIPPER_TOKEN", "")  # bridge -> shipper
    # Claude (fern-github) -> shipper's /github calls only. Claude can read it, so it must never unlock more.
    github_tools_token: str = os.getenv("FERN_GITHUB_TOOLS_TOKEN", "")
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
