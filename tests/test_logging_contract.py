"""Guards against a class of bug that took production down.

``logging.makeRecord`` raises ``KeyError`` if an ``extra`` key collides with a built-in
LogRecord attribute. ``extra={"created": n}`` shipped and crashed the sync loop, because
``created`` is the record's timestamp. A unit test cannot catch this without exercising
every log line, so the source is scanned instead.
"""

from __future__ import annotations

import ast
import logging
import pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"

RESERVED = set(logging.LogRecord("n", 20, "p", 1, "m", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def _extra_keys() -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "extra" or not isinstance(kw.value, ast.Dict):
                    continue
                for key in kw.value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        found.append((path.name, key.lineno, key.value))
    return found


def test_no_log_extra_shadows_a_logrecord_attribute() -> None:
    collisions = [(f, ln, k) for f, ln, k in _extra_keys() if k in RESERVED]
    assert not collisions, (
        "logging.makeRecord raises KeyError for these keys: "
        + ", ".join(f"{f}:{ln} {k!r}" for f, ln, k in collisions)
    )


def test_the_scan_actually_finds_extra_keys() -> None:
    """Guard the guard: a broken scan would silently pass the test above."""
    assert len(_extra_keys()) > 20


def test_reserved_key_really_does_raise() -> None:
    """Pins the behaviour this suite is protecting against."""
    log = logging.getLogger("contract-test")
    log.addHandler(logging.NullHandler())
    try:
        log.warning("boom", extra={"created": 1})
    except KeyError as exc:
        assert "created" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected KeyError for a reserved extra key")
