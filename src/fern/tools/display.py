"""On-screen cards: fern can show things, not only say them (like ChatGPT's "Intelligent UI").

The brain never sends HTML. It queues a typed card, the same way it queues an alarm, and the voice page renders it
with its own components. Photos come from Pexels (a fixed host); the page loads them from images.pexels.com.
"""

import logging
import secrets
from typing import Any, Literal, TypedDict

import httpx
from agents import RunContextWrapper

from fern.core import EgressBlockedError, FernContext, Tier, describe_http_error, tool

log = logging.getLogger("fern")
PEXELS_SEARCH = "https://api.pexels.com/v1/search"
PHOTO_HOST = "https://images.pexels.com/"
MAX_ITEMS = 30  # rows, ingredients, steps or checklist items per card
MAX_TEXT = 300


class Ingredient(TypedDict):
    name: str
    amount: float | None  # for the recipe's servings; null for "to taste"
    unit: str  # "g", "tbsp", "" for whole items


class RecipeCard(TypedDict):
    type: Literal["recipe"]
    title: str
    servings: int
    ingredients: list[Ingredient]
    steps: list[str]


class ChecklistCard(TypedDict):
    type: Literal["checklist"]
    title: str
    items: list[str]


class ChoicesCard(TypedDict):
    type: Literal["choices"]
    question: str
    options: list[str]  # 2-6 short answers; a tap sends it as if the user said it


class TableCard(TypedDict):
    type: Literal["table"]
    title: str
    columns: list[str]
    rows: list[list[str]]


def _trimmed(card: RecipeCard | ChecklistCard | ChoicesCard | TableCard) -> dict:
    """Cap sizes so a runaway card can't flood the screen."""
    data: dict[str, Any] = dict(card)
    for key in ("ingredients", "steps", "items", "rows"):
        if key in data:
            data[key] = data[key][:MAX_ITEMS]
    if "options" in data:
        data["options"] = [option[:80] for option in data["options"][:6]]
    for key in ("title", "question"):
        if key in data:
            data[key] = data[key][:MAX_TEXT]
    return data


@tool(Tier.READ)
async def show_card(
    run: RunContextWrapper[FernContext], card: RecipeCard | ChecklistCard | ChoicesCard | TableCard
) -> str:
    """Show a card on the user's screen when it helps more than words: a recipe (ingredients scale with servings, steps
    can be ticked off), a checklist, a few choices to tap, or a comparison table. Afterwards keep the spoken reply to
    one sentence; the card has the details.

    Args:
        card: The card. recipe = title, servings, ingredients (name, amount for those servings or null, unit),
            steps. checklist = title, items. choices = question, 2-6 short options. table = title, columns, rows.
    """
    data = _trimmed(card)
    run.context.device_commands.append({"type": "show.card", "card": data})
    return f"Showing the {data['type']} on screen."


@tool(Tier.READ)
async def show_images(run: RunContextWrapper[FernContext], query: str, count: int = 6) -> str:
    """Show photos on the user's screen, e.g. a dish they want to cook or a place they mention.

    Args:
        query: What to show, in English, e.g. 'pad thai'.
        count: How many photos, 1-10.
    """
    context = run.context
    if not context.settings.pexels_key:
        return "error: photos aren't set up (FERN_PEXELS_KEY)"
    try:
        response = await context.http.get(
            PEXELS_SEARCH,
            params={"query": query[:100], "per_page": max(1, min(count, 10))},
            headers={"Authorization": context.settings.pexels_key},
        )
        response.raise_for_status()
    except (httpx.HTTPError, EgressBlockedError) as error:
        reference = secrets.token_hex(3)
        log.warning("show_images failed [ref %s]: %s", reference, describe_http_error(error))
        return f"error: couldn't get photos. Fern logs reference {reference}."
    images = [
        {
            "src": photo["src"]["medium"],
            "alt": photo.get("alt") or query,
            "credit": photo.get("photographer", ""),
            "link": photo.get("url", ""),
        }
        for photo in response.json().get("photos", [])
        if str(photo.get("src", {}).get("medium", "")).startswith(PHOTO_HOST)  # only Pexels' own image host
    ]
    if not images:
        return f"No photos found for {query}."
    context.device_commands.append({"type": "show.images", "query": query[:100], "images": images})
    return f"Showing {len(images)} photos of {query}."


TOOLS = [show_card, show_images]
