"""The tracking edge.

Deliberately built on bare Starlette rather than FastAPI. Every millisecond here
is a millisecond of redirect or ingest latency, and these routes return no
serialised models worth the cost of a response-model layer.

Nothing in this module may import SQLAlchemy. The tracker does not read Postgres
on a request path — only on an API-key cache miss.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mmp_core.settings import Settings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mmp_core import (
    HealthRegistry,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    configure_logging,
    health_routes,
    load_settings,
    service_lifespan,
    tune_for_latency,
    unhandled_exception_handler,
)
from mmp_tracker.ingest import ingest_events
from mmp_tracker.state import TrackerState


def create_app(settings: Settings | None = None) -> Starlette:
    settings = settings or load_settings(service_name="tracker")
    configure_logging(service="tracker", level=settings.log_level, json_output=settings.log_json)
    registry = HealthRegistry(service="tracker", version=settings.version)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        state = await TrackerState.create(settings)
        app.state.tracker = state
        # After everything is constructed, before serving. See mmp_core.gc_tuning:
        # untuned, generation-2 collections were the tracker's entire p99 tail.
        tune_for_latency()
        registry.register("redis", state.ping_redis)
        registry.register("postgres", state.ping_database)
        registry.register("buffer", state.ping_buffer)
        async with service_lifespan(
            registry,
            closers=[state.close],
            drain_grace_seconds=settings.shutdown_grace_seconds,
        ):
            yield

    async def stats(request: Request) -> JSONResponse:
        """Operational counters. Not public — bound to the internal port in
        production; here it is the cheapest way to see the pipeline's state."""
        state: TrackerState = request.app.state.tracker
        return JSONResponse(
            {
                "accepted_total": state.accepted_total,
                "buffer_depth": state.buffer.depth,
                "buffer_shipped": state.buffer.shipped,
                "buffer_dropped": state.buffer.dropped,
                "failed_flushes": state.buffer.failed_flushes,
            }
        )

    routes = [Route(path, handler) for path, handler in health_routes(registry)]
    routes += [
        Route("/v1/events", ingest_events, methods=["POST"]),
        Route("/internal/stats", stats, methods=["GET"]),
    ]

    return Starlette(
        routes=routes,
        lifespan=lifespan,
        middleware=[
            Middleware(SecurityHeadersMiddleware),
            # Access logging off on the hot path: one structured line per event
            # batch at thousands per second is a log bill, not observability.
            # Counters above and metrics (Phase 10) cover it instead.
            Middleware(RequestContextMiddleware, access_log=False),
        ],
        exception_handlers={Exception: unhandled_exception_handler},
    )


app = create_app()
