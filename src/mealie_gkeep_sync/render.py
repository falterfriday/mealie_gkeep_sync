"""Translate between Mealie's structured items and Keep's single line of text."""

from __future__ import annotations

from typing import Any

from .models import MealieItem, MealieUnit, ParsedIngredient


def format_quantity(quantity: float) -> str:
    """Render a quantity the way a person would write it on a list."""
    if quantity == int(quantity):
        return str(int(quantity))
    # Trim float noise (0.3333333 -> 0.333) without forcing trailing zeros.
    return f"{quantity:.3f}".rstrip("0").rstrip(".")


def _unit_label(unit: MealieUnit, quantity: float) -> str:
    plural = quantity != 1
    if unit.use_abbreviation and unit.abbreviation:
        if plural and unit.plural_abbreviation:
            return unit.plural_abbreviation
        return unit.abbreviation
    if plural and unit.plural_name:
        return unit.plural_name
    return unit.name


def render_item(item: MealieItem) -> str:
    """Render a Mealie item as the text that should appear in Keep.

    Prefers Mealie's own ``display`` when the server provides it, so the Keep list reads
    identically to the Mealie UI. Falls back to composing the parts ourselves for older
    Mealie versions that omit the field.
    """
    if item.display and item.display.strip():
        return item.display.strip()

    if not item.is_food:
        return (item.note or "").strip()

    parts: list[str] = []
    if item.quantity:
        parts.append(format_quantity(item.quantity))
    if item.unit:
        label = _unit_label(item.unit, item.quantity)
        if label:
            parts.append(label)
    if item.food:
        name = item.food.name
        if item.quantity != 1 and item.food.plural_name:
            name = item.food.plural_name
        if name:
            parts.append(name)

    text = " ".join(parts).strip()
    note = (item.note or "").strip()
    if note and text:
        return f"{text} ({note})"
    return text or note


def normalise_text(text: str) -> str:
    """Collapse whitespace so cosmetic edits do not read as real changes."""
    return " ".join(text.split()).strip()


def item_fields(
    text: str,
    *,
    parsed: ParsedIngredient | None = None,
    min_confidence: float = 0.6,
    food_id: str | None = None,
) -> dict[str, Any]:
    """The structured Mealie fields for one line of Keep text.

    A parse is only accepted when the parser is confident, matched a food that already
    exists in Mealie (or one we were told to create), *and* either found no unit or
    matched a real one. That last condition matters: accepting a parse whose unit did not
    resolve would drop the unit word entirely, and since Mealie is the canonical renderer
    that loss would then be written back over the user's text in Keep.

    Anything failing those checks becomes an unstructured note, which also keeps
    shopping-list typos out of the food database.
    """
    resolved_food_id = food_id or (parsed.food.id if parsed and parsed.food else None)
    unit_ok = not parsed or not parsed.unit or bool(parsed.unit.id)

    if parsed and resolved_food_id and unit_ok and parsed.confidence >= min_confidence:
        return {
            "isFood": True,
            "foodId": resolved_food_id,
            "unitId": parsed.unit.id if parsed.unit else None,
            "quantity": parsed.quantity or 1,
            "note": normalise_text(parsed.note or ""),
        }

    return {
        "isFood": False,
        "foodId": None,
        "unitId": None,
        "quantity": 1,
        "note": normalise_text(text),
    }


def build_create_payload(
    text: str,
    shopping_list_id: str,
    *,
    checked: bool = False,
    keep_id: str | None = None,
    parsed: ParsedIngredient | None = None,
    min_confidence: float = 0.6,
    food_id: str | None = None,
    extras_key: str = "gkeep_item_id",
) -> dict[str, Any]:
    """Build a Mealie create payload for text authored in Keep."""
    return {
        "shoppingListId": shopping_list_id,
        "checked": checked,
        "extras": {extras_key: keep_id} if keep_id else {},
        **item_fields(text, parsed=parsed, min_confidence=min_confidence, food_id=food_id),
    }
