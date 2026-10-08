"""Home Assistant bridge. Deferred group: loaded only when a request needs the home.
Domains are allowlisted and split by tier; the HA token stays in the HTTP client, never in context."""

from agents import RunContextWrapper

from fern.core import FernContext, Tier, tool

GROUPS = {"home": "Home Assistant: read device states, control lights/media, locks/climate (approval)."}
ACT_DOMAINS = {"light", "switch", "fan", "media_player", "scene"}
RISKY_DOMAINS = {"lock", "climate", "cover", "alarm_control_panel", "water_heater", "vacuum"}


async def _home_assistant(context: FernContext, method: str, path: str, json: dict | None = None):
    settings = context.settings
    if not settings.home_url:
        raise RuntimeError("Home Assistant not configured")
    response = await context.http.request(
        method,
        f"{settings.home_url}/api/{path}",
        json=json,
        headers={"Authorization": f"Bearer {settings.home_token}"},
    )
    response.raise_for_status()
    return response.json()


async def _call_service(
    context: FernContext, allowed_domains: set[str], domain: str, service: str, entity_id: str
) -> str:
    if domain not in allowed_domains or not entity_id.startswith(f"{domain}."):
        return f"error: {domain}.{service} on {entity_id} not allowed by this tool"
    await _home_assistant(context, "POST", f"services/{domain}/{service}", {"entity_id": entity_id})
    return f"ok: {domain}.{service} {entity_id}"


@tool(Tier.READ, group="home")
async def home_states(run: RunContextWrapper[FernContext], domain: str) -> str:
    """List entities and their states for one domain (e.g. 'light', 'lock').

    Args:
        domain: Home Assistant domain.
    """
    entities = await _home_assistant(run.context, "GET", "states")
    return "\n".join(
        f"{entity['entity_id']}: {entity['state']}"
        for entity in entities
        if entity["entity_id"].startswith(f"{domain}.")
    )


@tool(Tier.ACT, group="home")
async def home_control(run: RunContextWrapper[FernContext], domain: str, service: str, entity_id: str) -> str:
    """Control lights, switches, fans, media players, scenes.

    Args:
        domain: One of light, switch, fan, media_player, scene.
        service: e.g. turn_on, turn_off, media_pause.
        entity_id: e.g. light.kitchen.
    """
    return await _call_service(run.context, ACT_DOMAINS, domain, service, entity_id)


@tool(Tier.RISKY, group="home")
async def home_secure(run: RunContextWrapper[FernContext], domain: str, service: str, entity_id: str) -> str:
    """Control locks, climate, covers, alarm panels, water heater, vacuum. Requires human approval.

    Args:
        domain: One of lock, climate, cover, alarm_control_panel, water_heater, vacuum.
        service: e.g. lock, unlock, set_temperature, open_cover.
        entity_id: e.g. lock.front_door.
    """
    return await _call_service(run.context, RISKY_DOMAINS, domain, service, entity_id)


TOOLS = [home_states, home_control, home_secure]
