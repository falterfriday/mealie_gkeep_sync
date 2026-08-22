"""Rendering Mealie items to Keep text, and structuring Keep text for Mealie."""

from __future__ import annotations

import pytest
from conftest import mealie_item

from mealie_gkeep_sync.models import MealieFood, MealieItem, MealieUnit, ParsedIngredient
from mealie_gkeep_sync.render import (
    build_create_payload,
    has_explicit_amount,
    item_fields,
    normalise_text,
    render_item,
)


class TestRenderStripsAmounts:
    """Keep answers "what am I buying?"; Mealie holds how much."""

    def test_quantity_and_unit_are_dropped(self) -> None:
        item = mealie_item(
            "m1",
            quantity=1,
            food=MealieFood(id="f1", name="Basil"),
            unit=MealieUnit(id="u1", name="cup"),
        )
        assert render_item(item) == "Basil"

    def test_mealie_display_is_ignored(self) -> None:
        """display embeds exactly the amount we are stripping, so it must not win."""
        item = mealie_item(
            "m1",
            display="2 lb Chicken breast",
            quantity=2,
            food=MealieFood(id="f1", name="Chicken breast"),
            unit=MealieUnit(id="u1", name="pound"),
        )
        assert render_item(item) == "Chicken breast"

    def test_plural_name_is_not_used(self) -> None:
        """A plural would leak the quantity back in and churn text as it crossed 1."""
        item = mealie_item(
            "m1",
            quantity=6,
            food=MealieFood(id="f1", name="Onion", plural_name="Onions"),
        )
        assert render_item(item) == "Onion"

    def test_abbreviated_unit_is_dropped(self) -> None:
        item = mealie_item(
            "m1",
            quantity=3,
            food=MealieFood(id="f1", name="Flour"),
            unit=MealieUnit(
                id="u1",
                name="tablespoon",
                abbreviation="tbsp",
                plural_abbreviation="tbsp",
                use_abbreviation=True,
            ),
        )
        assert render_item(item) == "Flour"

    def test_quantity_change_produces_identical_text(self) -> None:
        """The point of using food.name: editing an amount causes no Keep write."""
        food = MealieFood(id="f1", name="Basil")
        unit = MealieUnit(id="u1", name="cup")
        one = mealie_item("m1", quantity=1, food=food, unit=unit)
        many = mealie_item("m1", quantity=4, food=food, unit=unit)
        assert render_item(one) == render_item(many)

    def test_note_is_dropped(self) -> None:
        """Only the food crosses over; the note stays in Mealie with the amounts."""
        item = mealie_item(
            "m1",
            quantity=2,
            food=MealieFood(id="f1", name="Milk"),
            unit=MealieUnit(id="u1", name="litre"),
            note="oat",
        )
        assert render_item(item) == "Milk"

    def test_unstructured_item_passes_note_through(self) -> None:
        item = mealie_item("m1", note="  Paper towels  ")
        assert render_item(item) == "Paper towels"

    def test_unstructured_text_is_verbatim_even_with_a_number(self) -> None:
        """Nothing parsed the amount out, so guessing risks mangling "2% milk"."""
        item = mealie_item("m1", note="2% milk")
        assert render_item(item) == "2% milk"

    def test_food_item_without_food_record_falls_back_to_note(self) -> None:
        item = mealie_item("m1", quantity=3, note="Something odd")
        assert render_item(item) == "Something odd"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Basil", False),
        ("Chicken breast", False),
        ("1 cup basil", True),
        ("\u00bd tsp salt", True),
        ("\u2154 cup flour", True),
        ("2% milk", True),
    ],
)
def test_has_explicit_amount(text: str, expected: bool) -> None:
    assert has_explicit_amount(text) is expected


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
        assert fields["foodId"] == "f1"
        assert fields["unitId"] == "u1"
        assert fields["quantity"] == 2

    def test_low_confidence_falls_back_to_note(self) -> None:
        parsed = ParsedIngredient(
            confidence=0.2, quantity=1, food=MealieFood(id="f1", name="Thing")
        )
        fields = item_fields("mystery thing", parsed=parsed, min_confidence=0.6)
        assert fields["foodId"] is None
        assert fields["note"] == "mystery thing"

    def test_unmatched_food_falls_back_to_note(self) -> None:
        """No food ID means Mealie has no such food; do not invent one."""
        parsed = ParsedIngredient(
            confidence=0.9, quantity=1, food=MealieFood(id=None, name="Kumquats")
        )
        fields = item_fields("kumquats", parsed=parsed, min_confidence=0.6)
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
        assert fields["foodId"] is None
        assert fields["note"] == "2 schmoo sugar"

    def test_no_parse_is_unstructured(self) -> None:
        fields = item_fields("Paper towels", parsed=None)
        assert fields == {
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
        assert fields["foodId"] == "new-food"


def test_build_create_payload_stamps_keep_id() -> None:
    payload = build_create_payload("Paper towels", "list-1", checked=True, keep_id="k1")
    assert payload["shoppingListId"] == "list-1"
    assert payload["checked"] is True
    assert payload["extras"] == {"gkeep_item_id": "k1"}
    assert payload["note"] == "Paper towels"


class TestAmountPreservationOnEdits:
    """Keep carries no amounts, so a rename there must not reset Mealie's quantity."""

    def _current(self) -> object:
        return mealie_item(
            "m1",
            quantity=1,
            food=MealieFood(id="f1", name="Basil"),
            unit=MealieUnit(id="u-cup", name="cup"),
        )

    def test_rename_without_an_amount_keeps_quantity_and_unit(self) -> None:
        """"1 cup Basil" renamed to "Parsley" in Keep stays 1 cup, not a bare 1."""
        parsed = ParsedIngredient(
            confidence=0.9, quantity=1, note="", food=MealieFood(id="f2", name="Parsley")
        )
        fields = item_fields("Parsley", parsed=parsed, current=self._current())
        assert fields["foodId"] == "f2"
        assert fields["quantity"] == 1
        assert fields["unitId"] == "u-cup"

    def test_typed_amount_overrides_the_existing_one(self) -> None:
        parsed = ParsedIngredient(
            confidence=0.9,
            quantity=3,
            note="",
            food=MealieFood(id="f1", name="Basil"),
            unit=MealieUnit(id="u-tbsp", name="tablespoon"),
        )
        fields = item_fields("3 tbsp basil", parsed=parsed, current=self._current())
        assert fields["quantity"] == 3
        assert fields["unitId"] == "u-tbsp"

    def test_creates_have_no_current_so_the_parse_wins(self) -> None:
        parsed = ParsedIngredient(
            confidence=0.9,
            quantity=2,
            note="",
            food=MealieFood(id="f1", name="Chicken breast"),
            unit=MealieUnit(id="u-lb", name="pound"),
        )
        payload = build_create_payload("2 lb chicken", "list-1", parsed=parsed, keep_id="k1")
        assert payload["quantity"] == 2
        assert payload["unitId"] == "u-lb"

    def test_unstructured_rename_keeps_quantity(self) -> None:
        current = mealie_item("m1", note="Paper towels", quantity=3)
        fields = item_fields("Kitchen roll", parsed=None, current=current)
        assert fields["foodId"] is None
        assert fields["note"] == "Kitchen roll"
        assert fields["quantity"] == 3

    def test_no_current_still_defaults_to_one(self) -> None:
        fields = item_fields("Paper towels", parsed=None)
        assert fields["quantity"] == 1


class TestRealMealiePayloads:
    """Rendering driven by payloads shaped exactly as Mealie's API returns them.

    Regression cover for a bug the factory-based tests could not catch: the model used to
    declare an ``is_food`` field, which Mealie does not send. It defaulted to False on
    every real payload, so every item fell through to the note branch and Keep received
    quantities and notes instead of the food name.
    """

    def test_food_item_renders_its_food_name(self) -> None:
        raw = {
            "id": "m1",
            "shoppingListId": "list-1",
            "checked": False,
            "position": 0,
            "quantity": 1.0,
            "note": "",
            "display": "1 cup Basil",
            "foodId": "f1",
            "unitId": "u1",
            "food": {"id": "f1", "name": "Basil", "pluralName": "Basil"},
            "unit": {"id": "u1", "name": "cup", "abbreviation": "c"},
            "extras": {},
            "recipeReferences": [],
        }
        item = MealieItem.model_validate(raw)
        assert render_item(item) == "Basil"

    def test_food_item_with_a_note_still_renders_only_the_food(self) -> None:
        raw = {
            "id": "m2",
            "shoppingListId": "list-1",
            "quantity": 2.0,
            "note": "fresh",
            "display": "2 bunches Parsley (fresh)",
            "foodId": "f2",
            "food": {"id": "f2", "name": "Parsley"},
            "unit": {"id": "u2", "name": "bunch"},
            "extras": {},
        }
        item = MealieItem.model_validate(raw)
        assert render_item(item) == "Parsley"

    def test_free_text_item_falls_back_to_its_note(self) -> None:
        """No food record, so the note is all there is - dropping it would lose the item."""
        raw = {
            "id": "m3",
            "shoppingListId": "list-1",
            "quantity": 1.0,
            "note": "Paper towels",
            "display": "Paper towels",
            "foodId": None,
            "food": None,
            "extras": {},
        }
        item = MealieItem.model_validate(raw)
        assert render_item(item) == "Paper towels"

    def test_snake_case_payloads_also_work(self) -> None:
        """populate_by_name means either casing validates; neither may resurrect is_food."""
        raw = {
            "id": "m4",
            "shopping_list_id": "list-1",
            "quantity": 3.0,
            "note": "",
            "food_id": "f4",
            "food": {"id": "f4", "name": "Eggs"},
            "extras": {},
        }
        item = MealieItem.model_validate(raw)
        assert render_item(item) == "Eggs"

    def test_model_has_no_is_food_field(self) -> None:
        assert "is_food" not in MealieItem.model_fields
