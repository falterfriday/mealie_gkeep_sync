"""Mealie client behaviour that is easy to get wrong: prefix detection, payload
construction, and degrading gracefully when the parser is unavailable."""

from __future__ import annotations

import httpx
import pytest
import respx
from conftest import mealie_item

from mealie_gkeep_sync.errors import AuthError, ConfigError
from mealie_gkeep_sync.mealie import MealieClient, build_update_payload
from mealie_gkeep_sync.models import KEEP_ID_EXTRA

BASE = "https://mealie.example.com"


@pytest.fixture
def client() -> MealieClient:
    return MealieClient(BASE, "token")


@respx.mock
def test_detects_households_prefix(client: MealieClient) -> None:
    respx.get(f"{BASE}/api/households/shopping/lists").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    assert client.prefix == "households"


@respx.mock
def test_falls_back_to_groups_prefix(client: MealieClient) -> None:
    """Mealie before v1.2 served shopping lists under /groups."""
    respx.get(f"{BASE}/api/households/shopping/lists").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE}/api/groups/shopping/lists").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    assert client.prefix == "groups"


@respx.mock
def test_missing_shopping_api_is_a_config_error(client: MealieClient) -> None:
    respx.get(f"{BASE}/api/households/shopping/lists").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE}/api/groups/shopping/lists").mock(return_value=httpx.Response(404))
    with pytest.raises(ConfigError, match="households or /api/groups"):
        _ = client.prefix


@respx.mock
def test_bad_token_raises_auth_error(client: MealieClient) -> None:
    respx.get(f"{BASE}/api/households/shopping/lists").mock(return_value=httpx.Response(401))
    with pytest.raises(AuthError, match="MEALIE_API_TOKEN"):
        _ = client.prefix


@respx.mock
def test_resolve_list_by_name_is_case_insensitive(client: MealieClient) -> None:
    respx.get(f"{BASE}/api/households/shopping/lists").mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": "abc", "name": "Groceries"}], "total_pages": 1}
        )
    )
    assert client.resolve_list(list_id=None, list_name="groceries").id == "abc"


@respx.mock
def test_resolve_list_reports_available_names(client: MealieClient) -> None:
    respx.get(f"{BASE}/api/households/shopping/lists").mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": "abc", "name": "Groceries"}], "total_pages": 1}
        )
    )
    with pytest.raises(ConfigError, match="Groceries"):
        client.resolve_list(list_id=None, list_name="Nope")


@respx.mock
def test_fetch_items_keeps_raw_payload(client: MealieClient) -> None:
    raw = {
        "id": "m1",
        "shoppingListId": "list-1",
        "checked": False,
        "note": "Milk",
        "isFood": False,
        "extras": {KEEP_ID_EXTRA: "k1"},
        "recipeReferences": [{"recipeId": "r1"}],
    }
    respx.get(f"{BASE}/api/households/shopping/lists").mock(
        return_value=httpx.Response(200, json={"items": [], "total_pages": 1})
    )
    respx.get(f"{BASE}/api/households/shopping/lists/list-1").mock(
        return_value=httpx.Response(200, json={"id": "list-1", "listItems": [raw]})
    )
    items = client.fetch_items("list-1")
    assert items[0].linked_keep_id == "k1"
    assert items[0].raw["recipeReferences"] == [{"recipeId": "r1"}]


@respx.mock
def test_parser_outage_degrades_to_plain_notes(client: MealieClient) -> None:
    """A parser 500 must not stall the sync; items just land unstructured."""
    respx.post(f"{BASE}/api/parser/ingredients").mock(return_value=httpx.Response(500))
    results = client.parse_ingredients(["2 lb chicken"])
    assert len(results) == 1
    assert results[0].confidence == 0.0
    assert results[0].note == "2 lb chicken"


@respx.mock
def test_parse_ingredients_maps_confidence_and_food(client: MealieClient) -> None:
    respx.post(f"{BASE}/api/parser/ingredients").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "confidence": {"average": 0.92},
                    "ingredient": {
                        "quantity": 2,
                        "note": "",
                        "food": {"id": "f1", "name": "Chicken breast"},
                        "unit": {"id": "u1", "name": "pound"},
                    },
                }
            ],
        )
    )
    parsed = client.parse_ingredients(["2 lb chicken breast"])[0]
    assert parsed.confidence == pytest.approx(0.92)
    assert parsed.food is not None and parsed.food.id == "f1"
    assert parsed.quantity == 2


class TestBuildUpdatePayload:
    def test_preserves_unmodelled_fields(self) -> None:
        """Recipe references and future Mealie fields must survive a round trip."""
        raw = {
            "id": "m1",
            "shoppingListId": "list-1",
            "checked": False,
            "note": "Milk",
            "recipeReferences": [{"recipeId": "r1"}],
            "someFutureField": 42,
        }
        item = mealie_item("m1", note="Milk", raw=raw)
        payload = build_update_payload(item, checked=True)
        assert payload["recipeReferences"] == [{"recipeId": "r1"}]
        assert payload["someFutureField"] == 42
        assert payload["checked"] is True

    def test_merges_extras_without_dropping_other_keys(self) -> None:
        raw = {
            "id": "m1",
            "shoppingListId": "list-1",
            "extras": {"other_integration": "keep-me"},
        }
        item = mealie_item("m1", raw=raw)
        payload = build_update_payload(item, keep_id="k1")
        assert payload["extras"] == {"other_integration": "keep-me", KEEP_ID_EXTRA: "k1"}

    def test_overrides_apply(self) -> None:
        item = mealie_item("m1", note="old")
        payload = build_update_payload(item, overrides={"note": "new", "isFood": False})
        assert payload["note"] == "new"
