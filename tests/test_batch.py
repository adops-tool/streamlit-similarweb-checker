from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from aiohttp import web

from tests.support import FAST_SETTINGS, traffic_api
from traffic_checker.batch import BatchOutcome, run_batch, run_with_heartbeat
from traffic_checker.client import TrafficClient
from traffic_checker.models import LookupResult, LookupStatus, Quota

API_KEY = "test-key-0123456789"


def run(handler, domains, settings=FAST_SETTINGS, on_result=None):
    async def scenario():
        async with (
            traffic_api(handler) as url,
            TrafficClient(API_KEY, settings, api_url=url) as client,
        ):
            return await run_batch(client, domains, on_result)

    return asyncio.run(scenario())


def test_every_domain_is_looked_up_and_reported():
    async def handler(request):
        return web.json_response({"visits": len(request.query["domain"])})

    domains = [f"site{index}.com" for index in range(20)]
    seen: list[LookupResult] = []

    results = run(handler, domains, on_result=seen.append)

    assert sorted(result.domain for result in results) == sorted(domains)
    assert all(result.status is LookupStatus.OK for result in results)
    assert seen == results


def test_empty_batch_is_a_no_op():
    async def handler(request):
        raise AssertionError("no request expected")

    assert run(handler, []) == []


def test_concurrency_never_exceeds_the_configured_bound():
    in_flight = peak = 0

    async def handler(request):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return web.json_response({"visits": 1})

    settings = replace(FAST_SETTINGS, max_concurrency=2)

    run(handler, [f"site{index}.com" for index in range(10)], settings=settings)

    assert peak == 2


def test_batch_stops_once_the_quota_is_exhausted():
    requested: list[str] = []

    async def handler(request):
        domain = request.query["domain"]
        requested.append(domain)
        if domain == "second.com":
            return web.Response(status=429, text="You have exceeded the MONTHLY quota")
        return web.json_response({"visits": 1})

    settings = replace(FAST_SETTINGS, max_concurrency=1)

    results = run(handler, ["first.com", "second.com", "third.com"], settings=settings)

    assert requested == ["first.com", "second.com"]
    assert [result.status for result in results] == [
        LookupStatus.OK,
        LookupStatus.QUOTA_EXHAUSTED,
    ]
    assert BatchOutcome(results, Quota()).halted_by is LookupStatus.QUOTA_EXHAUSTED


def test_callback_failure_aborts_the_batch():
    requested: list[str] = []

    async def handler(request):
        requested.append(request.query["domain"])
        return web.json_response({"visits": 1})

    def on_result(result):
        raise KeyError("callback failed")

    settings = replace(FAST_SETTINGS, max_concurrency=1)

    with pytest.raises(KeyError):
        run(handler, ["a.com", "b.com", "c.com"], settings=settings, on_result=on_result)

    assert requested == ["a.com"]


def test_outcome_without_fatal_status_is_not_halted():
    results = [
        LookupResult("a.com", LookupStatus.OK, visits=1),
        LookupResult("b.com", LookupStatus.TIMEOUT),
    ]

    assert BatchOutcome(results, Quota()).halted_by is None


def test_heartbeat_ticks_while_the_operation_runs():
    beats = 0

    def heartbeat():
        nonlocal beats
        beats += 1

    async def operation():
        await asyncio.sleep(0.05)
        return "done"

    result = asyncio.run(run_with_heartbeat(operation(), heartbeat, interval=0.01))

    assert result == "done"
    assert beats >= 3


def test_operation_errors_pass_through_the_heartbeat_wrapper():
    async def operation():
        raise LookupError("boom")

    with pytest.raises(LookupError):
        asyncio.run(run_with_heartbeat(operation(), lambda: None, interval=0.01))


def test_interrupting_heartbeat_cancels_requests_in_flight():
    class Interrupt(BaseException):
        """Stands in for the control-flow exceptions UI frameworks raise."""

    started: list[str] = []
    finished: list[str] = []

    async def handler(request):
        started.append(request.query["domain"])
        await asyncio.sleep(0.2)
        finished.append(request.query["domain"])
        return web.json_response({"visits": 1})

    def heartbeat():
        if started:
            raise Interrupt

    async def scenario():
        async with (
            traffic_api(handler) as url,
            TrafficClient(API_KEY, FAST_SETTINGS, api_url=url) as client,
        ):
            domains = [f"site{index}.com" for index in range(20)]
            await run_with_heartbeat(run_batch(client, domains), heartbeat, interval=0.01)

    with pytest.raises(Interrupt):
        asyncio.run(scenario())

    assert 1 <= len(started) <= FAST_SETTINGS.max_concurrency
    assert finished == []
