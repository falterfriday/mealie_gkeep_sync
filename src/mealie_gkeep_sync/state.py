"""Durable sync state: ID links, the merge base, and the Keep node cache.

Two files live side by side in ``state_dir``:

* ``sync-state.json``  - our links (mealie_id <-> keep_id) plus the last-synced text and
  checked value for each pair. That snapshot is the base of the three-way merge.
* ``keep-state.json``  - gkeepapi's own serialised node cache, which doubles as the Keep
  sync cursor so restarts do not force a full resync.

Both are written atomically (temp file + ``os.replace``) so an unclean shutdown cannot
leave a half-written file behind.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from .models import Link

log = logging.getLogger(__name__)

STATE_VERSION = 1


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def read_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(
            "Ignoring unreadable state file; it will be rebuilt",
            extra={"path": str(path), "error": str(exc)},
        )
        return None


class LinkStore:
    """The set of synced pairs and the values they last agreed on."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._links: dict[str, Link] = {}
        self.mealie_list_id: str | None = None
        self.keep_list_id: str | None = None

    def load(self) -> None:
        data = read_json(self._path)
        if not isinstance(data, dict):
            log.info("No previous sync state; starting fresh", extra={"path": str(self._path)})
            return

        version = data.get("version")
        if version != STATE_VERSION:
            log.warning(
                "Sync state version mismatch; starting fresh",
                extra={"found": version, "expected": STATE_VERSION},
            )
            return

        self.mealie_list_id = data.get("mealie_list_id")
        self.keep_list_id = data.get("keep_list_id")
        for entry in data.get("links", []):
            try:
                link = Link.from_json(entry)
            except (KeyError, TypeError, ValueError):
                log.warning("Skipping malformed link entry", extra={"entry": entry})
                continue
            self._links[link.mealie_id] = link

        log.info("Loaded sync state", extra={"links": len(self._links)})

    def save(self) -> None:
        atomic_write_json(
            self._path,
            {
                "version": STATE_VERSION,
                "mealie_list_id": self.mealie_list_id,
                "keep_list_id": self.keep_list_id,
                "links": [link.to_json() for link in self._links.values()],
            },
        )

    def reset_if_lists_changed(self, mealie_list_id: str, keep_list_id: str) -> bool:
        """Drop all links if either configured list changed; stale IDs are meaningless.

        Returns True when state was discarded.
        """
        changed = (
            self.mealie_list_id is not None
            and self.keep_list_id is not None
            and (self.mealie_list_id != mealie_list_id or self.keep_list_id != keep_list_id)
        )
        if changed:
            log.warning(
                "Configured lists changed; discarding previous links",
                extra={
                    "old_mealie": self.mealie_list_id,
                    "new_mealie": mealie_list_id,
                    "old_keep": self.keep_list_id,
                    "new_keep": keep_list_id,
                },
            )
            self._links.clear()

        self.mealie_list_id = mealie_list_id
        self.keep_list_id = keep_list_id
        return changed

    # -- link access -------------------------------------------------------

    @property
    def links(self) -> list[Link]:
        return list(self._links.values())

    def add(self, link: Link) -> None:
        self._links[link.mealie_id] = link

    def remove(self, mealie_id: str) -> None:
        self._links.pop(mealie_id, None)

    def replace_all(self, links: list[Link]) -> None:
        self._links = {link.mealie_id: link for link in links}
