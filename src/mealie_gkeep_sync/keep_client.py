"""Google Keep access, wrapped around gkeepapi.

gkeepapi keeps a local node tree and pushes accumulated mutations on ``sync()``. This
wrapper hides that model behind explicit read/mutate/flush calls and normalises gkeepapi's
exceptions into this app's transient/auth split.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import gkeepapi
import requests
from gkeepapi import exception as gkeep_exc
from gkeepapi.node import List as KeepList
from gkeepapi.node import ListItem as KeepListItem

from .errors import AuthError, ConfigError, TransientError
from .models import KeepItem
from .state import atomic_write_json, read_json

log = logging.getLogger(__name__)


def _as_utc(value: datetime | None) -> datetime | None:
    """gkeepapi returns naive UTC datetimes; make them comparable with Mealie's."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class KeepClient:
    def __init__(
        self,
        email: str,
        master_token: str,
        *,
        state_path: Path,
        list_name: str,
        create_if_missing: bool = False,
    ) -> None:
        self._email = email
        self._master_token = master_token
        self._state_path = state_path
        self._list_name = list_name
        self._create_if_missing = create_if_missing
        self._keep = gkeepapi.Keep()
        self._list: KeepList | None = None
        self._connected = False

    # -- lifecycle ---------------------------------------------------------

    @contextmanager
    def _translated(self) -> Iterator[None]:
        """Map gkeepapi's failures onto this app's transient/auth split.

        gkeepapi talks to Google via ``requests``, so connection and timeout errors
        surface as ``requests`` exceptions rather than gkeepapi ones. Without this they
        reach the runner's catch-all and log a full traceback on every retry, which in a
        homelab with flaky DNS buries the errors that actually need attention.
        """
        try:
            yield
        except gkeep_exc.BrowserLoginRequiredException as exc:
            raise AuthError(
                "Google requires a browser login. The master token is no longer valid; "
                "regenerate it with tools/get_master_token.py."
            ) from exc
        except gkeep_exc.LoginException as exc:
            raise AuthError(
                f"Google rejected the master token for {self._email}: {exc}. "
                "It is invalidated by password and 2FA changes; regenerate it with "
                "tools/get_master_token.py."
            ) from exc
        except gkeep_exc.APIException as exc:
            raise self._map_api_exception(exc) from exc
        except requests.exceptions.RequestException as exc:
            raise TransientError(f"Cannot reach Google Keep: {exc}") from exc

    def connect(self) -> None:
        """Authenticate and perform the initial sync, resuming from cached state."""
        state = read_json(self._state_path)
        try:
            with self._translated():
                # authenticate(), not resume() - gkeepapi 0.17 renamed it and resume()
                # now emits a deprecation warning on every start.
                self._keep.authenticate(self._email, self._master_token, state=state, sync=True)
        except gkeep_exc.ResyncRequiredException:
            log.warning("Keep state is stale; performing a full resync")
            with self._translated():
                self._keep.sync(resync=True)

        self._connected = True
        self._persist_state()
        log.info("Connected to Google Keep", extra={"account": self._email})

    @property
    def connected(self) -> bool:
        return self._connected

    @staticmethod
    def _map_api_exception(exc: gkeep_exc.APIException) -> Exception:
        code = getattr(exc, "code", None)
        if code in (401, 403):
            return AuthError(f"Google Keep rejected our credentials (HTTP {code}).")
        return TransientError(f"Google Keep API error: {exc}")

    # -- list resolution ---------------------------------------------------

    def resolve_list(self) -> str:
        """Find (or optionally create) the target Keep list. Returns its node ID."""
        matches = [
            node
            for node in self._keep.find(func=self._is_target_list)
            if isinstance(node, KeepList)
        ]

        if len(matches) > 1:
            raise ConfigError(
                f"Found {len(matches)} Keep lists titled {self._list_name!r}. "
                "Rename the duplicates so the target is unambiguous."
            )

        if matches:
            self._list = matches[0]
            return str(self._list.id)

        if not self._create_if_missing:
            available = sorted(
                str(node.title)
                for node in self._keep.find(func=lambda n: isinstance(n, KeepList))
                if not node.trashed and not node.deleted
            )
            raise ConfigError(
                f"No Google Keep list titled {self._list_name!r}. "
                f"Available lists: {', '.join(available) or '(none)'}. "
                "Set KEEP_CREATE_LIST_IF_MISSING=true to create it."
            )

        log.info("Creating Keep list", extra={"title": self._list_name})
        self._list = self._keep.createList(self._list_name)
        self.flush()
        return str(self._list.id)

    def _is_target_list(self, node: Any) -> bool:
        return (
            isinstance(node, KeepList)
            and node.title == self._list_name
            and not node.trashed
            and not node.deleted
        )

    @property
    def _target(self) -> KeepList:
        if self._list is None:
            raise ConfigError("Keep list has not been resolved; call resolve_list() first.")
        return self._list

    # -- reads -------------------------------------------------------------

    def refresh(self) -> None:
        """Pull remote changes into the local tree."""
        try:
            with self._translated():
                self._keep.sync()
        except gkeep_exc.ResyncRequiredException:
            log.warning("Keep requested a full resync")
            with self._translated():
                self._keep.sync(resync=True)
        self._persist_state()

    def fetch_items(self) -> list[KeepItem]:
        items: list[KeepItem] = []
        for node in self._target.items:
            if node.deleted or node.trashed:
                continue
            items.append(
                KeepItem(
                    id=str(node.id),
                    text=node.text,
                    checked=bool(node.checked),
                    updated_at=_as_utc(getattr(node.timestamps, "updated", None)),
                )
            )
        return items

    # -- mutations (local until flush) -------------------------------------

    def add_item(self, text: str, checked: bool = False) -> str:
        """Append an item. The node ID is assigned locally, so it is usable immediately."""
        node = self._target.add(text, checked)
        return str(node.id)

    def update_item(
        self, item_id: str, *, text: str | None = None, checked: bool | None = None
    ) -> None:
        node = self._find_item(item_id)
        if node is None:
            log.warning("Keep item vanished before update", extra={"keep_id": item_id})
            return
        if text is not None:
            node.text = text
        if checked is not None:
            node.checked = checked

    def delete_item(self, item_id: str) -> None:
        node = self._find_item(item_id)
        if node is None:
            log.warning("Keep item vanished before delete", extra={"keep_id": item_id})
            return
        node.delete()

    def _find_item(self, item_id: str) -> KeepListItem | None:
        for node in self._target.items:
            if str(node.id) == item_id:
                return node
        return None

    def flush(self) -> None:
        """Push accumulated local mutations to Google and persist the node cache."""
        try:
            with self._translated():
                self._keep.sync()
        except gkeep_exc.ResyncRequiredException:
            log.warning("Keep requested a full resync during flush")
            with self._translated():
                self._keep.sync(resync=True)
        self._persist_state()

    def _persist_state(self) -> None:
        try:
            atomic_write_json(self._state_path, self._keep.dump())
        except OSError as exc:
            # A failed cache write costs a slower next start, nothing more.
            log.warning(
                "Could not persist Keep state cache",
                extra={"path": str(self._state_path), "error": str(exc)},
            )
