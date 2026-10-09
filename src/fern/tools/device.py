"""Device-side actions. The brain never touches the phone directly: it queues commands the
device executes (e.g. an iOS Shortcut reads `commands` and runs 'Create Alarm')."""

from datetime import datetime

from agents import RunContextWrapper

from fern.core import FernContext, Tier, tool


@tool(Tier.ACT)
async def set_alarm(run: RunContextWrapper[FernContext], time: str, label: str = "") -> str:
    """Set an alarm on the user's current device.

    Args:
        time: 24h local time, 'HH:MM'.
        label: Optional alarm label.
    """
    try:
        datetime.strptime(time, "%H:%M")
    except ValueError:
        return "error: time must be HH:MM (24h)"
    context = run.context
    context.device_commands.append({"type": "alarm.set", "device": context.device, "time": time, "label": label[:60]})
    return f"Alarm set for {time}."


TOOLS = [set_alarm]
