"""Three-way reconciliation between Mealie and Google Keep.

The merge base is the ``Link`` snapshot written at the end of the previous successful
sync: the text and checked value both sides last agreed on. Comparing each side against
that base is what distinguishes "changed here" from "changed there" from "deleted", which
a two-way diff cannot do.

``plan_sync`` is deliberately pure - it takes plain data and returns a plan. All I/O lives
in :mod:`mealie_gkeep_sync.sync`, so the full matrix below is testable without a network.

Per pair, with base B, Mealie value M and Keep value K:

===============  ==============  ===================================================
Mealie           Keep            Outcome
===============  ==============  ===================================================
M == B           K == B          nothing to do
M != B           K == B          push Mealie -> Keep
M == B           K != B          push Keep -> Mealie
M != B, K != B   M == K          both landed on the same value; just move the base
M != B, K != B   M != K          conflict, resolved by :class:`ConflictStrategy`
present          missing         deleted in Keep -> delete in Mealie
missing          present         deleted in Mealie -> delete in Keep
missing          missing         both gone; drop the link
===============  ==============  ===================================================

Text and checked state are resolved independently, so renaming an item on one side while
ticking it off on the other is not a conflict.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from .config import ConflictStrategy
from .models import KeepItem, Link, MealieItem, Side
from .render import normalise_text, render_item

log = logging.getLogger(__name__)


# --- actions --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreateKeep:
    mealie_id: str
    text: str
    checked: bool


@dataclass(frozen=True, slots=True)
class UpdateKeep:
    keep_id: str
    text: str | None = None
    checked: bool | None = None


@dataclass(frozen=True, slots=True)
class DeleteKeep:
    keep_id: str


@dataclass(frozen=True, slots=True)
class CreateMealie:
    keep_id: str
    text: str
    checked: bool


@dataclass(frozen=True, slots=True)
class UpdateMealie:
    item: MealieItem
    text: str | None = None
    checked: bool | None = None
    keep_id: str | None = None
    """Set when the item's ``extras`` must be (re)stamped with its Keep ID."""


@dataclass(frozen=True, slots=True)
class DeleteMealie:
    mealie_id: str


@dataclass(slots=True)
class Plan:
    keep_creates: list[CreateKeep] = field(default_factory=list)
    keep_updates: list[UpdateKeep] = field(default_factory=list)
    keep_deletes: list[DeleteKeep] = field(default_factory=list)
    mealie_creates: list[CreateMealie] = field(default_factory=list)
    mealie_updates: list[UpdateMealie] = field(default_factory=list)
    mealie_deletes: list[DeleteMealie] = field(default_factory=list)
    surviving_links: list[Link] = field(default_factory=list)
    conflicts: int = 0

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.keep_creates,
                self.keep_updates,
                self.keep_deletes,
                self.mealie_creates,
                self.mealie_updates,
                self.mealie_deletes,
            )
        )

    def summary(self) -> dict[str, int]:
        return {
            "keep_creates": len(self.keep_creates),
            "keep_updates": len(self.keep_updates),
            "keep_deletes": len(self.keep_deletes),
            "mealie_creates": len(self.mealie_creates),
            "mealie_updates": len(self.mealie_updates),
            "mealie_deletes": len(self.mealie_deletes),
            "conflicts": self.conflicts,
        }


# --- conflict resolution --------------------------------------------------


def _winner(
    strategy: ConflictStrategy,
    mealie_at: datetime | None,
    keep_at: datetime | None,
) -> Side:
    """Decide which side wins a genuine conflict."""
    if strategy is ConflictStrategy.MEALIE:
        return "mealie"
    if strategy is ConflictStrategy.KEEP:
        return "keep"

    # NEWEST: fall back to Mealie when either timestamp is unavailable, since a missing
    # timestamp would otherwise silently hand the win to whichever side reported one.
    if mealie_at is None or keep_at is None:
        return "mealie"
    return "mealie" if mealie_at >= keep_at else "keep"


# --- planning -------------------------------------------------------------


def plan_sync(
    mealie_items: list[MealieItem],
    keep_items: list[KeepItem],
    links: list[Link],
    *,
    strategy: ConflictStrategy = ConflictStrategy.NEWEST,
) -> Plan:
    """Compute the actions that bring both sides into agreement."""
    plan = Plan()

    by_mealie_id = {item.id: item for item in mealie_items}
    by_keep_id = {item.id: item for item in keep_items}

    linked_mealie: set[str] = set()
    linked_keep: set[str] = set()

    for link in links:
        mealie_item = by_mealie_id.get(link.mealie_id)
        keep_item = by_keep_id.get(link.keep_id)

        if mealie_item is None and keep_item is None:
            # Removed on both sides between syncs; the link is just garbage now.
            continue

        if mealie_item is None:
            linked_keep.add(link.keep_id)
            plan.keep_deletes.append(DeleteKeep(link.keep_id))
            continue

        if keep_item is None:
            linked_mealie.add(link.mealie_id)
            plan.mealie_deletes.append(DeleteMealie(link.mealie_id))
            continue

        linked_mealie.add(link.mealie_id)
        linked_keep.add(link.keep_id)
        _reconcile_pair(plan, link, mealie_item, keep_item, strategy)

    _plan_unlinked(plan, mealie_items, keep_items, linked_mealie, linked_keep, strategy)
    return plan


