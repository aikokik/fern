"""Push notifications to the phone via ntfy (free app). The topic name is the secret: keep it long and random."""

import logging

import httpx

from fern.config import Settings

log = logging.getLogger("fern")


async def push(http: httpx.AsyncClient, settings: Settings, title: str, message: str, url: str | None = None) -> None:
    if not settings.ntfy_topic:
        return
    headers = {"Title": title, "Tags": "seedling"} | ({"Click": url} if url else {})
    try:
        topic_url = f"{settings.ntfy_server}/{settings.ntfy_topic}"
        response = await http.post(topic_url, content=message.encode(), headers=headers)
        response.raise_for_status()
    except httpx.HTTPError:
        log.warning("push failed", exc_info=True)
