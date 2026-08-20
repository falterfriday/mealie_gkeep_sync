"""Exception types.

The split that matters operationally is *transient* (retry, stay ready) versus *auth*
(a human must act; keep the process alive and fail readiness so it is visible).
"""

from __future__ import annotations


class SyncError(Exception):
    """Base class for all errors raised by this app."""


class TransientError(SyncError):
    """A failure that is expected to resolve on its own; safe to retry."""


class AuthError(SyncError):
    """Credentials were rejected. Retrying will not help."""


class ConfigError(SyncError):
    """The configured Mealie or Keep list could not be resolved."""
