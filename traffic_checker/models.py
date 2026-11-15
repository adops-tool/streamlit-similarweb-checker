"""Value objects shared across the package."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class LookupStatus(str, Enum):
    """Outcome of a single domain lookup."""

    OK = "ok"
    NO_DATA = "no_data"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    HTTP_ERROR = "http_error"
    INVALID_RESPONSE = "invalid_response"
    AUTH_ERROR = "auth_error"
    QUOTA_EXHAUSTED = "quota_exhausted"

    @property
    def is_final(self) -> bool:
        """Whether the API gave a definitive answer that never needs a re-request."""
        return self in _FINAL_STATUSES

    @property
    def is_fatal(self) -> bool:
        """Whether every further request with the same key is bound to fail."""
        return self in _FATAL_STATUSES

    @property
    def label(self) -> str:
        """Human-readable name used in the UI and in exports."""
        return _STATUS_LABELS[self]


_FINAL_STATUSES = frozenset({LookupStatus.OK, LookupStatus.NO_DATA})
_FATAL_STATUSES = frozenset({LookupStatus.AUTH_ERROR, LookupStatus.QUOTA_EXHAUSTED})

_STATUS_LABELS = {
    LookupStatus.OK: "OK",
    LookupStatus.NO_DATA: "No data",
    LookupStatus.RATE_LIMITED: "Rate limited",
    LookupStatus.TIMEOUT: "Timeout",
    LookupStatus.NETWORK_ERROR: "Network error",
    LookupStatus.HTTP_ERROR: "HTTP error",
    LookupStatus.INVALID_RESPONSE: "Invalid response",
    LookupStatus.AUTH_ERROR: "Auth error",
    LookupStatus.QUOTA_EXHAUSTED: "Quota exhausted",
}


@dataclass(frozen=True)
class LookupResult:
    """Result of looking up one normalized domain.

    ``visits`` is populated only for ``LookupStatus.OK``; every other status
    leaves it ``None`` so that "zero traffic" and "unknown" never get mixed up.
    """

    domain: str
    status: LookupStatus
    visits: int | None = None
    detail: str = ""

    @property
    def label(self) -> str:
        """Status label, preferring the concrete detail (e.g. ``HTTP 502``)."""
        return self.detail or self.status.label


@dataclass(frozen=True)
class Quota:
    """Request allowance reported by RapidAPI; ``None`` means "not reported"."""

    remaining: int | None = None
    limit: int | None = None
