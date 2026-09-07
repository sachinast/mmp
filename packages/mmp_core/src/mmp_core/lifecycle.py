"""Graceful startup and shutdown.

The shutdown order is deliberate. On SIGTERM the instance first reports
not-ready and waits ``drain_grace_seconds``, so the load balancer stops sending
new work *before* anything is torn down. Only then do resources close. Skipping
the grace period is the usual cause of a handful of 502s on every deploy.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from mmp_core.health import HealthRegistry
from mmp_core.logging import get_logger

log = get_logger(__name__)

Closer = Callable[[], Awaitable[None]]


@asynccontextmanager
async def service_lifespan(
    registry: HealthRegistry,
    *,
    startup: Callable[[], Awaitable[None]] | None = None,
    closers: list[Closer] | None = None,
    drain_grace_seconds: float = 5.0,
) -> AsyncIterator[None]:
    log.info("service_starting", service=registry.service, version=registry.version)
    if startup is not None:
        await startup()
    log.info("service_ready", service=registry.service)
    try:
        yield
    finally:
        registry.start_draining()
        log.info("service_draining", grace_seconds=drain_grace_seconds)
        await asyncio.sleep(drain_grace_seconds)
        for closer in reversed(closers or []):
            try:
                await closer()
            except Exception:
                log.exception("closer_failed")
        log.info("service_stopped", service=registry.service)
