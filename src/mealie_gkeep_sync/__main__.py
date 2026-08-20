"""Entry point: sync loop with backoff, health endpoints, and graceful shutdown."""

from __future__ import annotations

import logging
import signal
import sys
import threading
from types import FrameType

from pydantic import ValidationError

from .config import Settings, load_settings
from .errors import AuthError, ConfigError, TransientError
from .health import HealthServer, HealthState
from .keep_client import KeepClient
from .logging_setup import configure_logging
from .mealie import MealieClient
from .state import LinkStore
from .sync import Syncer

log = logging.getLogger("mealie_gkeep_sync")

#: Auth and config failures need a human. Back off hard rather than hammering Google,
#: but keep the process alive so /readyz and the logs stay inspectable.
BLOCKED_RETRY_SECONDS = 300.0
MAX_TRANSIENT_BACKOFF = 300.0


class Runner:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._stop = threading.Event()
        self._health = HealthState(
            stale_after_seconds=max(settings.sync_interval_seconds * 3, 300.0)
        )
        self._mealie = MealieClient(
            settings.mealie_base_url,
            settings.mealie_api_token,
            verify_ssl=settings.mealie_verify_ssl,
            timeout=settings.mealie_timeout_seconds,
        )
        self._keep = KeepClient(
            settings.google_email,
            settings.google_master_token,
            state_path=settings.keep_state_path,
            list_name=settings.keep_list_name,
            create_if_missing=settings.keep_create_list_if_missing,
        )
        self._syncer = Syncer(
            settings, self._mealie, self._keep, LinkStore(settings.link_state_path)
        )
        self._connected = False

    def request_stop(self, signum: int, _frame: FrameType | None) -> None:
        log.info("Shutdown signal received", extra={"signal": signal.Signals(signum).name})
        self._stop.set()

    def run(self) -> int:
        health = HealthServer(self._settings.health_host, self._settings.health_port, self._health)
        health.start()

        if self._settings.dry_run:
            log.warning("DRY RUN enabled - no changes will be written to either side")

        transient_backoff = self._settings.sync_interval_seconds
        try:
            while not self._stop.is_set():
                delay = self._settings.sync_interval_seconds
                try:
                    if not self._connected:
                        self._connect()
                    outcome = self._syncer.run_once()
                    self._health.record_success(outcome.summary)
                    if not outcome.summary or all(v == 0 for v in outcome.summary.values()):
                        log.debug("Sync complete, nothing to do")
                    else:
                        log.info("Sync complete", extra=outcome.summary)
                    transient_backoff = self._settings.sync_interval_seconds

                except AuthError as exc:
                    # Credentials will not fix themselves; surface loudly and slow down.
                    self._connected = False
                    self._health.record_failure(str(exc), auth=True)
                    log.error("Authentication failed", extra={"error": str(exc)})
                    delay = BLOCKED_RETRY_SECONDS

                except ConfigError as exc:
                    self._connected = False
                    self._health.record_failure(str(exc))
                    log.error("Configuration problem", extra={"error": str(exc)})
                    delay = BLOCKED_RETRY_SECONDS

                except TransientError as exc:
                    self._health.record_failure(str(exc))
                    log.warning(
                        "Sync failed, will retry",
                        extra={"error": str(exc), "retry_in": transient_backoff},
                    )
                    delay = transient_backoff
                    transient_backoff = min(transient_backoff * 2, MAX_TRANSIENT_BACKOFF)

                except Exception as exc:
                    self._connected = False
                    self._health.record_failure(f"{type(exc).__name__}: {exc}")
                    log.exception("Unexpected sync error")
                    delay = transient_backoff
                    transient_backoff = min(transient_backoff * 2, MAX_TRANSIENT_BACKOFF)

                self._stop.wait(delay)
        finally:
            health.stop()
            self._mealie.close()
            log.info("Stopped")
        return 0

    def _connect(self) -> None:
        if not self._keep.connected:
            self._keep.connect()
        self._syncer.connect()
        self._connected = True


def main() -> int:
    try:
        settings = load_settings()
    except ValidationError as exc:
        # Logging is not configured yet, so write the config error plainly to stderr.
        print("Invalid configuration:", file=sys.stderr)
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"])
            print(f"  {location.upper()}: {error['msg']}", file=sys.stderr)
        return 2

    configure_logging(settings.log_level, settings.log_format)
    log.info(
        "Starting mealie-gkeep-sync",
        extra={
            "interval": settings.sync_interval_seconds,
            "conflict_strategy": settings.conflict_strategy.value,
            "dry_run": settings.dry_run,
        },
    )

    runner = Runner(settings)
    signal.signal(signal.SIGTERM, runner.request_stop)
    signal.signal(signal.SIGINT, runner.request_stop)
    return runner.run()


if __name__ == "__main__":
    raise SystemExit(main())
