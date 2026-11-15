"""Async client for the DataLoom SimilarWeb ``/traffic`` endpoint."""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import TracebackType

import aiohttp

from traffic_checker.config import API_HOST, API_URL, VALIDATION_DOMAIN, ClientSettings
from traffic_checker.models import LookupResult, LookupStatus, Quota
from traffic_checker.parsing import extract_visits

_QUOTA_REMAINING_HEADER = "x-ratelimit-requests-remaining"
_QUOTA_LIMIT_HEADER = "x-ratelimit-requests-limit"

# RapidAPI answers 429 both for the per-second rate limit and for an exhausted
# plan quota; only the wording of the response body tells the two apart.
_QUOTA_BODY_MARKERS = ("quota", "monthly", "daily")

# No legitimate response comes anywhere near this size.
_MAX_BODY_BYTES = 1024 * 1024


class InvalidApiKeyError(ValueError):
    """The supplied API key cannot be sent as an HTTP header value."""


def clean_api_key(raw: str) -> str:
    """Strip surrounding whitespace from a key and make sure it is header-safe."""
    key = raw.strip()
    if not key:
        raise InvalidApiKeyError("The API key is empty.")
    if not key.isascii() or not key.isprintable() or any(ch.isspace() for ch in key):
        raise InvalidApiKeyError(
            "The API key contains whitespace or non-ASCII characters. "
            "Copy it again from the RapidAPI dashboard."
        )
    return key


class RateLimiter:
    """Spaces out acquisitions so that at most ``rate`` of them start per second."""

    def __init__(self, rate: float) -> None:
        self._interval = 1.0 / rate
        self._next_slot = 0.0

    async def acquire(self) -> None:
        now = asyncio.get_running_loop().time()
        slot = max(now, self._next_slot)
        # Reserving the slot before sleeping keeps concurrent callers in order
        # without a lock: there is no await between the read and the write.
        self._next_slot = slot + self._interval
        if slot > now:
            await asyncio.sleep(slot - now)


@dataclass(frozen=True)
class _Outcome:
    """Result of one HTTP attempt plus what the retry loop needs to know."""

    result: LookupResult
    retryable: bool = False
    retry_after: float | None = None


