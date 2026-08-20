"""Rendering Mealie items to Keep text, and structuring Keep text for Mealie."""

from __future__ import annotations

import pytest
from conftest import mealie_item

from mealie_gkeep_sync.models import MealieFood, MealieUnit, ParsedIngredient
from mealie_gkeep_sync.render import (
    build_create_payload,
    format_quantity,
    item_fields,
    normalise_text,
    render_item,
)


@pytest.mark.parametrize(
    ("quantity", "expected"),
    [(1, "1"), (1.0, "1"), (2.5, "2.5"), (0.5, "0.5"), (3.0, "3"), (0.333333, "0.333")],
)
def test_format_quantity(quantity: float, expected: str) -> None:
    assert format_quantity(quantity) == expected


def test_render_prefers_mealie_display() -> None:
    """Mealie's own display keeps the Keep list reading like the Mealie UI."""
    item = mealie_item("m1", display="2 lb Chicken breast", note="ignored", is_food=True)
    assert render_item(item) == "2 lb Chicken breast"


def test_render_falls_back_to_note_for_unstructured_items() -> None:
    item = mealie_item("m1", note="  Paper towels  ")
    assert render_item(item) == "Paper towels"


def test_render_composes_structured_item_without_display() -> None:
    item = mealie_item(
        "m1",
        is_food=True,
        quantity=2,
        food=MealieFood(id="f1", name="Onion", plural_name="Onions"),
        unit=MealieUnit(id="u1", name="pound", plural_name="pounds"),
    )
    assert render_item(item) == "2 pounds Onions"


def test_render_uses_abbreviation_when_configured() -> None:
    item = mealie_item(
        "m1",
        is_food=True,
        quantity=3,
        food=MealieFood(id="f1", name="Flour"),
        unit=MealieUnit(
            id="u1", name="tablespoon", abbreviation="tbsp", plural_abbreviation="tbsp",
            use_abbreviation=True,
        ),
    )
    assert render_item(item) == "3 tbsp Flour"


def test_render_appends_note_to_structured_item() -> None:
    item = mealie_item(
        "m1",
        is_food=True,
        quantity=1,
        food=MealieFood(id="f1", name="Milk"),
        note="oat",
    )
    assert render_item(item) == "1 Milk (oat)"


def test_normalise_text_collapses_whitespace() -> None:
    assert normalise_text("  two   spaces\tand\ntabs ") == "two spaces and tabs"


class TestItemFields:
    """A parse is only trusted when it fully round-trips."""

    def test_confident_parse_with_known_food_is_structured(self) -> None:
        parsed = ParsedIngredient(
            confidence=0.9,
            quantity=2,
            note="",
            food=MealieFood(id="f1", name="Chicken breast"),
            unit=MealieUnit(id="u1", name="pound"),
        )
        fields = item_fields("2 lb chicken breast", parsed=parsed, min_confidence=0.6)
        assert fields["isFood"] is True
        assert fields["foodId"] == "f1"
        assert fields["unitId"] == "u1"
        assert fields["quantity"] == 2

    def test_low_confidence_falls_back_to_note(self) -> None:
        parsed = ParsedIngredient(
            confidence=0.2, quantity=1, food=MealieFood(id="f1", name="Thing")
        )
        fields = item_fields("mystery thing", parsed=parsed, min_confidence=0.6)
        assert fields["isFood"] is False
        assert fields["note"] == "mystery thing"

    def test_unmatched_food_falls_back_to_note(self) -> None:
        """No food ID means Mealie has no such food; do not invent one."""
        parsed = ParsedIngredient(
            confidence=0.9, quantity=1, food=MealieFood(id=None, name="Kumquats")
        )
        fields = item_fields("kumquats", parsed=parsed, min_confidence=0.6)
        assert fields["isFood"] is False
        assert fields["foodId"] is None

    def test_unmatched_unit_falls_back_to_note(self) -> None:
        """Accepting this parse would drop 'schmoo' and overwrite the user's Keep text."""
        parsed = ParsedIngredient(
            confidence=0.9,
            quantity=2,
            note="",
            food=MealieFood(id="f1", name="Sugar"),
            unit=MealieUnit(id=None, name="schmoo"),
        )
        fields = item_fields("2 schmoo sugar", parsed=parsed, min_confidence=0.6)
        assert fields["isFood"] is False
        assert fields["note"] == "2 schmoo sugar"

    def test_no_parse_is_unstructured(self) -> None:
        fields = item_fields("Paper towels", parsed=None)
        assert fields == {
            "isFood": False,
            "foodId": None,
            "unitId": None,
            "quantity": 1,
            "note": "Paper towels",
        }

    def test_explicit_food_id_overrides_missing_match(self) -> None:
        """Path taken when CREATE_MISSING_FOODS created the food for us."""
        parsed = ParsedIngredient(
            confidence=0.9, quantity=1, note="", food=MealieFood(id=None, name="Kumquats")
        )
        fields = item_fields("kumquats", parsed=parsed, food_id="new-food")
        assert fields["isFood"] is True
        assert fields["foodId"] == "new-food"


def test_build_create_payload_stamps_keep_id() -> None:
    payload = build_create_payload("Paper towels", "list-1", checked=True, keep_id="k1")
    assert payload["shoppingListId"] == "list-1"
    assert payload["checked"] is True
    assert payload["extras"] == {"gkeep_item_id": "k1"}
    assert payload["note"] == "Paper towels"
