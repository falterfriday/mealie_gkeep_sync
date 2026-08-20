"""Log formatting.

Regression cover for a real bug: the text formatter used to drop ``extra=...`` fields,
so an "Authentication failed" line printed with no indication of *why*.
"""

from __future__ import annotations

import json
import logging

from mealie_gkeep_sync.logging_setup import JsonFormatter, TextFormatter


def _record(msg: str = "Authentication failed", **extras: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="mealie_gkeep_sync",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for key, value in extras.items():
        setattr(record, key, value)
    return record


def test_text_formatter_includes_extras() -> None:
    line = TextFormatter().format(_record(error="BadAuthentication", account="a@b.com"))
    assert "Authentication failed" in line
    assert "BadAuthentication" in line
    assert "a@b.com" in line


def test_text_formatter_without_extras_is_clean() -> None:
    line = TextFormatter().format(_record("Stopped"))
    assert line.endswith("Stopped")
    assert "[" not in line.split("Stopped")[-1]


def test_json_formatter_promotes_extras_to_fields() -> None:
    payload = json.loads(JsonFormatter().format(_record(error="BadAuthentication", retry_in=300)))
    assert payload["msg"] == "Authentication failed"
    assert payload["level"] == "ERROR"
    assert payload["error"] == "BadAuthentication"
    assert payload["retry_in"] == 300


def test_json_formatter_serialises_unusual_values() -> None:
    """extra= values are arbitrary objects; the formatter must never raise."""
    payload = json.loads(JsonFormatter().format(_record(path=object())))
    assert isinstance(payload["path"], str)


def test_formatters_do_not_leak_reserved_record_fields() -> None:
    payload = json.loads(JsonFormatter().format(_record()))
    assert "pathname" not in payload
    assert "lineno" not in payload
