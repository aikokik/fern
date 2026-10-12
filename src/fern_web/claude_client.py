"""fern_web -> the Claude Code bridge on the Mac (fern_builder.bridge), via the egress proxy, with the bridge token."""

import json
from collections.abc import AsyncIterator
from urllib.parse import quote

import httpx

READ_TIMEOUT_SECONDS = 60  # the bridge pings every 15 s while Claude works, so a minute of silence means it's gone


class ClaudeUnavailableError(Exception):
    pass


class ClaudeClient:
    def __init__(self, url: str, token: str, transport: httpx.AsyncBaseTransport | None = None):
        self.configured = bool(url and token)
        self.http = httpx.AsyncClient(
            base_url=url or "http://unconfigured",
            timeout=httpx.Timeout(30, read=READ_TIMEOUT_SECONDS),
            transport=transport,
            headers={"Authorization": f"Bearer {token}"},
        )

    def _require_setup(self) -> None:
        if not self.configured:
            raise ClaudeUnavailableError("Claude Code isn't set up (FERN_BRIDGE_URL / FERN_BRIDGE_TOKEN)")

    async def send(self, text: str) -> AsyncIterator[dict]:
        """One Claude turn as it happens: progress, text, permission, question, ping, then done (or error)."""
        self._require_setup()
        async with self.http.stream("POST", "/messages", json={"text": text}) as response:
            if response.status_code >= 400:
                await response.aread()
                raise ClaudeUnavailableError(f"bridge returned {response.status_code}")
            async for line in response.aiter_lines():
                if line.strip():
                    yield json.loads(line)

    async def _post(self, path: str, payload: dict | None = None) -> None:
        self._require_setup()
        response = await self.http.post(path, json=payload or {})
        if response.status_code >= 400:
            raise ClaudeUnavailableError(f"bridge returned {response.status_code}")

    async def decide(self, request_id: str, decision: str) -> None:
        await self._post(f"/permissions/{quote(request_id, safe='')}", {"decision": decision})

    async def answer(self, request_id: str, answers: dict[str, str]) -> None:
        await self._post(f"/questions/{quote(request_id, safe='')}", {"answers": answers})

    async def interrupt(self) -> None:
        await self._post("/interrupt")

    async def new_chat(self) -> None:
        await self._post("/new")
