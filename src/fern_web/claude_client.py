"""fern_web -> Claude Code builder bridge (fern_builder.bridge), over the sealed network with the bridge token."""

from typing import Any
from urllib.parse import quote

import httpx

BUILD_TIMEOUT_SECONDS = 1900  # a build turn may take ~30 minutes (bridge allows 1800)


class ClaudeUnavailableError(Exception):
    pass


class ClaudeClient:
    def __init__(self, url: str, token: str, transport: httpx.AsyncBaseTransport | None = None):
        self.configured = bool(url and token)
        self.http = httpx.AsyncClient(
            base_url=url or "http://unconfigured",
            timeout=BUILD_TIMEOUT_SECONDS,
            transport=transport,
            headers={"Authorization": f"Bearer {token}"},
        )

    async def _call(self, method: str, path: str, json: dict | None = None) -> Any:
        if not self.configured:
            raise ClaudeUnavailableError("Claude Code isn't set up (FERN_BRIDGE_URL / FERN_BRIDGE_TOKEN)")
        response = await self.http.request(method, path, json=json)
        if response.status_code >= 400:
            detail = response.json().get("detail", "") if response.content else ""
            raise ClaudeUnavailableError(f"bridge returned {response.status_code} {detail}".strip())
        return response.json()

    async def turn(self, topic: str, text: str) -> dict:
        return await self._call("POST", f"/topics/{_path(topic)}/turn", {"text": text})

    async def build(self, topic: str) -> dict:
        return await self._call("POST", f"/topics/{_path(topic)}/build", {})

    async def ship(self, topic: str, title: str) -> dict:
        return await self._call("POST", f"/topics/{_path(topic)}/ship", {"title": title})

    async def topics(self) -> list[dict]:
        return await self._call("GET", "/topics")


def _path(topic: str) -> str:
    return quote(topic, safe="")  # the bridge turns it into a slug; this just keeps it one path segment
