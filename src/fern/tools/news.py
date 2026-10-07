import asyncio
import time

from urllib.parse import urlparse

from agents import RunContextWrapper

from fern import reader
from fern.core import Ctx, Tier, tool, untrusted

HN = "https://hacker-news.firebaseio.com/v0"
_cache: tuple[float, list[str]] = (0.0, [])


@tool(Tier.READ)
async def hn_top(ctx: RunContextWrapper[Ctx], n: int = 10) -> str:
    """Top Hacker News stories, one per line: '[item_id] title (points, domain)'.

    Args:
        n: Number of stories, 1-30.
    """
    global _cache
    n = max(1, min(n, 30))
    if time.monotonic() - _cache[0] > 600 or len(_cache[1]) < n:  # 10-min cache: briefing + follow-ups cost 1 fetch
        http = ctx.context.http
        ids = (await http.get(f"{HN}/topstories.json")).json()[:30]
        items = [r.json() for r in await asyncio.gather(*(http.get(f"{HN}/item/{i}.json") for i in ids))]
        lines = [
            f"[{it['id']}] {it['title']} ({it.get('score', 0)} pts, {urlparse(it.get('url', '')).netloc or 'news.ycombinator.com'})"
            for it in items if it and it.get("title")
        ]
        _cache = (time.monotonic(), lines)
    return untrusted("hackernews", "\n".join(_cache[1][:n]))


@tool(Tier.READ, timeout=30)
async def read_story(ctx: RunContextWrapper[Ctx], item_id: int) -> str:
    """Main text of a story's article (or the post body for Ask/Show HN), first ~4000 characters.

    Args:
        item_id: The number in brackets from hn_top.
    """
    s = ctx.context.settings
    if s.reader_url:  # Docker: separate reader container
        text = (await ctx.context.http.get(f"{s.reader_url}/item/{item_id}")).text
    else:
        text = await reader.read(reader.client(), item_id)
    return untrusted("article", text)


TOOLS = [hn_top, read_story]
