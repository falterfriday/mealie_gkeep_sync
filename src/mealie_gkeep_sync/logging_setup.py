"""Structured logging."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from .config import LogFormat

_RESERVED = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
    | {"message", "asctime", "taskName"}
)


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    """The fields passed via ``extra=...``, which carry most of the diagnostic detail."""
    return {key: value for key, value in record.__dict__.items() if key not in _RESERVED}


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per line, including any extra=... fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update(_extras(record))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable lines that still carry the extra=... fields.

    Without this, text mode would drop the detail that explains *why* something failed -
    "Authentication failed" with the actual reason invisible.
    """

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        extras = _extras(record)
        if extras:
            rendered = " ".join(f"{key}={value!r}" for key, value in extras.items())
            line = f"{line} [{rendered}]"
        return line


def configure_logging(level: str, fmt: LogFormat) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt is LogFormat.JSON else TextFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # gkeepapi and httpx are chatty at DEBUG and leak request details.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("gkeepapi").setLevel(logging.INFO)
