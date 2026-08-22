"""Translate between Mealie's structured items and Keep's single line of text."""

from __future__ import annotations

import re
from typing import Any

from .models import MealieItem, ParsedIngredient

#: Digits and the Unicode vulgar fractions (¼ ½ ¾, ⅐ through ⅞). Used to spot an amount
#: that a person typed by hand in Keep.
_AMOUNT_PATTERN = re.compile(r"[0-9¼-¾⅐-⅞]")


def has_explicit_amount(text: str) -> bool:
    """Whether a line of Keep text appears to state an amount of its own.

    Keep never receives quantities from Mealie, so text arriving from Keep normally has
    none either. When it does, the user typed it and it should win over what Mealie holds.
    """
    return bool(_AMOUNT_PATTERN.search(text))


def render_item(item: MealieItem) -> str:
    """Render a Mealie item as the text that should appear in Keep.

    Only the food name crosses over. Keep answers "what am I buying?" - quantity, unit
    and note all stay in Mealie, so ``1 cup Basil (fresh)`` there appears simply as
    ``Basil`` in Keep.

    A food item is one with a ``food`` record; Mealie has no "is this a food" flag, and
    inventing one in our model made every real item fall through to the note. Items with
    no food record are free text, so their note is all there is to show - returning
    nothing would make them vanish from Keep entirely.

    Mealie's own ``display`` field is not used: it embeds exactly the amounts being
    stripped. Composing from ``food.name`` also means the text does not vary with
    quantity, so editing an amount in Mealie produces no Keep write at all. (The singular
    ``name`` is used rather than ``plural_name`` for the same reason - a plural would leak
    the quantity back in and churn the text whenever it crossed 1.)
    """
    if item.food is not None:
        name = (item.food.name or "").strip()
        if name:
            return name

    return (item.note or "").strip()


def normalise_text(text: str) -> str:
    """Collapse whitespace so cosmetic edits do not read as real changes."""
    return " ".join(text.split()).strip()


def item_fields(
    text: str,
    *,
    parsed: ParsedIngredient | None = None,
    min_confidence: float = 0.6,
    food_id: str | None = None,
    current: MealieItem | None = None,
) -> dict[str, Any]:
    """The structured Mealie fields for one line of Keep text.

    A parse is only accepted when the parser is confident, matched a food that already
    exists in Mealie (or one we were told to create), *and* either found no unit or
    matched a real one. That last condition keeps a word the user typed from being
    silently discarded: an unresolvable unit like "2 schmoo sugar" would otherwise be
    dropped on the floor, so the whole line is kept verbatim as a note instead. Failing
    those checks also keeps shopping-list typos out of the food database.

    An item is a structured food to Mealie precisely when ``foodId`` is set; there is no
    "is this a food" flag to send, so a note item is simply one with ``foodId`` null.

    ``current`` is the existing Mealie item when this text is an *edit* rather than a new
    item. Keep shows only the food name, so a rename there ("Basil" -> "Parsley") carries
    no quantity, unit or note, and re-parsing it blind would quietly reset Mealie's
    "1 cup ... (fresh)" to a bare 1 with no note. Those fields are therefore carried over
    unless the new text actually supplies them.
    """
    resolved_food_id = food_id or (parsed.food.id if parsed and parsed.food else None)
    unit_ok = not parsed or not parsed.unit or bool(parsed.unit.id)
    keep_amount = current is not None and not has_explicit_amount(text)

    if parsed and resolved_food_id and unit_ok and parsed.confidence >= min_confidence:
        if keep_amount and current is not None:
            quantity: float = current.quantity
            unit_id = current.unit_id
        else:
            quantity = parsed.quantity or 1
            unit_id = parsed.unit.id if parsed.unit else None

        note = normalise_text(parsed.note or "")
        if not note and current is not None and current.food is not None:
            # Keep shows only the food name, so a rename there carries no note. Carry the
            # existing one over rather than silently clearing it. Only when the item was
            # already a food: an unstructured item's note *is* its text, and that text is
            # precisely what is being replaced.
            note = normalise_text(current.note or "")

        return {
            "foodId": resolved_food_id,
            "unitId": unit_id,
            "quantity": quantity,
            "note": note,
        }

    return {
        "foodId": None,
        "unitId": None,
        "quantity": current.quantity if keep_amount and current is not None else 1,
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
    """Build a Mealie create payload for text authored in Keep.

    No ``current`` is passed on: a brand new item has no prior amount to preserve, so an
    amount typed in Keep ("2 lb chicken") is parsed and kept in Mealie even though the
    text then converges to the bare food name in Keep.
    """
    return {
        "shoppingListId": shopping_list_id,
        "checked": checked,
        "extras": {extras_key: keep_id} if keep_id else {},
        **item_fields(text, parsed=parsed, min_confidence=min_confidence, food_id=food_id),
    }