def _reconcile_pair(
    plan: Plan,
    link: Link,
    mealie_item: MealieItem,
    keep_item: KeepItem,
    strategy: ConflictStrategy,
) -> None:
    mealie_text = normalise_text(render_item(mealie_item))
    keep_text = normalise_text(keep_item.text)
    base_text = normalise_text(link.text)

    text, text_target = _resolve_field(
        base_text, mealie_text, keep_text, strategy, mealie_item.updated_at, keep_item.updated_at
    )
    checked, checked_target = _resolve_field(
        link.checked,
        mealie_item.checked,
        keep_item.checked,
        strategy,
        mealie_item.updated_at,
        keep_item.updated_at,
    )

    if text_target == "conflict" or checked_target == "conflict":
        plan.conflicts += 1

    push_to_keep_text = text if text_target in ("keep", "conflict") and text != keep_text else None
    push_to_keep_checked = (
        checked if checked_target in ("keep", "conflict") and checked != keep_item.checked else None
    )
    if push_to_keep_text is not None or push_to_keep_checked is not None:
        plan.keep_updates.append(
            UpdateKeep(link.keep_id, text=push_to_keep_text, checked=push_to_keep_checked)
        )

    push_to_mealie_text = (
        text if text_target in ("mealie", "conflict") and text != mealie_text else None
    )
    push_to_mealie_checked = (
        checked
        if checked_target in ("mealie", "conflict") and checked != mealie_item.checked
        else None
    )
    # Re-stamp extras if the Keep ID is missing or stale, so links survive state loss.
    keep_id_fix = link.keep_id if mealie_item.linked_keep_id != link.keep_id else None

    if push_to_mealie_text is not None or push_to_mealie_checked is not None or keep_id_fix:
        plan.mealie_updates.append(
            UpdateMealie(
                item=mealie_item,
                text=push_to_mealie_text,
                checked=push_to_mealie_checked,
                keep_id=keep_id_fix,
            )
        )

    plan.surviving_links.append(
        Link(mealie_id=mealie_item.id, keep_id=keep_item.id, text=text, checked=checked)
    )


def _resolve_field[T](
    base: T,
    mealie_value: T,
    keep_value: T,
    strategy: ConflictStrategy,
    mealie_at: datetime | None,
    keep_at: datetime | None,
) -> tuple[T, str]:
    """Resolve one field, returning the agreed value and where it needs pushing.

    The second element is ``"none"``, ``"keep"`` (push to Keep), ``"mealie"`` (push to
    Mealie), or ``"conflict"`` (both changed; the loser gets overwritten).
    """
    mealie_changed = mealie_value != base
    keep_changed = keep_value != base

    if not mealie_changed and not keep_changed:
        return base, "none"
    if mealie_changed and not keep_changed:
        return mealie_value, "keep"
    if keep_changed and not mealie_changed:
        return keep_value, "mealie"
    if mealie_value == keep_value:
        # Both sides changed to the same thing; only the base is out of date.
        return mealie_value, "none"

    side = _winner(strategy, mealie_at, keep_at)
    return (mealie_value if side == "mealie" else keep_value), "conflict"


def _plan_unlinked(
    plan: Plan,
    mealie_items: list[MealieItem],
    keep_items: list[KeepItem],
    linked_mealie: set[str],
    linked_keep: set[str],
    strategy: ConflictStrategy,
) -> None:
    """Handle items with no link: new on one side, or a link to be rebuilt from extras."""
    keep_by_id = {item.id: item for item in keep_items}
    claimed_keep: set[str] = set()

    for mealie_item in mealie_items:
        if mealie_item.id in linked_mealie:
            continue

        stamped = mealie_item.linked_keep_id
        candidate = keep_by_id.get(stamped) if stamped else None
        if (
            candidate is not None
            and stamped not in linked_keep
            and stamped not in claimed_keep
        ):
            # State was lost but the Mealie item still carries its Keep ID. Rebuild the
            # link rather than duplicating the item on both sides.
            claimed_keep.add(str(stamped))
            log.info(
                "Rebuilding link from Mealie extras",
                extra={"mealie_id": mealie_item.id, "keep_id": stamped},
            )
            _reconcile_without_base(plan, mealie_item, candidate, strategy)
            continue

        text = normalise_text(render_item(mealie_item))
        if not text:
            # An empty Mealie item has nothing meaningful to show in Keep.
            continue
        plan.keep_creates.append(
            CreateKeep(mealie_id=mealie_item.id, text=text, checked=mealie_item.checked)
        )

    for keep_item in keep_items:
        if keep_item.id in linked_keep or keep_item.id in claimed_keep:
            continue
        text = normalise_text(keep_item.text)
        if not text:
            continue
        plan.mealie_creates.append(
            CreateMealie(keep_id=keep_item.id, text=text, checked=keep_item.checked)
        )


def _reconcile_without_base(
    plan: Plan,
    mealie_item: MealieItem,
    keep_item: KeepItem,
    strategy: ConflictStrategy,
) -> None:
    """Re-link a pair with no merge base, deciding by strategy alone."""
    mealie_text = normalise_text(render_item(mealie_item))
    keep_text = normalise_text(keep_item.text)

    if mealie_text == keep_text and mealie_item.checked == keep_item.checked:
        plan.surviving_links.append(
            Link(mealie_item.id, keep_item.id, text=mealie_text, checked=mealie_item.checked)
        )
        plan.mealie_updates.append(UpdateMealie(item=mealie_item, keep_id=keep_item.id))
        return

    side = _winner(strategy, mealie_item.updated_at, keep_item.updated_at)
    text = mealie_text if side == "mealie" else keep_text
    checked = mealie_item.checked if side == "mealie" else keep_item.checked

    if side == "mealie":
        plan.keep_updates.append(UpdateKeep(keep_item.id, text=text, checked=checked))
        plan.mealie_updates.append(UpdateMealie(item=mealie_item, keep_id=keep_item.id))
    else:
        plan.mealie_updates.append(
            UpdateMealie(item=mealie_item, text=text, checked=checked, keep_id=keep_item.id)
        )

    plan.surviving_links.append(
        Link(mealie_item.id, keep_item.id, text=text, checked=checked)
    )
