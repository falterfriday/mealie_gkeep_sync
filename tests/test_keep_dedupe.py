"""Duplicate Keep lines are combined before anything reaches Mealie.

Adding "milk" when the list already says "Milk" should not create a second Mealie item.
Mealie would merge them anyway, and the spare Keep line would linger unlinked forever -
which is exactly what happened to "ground turkey" in production.
"""

from __future__ import annotations

import pytest
from conftest import keep_item, link, mealie_item

from mealie_gkeep_sync.engine import plan_sync
from mealie_gkeep_sync.models import MealieFood


class TestCaseInsensitiveCombine:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("Milk", "milk"),
            ("milk", "MILK"),
            ("Ground Turkey", "ground turkey"),
            ("Milk", "  Milk  "),
            ("Crème fraîche", "CRÈME FRAÎCHE"),
        ],
    )
    def test_duplicates_collapse(self, a: str, b: str) -> None:
        plan = plan_sync([], [keep_item("k1", a), keep_item("k2", b)], [])
        assert [d.keep_id for d in plan.keep_deletes] == ["k2"]
        assert [c.keep_id for c in plan.mealie_creates] == ["k1"], "only one reaches Mealie"

    def test_distinct_items_are_untouched(self) -> None:
        plan = plan_sync([], [keep_item("k1", "Milk"), keep_item("k2", "Bread")], [])
        assert plan.keep_deletes == []
        assert {c.keep_id for c in plan.mealie_creates} == {"k1", "k2"}

    def test_three_way_duplicate_leaves_one(self) -> None:
        keep = [keep_item("k1", "Milk"), keep_item("k2", "milk"), keep_item("k3", "MILK")]
        plan = plan_sync([], keep, [])
        assert sorted(d.keep_id for d in plan.keep_deletes) == ["k2", "k3"]
        assert [c.keep_id for c in plan.mealie_creates] == ["k1"]

    def test_blank_lines_are_not_treated_as_duplicates(self) -> None:
        plan = plan_sync([], [keep_item("k1", ""), keep_item("k2", "   ")], [])
        assert plan.keep_deletes == []


class TestLinkedItemsAreProtected:
    def test_the_linked_line_survives_not_the_first(self) -> None:
        """Deleting the linked one would orphan its Mealie item and restart the loop."""
        item = mealie_item("m1", food=MealieFood(id="f1", name="Milk"), keep_id="k2")
        keep = [keep_item("k1", "milk"), keep_item("k2", "Milk")]
        plan = plan_sync([item], keep, [link("m1", "k2", "Milk")])
        assert [d.keep_id for d in plan.keep_deletes] == ["k1"]
        assert plan.mealie_creates == [], "nothing new should be created"

    def test_two_linked_duplicates_are_left_alone(self) -> None:
        """Legitimate: '1 cup Basil' and '2 tbsp Basil' both render as 'Basil'."""
        items = [
            mealie_item("m1", food=MealieFood(id="f1", name="Basil"), keep_id="k1"),
            mealie_item("m2", food=MealieFood(id="f1", name="Basil"), keep_id="k2"),
        ]
        keep = [keep_item("k1", "Basil"), keep_item("k2", "Basil")]
        links = [link("m1", "k1", "Basil"), link("m2", "k2", "Basil")]
        plan = plan_sync(items, keep, links)
        assert plan.keep_deletes == []
        assert plan.mealie_deletes == []

    def test_no_mealie_item_is_deleted_as_a_side_effect(self) -> None:
        item = mealie_item("m1", food=MealieFood(id="f1", name="Milk"), keep_id="k1")
        keep = [keep_item("k1", "Milk"), keep_item("k2", "milk")]
        plan = plan_sync([item], keep, [link("m1", "k1", "Milk")])
        assert plan.mealie_deletes == []


class TestCheckedHandling:
    def test_readding_a_ticked_item_unticks_it(self) -> None:
        """The user bought it, then added it again - they want it again."""
        keep = [keep_item("k1", "Milk", checked=True), keep_item("k2", "milk", checked=False)]
        plan = plan_sync([], keep, [])
        assert [d.keep_id for d in plan.keep_deletes] == ["k2"]
        assert [(u.keep_id, u.checked) for u in plan.keep_updates] == [("k1", False)]
        assert plan.mealie_creates[0].checked is False

    def test_both_ticked_stays_ticked(self) -> None:
        keep = [keep_item("k1", "Milk", checked=True), keep_item("k2", "milk", checked=True)]
        plan = plan_sync([], keep, [])
        assert plan.keep_updates == []
        assert plan.mealie_creates[0].checked is True

    def test_both_unticked_needs_no_update(self) -> None:
        keep = [keep_item("k1", "Milk"), keep_item("k2", "milk")]
        plan = plan_sync([], keep, [])
        assert plan.keep_updates == []


class TestConvergence:
    def test_second_cycle_is_idle(self) -> None:
        """Combining must settle, not repeat."""
        item = mealie_item("m1", food=MealieFood(id="f1", name="Milk"), keep_id="k1")
        links = [link("m1", "k1", "Milk")]
        # Cycle 1: duplicate present.
        plan1 = plan_sync([item], [keep_item("k1", "Milk"), keep_item("k2", "milk")], links)
        assert [d.keep_id for d in plan1.keep_deletes] == ["k2"]
        # Cycle 2: the duplicate is gone, as the delete took effect.
        plan2 = plan_sync([item], [keep_item("k1", "Milk")], plan1.surviving_links)
        assert plan2.is_empty

    def test_the_ground_turkey_case(self) -> None:
        """The real production pair, which previously warned on every single cycle."""
        item = mealie_item("m1", food=MealieFood(id="f1", name="ground turkey"),
                           keep_id="cbx.al1beu21ezw8")
        keep = [
            keep_item("cbx.al1beu21ezw8", "ground turkey"),
            keep_item("cbx.kifakouvaztn", "Ground Turkey"),
        ]
        plan = plan_sync([item], keep, [link("m1", "cbx.al1beu21ezw8", "ground turkey")])
        assert [d.keep_id for d in plan.keep_deletes] == ["cbx.kifakouvaztn"]
        assert plan.mealie_creates == []