class TrafficClient:
    """Rate-limited, retrying client. Use as an async context manager."""

    def __init__(
        self,
        api_key: str,
        settings: ClientSettings | None = None,
        *,
        api_url: str = API_URL,
    ) -> None:
        self.settings = settings or ClientSettings()
        self.quota = Quota()
        self._headers = {
            "x-rapidapi-host": API_HOST,
            "x-rapidapi-key": clean_api_key(api_key),
            "Accept": "application/json",
        }
        self._api_url = api_url
        self._limiter = RateLimiter(self.settings.requests_per_second)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> TrafficClient:
        self._session = aiohttp.ClientSession(
            headers=self._headers,
            timeout=aiohttp.ClientTimeout(total=self.settings.request_timeout),
            connector=aiohttp.TCPConnector(limit=self.settings.max_concurrency),
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def fetch(self, domain: str) -> LookupResult:
        """Look up one domain, retrying transient failures with backoff."""
        outcome = await self._request(domain)
        for attempt in range(1, self.settings.max_attempts):
            if not outcome.retryable:
                break
            await asyncio.sleep(self._retry_delay(attempt, outcome.retry_after))
            outcome = await self._request(domain)
        return outcome.result

    async def _request(self, domain: str) -> _Outcome:
        if self._session is None:
            raise RuntimeError("TrafficClient must be entered with 'async with' first")

        await self._limiter.acquire()
        try:
            # Redirects are refused on purpose: following one would forward the
            # API key header to whatever host the redirect points at.
            async with self._session.get(
                self._api_url, params={"domain": domain}, allow_redirects=False
            ) as response:
                self._record_quota(response.headers)
                return await self._interpret(domain, response)
        except asyncio.TimeoutError:
            return _Outcome(LookupResult(domain, LookupStatus.TIMEOUT), retryable=True)
        except aiohttp.ClientError as exc:
            result = LookupResult(domain, LookupStatus.NETWORK_ERROR, detail=type(exc).__name__)
            return _Outcome(result, retryable=True)

    async def _interpret(self, domain: str, response: aiohttp.ClientResponse) -> _Outcome:
        status = response.status

        if status == 200:
            try:
                payload = json.loads(await _read_body(response))
            except ValueError:
                return _Outcome(LookupResult(domain, LookupStatus.INVALID_RESPONSE), retryable=True)
            visits = extract_visits(payload)
            if visits is None:
                return _Outcome(LookupResult(domain, LookupStatus.NO_DATA))
            return _Outcome(LookupResult(domain, LookupStatus.OK, visits=visits))

        if status == 404:
            # The provider has no record of the domain; asking again will not help.
            return _Outcome(LookupResult(domain, LookupStatus.NO_DATA))

        if status in (401, 403):
            return _Outcome(LookupResult(domain, LookupStatus.AUTH_ERROR))

        if status == 429:
            if self.quota.remaining == 0 or await _mentions_quota(response):
                self.quota = replace(self.quota, remaining=0)
                return _Outcome(LookupResult(domain, LookupStatus.QUOTA_EXHAUSTED))
            return _Outcome(
                LookupResult(domain, LookupStatus.RATE_LIMITED),
                retryable=True,
                retry_after=_parse_retry_after(response.headers),
            )

        result = LookupResult(domain, LookupStatus.HTTP_ERROR, detail=f"HTTP {status}")
        # Remaining 4xx codes mean the request itself is at fault; repeating it
        # would only burn quota.
        return _Outcome(result, retryable=status >= 500 or status == 408)

    def _record_quota(self, headers: Mapping[str, str]) -> None:
        remaining = _parse_int(headers.get(_QUOTA_REMAINING_HEADER))
        limit = _parse_int(headers.get(_QUOTA_LIMIT_HEADER))
        if remaining is not None or limit is not None:
            self.quota = Quota(
                remaining=remaining if remaining is not None else self.quota.remaining,
                limit=limit if limit is not None else self.quota.limit,
            )

    def _retry_delay(self, attempt: int, retry_after: float | None) -> float:
        delay = min(self.settings.backoff_cap, self.settings.backoff_base * 2 ** (attempt - 1))
        # Jitter keeps parallel workers from retrying in lockstep.
        delay *= random.uniform(0.75, 1.25)
        if retry_after is not None:
            delay = max(delay, min(retry_after, self.settings.backoff_cap))
        return delay


@dataclass(frozen=True)
class KeyCheck:
    """Outcome of probing an API key with a single request."""

    ok: bool
    message: str
    quota: Quota


async def check_api_key(
    api_key: str,
    settings: ClientSettings | None = None,
    *,
    api_url: str = API_URL,
) -> KeyCheck:
    """Verify a key with one request for a well-known domain.

    The probe counts against the plan quota like any other request.
    """
    probe_settings = replace(settings or ClientSettings(), max_attempts=1)
    async with TrafficClient(api_key, probe_settings, api_url=api_url) as client:
        result = await client.fetch(VALIDATION_DOMAIN)
        quota = client.quota

    if result.status.is_final:
        return KeyCheck(True, "The API key is valid.", quota)
    if result.status is LookupStatus.AUTH_ERROR:
        message = "The API rejected the key. Check its value and the subscription to the API."
    elif result.status is LookupStatus.QUOTA_EXHAUSTED:
        message = "The key is recognized, but its request quota is exhausted."
    else:
        message = f"The key could not be verified ({result.label.lower()}). Try again shortly."
    return KeyCheck(False, message, quota)


async def _read_body(response: aiohttp.ClientResponse) -> bytes:
    """Read the response body, refusing anything beyond ``_MAX_BODY_BYTES``."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        size += len(chunk)
        if size > _MAX_BODY_BYTES:
            raise ValueError("response body exceeds the size limit")
        chunks.append(chunk)
    return b"".join(chunks)


async def _mentions_quota(response: aiohttp.ClientResponse) -> bool:
    try:
        body = (await _read_body(response)).decode("utf-8", errors="replace").lower()
    except (ValueError, aiohttp.ClientError, asyncio.TimeoutError):
        return False
    return any(marker in body for marker in _QUOTA_BODY_MARKERS)


def _parse_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _parse_retry_after(headers: Mapping[str, str]) -> float | None:
    # Only the delta-seconds form is honoured; the HTTP-date form is not used
    # by RapidAPI and falls back to regular backoff.
    try:
        seconds = float(headers["Retry-After"])
    except (KeyError, ValueError):
        return None
    return seconds if seconds >= 0 else None
