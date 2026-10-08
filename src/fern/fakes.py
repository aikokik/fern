"""Deterministic fake outside world (Hacker News + Home Assistant) for tests and evals."""

import httpx

HA_STATES = [
    {"entity_id": "light.kitchen", "state": "on"},
    {"entity_id": "light.bedroom", "state": "off"},
    {"entity_id": "lock.front_door", "state": "locked"},
    {"entity_id": "media_player.living_room_tv", "state": "playing"},
]


def world(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("topstories.json"):
        return httpx.Response(200, json=list(range(1, 31)))
    if "/item/" in path:
        story_id = int(path.rsplit("/", 1)[1].split(".")[0])
        story = {
            "id": story_id,
            "title": f"Story {story_id}",
            "score": 10 * story_id,
            "url": f"https://ex{story_id}.com/a",
        }
        return httpx.Response(200, json=story)
    if request.url.host.startswith("ex") and path == "/a":
        story_id = request.url.host[2:].split(".")[0]
        html = f"<nav>menu</nav><h1>Story {story_id}</h1><p>Article {story_id} body.</p><script>x()</script>"
        return httpx.Response(200, html=html)
    if path == "/api/states":
        return httpx.Response(200, json=HA_STATES)
    if path.startswith("/api/services/"):
        return httpx.Response(200, json=[])
    return httpx.Response(404)
