"""Blank entries must never propagate into Google Keep.

A Mealie item with no food record and no note renders to "". Before this guard that
empty render was treated as an edit and pushed to Keep, blanking the line the user could
actually see. Creates were already guarded; the already-linked path was not.
"""

from __future__ import annotations

from conftest import keep_item, link, mealie_item

from mealie_gkeep_sync.engine import plan_sync
from mealie_gkeep_sync.models import MealieFood
from mealie_gkeep_sync.render import render_item


class TestBlankFromMealie:
    def test_empty_mealie_item_renders_empty(self) -> None:
        assert render_item(mealie_item("m1", note="")) == ""
        assert render_item(mealie_item("m1", note="   ")) == ""

    def test_empty_render_never_blanks_the_keep_item(self) -> None:
        item = mealie_item("m1", note="", keep_id="k1")
        plan = plan_sync([item], [keep_item("k1", "Milk")], [link("m1", "k1", "Milk")])
        assert not any(u.text == "" for u in plan.keep_updates)
        assert plan.keep_updates == []

    def test_whitespace_only_note_never_blanks_either(self) -> None:
        item = mealie_item("m1", note="   ", keep_id="k1")
        plan = plan_sync([item], [keep_item("k1", "Bread")], [link("m1", "k1", "Bread")])
        assert plan.keep_updates == []

    def test_empty_render_does_not_churn_across_cycles(self) -> None:
        """It must be inert, not merely non-destructive."""
        item = mealie_item("m1", note="", keep_id="k1")
        keep = [keep_item("k1", "Milk")]
        links = [link("m1", "k1", "Milk")]
        for _ in range(3):
            plan = plan_sync([item], keep, links)
            assert plan.is_empty
            links = plan.surviving_links

    def test_checked_still_syncs_when_text_is_empty(self) -> None:
        """Suppressing the text must not suppress everything else."""
        item = mealie_item("m1", note="", keep_id="k1", checked=True)
        plan = plan_sync(
            [item], [keep_item("k1", "Milk")], [link("m1", "k1", "Milk", checked=False)]
        )
        assert [u.checked for u in plan.keep_updates] == [True]
        assert all(u.text is None for u in plan.keep_updates)

    def test_keep_side_edit_still_wins_and_restores_content(self) -> None:
        """An empty Mealie item is missing data, so a real Keep edit should repair it."""
        item = mealie_item("m1", note="", keep_id="k1")
        plan = plan_sync([item], [keep_item("k1", "Oat milk")], [link("m1", "k1", "Milk")])
        assert [u.text for u in plan.mealie_updates] == ["Oat milk"]

    def test_real_content_still_propagates(self) -> None:
        """The guard must not suppress legitimate Mealie-side renames."""
        item = mealie_item("m1", food=MealieFood(id="f1", name="Oat milk"), keep_id="k1")
        plan = plan_sync([item], [keep_item("k1", "Milk")], [link("m1", "k1", "Milk")])
        assert [u.text for u in plan.keep_updates] == ["Oat milk"]


class TestBlankFromKeep:
    def test_blank_keep_entry_is_not_imported(self) -> None:
        for text in ("", "   ", "\t\n"):
            plan = plan_sync([], [keep_item("k1", text)], [])
            assert plan.mealie_creates == [], f"{text!r} should be ignored"

    def test_blank_keep_entry_alongside_real_ones(self) -> None:
        plan = plan_sync([], [keep_item("k1", "  "), keep_item("k2", "Milk")], [])
        assert [c.keep_id for c in plan.mealie_creates] == ["k2"]

    def test_user_blanking_a_linked_keep_item_does_not_wipe_mealie(self) -> None:
        """Blanking in Keep is almost certainly a slip, not an instruction."""
        item = mealie_item("m1", food=MealieFood(id="f1", name="Milk"), keep_id="k1")
        plan = plan_sync([item], [keep_item("k1", "")], [link("m1", "k1", "Milk")])
        assert all(u.text != "" for u in plan.mealie_updates)


class TestKeepClientRefusesBlanks:
    def test_update_item_refuses_empty_text(self, tmp_path) -> None:
        """Last line of defence, independent of the engine."""
        from mealie_gkeep_sync.keep_client import KeepClient

        client = KeepClient(
            "a@b.com", "t", state_path=tmp_path / "s.json", list_name="L"
        )

        class Node:
            def __init__(self) -> None:
                self.id = "k1"
                self.text = "Milk"
                self.checked = False

        node = Node()
        client._find_item = lambda item_id: node  # type: ignore[method-assign]

        client.update_item("k1", text="")
        assert node.text == "Milk", "must not blank"
        client.update_item("k1", text="   ")
        assert node.text == "Milk", "must not blank on whitespace"
        client.update_item("k1", text="Oat milk")
        assert node.text == "Oat milk", "real text must still apply"
