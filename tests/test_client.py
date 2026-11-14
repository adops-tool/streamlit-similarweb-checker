from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest
from aiohttp import web

from tests.support import FAST_SETTINGS, traffic_api
from traffic_checker.client import (
    InvalidApiKeyError,
    RateLimiter,
    TrafficClient,
    check_api_key,
    clean_api_key,
)
from traffic_checker.config import API_HOST
from traffic_checker.models import LookupStatus, Quota

API_KEY = "test-key-0123456789"


class Recorder:
    """Scripted handler: replays ``responses`` in order, repeating the last one."""

    def __init__(self, *responses: web.Response | Exception) -> None:
        self._responses = list(responses)
        self.requests: list[web.Request] = []

    async def __call__(self, request: web.Request) -> web.Response:
        self.requests.append(request)
        index = min(len(self.requests), len(self._responses)) - 1
        scripted = self._responses[index]
        # A Response object is bound to a single request, so hand out copies.
        return web.Response(
            status=scripted.status,
            body=scripted.body,
            headers={k: v for k, v in scripted.headers.items() if k.lower() != "content-length"},
        )


def fetch(handler, domain="example.com", settings=FAST_SETTINGS):
    """Run a single lookup against ``handler``; return the result and the client."""

    async def scenario():
        async with (
            traffic_api(handler) as url,
            TrafficClient(API_KEY, settings, api_url=url) as client,
        ):
            return await client.fetch(domain), client

    return asyncio.run(scenario())


def test_successful_lookup_sends_credentials_and_reads_quota():
    handler = Recorder(
        web.json_response(
            {"Visits": 123456.7},
            headers={
                "x-ratelimit-requests-remaining": "41",
                "x-ratelimit-requests-limit": "500",
            },
        )
    )

    result, client = fetch(handler)

    assert (result.domain, result.status, result.visits) == ("example.com", LookupStatus.OK, 123456)
    assert client.quota == Quota(remaining=41, limit=500)
    (request,) = handler.requests
    assert request.query["domain"] == "example.com"
    assert request.headers["x-rapidapi-key"] == API_KEY
    assert request.headers["x-rapidapi-host"] == API_HOST


def test_payload_without_a_figure_is_no_data_rather_than_zero():
    result, _ = fetch(Recorder(web.json_response({"Title": "Example"})))

    assert result.status is LookupStatus.NO_DATA
    assert result.visits is None


def test_unknown_domain_is_final_and_not_retried():
    handler = Recorder(web.Response(status=404))

    result, _ = fetch(handler)

    assert result.status is LookupStatus.NO_DATA
    assert len(handler.requests) == 1


def test_server_error_is_retried_until_success():
    handler = Recorder(web.Response(status=502), web.json_response({"visits": 10}))

    result, _ = fetch(handler)

    assert result.status is LookupStatus.OK
    assert len(handler.requests) == 2


def test_persistent_server_error_gives_up_after_max_attempts():
    handler = Recorder(web.Response(status=503))

    result, _ = fetch(handler)

    assert result.status is LookupStatus.HTTP_ERROR
    assert result.label == "HTTP 503"
    assert len(handler.requests) == FAST_SETTINGS.max_attempts


def test_client_error_is_not_retried():
    handler = Recorder(web.Response(status=400))

    result, _ = fetch(handler)

    assert result.status is LookupStatus.HTTP_ERROR
    assert len(handler.requests) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_key_is_fatal_and_not_retried(status):
    handler = Recorder(web.Response(status=status))

    result, _ = fetch(handler)

    assert result.status is LookupStatus.AUTH_ERROR
    assert result.status.is_fatal
    assert len(handler.requests) == 1


def test_exhausted_quota_is_told_apart_from_rate_limiting():
    handler = Recorder(
        web.json_response(
            {"message": "You have exceeded the MONTHLY quota for Requests on your current plan."},
            status=429,
        )
    )

    result, client = fetch(handler)

    assert result.status is LookupStatus.QUOTA_EXHAUSTED
    assert client.quota.remaining == 0
    assert len(handler.requests) == 1


def test_zero_remaining_header_marks_quota_exhaustion():
    handler = Recorder(web.Response(status=429, headers={"x-ratelimit-requests-remaining": "0"}))

    result, _ = fetch(handler)

    assert result.status is LookupStatus.QUOTA_EXHAUSTED


