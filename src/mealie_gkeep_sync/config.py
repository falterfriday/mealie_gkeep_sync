"""Environment-driven configuration."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConflictStrategy(StrEnum):
    """How to resolve a field that changed on both sides between syncs."""

    NEWEST = "newest"
    """Prefer whichever side reports the later modification timestamp."""

    MEALIE = "mealie"
    """Mealie always wins."""

    KEEP = "keep"
    """Google Keep always wins."""


class LogFormat(StrEnum):
    JSON = "json"
    TEXT = "text"


class Settings(BaseSettings):
    """Runtime configuration, sourced from environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Mealie -----------------------------------------------------------
    mealie_base_url: str = Field(description="Base URL of the Mealie instance.")
    mealie_api_token: str = Field(description="Mealie API token (user profile > API tokens).")
    mealie_list_id: str | None = Field(
        default=None, description="UUID of the Mealie shopping list to sync."
    )
    mealie_list_name: str | None = Field(
        default=None, description="Name of the Mealie shopping list, if the ID is not known."
    )
    mealie_verify_ssl: bool = True
    mealie_timeout_seconds: float = 30.0

    # --- Google Keep ------------------------------------------------------
    google_email: str = Field(description="Google account email that owns the Keep list.")
    google_master_token: str = Field(
        description="gpsoauth master token. Password-equivalent; store as a Secret."
    )
    keep_list_name: str = Field(description="Title of the Google Keep list to sync.")
    keep_create_list_if_missing: bool = Field(
        default=False,
        description="Create the Keep list when no list with that title exists.",
    )

    # --- Sync behaviour ---------------------------------------------------
    sync_interval_seconds: float = 60.0
    conflict_strategy: ConflictStrategy = ConflictStrategy.NEWEST
    parse_ingredients: bool = Field(
        default=True,
        description="Send Keep-authored text through Mealie's ingredient parser.",
    )
    parser_min_confidence: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description="Below this parser confidence, fall back to an unstructured note item.",
    )
    create_missing_foods: bool = Field(
        default=False,
        description=(
            "Let Mealie create new food records for unrecognised text. Off by default so "
            "shopping-list typos do not pollute the food database."
        ),
    )
    dry_run: bool = Field(
        default=False, description="Log the action plan without writing to either side."
    )

    # --- Operational ------------------------------------------------------
    state_dir: Path = Path("/data")
    health_port: int = 8080
    # Binding all interfaces is required, not an oversight: kubelet delivers probes from
    # outside the container's loopback. The port is never published beyond the pod.
    health_host: str = "0.0.0.0"  # nosec B104
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON

    @property
    def link_state_path(self) -> Path:
        """Where our own ID links and last-synced snapshot live."""
        return self.state_dir / "sync-state.json"

    @property
    def keep_state_path(self) -> Path:
        """Where gkeepapi's serialised node cache lives."""
        return self.state_dir / "keep-state.json"

    @model_validator(mode="after")
    def _check_list_selector(self) -> Settings:
        if not self.mealie_list_id and not self.mealie_list_name:
            raise ValueError("Set either MEALIE_LIST_ID or MEALIE_LIST_NAME.")
        return self

    @model_validator(mode="after")
    def _normalise_base_url(self) -> Settings:
        object.__setattr__(self, "mealie_base_url", self.mealie_base_url.rstrip("/"))
        return self


def load_settings() -> Settings:
    """Load settings from the environment, raising a readable error if incomplete."""
    return Settings()  # type: ignore[call-arg]
