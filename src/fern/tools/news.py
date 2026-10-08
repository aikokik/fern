import asyncio
import time
from urllib.parse import urlparse

from agents import RunContextWrapper

from fern import reader
from fern.core import FernContext, Tier, tool, untrusted

HACKER_NEWS_API = "https://hacker-news.firebaseio.com/v0"
CACHE_SECONDS = 600  # briefing + follow-up questions cost one fetch
_cached_at, _cached_lines = 0.0, []


@tool(Tier.READ)
async def hn_top(run: RunContextWrapper[FernContext], count: int = 10) -> str:
    """Top Hacker News stories, one per line: '[item_id] title (points, domain)'.

    Args:
        count: Number of stories, 1-30.
    """
    global _cached_at, _cached_lines
    count = max(1, min(count, 30))
    if time.monotonic() - _cached_at > CACHE_SECONDS or len(_cached_lines) < count:
        http = run.context.http
        story_ids = (await http.get(f"{HACKER_NEWS_API}/topstories.json")).json()[:30]
        story_requests = [http.get(f"{HACKER_NEWS_API}/item/{story_id}.json") for story_id in story_ids]
        stories = [response.json() for response in await asyncio.gather(*story_requests)]
        _cached_lines = [
            f"[{story['id']}] {story['title']} ({story.get('score', 0)} pts, {_domain(story)})"
            for story in stories
            if story and story.get("title")
        ]
        _cached_at = time.monotonic()
    return untrusted("hackernews", "\n".join(_cached_lines[:count]))


@tool(Tier.READ, timeout=30)
async def read_story(run: RunContextWrapper[FernContext], item_id: int) -> str:
    """Main text of a story's article (or the post body for Ask/Show HN), first ~4000 characters.

    Args:
        item_id: The number in brackets from hn_top.
    """
    settings = run.context.settings
    if settings.reader_url:  # Docker: separate reader container
        text = (await run.context.http.get(f"{settings.reader_url}/item/{item_id}")).text
    else:
        text = await reader.read(reader.client(), item_id)
    return untrusted("article", text)


def _domain(story: dict) -> str:
    return urlparse(story.get("url", "")).netloc or "news.ycombinator.com"


TOOLS = [hn_top, read_story]
