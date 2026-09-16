"""Mealie's bulk create merges items; the sync must not fight it.

Mealie consolidates duplicates within a batch and folds new items into pre-existing
unchecked entries. Absorbed items return under ``updatedItems``. Reading only
``createdItems`` left the Keep item unlinked, so every cycle re-created it, Mealie
re-merged it, and the target item's quantity climbed forever.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import keep_item, link, mealie_item

from mealie_gkeep_sync.config import ConflictStrategy, Settings
from mealie_gkeep_sync.engine import plan_sync
from mealie_gkeep_sync.models import KEEP_ID_EXTRA, MealieItem
from mealie_gkeep_sync.state import LinkStore
from mealie_gkeep_sync.sync import Syncer

LIST_ID = "11111111-1111-1111-1111-111111111111"


class FakeMealie:
    """Stands in for MealieClient, reproducing Mealie's merge semantics."""

    def __init__(self, response: list[MealieItem]) -> None:
        self.response = response
        self.create_calls: list[list[dict[str, Any]]] = []

    def create_items(self, payloads: list[dict[str, Any]]) -> list[MealieItem]:
        self.create_calls.append(payloads)
        return self.response

    def update_items(self, payloads: list[dict[str, Any]]) -> list[MealieItem]:
        return []

    def delete_items(self, ids: list[str]) -> None:
        return None

    def parse_ingredients(self, texts: list[str]) -> list[Any]:
        return []


class FakeKeep:
    def __init__(self) -> None:
        self.updates: list[tuple[str, str | None]] = []
        self.flushed = 0

    def update_item(self, item_id: str, *, text: str | None = None, checked: bool | None = None):
        self.updates.append((item_id, text))

    def delete_item(self, item_id: str) -> None:
        return None

    def add_item(self, text: str, checked: bool = False) -> str:
        return "new-keep-id"

    def flush(self) -> None:
        self.flushed += 1


def _settings(**over: Any) -> Settings:
    base: dict[str, Any] = dict(
        mealie_base_url="http://m",
        mealie_api_token="t",
        mealie_list_name="L",
        google_email="a@b.com",
        google_master_token="aas_et/x",
        keep_list_name="L",
        parse_ingredients=False,
        conflict_strategy=ConflictStrategy.NEWEST,
    )
    base.update(over)
    return Settings(**base)


def _syncer(mealie: FakeMealie, keep: FakeKeep, store: LinkStore) -> Syncer:
    syncer = Syncer(_settings(), mealie, keep, store)  # type: ignore[arg-type]
    syncer._mealie_list_id = LIST_ID
    return syncer


def _merged_item(item_id: str, note: str, keep_id: str) -> MealieItem:
    """A pre-existing Mealie item that absorbed our new one and took its extras."""
    return mealie_item(item_id, note=note, keep_id=keep_id)


class TestAbsorbedItems:
    def test_item_returned_under_updated_items_is_still_linked(self, tmp_path) -> None:
        """The crash-adjacent bug: absorbed items came back as updates and were dropped."""
        plan = plan_sync([], [keep_item("k1", "Milk")], [])
        assert len(plan.mealie_creates) == 1

        mealie = FakeMealie([_merged_item("m1", "Milk", "k1")])
        keep, store = FakeKeep(), LinkStore(tmp_path / "s.json")
        links: dict[str, Any] = {}

        absorbed = _syncer(mealie, keep, store)._apply_mealie_creates(plan, [None], links)

        assert absorbed == {}
        assert links["m1"].keep_id == "k1"

    def test_unreturned_item_is_recorded_as_absorbed(self, tmp_path) -> None:
        """Mealie kept the existing item's extras, so nothing carries our Keep ID."""
        plan = plan_sync([], [keep_item("k1", "Milk")], [])
        mealie = FakeMealie([])  # merged away entirely
        keep, store = FakeKeep(), LinkStore(tmp_path / "s.json")
        links: dict[str, Any] = {}

        absorbed = _syncer(mealie, keep, store)._apply_mealie_creates(plan, [None], links)

        assert absorbed == {"k1": "Milk"}
        assert links == {}

    def test_collision_keeps_the_existing_link(self, tmp_path) -> None:
        """Stealing the link would leave the other Keep item to merge again next cycle."""
        plan = plan_sync([], [keep_item("k2", "Milk")], [])
        # Mealie merged k2 into m1, which our state already links to k1.
        mealie = FakeMealie([_merged_item("m1", "Milk", "k2")])
        keep, store = FakeKeep(), LinkStore(tmp_path / "s.json")
        links = {"m1": link("m1", "k1", "Milk")}

        absorbed = _syncer(mealie, keep, store)._apply_mealie_creates(plan, [None], links)

        assert absorbed == {"k2": "Milk"}
        assert links["m1"].keep_id == "k1", "existing link must survive"

    def test_response_order_does_not_drive_matching(self, tmp_path) -> None:
        """Mealie merges, so the response is not index-aligned with the request."""
        plan = plan_sync([], [keep_item("kA", "Milk"), keep_item("kB", "Bread")], [])
        assert len(plan.mealie_creates) == 2
        # Returned in the opposite order to the request.
        mealie = FakeMealie(
            [_merged_item("mB", "Bread", "kB"), _merged_item("mA", "Milk", "kA")]
        )
        keep, store = FakeKeep(), LinkStore(tmp_path / "s.json")
        links: dict[str, Any] = {}

        _syncer(mealie, keep, store)._apply_mealie_creates(plan, [None, None], links)

        assert links["mA"].keep_id == "kA"
        assert links["mB"].keep_id == "kB"

    def test_create_payload_carries_the_keep_id(self, tmp_path) -> None:
        """Extras are the only reliable way to match the response back."""
        plan = plan_sync([], [keep_item("k1", "Milk")], [])
        mealie = FakeMealie([_merged_item("m1", "Milk", "k1")])
        _syncer(mealie, FakeKeep(), LinkStore(tmp_path / "s.json"))._apply_mealie_creates(
            plan, [None], {}
        )
        assert mealie.create_calls[0][0]["extras"] == {KEEP_ID_EXTRA: "k1"}


