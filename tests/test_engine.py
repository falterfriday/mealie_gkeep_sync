"""The reconciliation matrix.

These cover the whole table in engine.py's docstring, plus the recovery paths that only
appear when state is lost.
"""

from __future__ import annotations

from conftest import keep_item, link, mealie_item

from mealie_gkeep_sync.config import ConflictStrategy
from mealie_gkeep_sync.engine import plan_sync
from mealie_gkeep_sync.models import KEEP_ID_EXTRA


class TestNoChanges:
    def test_matching_sides_produce_no_actions(self) -> None:
        plan = plan_sync(
            [mealie_item("m1", note="Milk", keep_id="k1")],
            [keep_item("k1", "Milk")],
            [link("m1", "k1", "Milk")],
        )
        assert plan.is_empty
        assert len(plan.surviving_links) == 1
        assert plan.surviving_links[0].text == "Milk"

    def test_whitespace_only_difference_is_not_a_change(self) -> None:
        plan = plan_sync(
            [mealie_item("m1", note="Milk", keep_id="k1")],
            [keep_item("k1", "  Milk  ")],
            [link("m1", "k1", "Milk")],
        )
        assert plan.is_empty


class TestOneSidedEdits:
    def test_mealie_text_edit_pushes_to_keep(self) -> None:
        plan = plan_sync(
            [mealie_item("m1", note="Oat milk", keep_id="k1")],
            [keep_item("k1", "Milk")],
            [link("m1", "k1", "Milk")],
        )
        assert len(plan.keep_updates) == 1
        assert plan.keep_updates[0].text == "Oat milk"
        assert plan.keep_updates[0].checked is None
        assert not plan.mealie_updates
        assert plan.conflicts == 0

    def test_keep_text_edit_pushes_to_mealie(self) -> None:
        plan = plan_sync(
            [mealie_item("m1", note="Milk", keep_id="k1")],
            [keep_item("k1", "Oat milk")],
            [link("m1", "k1", "Milk")],
        )
        assert len(plan.mealie_updates) == 1
        assert plan.mealie_updates[0].text == "Oat milk"
        assert not plan.keep_updates

    def test_keep_check_pushes_to_mealie(self) -> None:
        plan = plan_sync(
            [mealie_item("m1", note="Milk", keep_id="k1")],
            [keep_item("k1", "Milk", checked=True)],
            [link("m1", "k1", "Milk", checked=False)],
        )
        assert len(plan.mealie_updates) == 1
        assert plan.mealie_updates[0].checked is True
        assert plan.surviving_links[0].checked is True

    def test_mealie_check_pushes_to_keep(self) -> None:
        plan = plan_sync(
            [mealie_item("m1", note="Milk", checked=True, keep_id="k1")],
            [keep_item("k1", "Milk")],
            [link("m1", "k1", "Milk", checked=False)],
        )
        assert len(plan.keep_updates) == 1
        assert plan.keep_updates[0].checked is True


class TestConflicts:
    def test_both_sides_same_new_value_is_not_a_conflict(self) -> None:
        plan = plan_sync(
            [mealie_item("m1", note="Oat milk", keep_id="k1")],
            [keep_item("k1", "Oat milk")],
            [link("m1", "k1", "Milk")],
        )
        assert plan.is_empty
        assert plan.conflicts == 0
        assert plan.surviving_links[0].text == "Oat milk"

    def test_newest_strategy_prefers_later_timestamp(self) -> None:
        plan = plan_sync(
            [mealie_item("m1", note="Almond milk", keep_id="k1", updated_offset=10)],
            [keep_item("k1", "Oat milk", updated_offset=5)],
            [link("m1", "k1", "Milk")],
            strategy=ConflictStrategy.NEWEST,
        )
        assert plan.conflicts == 1
        assert plan.keep_updates[0].text == "Almond milk"
        assert plan.surviving_links[0].text == "Almond milk"

    def test_newest_strategy_can_pick_keep(self) -> None:
        plan = plan_sync(
            [mealie_item("m1", note="Almond milk", keep_id="k1", updated_offset=1)],
            [keep_item("k1", "Oat milk", updated_offset=30)],
            [link("m1", "k1", "Milk")],
            strategy=ConflictStrategy.NEWEST,
        )
        assert plan.mealie_updates[0].text == "Oat milk"

    def test_mealie_strategy_always_wins(self) -> None:
        plan = plan_sync(
            [mealie_item("m1", note="Almond milk", keep_id="k1", updated_offset=1)],
            [keep_item("k1", "Oat milk", updated_offset=99)],
            [link("m1", "k1", "Milk")],
            strategy=ConflictStrategy.MEALIE,
        )
        assert plan.keep_updates[0].text == "Almond milk"

    def test_keep_strategy_always_wins(self) -> None:
        plan = plan_sync(
            [mealie_item("m1", note="Almond milk", keep_id="k1", updated_offset=99)],
            [keep_item("k1", "Oat milk", updated_offset=1)],
            [link("m1", "k1", "Milk")],
            strategy=ConflictStrategy.KEEP,
        )
        assert plan.mealie_updates[0].text == "Oat milk"

    def test_text_and_checked_resolve_independently(self) -> None:
        """Renaming on one side while ticking off on the other is not a conflict."""
        plan = plan_sync(
            [mealie_item("m1", note="Oat milk", keep_id="k1")],
            [keep_item("k1", "Milk", checked=True)],
            [link("m1", "k1", "Milk", checked=False)],
        )
        assert plan.conflicts == 0
        assert plan.keep_updates[0].text == "Oat milk"
        assert plan.keep_updates[0].checked is None
        assert plan.mealie_updates[0].checked is True
        assert plan.mealie_updates[0].text is None


