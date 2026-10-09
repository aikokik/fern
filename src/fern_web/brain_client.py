"""The only way fern_web talks to the brain: its HTTP API, with the caller's own device token."""

from typing import Any

import httpx


class BrainError(Exception):
    def __init__(self, status: int):
        super().__init__(f"brain returned {status}")
        self.status = status


class BrainClient:
    def __init__(self, url: str, transport: httpx.AsyncBaseTransport | None = None):
        self.http = httpx.AsyncClient(base_url=url, timeout=120, transport=transport)

    async def _call(self, token: str, method: str, path: str, json: dict | None = None) -> Any:
        response = await self.http.request(method, path, json=json, headers={"Authorization": f"Bearer {token}"})
        if response.status_code >= 400:
            raise BrainError(response.status_code)
        return response.json() if response.content else None

    async def profile(self, token: str) -> dict:  # doubles as the auth check
        return await self._call(token, "GET", "/v1/profile")

    async def chat(self, token: str, text: str) -> dict:
        return await self._call(token, "POST", "/v1/chat", {"text": text, "session": "voice"})

    async def approve(self, token: str, pending_id: str, approve: bool) -> dict:
        return await self._call(token, "POST", f"/v1/approvals/{pending_id}", {"approve": approve})

    async def notices(self, token: str) -> list[str]:
        return await self._call(token, "GET", "/v1/notices")

    async def voice_log(self, token: str, seconds: float, transcript: list[dict], usage: list[dict]) -> None:
        body = {"seconds": seconds, "transcript": transcript, "usage": usage}
        await self._call(token, "POST", "/v1/voice-log", body)

    async def healthy(self) -> bool:
        try:
            return (await self.http.get("/healthz")).status_code == 200
        except httpx.HTTPError:
            return False