class TestEngineSkipsAbsorbed:
    def test_absorbed_item_is_not_recreated(self) -> None:
        plan = plan_sync([], [keep_item("k1", "Milk")], [], absorbed={"k1": "Milk"})
        assert plan.mealie_creates == []

    def test_editing_the_text_makes_it_eligible_again(self) -> None:
        plan = plan_sync([], [keep_item("k1", "Oat milk")], [], absorbed={"k1": "Milk"})
        assert len(plan.mealie_creates) == 1

    def test_absorbed_does_not_affect_other_items(self) -> None:
        plan = plan_sync(
            [], [keep_item("k1", "Milk"), keep_item("k2", "Bread")], [], absorbed={"k1": "Milk"}
        )
        assert [c.keep_id for c in plan.mealie_creates] == ["k2"]

    def test_absorbed_item_still_links_if_mealie_extras_point_at_it(self) -> None:
        """Skipping creation must not break the normal extras-based relink path."""
        item = mealie_item("m1", note="Milk", keep_id="k1")
        plan = plan_sync([item], [keep_item("k1", "Milk")], [], absorbed={"k1": "Milk"})
        assert len(plan.surviving_links) == 1
        assert plan.surviving_links[0].keep_id == "k1"


class TestAbsorbedPersistence:
    def test_round_trips(self, tmp_path) -> None:
        path = tmp_path / "s.json"
        store = LinkStore(path)
        store.reset_if_lists_changed("m-list", "k-list")
        store.record_absorbed({"k1": "Milk"})
        store.save()

        reloaded = LinkStore(path)
        reloaded.load()
        assert reloaded.absorbed == {"k1": "Milk"}

    def test_state_written_before_this_feature_still_loads(self, tmp_path) -> None:
        """No STATE_VERSION bump, so upgrading must not discard existing links."""
        import json

        from mealie_gkeep_sync.state import STATE_VERSION

        path = tmp_path / "s.json"
        path.write_text(
            json.dumps(
                {
                    "version": STATE_VERSION,
                    "mealie_list_id": "m",
                    "keep_list_id": "k",
                    "links": [{"mealie_id": "m1", "keep_id": "k1", "text": "Milk"}],
                }
            ),
            encoding="utf-8",
        )
        store = LinkStore(path)
        store.load()
        assert [lnk.mealie_id for lnk in store.links] == ["m1"], "links must survive upgrade"
        assert store.absorbed == {}

    def test_prune_forgets_deleted_keep_items(self, tmp_path) -> None:
        store = LinkStore(tmp_path / "s.json")
        store.record_absorbed({"k1": "Milk", "k2": "Bread"})
        store.prune_absorbed({"k1"})
        assert store.absorbed == {"k1": "Milk"}

    def test_changing_lists_clears_absorbed(self, tmp_path) -> None:
        store = LinkStore(tmp_path / "s.json")
        store.reset_if_lists_changed("m1", "k1")
        store.record_absorbed({"k1": "Milk"})
        store.reset_if_lists_changed("m1", "k2")
        assert store.absorbed == {}


@pytest.mark.parametrize("payload_key", ["updatedItems", "updated_items"])
def test_client_returns_created_and_updated(payload_key: str) -> None:
    """Both casings, because absorbed items only appear under updatedItems."""
    import httpx
    import respx

    from mealie_gkeep_sync.mealie import MealieClient

    base = "https://mealie.example.com"
    client = MealieClient(base, "token")
    with respx.mock:
        respx.get(f"{base}/api/households/shopping/lists").mock(
            return_value=httpx.Response(200, json={"items": [], "total_pages": 1})
        )
        respx.post(f"{base}/api/households/shopping/items/create-bulk").mock(
            return_value=httpx.Response(
                200,
                json={
                    "createdItems": [{"id": "m1", "shoppingListId": "L", "note": "Bread"}],
                    payload_key: [{"id": "m2", "shoppingListId": "L", "note": "Milk"}],
                },
            )
        )
        items = client.create_items([{"note": "x"}])
    assert {i.id for i in items} == {"m1", "m2"}