class TestDeletions:
    def test_missing_keep_item_deletes_from_mealie(self) -> None:
        plan = plan_sync(
            [mealie_item("m1", note="Milk")],
            [],
            [link("m1", "k1", "Milk")],
        )
        assert [action.mealie_id for action in plan.mealie_deletes] == ["m1"]
        assert not plan.keep_creates
        assert not plan.surviving_links

    def test_missing_mealie_item_deletes_from_keep(self) -> None:
        plan = plan_sync(
            [],
            [keep_item("k1", "Milk")],
            [link("m1", "k1", "Milk")],
        )
        assert [action.keep_id for action in plan.keep_deletes] == ["k1"]
        assert not plan.mealie_creates

    def test_both_gone_drops_the_link_silently(self) -> None:
        plan = plan_sync([], [], [link("m1", "k1", "Milk")])
        assert plan.is_empty
        assert not plan.surviving_links


class TestCreations:
    def test_new_mealie_item_is_created_in_keep(self) -> None:
        plan = plan_sync([mealie_item("m1", note="Bread")], [], [])
        assert len(plan.keep_creates) == 1
        assert plan.keep_creates[0].text == "Bread"
        assert plan.keep_creates[0].mealie_id == "m1"

    def test_new_keep_item_is_created_in_mealie(self) -> None:
        plan = plan_sync([], [keep_item("k1", "Bread", checked=True)], [])
        assert len(plan.mealie_creates) == 1
        assert plan.mealie_creates[0].keep_id == "k1"
        assert plan.mealie_creates[0].checked is True

    def test_empty_items_are_skipped(self) -> None:
        plan = plan_sync([mealie_item("m1", note="   ")], [keep_item("k1", "")], [])
        assert plan.is_empty


class TestRelinking:
    """Recovery when the state file is lost but Mealie still carries the Keep ID."""

    def test_extras_rebuild_the_link_instead_of_duplicating(self) -> None:
        plan = plan_sync(
            [mealie_item("m1", note="Milk", keep_id="k1")],
            [keep_item("k1", "Milk")],
            [],  # state lost
        )
        assert not plan.keep_creates
        assert not plan.mealie_creates
        assert len(plan.surviving_links) == 1
        assert plan.surviving_links[0].keep_id == "k1"

    def test_relink_resolves_divergence_by_strategy(self) -> None:
        plan = plan_sync(
            [mealie_item("m1", note="Milk", keep_id="k1", updated_offset=1)],
            [keep_item("k1", "Oat milk", updated_offset=50)],
            [],
            strategy=ConflictStrategy.NEWEST,
        )
        assert len(plan.mealie_updates) == 1
        assert plan.mealie_updates[0].text == "Oat milk"
        assert plan.surviving_links[0].text == "Oat milk"

    def test_stale_extras_pointing_nowhere_creates_a_new_keep_item(self) -> None:
        """Non-destructive: we re-create rather than assume a Keep-side delete."""
        plan = plan_sync(
            [mealie_item("m1", note="Milk", keep_id="gone")],
            [],
            [],
        )
        assert len(plan.keep_creates) == 1

    def test_two_mealie_items_cannot_claim_the_same_keep_item(self) -> None:
        plan = plan_sync(
            [
                mealie_item("m1", note="Milk", keep_id="k1"),
                mealie_item("m2", note="Milk", keep_id="k1"),
            ],
            [keep_item("k1", "Milk")],
            [],
        )
        claimed = [lnk for lnk in plan.surviving_links if lnk.keep_id == "k1"]
        assert len(claimed) == 1
        assert len(plan.keep_creates) == 1
        assert plan.keep_creates[0].mealie_id == "m2"


class TestExtrasStamping:
    def test_missing_keep_id_in_extras_is_restamped(self) -> None:
        plan = plan_sync(
            [mealie_item("m1", note="Milk")],  # no extras
            [keep_item("k1", "Milk")],
            [link("m1", "k1", "Milk")],
        )
        assert len(plan.mealie_updates) == 1
        assert plan.mealie_updates[0].keep_id == "k1"
        assert plan.mealie_updates[0].text is None

    def test_correct_extras_need_no_update(self) -> None:
        item = mealie_item("m1", note="Milk", keep_id="k1")
        assert item.extras[KEEP_ID_EXTRA] == "k1"
        plan = plan_sync([item], [keep_item("k1", "Milk")], [link("m1", "k1", "Milk")])
        assert not plan.mealie_updates
