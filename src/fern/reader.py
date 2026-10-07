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

HN = "https://hacker-news.firebaseio.com/v0"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) fern-reader/1.0"
SKIP = {"script", "style", "nav", "footer", "header", "aside", "form", "noscript", "svg", "button"}
KEEP = {"p", "h1", "h2", "h3", "li", "pre", "blockquote", "td"}
LIMIT = 4000
_cache: dict[int, tuple[float, str]] = {}
_client: httpx.AsyncClient | None = None


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out: list[str] = []
        self.skip = self.keep = 0

    def handle_starttag(self, tag, attrs):
        self.skip += tag in SKIP
        self.keep += tag in KEEP

    def handle_endtag(self, tag):
        self.skip -= tag in SKIP and self.skip > 0
        self.keep -= tag in KEEP and self.keep > 0

    def handle_data(self, data):
        if self.keep and not self.skip and (d := data.strip()):
            self.out.append(d)


def html_text(html: str) -> str:
    p = _Text()
    p.feed(html[:2_000_000])
    return " ".join(p.out)


async def _public_only(req: httpx.Request) -> None:
    infos = await asyncio.get_running_loop().getaddrinfo(req.url.host, req.url.port or 443)
    if any(not ipaddress.ip_address(i[4][0]).is_global for i in infos):
        raise httpx.ConnectError(f"refusing non-public address for {req.url.host}")


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=10, follow_redirects=True, max_redirects=5, headers={"User-Agent": UA},
            event_hooks={"request": [_public_only]}, trust_env=False,
        )
    return _client


async def read(http: httpx.AsyncClient, item_id: int) -> str:
    if (hit := _cache.get(item_id)) and time.monotonic() - hit[0] < 600:
        return hit[1]
    try:
        it = (await http.get(f"{HN}/item/{item_id}.json")).json() or {}
        if it.get("text"):  # Ask/Show HN post body
            text = html_text(it["text"])
        elif url := it.get("url"):
            r = await http.get(url)
            kind = r.headers.get("content-type", "")
            text = html_text(r.text) if r.is_success and "html" in kind else f"unreadable ({r.status_code} {kind})"
        else:
            text = "no article"
    except httpx.HTTPError as e:
        text = f"unreadable ({type(e).__name__})"
    text = text[:LIMIT] or "unreadable (no text)"
    _cache[item_id] = (time.monotonic(), text)
    return text


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/item/{item_id}", response_class=PlainTextResponse)
async def item(item_id: int) -> str:
    return await read(client(), item_id)
