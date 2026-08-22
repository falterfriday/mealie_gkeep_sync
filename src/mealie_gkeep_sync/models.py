"""Domain models shared across the sync.

Mealie serialises with camelCase aliases, so the read models below declare aliases and
are populated by alias. Writes deliberately work on the *raw* response dict rather than a
serialised model, so fields this app does not model (recipe references, label settings,
fields added by newer Mealie versions) survive a round trip untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Side = Literal["mealie", "keep"]

#: Key under which the Keep item id is stamped into a Mealie item's ``extras``.
#: This lets links be rebuilt from Mealie alone if the state file is ever lost.
KEEP_ID_EXTRA = "gkeep_item_id"


def _camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(word.capitalize() for word in tail)


class _MealieModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_camel,
        populate_by_name=True,
        extra="ignore",
    )


class MealieLabel(_MealieModel):
    id: str
    name: str


class MealieFood(_MealieModel):
    id: str | None = None
    name: str = ""
    plural_name: str | None = None


class MealieUnit(_MealieModel):
    id: str | None = None
    name: str = ""
    plural_name: str | None = None
    abbreviation: str = ""
    plural_abbreviation: str | None = None
    use_abbreviation: bool = False


class MealieShoppingList(_MealieModel):
    id: str
    name: str


class MealieItem(_MealieModel):
    """A Mealie shopping list item as returned by the API.

    Note there is no ``is_food`` field here, because Mealie has no such field: an item is
    a structured food when ``food``/``food_id`` are set, and free text otherwise. A model
    field named ``is_food`` would quietly default to False on every real payload.
    """

    id: str
    shopping_list_id: str
    checked: bool = False
    position: int = 0
    quantity: float = 1
    note: str | None = None
    #: Mealie computes this from quantity + unit + food + note. Deliberately unused for
    #: rendering (it embeds the amounts we strip), but kept for logs and debugging.
    display: str | None = None
    food_id: str | None = None
    unit_id: str | None = None
    label_id: str | None = None
    extras: dict[str, Any] = Field(default_factory=dict)
    food: MealieFood | None = None
    unit: MealieUnit | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    #: The unmodified API payload, kept so updates can be built without dropping
    #: fields this app does not model. Populated by the client, never by validation.
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True, repr=False)

    @property
    def linked_keep_id(self) -> str | None:
        value = (self.extras or {}).get(KEEP_ID_EXTRA)
        return str(value) if value else None


class ParsedIngredient(BaseModel):
    """Result of Mealie's ingredient parser, reduced to what we act on."""

    model_config = ConfigDict(extra="ignore")

    confidence: float = 0.0
    quantity: float = 1.0
    note: str | None = None
    food: MealieFood | None = None
    unit: MealieUnit | None = None


@dataclass(slots=True, frozen=True)
class KeepItem:
    """A Google Keep list item, normalised away from gkeepapi's node objects."""

    id: str
    text: str
    checked: bool
    updated_at: datetime | None = None


@dataclass(slots=True)
class Link:
    """A synced pair, plus the values as of the last successful sync.

    The stored ``text``/``checked`` are the three-way merge base: they are what both sides
    agreed on last time, which is what lets us tell "changed here" from "changed there".
    """

    mealie_id: str
    keep_id: str
    text: str
    checked: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "mealie_id": self.mealie_id,
            "keep_id": self.keep_id,
            "text": self.text,
            "checked": self.checked,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Link:
        return cls(
            mealie_id=str(data["mealie_id"]),
            keep_id=str(data["keep_id"]),
            text=str(data.get("text", "")),
            checked=bool(data.get("checked", False)),
        )
