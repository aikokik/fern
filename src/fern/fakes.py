"""Deterministic fake outside world (Home Assistant) for tests and evals."""

import httpx

HA_STATES = [
    {"entity_id": "light.kitchen", "state": "on"},
    {"entity_id": "light.bedroom", "state": "off"},
    {"entity_id": "lock.front_door", "state": "locked"},
    {"entity_id": "media_player.living_room_tv", "state": "playing"},
]


def world(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/states":
        return httpx.Response(200, json=HA_STATES)
    if path.startswith("/api/services/"):
        return httpx.Response(200, json=[])
    return httpx.Response(404)
