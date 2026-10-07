"""Device-side actions. The brain never touches the phone directly: it queues commands the
device executes (e.g. an iOS Shortcut reads `commands` and runs 'Create Alarm')."""

from datetime import datetime

from agents import RunContextWrapper

from fern.core import Ctx, Tier, tool


@tool(Tier.ACT)
async def set_alarm(ctx: RunContextWrapper[Ctx], time: str, label: str = "") -> str:
    """Set an alarm on the user's current device.

    Args:
        time: 24h local time, 'HH:MM'.
        label: Optional alarm label.
    """
    try:
        datetime.strptime(time, "%H:%M")
    except ValueError:
        return "error: time must be HH:MM (24h)"
    c = ctx.context
    c.commands.append({"type": "alarm.set", "device": c.device, "time": time, "label": label[:60]})
    return f"alarm set for {time} on {c.device}"


TOOLS = [set_alarm]