class TestProductionOscillation:
    """The production failure, and the two layers that now prevent it.

    Mealie item 449b6734… alternated between Keep items cbx.al1beu21ezw8 and
    cbx.kifakouvaztn every cycle: two Keep lines with the same text collapsed onto one
    Mealie item, so whichever was unlinked got re-created, Mealie re-merged it, the extras
    flipped, and the quantity climbed.

    Duplicate combining now resolves that *before* anything is created. The absorbed
    mechanism remains the backstop for merges combining cannot predict: Mealie merges on
    food ID and unit compatibility, so two Keep lines with genuinely different text can
    still land on the same item.
    """

    MEALIE_ID = "449b6734-f6a3-46d5-bd08-792fb3b062b9"
    KEEP_A = "cbx.al1beu21ezw8"
    KEEP_B = "cbx.kifakouvaztn"

    def test_identical_duplicates_never_reach_mealie(self) -> None:
        """First layer: the exact production case, now handled by combining."""
        mealie = [mealie_item(self.MEALIE_ID, note="Bananas", keep_id=self.KEEP_A)]
        keep = [keep_item(self.KEEP_A, "Bananas"), keep_item(self.KEEP_B, "Bananas")]
        plan = plan_sync(mealie, keep, [link(self.MEALIE_ID, self.KEEP_A, "Bananas")])

        assert plan.mealie_creates == [], "combining should pre-empt the create entirely"
        assert [d.keep_id for d in plan.keep_deletes] == [self.KEEP_B]

    def test_differing_text_that_mealie_still_merges_is_absorbed(self, tmp_path) -> None:
        """Second layer: combining cannot see this, because the texts differ."""
        # "chicken" and "chicken breast" are distinct lines, but Mealie's parser maps
        # both onto the same food and merges them.
        keep = [keep_item("kA", "chicken"), keep_item("kB", "chicken breast")]
        plan = plan_sync([], keep, [])
        assert len(plan.mealie_creates) == 2, "combining leaves both alone"

        # Mealie merges them: one item returns, carrying whichever extras won.
        merged = mealie_item("m1", note="chicken breast", keep_id="kA")
        fake = FakeMealie([merged])
        links: dict[str, Any] = {}

        absorbed = _syncer(fake, FakeKeep(), LinkStore(tmp_path / "s.json"))._apply_mealie_creates(
            plan, [None, None], links
        )

        assert links["m1"].keep_id == "kA"
        assert absorbed == {"kB": "chicken breast"}, "the unmatched one must be absorbed"

    def test_absorbed_item_is_not_retried(self) -> None:
        keep = [keep_item("kA", "chicken"), keep_item("kB", "chicken breast")]
        plan = plan_sync([], keep, [], absorbed={"kB": "chicken breast"})
        assert [c.keep_id for c in plan.mealie_creates] == ["kA"]

    def test_no_repeated_creates_across_many_cycles(self, tmp_path) -> None:
        """Ten cycles, one create total - the inflation loop is closed either way."""
        mealie = [mealie_item(self.MEALIE_ID, note="Bananas", keep_id=self.KEEP_A)]
        keep = [keep_item(self.KEEP_A, "Bananas"), keep_item(self.KEEP_B, "Bananas")]
        store = LinkStore(tmp_path / "s.json")
        fake = FakeMealie([mealie_item(self.MEALIE_ID, note="Bananas", keep_id=self.KEEP_B)])
        syncer = _syncer(fake, FakeKeep(), store)

        for _ in range(10):
            plan = plan_sync(mealie, keep, store.links, absorbed=store.absorbed)
            links = {lnk.mealie_id: lnk for lnk in plan.surviving_links}
            if plan.mealie_creates:
                store.record_absorbed(syncer._apply_mealie_creates(plan, [None], links))
            store.replace_all(list(links.values()))

        assert fake.create_calls == [], f"expected no creates, got {len(fake.create_calls)}"


class TestRelinkDoesNotChurn:
    """Production issued 24 no-op Mealie updates per cycle from the relink path."""

    def test_agreeing_relink_writes_nothing(self) -> None:
        item = mealie_item("m1", note="Milk", keep_id="k1")
        plan = plan_sync([item], [keep_item("k1", "Milk")], [])
        assert len(plan.surviving_links) == 1
        assert plan.mealie_updates == [], "extras already hold this Keep ID"
        assert plan.is_empty

    def test_stale_extras_are_still_corrected(self) -> None:
        """The guard must not disable genuine extras repair."""
        item = mealie_item("m1", note="Milk")  # no extras at all
        plan = plan_sync([item], [keep_item("k1", "Milk")], [link("m1", "k1", "Milk")])
        assert [u.keep_id for u in plan.mealie_updates] == ["k1"]

    def test_diverging_relink_still_updates(self) -> None:
        item = mealie_item("m1", note="Milk", keep_id="k1", updated_offset=1)
        plan = plan_sync([item], [keep_item("k1", "Oat milk", updated_offset=50)], [])
        assert [u.text for u in plan.mealie_updates] == ["Oat milk"]
