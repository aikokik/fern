"""Push notifications to the phone via ntfy (free app). The topic name is the secret: keep it long and random."""

import logging

import httpx

from fern.config import Settings

log = logging.getLogger("fern")


async def push(http: httpx.AsyncClient, s: Settings, title: str, message: str, url: str | None = None) -> None:
    if not s.ntfy_topic:
        return
    headers = {"Title": title, "Tags": "seedling"} | ({"Click": url} if url else {})
    try:
        r = await http.post(f"{s.ntfy_server}/{s.ntfy_topic}", content=message.encode(), headers=headers)
        r.raise_for_status()
    except httpx.HTTPError:
        log.warning("push failed", exc_info=True)
