"""Health endpoints.

Liveness and readiness are deliberately different signals:

* ``/healthz`` stays green while the process is running. A bad master token must not
  crash-loop the pod - a pod in CrashLoopBackOff hides its own logs behind restarts.
* ``/readyz`` goes red when syncing is actually broken (auth rejected, or no successful
  sync for several intervals), which is what surfaces the problem in ``kubectl get pods``
  while the container stays up and inspectable.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

log = logging.getLogger(__name__)


class HealthState:
    """Thread-safe record of the most recent sync attempt."""

    def __init__(self, stale_after_seconds: float) -> None:
        self._lock = threading.Lock()
        self._stale_after = stale_after_seconds
        self._last_success: datetime | None = None
        self._last_error: str | None = None
        self._last_summary: dict[str, int] = {}
        self._auth_failed = False
        self._started = datetime.now(UTC)

    def record_success(self, summary: dict[str, int]) -> None:
        with self._lock:
            self._last_success = datetime.now(UTC)
            self._last_error = None
            self._last_summary = summary
            self._auth_failed = False

    def record_failure(self, error: str, *, auth: bool = False) -> None:
        with self._lock:
            self._last_error = error
            if auth:
                self._auth_failed = True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            age = (
                (datetime.now(UTC) - self._last_success).total_seconds()
                if self._last_success
                else None
            )
            ready = (
                not self._auth_failed
                and self._last_success is not None
                and age is not None
                and age <= self._stale_after
            )
            return {
                "ready": ready,
                "auth_failed": self._auth_failed,
                "last_success": self._last_success.isoformat() if self._last_success else None,
                "seconds_since_success": round(age, 1) if age is not None else None,
                "last_error": self._last_error,
                "last_sync": self._last_summary,
                "started": self._started.isoformat(),
            }

    @property
    def ready(self) -> bool:
        return bool(self.snapshot()["ready"])


def _make_handler(state: HealthState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/healthz":
                self._respond(200, {"status": "alive"})
            elif path in ("/readyz", "/"):
                snapshot = state.snapshot()
                self._respond(200 if snapshot["ready"] else 503, snapshot)
            else:
                self._respond(404, {"error": "not found"})

        def _respond(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            # Probe traffic every few seconds would otherwise drown the real logs.
            log.debug("health request", extra={"request": format % args})

    return Handler


class HealthServer:
    def __init__(self, host: str, port: int, state: HealthState) -> None:
        self._server = ThreadingHTTPServer((host, port), _make_handler(state))
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="health", daemon=True
        )

    def start(self) -> None:
        self._thread.start()
        host, port = self._server.server_address[:2]
        log.info("Health server listening", extra={"host": str(host), "port": port})

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
