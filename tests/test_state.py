"""Durability of the link store."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

from conftest import link

from mealie_gkeep_sync.state import STATE_VERSION, LinkStore, atomic_write_json, read_json


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "sync-state.json"
    store = LinkStore(path)
    store.reset_if_lists_changed("mealie-1", "keep-1")
    store.replace_all([link("m1", "k1", "Milk", checked=True), link("m2", "k2", "Bread")])
    store.save()

    reloaded = LinkStore(path)
    reloaded.load()
    assert reloaded.mealie_list_id == "mealie-1"
    assert reloaded.keep_list_id == "keep-1"
    assert {lnk.mealie_id for lnk in reloaded.links} == {"m1", "m2"}
    milk = next(lnk for lnk in reloaded.links if lnk.mealie_id == "m1")
    assert milk.text == "Milk"
    assert milk.checked is True


def test_missing_file_starts_empty(tmp_path: Path) -> None:
    store = LinkStore(tmp_path / "absent.json")
    store.load()
    assert store.links == []


def test_corrupt_file_is_discarded_not_fatal(tmp_path: Path) -> None:
    path = tmp_path / "sync-state.json"
    path.write_text("{ not json", encoding="utf-8")
    store = LinkStore(path)
    store.load()
    assert store.links == []


def test_version_mismatch_starts_fresh(tmp_path: Path) -> None:
    path = tmp_path / "sync-state.json"
    path.write_text(
        json.dumps({"version": STATE_VERSION + 99, "links": [{"mealie_id": "m", "keep_id": "k"}]}),
        encoding="utf-8",
    )
    store = LinkStore(path)
    store.load()
    assert store.links == []


def test_malformed_link_entries_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "sync-state.json"
    path.write_text(
        json.dumps(
            {
                "version": STATE_VERSION,
                "links": [{"mealie_id": "m1", "keep_id": "k1", "text": "Milk"}, {"bogus": True}],
            }
        ),
        encoding="utf-8",
    )
    store = LinkStore(path)
    store.load()
    assert [lnk.mealie_id for lnk in store.links] == ["m1"]


def test_changing_lists_discards_stale_links(tmp_path: Path) -> None:
    """IDs from a different list pair would mislink items, so they must go."""
    path = tmp_path / "sync-state.json"
    store = LinkStore(path)
    store.reset_if_lists_changed("mealie-1", "keep-1")
    store.replace_all([link("m1", "k1", "Milk")])
    store.save()

    reloaded = LinkStore(path)
    reloaded.load()
    discarded = reloaded.reset_if_lists_changed("mealie-2", "keep-1")
    assert discarded is True
    assert reloaded.links == []


def test_same_lists_keep_links(tmp_path: Path) -> None:
    path = tmp_path / "sync-state.json"
    store = LinkStore(path)
    store.reset_if_lists_changed("mealie-1", "keep-1")
    store.replace_all([link("m1", "k1", "Milk")])
    store.save()

    reloaded = LinkStore(path)
    reloaded.load()
    assert reloaded.reset_if_lists_changed("mealie-1", "keep-1") is False
    assert len(reloaded.links) == 1


def test_atomic_write_leaves_no_temp_files(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state.json"
    atomic_write_json(path, {"hello": "world"})
    assert read_json(path) == {"hello": "world"}
    assert [p.name for p in path.parent.iterdir()] == ["state.json"]


def test_atomic_write_does_not_clobber_on_failure(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    atomic_write_json(path, {"good": 1})

    class Explosive:
        def __str__(self) -> str:
            raise ValueError("boom")

    with contextlib.suppress(TypeError, ValueError):
        atomic_write_json(path, {"bad": Explosive()})

    # The previous good content survives, and no temp file is left behind.
    assert read_json(path) == {"good": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]
