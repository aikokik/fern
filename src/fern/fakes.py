"""Deterministic fake outside world (Hacker News + Home Assistant) for tests and evals."""

import httpx

HA_STATES = [
    {"entity_id": "light.kitchen", "state": "on"},
    {"entity_id": "light.bedroom", "state": "off"},
    {"entity_id": "lock.front_door", "state": "locked"},
    {"entity_id": "media_player.living_room_tv", "state": "playing"},
]


def world(req: httpx.Request) -> httpx.Response:
    path = req.url.path
    if path.endswith("topstories.json"):
        return httpx.Response(200, json=list(range(1, 31)))
    if "/item/" in path:
        i = int(path.rsplit("/", 1)[1].split(".")[0])
        return httpx.Response(200, json={"id": i, "title": f"Story {i}", "score": 10 * i, "url": f"https://ex{i}.com/a"})
    if req.url.host.startswith("ex") and req.url.path == "/a":
        i = req.url.host[2:].split(".")[0]
        html = f"<nav>menu</nav><h1>Story {i}</h1><p>Article {i} body.</p><script>x()</script>"
        return httpx.Response(200, html=html)
    if path == "/api/states":
        return httpx.Response(200, json=HA_STATES)
    if path.startswith("/api/services/"):
        return httpx.Response(200, json=[])
    return httpx.Response(404)
