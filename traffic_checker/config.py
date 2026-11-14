"""Static configuration and tunable defaults."""

from __future__ import annotations

from dataclasses import dataclass

API_HOST = "similarweb-insights.p.rapidapi.com"
API_URL = f"https://{API_HOST}/traffic"

# Name of the environment variable (and Streamlit secret) holding a
# pre-provisioned RapidAPI key.
API_KEY_ENV_VAR = "RAPIDAPI_KEY"

# A domain SimilarWeb is guaranteed to know, used to probe a key before a batch.
VALIDATION_DOMAIN = "google.com"

# Upper bound on input rows. Keeps memory use and accidental quota burn in check.
MAX_INPUT_ROWS = 50_000


@dataclass(frozen=True)
class ClientSettings:
    """Tunables of the API client.

    The defaults mirror the entry-level DataLoom plan on RapidAPI. Raise
    ``requests_per_second`` only if the subscription allows it; the provider
    answers anything above the plan ceiling with HTTP 429.
    """

    requests_per_second: float = 3.0
    max_concurrency: int = 3
    request_timeout: float = 15.0
    max_attempts: int = 3
    backoff_base: float = 1.5
    backoff_cap: float = 30.0

    def __post_init__(self) -> None:
        if self.requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        if self.request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.backoff_base <= 0 or self.backoff_cap <= 0:
            raise ValueError("backoff_base and backoff_cap must be positive")
