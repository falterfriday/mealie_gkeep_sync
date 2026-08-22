"""Shared test factories."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mealie_gkeep_sync.models import (
    KEEP_ID_EXTRA,
    KeepItem,
    Link,
    MealieFood,
    MealieItem,
    MealieUnit,
)

LIST_ID = "11111111-1111-1111-1111-111111111111"
BASE_TIME = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def mealie_item(
    item_id: str,
    *,
    note: str | None = None,
    display: str | None = None,
    checked: bool = False,
    quantity: float = 1,
    food: MealieFood | None = None,
    unit: MealieUnit | None = None,
    keep_id: str | None = None,
    updated_offset: int = 0,
    raw: dict[str, Any] | None = None,
) -> MealieItem:
    extras = {KEEP_ID_EXTRA: keep_id} if keep_id else {}
    item = MealieItem(
        id=item_id,
        shopping_list_id=LIST_ID,
        checked=checked,
        quantity=quantity,
        note=note,
        display=display,
        # Mealie has no "is food" flag: an item is a food exactly when it has a food
        # record, so the fixture must not invent one either.
        food=food,
        unit=unit,
        # Real Mealie payloads carry both the nested object and its id, so the fixture
        # must too - code that reads food_id/unit_id would otherwise see None here.
        food_id=food.id if food else None,
        unit_id=unit.id if unit else None,
        extras=extras,
        updated_at=BASE_TIME + timedelta(minutes=updated_offset),
    )
    item.raw = raw if raw is not None else {
        "id": item_id,
        "shoppingListId": LIST_ID,
        "checked": checked,
        "note": note,
        "extras": dict(extras),
    }
    return item


def keep_item(
    item_id: str,
    text: str,
    *,
    checked: bool = False,
    updated_offset: int = 0,
) -> KeepItem:
    return KeepItem(
        id=item_id,
        text=text,
        checked=checked,
        updated_at=BASE_TIME + timedelta(minutes=updated_offset),
    )


def link(mealie_id: str, keep_id: str, text: str, checked: bool = False) -> Link:
    return Link(mealie_id=mealie_id, keep_id=keep_id, text=text, checked=checked)


@pytest.fixture
def state_dir(tmp_path: Any) -> Any:
    return tmp_path
