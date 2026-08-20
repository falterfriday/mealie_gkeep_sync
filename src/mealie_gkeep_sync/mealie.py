"""Thin synchronous Mealie API client.

Only the endpoints this app needs, kept synchronous to match gkeepapi. Writes are built
from each item's raw response dict so fields we do not model survive the round trip.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .errors import AuthError, ConfigError, TransientError
from .models import KEEP_ID_EXTRA, MealieItem, MealieShoppingList, ParsedIngredient

log = logging.getLogger(__name__)

#: Mealie moved shopping lists under /households in v1.2. Older instances use /groups.
_PREFIXES = ("households", "groups")

_retry = retry(
    retry=retry_if_exception_type(TransientError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)


class MealieClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        verify_ssl: bool = True,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "mealie-gkeep-sync",
            },
            verify=verify_ssl,
            timeout=timeout,
        )
        self._prefix: str | None = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> MealieClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- plumbing ----------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise TransientError(f"Mealie request timed out: {method} {url}") from exc
        except httpx.TransportError as exc:
            raise TransientError(f"Cannot reach Mealie: {exc}") from exc

        if response.status_code in (401, 403):
            raise AuthError(
                "Mealie rejected the API token "
                f"(HTTP {response.status_code}). Check MEALIE_API_TOKEN."
            )
        if response.status_code >= 500:
            raise TransientError(f"Mealie returned HTTP {response.status_code} for {url}")
        return response

    @_retry
    def _json(self, method: str, url: str, **kwargs: Any) -> Any:
        response = self._request(method, url, **kwargs)
        response.raise_for_status()
        if not response.content:
            return None
        return response.json()

    @property
    def prefix(self) -> str:
        """Resolve `households` vs `groups` once, by probing."""
        if self._prefix is not None:
            return self._prefix

        for candidate in _PREFIXES:
            response = self._request(
                "GET", f"/api/{candidate}/shopping/lists", params={"perPage": 1}
            )
            if response.status_code == 404:
                continue
            response.raise_for_status()
            self._prefix = candidate
            log.info("Detected Mealie shopping list API prefix", extra={"prefix": candidate})
            return candidate

        raise ConfigError(
            "Could not find the Mealie shopping list API under /api/households or /api/groups. "
            "Check MEALIE_BASE_URL points at the Mealie root."
        )

    def _paged(self, url: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = self._json(
                "GET", url, params={**(params or {}), "page": page, "perPage": 100}
            )
            items = payload.get("items", []) if isinstance(payload, dict) else []
            results.extend(items)
            total_pages = payload.get("total_pages") or payload.get("totalPages") or 1
            if page >= int(total_pages) or not items:
                return results
            page += 1

    # -- lists -------------------------------------------------------------

    def resolve_list(self, *, list_id: str | None, list_name: str | None) -> MealieShoppingList:
        """Find the target shopping list by ID, else by exact then case-insensitive name."""
        if list_id:
            payload = self._json("GET", f"/api/{self.prefix}/shopping/lists/{list_id}")
            if not payload:
                raise ConfigError(f"Mealie shopping list {list_id} not found.")
            return MealieShoppingList.model_validate(payload)

        raw_lists = self._paged(f"/api/{self.prefix}/shopping/lists")
        lists = [MealieShoppingList.model_validate(item) for item in raw_lists]
        for candidate in lists:
            if candidate.name == list_name:
                return candidate
        for candidate in lists:
            if candidate.name.casefold() == (list_name or "").casefold():
                return candidate

        available = ", ".join(sorted(item.name for item in lists)) or "(none)"
        raise ConfigError(
            f"No Mealie shopping list named {list_name!r}. Available lists: {available}"
        )

    def fetch_items(self, list_id: str) -> list[MealieItem]:
        """Return the list's items, each carrying its raw payload for safe updates."""
        payload = self._json("GET", f"/api/{self.prefix}/shopping/lists/{list_id}")
        raw_items = (payload or {}).get("listItems") or (payload or {}).get("list_items") or []

        items: list[MealieItem] = []
        for raw in raw_items:
            item = MealieItem.model_validate(raw)
            item.raw = raw
            items.append(item)
        return items

    # -- writes ------------------------------------------------------------

    def create_items(self, payloads: list[dict[str, Any]]) -> list[MealieItem]:
        if not payloads:
            return []
        response = self._json(
            "POST", f"/api/{self.prefix}/shopping/items/create-bulk", json=payloads
        )
        created = (response or {}).get("createdItems", []) or (response or {}).get(
            "created_items", []
        )
        result: list[MealieItem] = []
        for raw in created:
            item = MealieItem.model_validate(raw)
            item.raw = raw
            result.append(item)
        return result

    def update_items(self, payloads: list[dict[str, Any]]) -> list[MealieItem]:
        if not payloads:
            return []
        response = self._json("PUT", f"/api/{self.prefix}/shopping/items", json=payloads)
        updated = (response or {}).get("updatedItems", []) or (response or {}).get(
            "updated_items", []
        )
        result: list[MealieItem] = []
        for raw in updated:
            item = MealieItem.model_validate(raw)
            item.raw = raw
            result.append(item)
        return result

    def create_food(self, name: str) -> str | None:
        """Create a food record, for when CREATE_MISSING_FOODS is enabled."""
        payload = self._json("POST", "/api/foods", json={"name": name})
        food_id = (payload or {}).get("id")
        if food_id:
            log.info("Created Mealie food", extra={"food": name, "food_id": food_id})
        return str(food_id) if food_id else None

    def delete_items(self, item_ids: list[str]) -> None:
        if not item_ids:
            return
        # FastAPI expects the ids as repeated query params, not a JSON body.
        self._json(
            "DELETE",
            f"/api/{self.prefix}/shopping/items",
            params=[("ids", item_id) for item_id in item_ids],
        )

    # -- parsing -----------------------------------------------------------

    def parse_ingredients(self, texts: list[str]) -> list[ParsedIngredient]:
        """Batch-parse free text into structured ingredients.

        Falls back to zero-confidence results if the parser is unavailable, so a parser
        outage degrades to unstructured items instead of stalling the sync.
        """
        if not texts:
            return []
        try:
            payload = self._json(
                "POST",
                "/api/parser/ingredients",
                json={"parser": "nlp", "ingredients": texts},
            )
        except (TransientError, httpx.HTTPStatusError) as exc:
            log.warning(
                "Ingredient parser unavailable; importing as plain notes",
                extra={"error": str(exc)},
            )
            return [ParsedIngredient(confidence=0.0, note=text) for text in texts]

        results: list[ParsedIngredient] = []
        for entry, original in zip(payload or [], texts, strict=False):
            ingredient = entry.get("ingredient") or {}
            confidence = (entry.get("confidence") or {}).get("average") or 0.0
            results.append(
                ParsedIngredient.model_validate(
                    {
                        "confidence": confidence,
                        "quantity": ingredient.get("quantity") or 1.0,
                        "note": ingredient.get("note") or original,
                        "food": ingredient.get("food"),
                        "unit": ingredient.get("unit"),
                    }
                )
            )
        return results


def build_update_payload(
    item: MealieItem,
    *,
    checked: bool | None = None,
    keep_id: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a bulk-update payload from an item's raw dict, changing only what we mean to.

    Starting from ``item.raw`` rather than a serialised model is deliberate: recipe
    references and any fields introduced by newer Mealie versions pass through untouched.
    """
    payload: dict[str, Any] = dict(item.raw) if item.raw else {}
    payload.setdefault("id", item.id)
    payload.setdefault("shoppingListId", item.shopping_list_id)

    if overrides:
        payload.update(overrides)
    if checked is not None:
        payload["checked"] = checked
    if keep_id is not None:
        extras = dict(payload.get("extras") or {})
        extras[KEEP_ID_EXTRA] = keep_id
        payload["extras"] = extras
    return payload
