"""Concurrent execution of a batch of domain lookups."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from traffic_checker.client import TrafficClient
from traffic_checker.config import ClientSettings
from traffic_checker.models import LookupResult, LookupStatus, Quota

ResultCallback = Callable[[LookupResult], None]

T = TypeVar("T")


@dataclass(frozen=True)
class BatchOutcome:
    """Everything a caller needs to report on a finished (or halted) batch."""

    results: list[LookupResult]
    quota: Quota

    @property
    def halted_by(self) -> LookupStatus | None:
        """The fatal status that cut the batch short, if any."""
        return next((r.status for r in self.results if r.status.is_fatal), None)


async def run_batch(
    client: TrafficClient,
    domains: Sequence[str],
    on_result: ResultCallback | None = None,
) -> list[LookupResult]:
    """Look up ``domains`` with a bounded pool of workers.

    ``on_result`` is invoked from the event loop thread right after each
    lookup completes, which lets callers checkpoint incrementally. Once a
    fatal status (rejected key, exhausted quota) is seen, no further domain
    is started; domains that were never attempted are absent from the result.
    """
    pending = iter(domains)
    results: list[LookupResult] = []
    halted = False

    async def worker() -> None:
        nonlocal halted
        while not halted:
            domain = next(pending, None)
            if domain is None:
                return
            result = await client.fetch(domain)
            results.append(result)
            if result.status.is_fatal:
                halted = True
            if on_result is not None:
                on_result(result)

    worker_count = min(client.settings.max_concurrency, len(domains))
    tasks = [asyncio.create_task(worker()) for _ in range(worker_count)]
    try:
        await asyncio.gather(*tasks)
    finally:
        # Reached with live tasks only when a worker or the callback raised;
        # the siblings must not keep issuing requests in the background.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return results


async def lookup_domains(
    api_key: str,
    domains: Sequence[str],
    settings: ClientSettings | None = None,
    on_result: ResultCallback | None = None,
) -> BatchOutcome:
    """Open a client, run the batch and report the final quota reading."""
    async with TrafficClient(api_key, settings) as client:
        results = await run_batch(client, domains, on_result)
        return BatchOutcome(results=results, quota=client.quota)


async def run_with_heartbeat(
    operation: Coroutine[Any, Any, T],
    heartbeat: Callable[[], None],
    interval: float,
) -> T:
    """Await ``operation`` while calling ``heartbeat`` every ``interval`` seconds.

    Meant for hosts that can interrupt a running job only from inside their
    own API calls, by raising there. The heartbeat gives such a host a regular
    opening while the operation is waiting on the network. Whatever the
    heartbeat raises propagates to the caller after the operation has been
    cancelled, so no request is left running in the background.
    """
    task = asyncio.ensure_future(operation)
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=interval)
            heartbeat()
        return task.result()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
