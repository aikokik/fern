"""Article reader: HN item id -> main text of the linked page.

Capability-limited by design: callers name an HN item, never a URL, so neither the model nor a compromised
brain can point it at arbitrary hosts. Private/loopback targets are refused on every hop (SSRF).
Runs in-process for local dev; in Docker it is its own container, the only one with open egress.
"""

import asyncio
import ipaddress
import time
from html.parser import HTMLParser

import httpx
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

HACKER_NEWS_API = "https://hacker-news.firebaseio.com/v0"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) fern-reader/1.0"
SKIPPED_TAGS = {"script", "style", "nav", "footer", "header", "aside", "form", "noscript", "svg", "button"}
TEXT_TAGS = {"p", "h1", "h2", "h3", "li", "pre", "blockquote", "td"}
MAX_CHARS = 4000
CACHE_SECONDS = 600
CACHE_MAX_ITEMS = 200  # bounded: the reader runs for weeks
_cache: dict[int, tuple[float, str]] = {}  # item id -> (fetched at, text)
_client: httpx.AsyncClient | None = None


class _ArticleTextParser(HTMLParser):
    """Collects text inside paragraph-like tags, ignoring navigation, scripts and other page chrome."""

    def __init__(self):
        super().__init__()
        self.chunks: list[str] = []
        self.skipped_depth = self.text_depth = 0

    def handle_starttag(self, tag, attrs):  # signature fixed by HTMLParser; attrs unused
        self.skipped_depth += tag in SKIPPED_TAGS
        self.text_depth += tag in TEXT_TAGS

    def handle_endtag(self, tag):
        self.skipped_depth -= tag in SKIPPED_TAGS and self.skipped_depth > 0
        self.text_depth -= tag in TEXT_TAGS and self.text_depth > 0

    def handle_data(self, data):
        if self.text_depth and not self.skipped_depth and (text := data.strip()):
            self.chunks.append(text)


def html_text(html: str) -> str:
    parser = _ArticleTextParser()
    parser.feed(html[:2_000_000])
    return " ".join(parser.chunks)


async def _public_addresses_only(request: httpx.Request) -> None:
    resolved = await asyncio.get_running_loop().getaddrinfo(request.url.host, request.url.port or 443)
    addresses = [address_info[4][0] for address_info in resolved]
    if any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise httpx.ConnectError(f"refusing non-public address for {request.url.host}")


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=10,
            follow_redirects=True,
            max_redirects=5,
            headers={"User-Agent": USER_AGENT},
            event_hooks={"request": [_public_addresses_only]},
            trust_env=False,
        )
    return _client


async def read(http: httpx.AsyncClient, item_id: int) -> str:
    if (cached := _cache.get(item_id)) and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    try:
        story = (await http.get(f"{HACKER_NEWS_API}/item/{item_id}.json")).json() or {}
        if story.get("text"):  # Ask/Show HN post body
            text = html_text(story["text"])
        elif article_url := story.get("url"):
            page = await http.get(article_url)
            content_type = page.headers.get("content-type", "")
            if page.is_success and "html" in content_type:
                text = html_text(page.text)
            else:
                text = f"unreadable ({page.status_code} {content_type})"
        else:
            text = "no article"
    except httpx.HTTPError as error:
        text = f"unreadable ({type(error).__name__})"
    text = text[:MAX_CHARS] or "unreadable (no text)"
    if len(_cache) >= CACHE_MAX_ITEMS:
        _cache.clear()
    _cache[item_id] = (time.monotonic(), text)
    return text


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/item/{item_id}", response_class=PlainTextResponse)
async def item(item_id: int) -> str:
    return await read(client(), item_id)
