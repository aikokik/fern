"""Token prices and the usage/cost report behind the /usage page.

Prices: OpenAI pricing page as of PRICES_AS_OF ($ per 1M tokens, $ per hosted tool call). Costs are computed at read
time from stored token counts, so updating a price here re-prices all history.
"""

from collections import defaultdict

PRICES_AS_OF = "2026-10-11"
TOKEN_PRICES: dict[str, dict[str, float]] = {
    # GPT-5.6+ also bill cache writes at 1.25x input; usage doesn't report them, so new input is slightly under-priced.
    "gpt-6-luna": {"input": 0.10, "cached": 0.01, "output": 0.50},
    "gpt-5.5": {"input": 5.00, "cached": 0.50, "output": 30.00},
    "gpt-5.4-mini": {"input": 0.75, "cached": 0.075, "output": 4.50},
    "gpt-5-mini": {"input": 0.25, "cached": 0.025, "output": 2.00},
    "gpt-5": {"input": 1.25, "cached": 0.125, "output": 10.00},
    "gpt-realtime-2.1-mini": {
        "input": 0.60, "cached": 0.06, "output": 2.40,
        "audio_input": 10.00, "audio_cached": 0.30, "audio_output": 20.00,
    },
    # Audio input is an estimate: OpenAI lists ~$0.003 per minute (~600 audio tokens).
    "gpt-4o-mini-transcribe": {"input": 0.0, "cached": 0.0, "output": 1.25, "audio_input": 5.00},
}  # fmt: skip
CALL_PRICES = {"web_search_call": 0.010, "file_search_call": 0.0025}
PARTS = ("voice", "transcription", "brain", "subagent", "tool")


def cost(model: str, counts: dict) -> float | None:
    """Dollars for one usage row; None if the model has no known price."""
    if model in CALL_PRICES:
        return counts["requests"] * CALL_PRICES[model]
    known = [name for name in TOKEN_PRICES if model.startswith(name)]  # dated snapshots share their base price
    if not known:
        return None
    prices = TOKEN_PRICES[max(known, key=len)]
    text_input = counts["input"] - counts["audio_input"]
    text_cached = counts["cached"] - counts["audio_cached"]
    text_output = counts["output"] - counts["audio_output"]
    dollars = (
        (text_input - text_cached) * prices["input"]
        + text_cached * prices["cached"]
        + text_output * prices["output"]
        + (counts["audio_input"] - counts["audio_cached"]) * prices.get("audio_input", 0)
        + counts["audio_cached"] * prices.get("audio_cached", 0)
        + counts["audio_output"] * prices.get("audio_output", 0)
    )
    return dollars / 1_000_000


def build_report(rows: list[dict]) -> dict:
    """Usage rows (per session, kind, model) -> per-session and total cost, calls and tokens for each part."""
    sessions: dict[str, dict] = {}
    totals = {part: {"calls": 0, "tokens": 0, "cost": 0.0} for part in PARTS}
    unpriced_models: set[str] = set()
    for row in rows:
        session = sessions.setdefault(
            row["session"],
            {"session": row["session"], "last_used": 0.0, "max_context": 0, "parts": defaultdict(_empty_part)},
        )
        row_cost = cost(row["model"], row)
        if row_cost is None:
            unpriced_models.add(row["model"])
        tokens = 0 if row["kind"] == "tool" else row["input"] + row["output"]
        for part in (session["parts"][row["kind"]], totals[row["kind"]]):
            part["calls"] += row["requests"]
            part["tokens"] += tokens
            part["cost"] += row_cost or 0.0
        session["last_used"] = max(session["last_used"], row["last_used"] or 0.0)
        session["max_context"] = max(session["max_context"], row["max_input"] or 0)
    for session in sessions.values():
        session["parts"] = dict(session["parts"])
        session["cost"] = sum(part["cost"] for part in session["parts"].values())
        session["calls"] = sum(part["calls"] for part in session["parts"].values())
        session["tokens"] = sum(part["tokens"] for part in session["parts"].values())
    return {
        "parts": list(PARTS),
        "prices_as_of": PRICES_AS_OF,
        "unpriced_models": sorted(unpriced_models),
        "totals": {
            "parts": totals,
            "cost": sum(part["cost"] for part in totals.values()),
            "calls": sum(part["calls"] for part in totals.values()),
            "tokens": sum(part["tokens"] for part in totals.values()),
        },
        "sessions": sorted(sessions.values(), key=lambda session: session["last_used"], reverse=True),
    }


def _empty_part() -> dict:
    return {"calls": 0, "tokens": 0, "cost": 0.0}
