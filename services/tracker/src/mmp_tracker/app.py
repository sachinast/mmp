"""The tracking edge.

Deliberately built on bare Starlette rather than FastAPI. Every millisecond
here is a millisecond of redirect latency, and this service's routes take no
path parameters worth validating with Pydantic and return no serialised models.

Nothing in this module may import SQLAlchemy. The tracker does not read
Postgres on a request path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route

from mmp_core import (
    HealthRegistry,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    configure_logging,
    health_routes,
    load_settings,
    service_lifespan,
    unhandled_exception_handler,
)


def create_app() -> Starlette:
    settings = load_settings(service_name="tracker")
    configure_logging(service="tracker", level=settings.log_level, json_output=settings.log_json)
    registry = HealthRegistry(service="tracker", version=settings.version)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        # Phase 3 registers the Redis stream producer here; Phase 4 registers
        # the tracking-link cache and its LISTEN/NOTIFY subscriber.
        async with service_lifespan(registry, drain_grace_seconds=settings.shutdown_grace_seconds):
            yield

    routes = [Route(path, handler) for path, handler in health_routes(registry)]

    return Starlette(
        routes=routes,
        lifespan=lifespan,
        middleware=[
            Middleware(SecurityHeadersMiddleware),
            # Access logging off on the hot path: one structured line per
            # redirect at 5k rps is a log bill, not observability. Metrics
            # cover it instead (Phase 10).
            Middleware(RequestContextMiddleware, access_log=False),
        ],
        exception_handlers={Exception: unhandled_exception_handler},
    )


app = create_app()
