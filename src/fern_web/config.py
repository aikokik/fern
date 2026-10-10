"""fern_web settings (env). This package is an interface: it holds no household data and never imports `fern`."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class WebSettings:
    brain_url: str = os.getenv("FERN_BRAIN_URL", "http://127.0.0.1:8765")
    model: str = os.getenv("FERN_REALTIME_MODEL", "gpt-realtime-2.1-mini")
    voice: str = os.getenv("FERN_REALTIME_VOICE", "marin")
    transcribe_model: str = os.getenv("FERN_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
    daily_minutes: float = float(os.getenv("FERN_VOICE_DAILY_MINUTES", "30"))
    idle_seconds: float = float(os.getenv("FERN_VOICE_IDLE_SECONDS", "120"))
    # Caps one spoken reply.
    max_output_tokens: int = int(os.getenv("FERN_VOICE_MAX_OUTPUT_TOKENS", "600"))
    # Conversation tokens the voice model re-reads each turn; the brain keeps full history, so keep this small.
    context_tokens: int = int(os.getenv("FERN_VOICE_CONTEXT_TOKENS", "4000"))
    notice_poll_seconds: float = 30
    # Claude Code builder (fern_builder bridge). Empty URL or token = Claude mode unavailable.
    bridge_url: str = os.getenv("FERN_BRIDGE_URL", "")
    bridge_token: str = os.getenv("FERN_BRIDGE_TOKEN", "")
    # Devices allowed to switch to Claude (comma list of device names); empty = every authenticated device.
    claude_devices: tuple[str, ...] = tuple(name for name in os.getenv("FERN_CLAUDE_DEVICES", "").split(",") if name)


web_settings = WebSettings()
