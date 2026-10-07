"""Home Assistant bridge. Deferred group: loaded only when a request needs the home.
Domains are allowlisted and split by tier; the HA token stays in the HTTP client, never in context."""

from agents import RunContextWrapper

from fern.core import Ctx, Tier, tool

GROUPS = {"home": "Home Assistant: read device states, control lights/media, locks/climate (approval)."}
ACT_DOMAINS = {"light", "switch", "fan", "media_player", "scene"}
RISKY_DOMAINS = {"lock", "climate", "cover", "alarm_control_panel", "water_heater", "vacuum"}


async def _call(c: Ctx, method: str, path: str, json: dict | None = None):
    if not c.settings.home_url:
        raise RuntimeError("Home Assistant not configured")
    r = await c.http.request(
        method, f"{c.settings.home_url}/api/{path}", json=json,
        headers={"Authorization": f"Bearer {c.settings.home_token}"},
    )
    r.raise_for_status()
    return r.json()


async def _service(c: Ctx, allowed: set[str], domain: str, service: str, entity_id: str) -> str:
    if domain not in allowed or not entity_id.startswith(f"{domain}."):
        return f"error: {domain}.{service} on {entity_id} not allowed by this tool"
    await _call(c, "POST", f"services/{domain}/{service}", {"entity_id": entity_id})
    return f"ok: {domain}.{service} {entity_id}"


@tool(Tier.READ, group="home")
async def home_states(ctx: RunContextWrapper[Ctx], domain: str) -> str:
    """List entities and their states for one domain (e.g. 'light', 'lock').

    Args:
        domain: Home Assistant domain.
    """
    states = await _call(ctx.context, "GET", "states")
    return "\n".join(f"{s['entity_id']}: {s['state']}" for s in states if s["entity_id"].startswith(f"{domain}."))


@tool(Tier.ACT, group="home")
async def home_control(ctx: RunContextWrapper[Ctx], domain: str, service: str, entity_id: str) -> str:
    """Control lights, switches, fans, media players, scenes.

    Args:
        domain: One of light, switch, fan, media_player, scene.
        service: e.g. turn_on, turn_off, media_pause.
        entity_id: e.g. light.kitchen.
    """
    return await _service(ctx.context, ACT_DOMAINS, domain, service, entity_id)


@tool(Tier.RISKY, group="home")
async def home_secure(ctx: RunContextWrapper[Ctx], domain: str, service: str, entity_id: str) -> str:
    """Control locks, climate, covers, alarm panels, water heater, vacuum. Requires human approval.

    Args:
        domain: One of lock, climate, cover, alarm_control_panel, water_heater, vacuum.
        service: e.g. lock, unlock, set_temperature, open_cover.
        entity_id: e.g. lock.front_door.
    """
    return await _service(ctx.context, RISKY_DOMAINS, domain, service, entity_id)


TOOLS = [home_states, home_control, home_secure]
