"""Orchestration: read both sides, plan, apply, persist.

One design point worth stating up front: **Mealie is the canonical renderer**. When text
authored in Keep is imported and Mealie parses it into a structured item, the way Mealie
renders that item back may differ from what the user typed ("2lb chicken" ->
"2 lb Chicken breast"). We push that canonical text back to Keep *within the same cycle*
and store it as the merge base, so both sides converge immediately. Storing the raw Keep
text as the base instead would make the next cycle see a phantom Mealie-side edit, and
storing it only on the Mealie side would ping-pong forever.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .config import Settings
from .engine import Plan, plan_sync
from .errors import ConfigError
from .keep_client import KeepClient
from .mealie import MealieClient, build_update_payload
from .models import KEEP_ID_EXTRA, Link, MealieItem, ParsedIngredient
from .render import build_create_payload, item_fields, normalise_text, render_item
from .state import LinkStore

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SyncOutcome:
    ok: bool
    at: datetime = field(default_factory=lambda: datetime.now(UTC))
    summary: dict[str, int] = field(default_factory=dict)
    error: str | None = None


class Syncer:
    def __init__(
        self,
        settings: Settings,
        mealie: MealieClient,
        keep: KeepClient,
        links: LinkStore,
    ) -> None:
        self._settings = settings
        self._mealie = mealie
        self._keep = keep
        self._links = links
        self._mealie_list_id: str | None = None

    # -- setup -------------------------------------------------------------

    def connect(self) -> None:
        """Resolve both lists and load prior state. Safe to call again to reconnect."""
        mealie_list = self._mealie.resolve_list(
            list_id=self._settings.mealie_list_id,
            list_name=self._settings.mealie_list_name,
        )
        self._mealie_list_id = mealie_list.id
        keep_list_id = self._keep.resolve_list()

        self._links.load()
        self._links.reset_if_lists_changed(mealie_list.id, keep_list_id)

        log.info(
            "Sync target resolved",
            extra={
                "mealie_list": mealie_list.name,
                "mealie_list_id": mealie_list.id,
                "keep_list": self._settings.keep_list_name,
                "keep_list_id": keep_list_id,
            },
        )

    # -- main cycle --------------------------------------------------------

    def run_once(self) -> SyncOutcome:
        if self._mealie_list_id is None:
            raise ConfigError("Syncer.connect() must run before run_once().")

        self._keep.refresh()
        mealie_items = self._mealie.fetch_items(self._mealie_list_id)
        keep_items = self._keep.fetch_items()

        plan = plan_sync(
            mealie_items,
            keep_items,
            self._links.links,
            strategy=self._settings.conflict_strategy,
        )

        if self._settings.dry_run:
            self._log_plan(plan)
            return SyncOutcome(ok=True, summary=plan.summary())

        if plan.is_empty:
            # Bases may still have moved (both sides edited to the same value), so the
            # link file is rewritten even when no remote call is needed.
            self._links.replace_all(plan.surviving_links)
            self._links.save()
            return SyncOutcome(ok=True, summary=plan.summary())

        log.info("Applying sync plan", extra=plan.summary())
        new_links = self._apply(plan, mealie_items)
        self._links.replace_all(new_links)
        self._links.save()
        return SyncOutcome(ok=True, summary=plan.summary())

    # -- apply -------------------------------------------------------------

    def _apply(self, plan: Plan, mealie_items: list[MealieItem]) -> list[Link]:
        links_by_mealie: dict[str, Link] = {
            link.mealie_id: link for link in plan.surviving_links
        }
        mealie_by_id = {item.id: item for item in mealie_items}
        # Keyed by Mealie item ID so an item never appears twice in one bulk PUT.
        update_payloads: dict[str, dict[str, Any]] = {}

        # 1. Keep-side deletes and updates. These mutate the local node tree only; the
        #    single flush() at the end pushes everything in one request.
        for keep_delete in plan.keep_deletes:
            self._keep.delete_item(keep_delete.keep_id)
        for keep_update in plan.keep_updates:
            self._keep.update_item(
                keep_update.keep_id, text=keep_update.text, checked=keep_update.checked
            )

        # 2. Keep creates. gkeepapi assigns node IDs locally, so the ID is available
        #    before the flush and can be stamped into Mealie's extras in this same cycle.
        for keep_create in plan.keep_creates:
            keep_id = self._keep.add_item(keep_create.text, keep_create.checked)
            links_by_mealie[keep_create.mealie_id] = Link(
                mealie_id=keep_create.mealie_id,
                keep_id=keep_id,
                text=keep_create.text,
                checked=keep_create.checked,
            )
            source = mealie_by_id.get(keep_create.mealie_id)
            if source is not None:
                update_payloads[keep_create.mealie_id] = build_update_payload(
                    source, keep_id=keep_id
                )

        # 3. Mealie deletes.
        self._mealie.delete_items([deletion.mealie_id for deletion in plan.mealie_deletes])
        for mealie_delete in plan.mealie_deletes:
            links_by_mealie.pop(mealie_delete.mealie_id, None)

        # 4. Parse every piece of Keep-authored text in one batch.
        texts_to_parse = [creation.text for creation in plan.mealie_creates] + [
            update.text for update in plan.mealie_updates if update.text is not None
        ]
        parsed = self._parse(texts_to_parse)
        create_parses = parsed[: len(plan.mealie_creates)]
        update_parses = parsed[len(plan.mealie_creates) :]

        # 5. Mealie creates, followed by convergence of the canonical text back to Keep.
        self._apply_mealie_creates(plan, create_parses, links_by_mealie)

        # 6. Mealie updates.
        self._apply_mealie_updates(plan, update_parses, links_by_mealie, update_payloads)

        # 7. One flush for every Keep mutation queued above.
        self._keep.flush()
        return list(links_by_mealie.values())

    def _apply_mealie_creates(
        self,
        plan: Plan,
        parses: list[ParsedIngredient | None],
        links_by_mealie: dict[str, Link],
    ) -> None:
        if not plan.mealie_creates:
            return

        payloads: list[dict[str, Any]] = []
        for action, parse in zip(plan.mealie_creates, parses, strict=False):
            payloads.append(
                build_create_payload(
                    action.text,
                    self._mealie_list_id or "",
                    checked=action.checked,
                    keep_id=action.keep_id,
                    parsed=parse,
                    min_confidence=self._settings.parser_min_confidence,
                    extras_key=KEEP_ID_EXTRA,
                )
            )

        created = self._mealie.create_items(payloads)
        if len(created) != len(plan.mealie_creates):
            log.warning(
                "Mealie created a different number of items than requested",
                extra={"requested": len(plan.mealie_creates), "created": len(created)},
            )

        for index, item in enumerate(created):
            # Prefer the round-tripped extras; fall back to positional order.
            keep_id = item.linked_keep_id
            if not keep_id and index < len(plan.mealie_creates):
                keep_id = plan.mealie_creates[index].keep_id
            if not keep_id:
                log.warning("Created Mealie item has no Keep link", extra={"mealie_id": item.id})
                continue

            canonical = normalise_text(render_item(item))
            original = plan.mealie_creates[index].text if index < len(plan.mealie_creates) else ""
            if canonical and canonical != original:
                self._keep.update_item(keep_id, text=canonical)
            text = canonical or original
            links_by_mealie[item.id] = Link(
                mealie_id=item.id, keep_id=keep_id, text=text, checked=item.checked
            )

    def _apply_mealie_updates(
        self,
        plan: Plan,
        parses: list[ParsedIngredient | None],
        links_by_mealie: dict[str, Link],
        update_payloads: dict[str, dict[str, Any]],
    ) -> None:
        parse_iter = iter(parses)
        text_changed: dict[str, str] = {}

        for action in plan.mealie_updates:
            overrides: dict[str, Any] | None = None
            if action.text is not None:
                parse = next(parse_iter, None)
                overrides = item_fields(
                    action.text,
                    parsed=parse,
                    min_confidence=self._settings.parser_min_confidence,
                    food_id=self._food_id_for(parse),
                )
                text_changed[action.item.id] = action.text

            base = update_payloads.get(action.item.id)
            payload = build_update_payload(
                action.item,
                checked=action.checked,
                keep_id=action.keep_id,
                overrides=overrides,
            )
            if base:
                payload = {**base, **payload}
            update_payloads[action.item.id] = payload

        updated = self._mealie.update_items(list(update_payloads.values()))

        # Converge Keep to Mealie's rendering for anything whose text we just rewrote.
        for item in updated:
            link = links_by_mealie.get(item.id)
            if link is None:
                continue
            if item.id in text_changed:
                canonical = normalise_text(render_item(item))
                if canonical and canonical != text_changed[item.id]:
                    self._keep.update_item(link.keep_id, text=canonical)
                if canonical:
                    link.text = canonical

    # -- helpers -----------------------------------------------------------

    def _parse(self, texts: list[str]) -> list[ParsedIngredient | None]:
        if not texts or not self._settings.parse_ingredients:
            return [None] * len(texts)
        results = self._mealie.parse_ingredients(texts)
        return [*results, *([None] * (len(texts) - len(results)))]

    def _food_id_for(self, parse: ParsedIngredient | None) -> str | None:
        """Optionally create a food record for an unrecognised ingredient."""
        if not self._settings.create_missing_foods:
            return None
        if not parse or not parse.food or parse.food.id or not parse.food.name:
            return None
        if parse.confidence < self._settings.parser_min_confidence:
            return None
        return self._mealie.create_food(parse.food.name)

    def _log_plan(self, plan: Plan) -> None:
        log.info("DRY RUN - no changes will be written", extra=plan.summary())
        for keep_create in plan.keep_creates:
            log.info("would create in Keep", extra={"text": keep_create.text})
        for keep_update in plan.keep_updates:
            log.info(
                "would update in Keep",
                extra={
                    "keep_id": keep_update.keep_id,
                    "text": keep_update.text,
                    "checked": keep_update.checked,
                },
            )
        for keep_delete in plan.keep_deletes:
            log.info("would delete from Keep", extra={"keep_id": keep_delete.keep_id})
        for mealie_create in plan.mealie_creates:
            log.info("would create in Mealie", extra={"text": mealie_create.text})
        for mealie_update in plan.mealie_updates:
            log.info(
                "would update in Mealie",
                extra={
                    "mealie_id": mealie_update.item.id,
                    "text": mealie_update.text,
                    "checked": mealie_update.checked,
                },
            )
        for mealie_delete in plan.mealie_deletes:
            log.info("would delete from Mealie", extra={"mealie_id": mealie_delete.mealie_id})
