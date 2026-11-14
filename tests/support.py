from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from aiohttp import web
from aiohttp.test_utils import TestServer

from traffic_checker.config import ClientSettings

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

# Settings that keep the retry and rate-limit machinery active but fast.
FAST_SETTINGS = ClientSettings(
    requests_per_second=1000,
    max_concurrency=3,
    request_timeout=2,
    max_attempts=3,
    backoff_base=0.001,
    backoff_cap=0.01,
)


@asynccontextmanager
async def traffic_api(handler: Handler) -> AsyncIterator[str]:
    """Serve ``handler`` at ``/traffic`` on a local port and yield its URL."""

    # aiohttp only accepts plain coroutine functions as route handlers, while
    # tests also pass callable objects.
    async def route(request: web.Request) -> web.StreamResponse:
        return await handler(request)

    app = web.Application()
    app.router.add_get("/traffic", route)
    async with TestServer(app) as server:
        yield str(server.make_url("/traffic"))
