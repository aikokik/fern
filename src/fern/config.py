"""All settings come from env (12-factor). Secrets never enter model context."""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def _parse_pairs(raw: str) -> dict[str, str]:
    """'phone=abc,ipad=def' -> {'phone': 'abc', 'ipad': 'def'}"""
    return dict(pair.split("=", 1) for pair in raw.split(",") if "=" in pair)


def _parse_list(raw: str) -> tuple[str, ...]:
    return tuple(item for item in raw.split(",") if item)


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(os.getenv("FERN_DATA", "./data"))
    skills_dir: Path = Path(os.getenv("FERN_SKILLS", "./skills"))
    # Virtual models: alias -> provider model string. "litellm/..." or "any-llm/..." route to other providers.
    models: dict[str, str] = field(
        default_factory=lambda: {"main": "gpt-5", "fast": "gpt-5-mini"} | json.loads(os.getenv("FERN_MODELS", "{}"))
    )
    # Codex-style approval policy: "untrusted" asks for ACT+RISKY, "on-risk" asks for RISKY, "never" disables RISKY.
    approval: str = os.getenv("FERN_APPROVAL", "on-risk")
    # App-level egress allowlist (the Docker proxy enforces the same list at network level).
    allowed_hosts: frozenset[str] = frozenset(
        os.getenv(
            "FERN_ALLOWED_HOSTS", "api.openai.com,hacker-news.firebaseio.com,api.anthropic.com,api.github.com,ntfy.sh"
        ).split(",")
    )
    device_tokens: dict[str, str] = field(default_factory=lambda: _parse_pairs(os.getenv("FERN_DEVICE_TOKENS", "")))
    reader_url: str = os.getenv("FERN_READER_URL", "")  # set in Docker; empty = read articles in-process
    home_url: str = os.getenv("HA_URL", "")
    home_token: str = os.getenv("HA_TOKEN", "")
    memory: str = os.getenv("FERN_MEMORY", "fern.memory:SQLiteMemory")  # swap in any Memory impl
    plugins: tuple[str, ...] = _parse_list(os.getenv("FERN_PLUGINS", ""))
    tracing: bool = os.getenv("FERN_TRACING") == "1"  # opt-in upload of traces to OpenAI's dashboard
    max_history_turns: int = int(os.getenv("FERN_MAX_TURNS_KEPT", "8"))
    tool_output_chars: int = int(os.getenv("FERN_TOOL_OUTPUT_CHARS", "4000"))
    # Claude Code builder (Routine API trigger) + GitHub status (read-only token) + push notifications (ntfy).
    builder_url: str = os.getenv("FERN_BUILDER_URL", "")
    builder_token: str = os.getenv("FERN_BUILDER_TOKEN", "")
    github_repo: str = os.getenv("FERN_GITHUB_REPO", "")  # owner/name
    github_token: str = os.getenv("FERN_GITHUB_TOKEN", "")
    ntfy_server: str = os.getenv("FERN_NTFY_SERVER", "https://ntfy.sh")
    ntfy_topic: str = os.getenv("FERN_NTFY_TOPIC", "")
    timezone: str = os.getenv("FERN_TZ", "")  # IANA name, e.g. Asia/Lisbon. Containers default to UTC, so set it.
    # Briefing voice (OpenAI TTS). Short replies are spoken on-device by the phone instead.
    tts_model: str = os.getenv("FERN_TTS_MODEL", "gpt-4o-mini-tts")
    tts_voice: str = os.getenv("FERN_TTS_VOICE", "coral")
    # Daily wake-up times (HH:MM) to pre-generate the briefing for, besides alarms set through fern.
    briefing_at: tuple[str, ...] = _parse_list(os.getenv("FERN_BRIEFING_AT", ""))

    @property
    def db(self) -> Path:
        return self.data_dir / "fern.db"

    def model(self, alias: str) -> str:
        return self.models.get(alias, alias)

    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self.timezone)) if self.timezone else datetime.now().astimezone()


settings = Settings()