def test_rate_limiting_is_retried():
    handler = Recorder(
        web.json_response(
            {"message": "You have exceeded the rate limit per second for your plan."},
            status=429,
            headers={"Retry-After": "0"},
        ),
        web.json_response({"visits": 5}),
    )

    result, _ = fetch(handler)

    assert result.status is LookupStatus.OK
    assert len(handler.requests) == 2


def test_persistent_rate_limiting_is_reported_as_such():
    result, _ = fetch(Recorder(web.Response(status=429, text="Too many requests")))

    assert result.status is LookupStatus.RATE_LIMITED
    assert not result.status.is_final


def test_malformed_body_is_reported_as_invalid_response():
    handler = Recorder(web.Response(text="<html>gateway</html>"))

    result, _ = fetch(handler)

    assert result.status is LookupStatus.INVALID_RESPONSE
    assert len(handler.requests) == FAST_SETTINGS.max_attempts


def test_oversized_body_is_rejected():
    result, _ = fetch(Recorder(web.Response(body=b"[" + b" " * (1024 * 1024 + 1) + b"]")))

    assert result.status is LookupStatus.INVALID_RESPONSE


def test_slow_response_times_out():
    async def handler(request):
        await asyncio.sleep(1)
        return web.json_response({"visits": 1})

    settings = replace(FAST_SETTINGS, request_timeout=0.05, max_attempts=2)

    result, _ = fetch(handler, settings=settings)

    assert result.status is LookupStatus.TIMEOUT


def test_connection_failure_is_a_network_error():
    async def scenario():
        async with traffic_api(Recorder(web.Response())) as url:
            closed_url = url
        # The server is gone by now, so the port refuses connections.
        async with TrafficClient(API_KEY, FAST_SETTINGS, api_url=closed_url) as client:
            return await client.fetch("example.com")

    result = asyncio.run(scenario())

    assert result.status is LookupStatus.NETWORK_ERROR
    assert result.detail


def test_redirects_are_not_followed():
    handler = Recorder(web.Response(status=302, headers={"Location": "http://127.0.0.1:9/steal"}))

    result, _ = fetch(handler)

    assert result.status is LookupStatus.HTTP_ERROR
    assert result.label == "HTTP 302"
    assert len(handler.requests) == 1


def test_fetch_requires_an_entered_client():
    client = TrafficClient(API_KEY, FAST_SETTINGS)

    with pytest.raises(RuntimeError):
        asyncio.run(client.fetch("example.com"))


@pytest.mark.parametrize("raw", ["", "   ", "abc def", "abc\r\nX-Injected: 1", "ключ"])
def test_unusable_api_keys_are_rejected(raw):
    with pytest.raises(InvalidApiKeyError):
        clean_api_key(raw)


def test_api_key_is_stripped():
    assert clean_api_key(f"  {API_KEY}\n") == API_KEY


def test_rate_limiter_spaces_out_acquisitions():
    async def scenario():
        limiter = RateLimiter(rate=50)
        started = time.perf_counter()
        await asyncio.gather(*(limiter.acquire() for _ in range(6)))
        return time.perf_counter() - started

    # Six acquisitions at 50/s span five 20 ms intervals.
    assert asyncio.run(scenario()) >= 0.09


def check(handler):
    async def scenario():
        async with traffic_api(handler) as url:
            return await check_api_key(API_KEY, FAST_SETTINGS, api_url=url)

    return asyncio.run(scenario())


def test_key_check_accepts_a_working_key():
    handler = Recorder(
        web.json_response({"visits": 1}, headers={"x-ratelimit-requests-remaining": "7"})
    )

    outcome = check(handler)

    assert outcome.ok
    assert outcome.quota.remaining == 7
    assert handler.requests[0].query["domain"] == "google.com"


def test_key_check_reports_a_rejected_key():
    outcome = check(Recorder(web.Response(status=403)))

    assert not outcome.ok
    assert "rejected" in outcome.message


def test_key_check_spends_a_single_request_on_transient_errors():
    handler = Recorder(web.Response(status=500))

    outcome = check(handler)

    assert not outcome.ok
    assert len(handler.requests) == 1
