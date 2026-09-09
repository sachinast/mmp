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

from mmp_core.metrics import metrics_endpoint, sample_stream_depths
from mmp_core.settings import Settings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
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
from mmp_tracker.deferred import conversion_values, resolve_deferred
from mmp_tracker.ingest import ingest_events
from mmp_tracker.redirect import redirect_click
from mmp_tracker.s2s import ingest_s2s
from mmp_tracker.skan import receive_postback
from mmp_tracker.state import TrackerState


async def service_root(request: Request) -> Response:
    """A signpost for anyone who lands on the tracker by hand.

    Deliberately says less than the API's equivalent. This service is the one
    on the public internet, so it names itself and the dashboard and stops
    there — it does not enumerate its own ingest surface for an anonymous
    caller who has not been given a key.
    """
    return JSONResponse(
        {
            "service": "tracker",
            "health": "/health",
            "note": "Event ingest and click redirects. Endpoints require an app key.",
        },
        headers={"cache-control": "no-store"},
    )


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
        registry.register("links", state.ping_links)
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
                "clicks_total": state.clicks_total,
                "unknown_codes": state.unknown_codes,
                "buffer_depth": state.buffer.depth,
                "buffer_shipped": state.buffer.shipped,
                "buffer_dropped": state.buffer.dropped,
                "click_buffer_depth": state.click_buffer.depth,
                "click_buffer_dropped": state.click_buffer.dropped,
                "failed_flushes": state.buffer.failed_flushes,
                "cached_links": state.links.size,
                "link_cache_loads": state.links.loads,
                "link_cache_notifications": state.links.notifications,
            }
        )

    async def _flush_audit() -> None:
        """Push accepted counts to Redis periodically.

        On the metrics scrape rather than a timer of its own: scrapes happen
        every fifteen seconds anyway, and one fewer background task is one fewer
        thing to drain on shutdown.
        """
        state = getattr(app_holder.get("app"), "state", None)
        tracker = getattr(state, "tracker", None) if state else None
        if tracker is not None:
            await tracker.accepted_counter.flush()

    async def _sample() -> None:
        state = getattr(app_holder.get("app"), "state", None)
        tracker = getattr(state, "tracker", None) if state else None
        if tracker is not None:
            from mmp_ingest.stream import CLICKS_GROUP, CLICKS_STREAM, EVENTS_GROUP, EVENTS_STREAM

            await _flush_audit()
            await sample_stream_depths(
                tracker.redis,
                {EVENTS_STREAM: EVENTS_GROUP, CLICKS_STREAM: CLICKS_GROUP},
            )

    app_holder: dict[str, object] = {}
    routes = [Route(path, handler) for path, handler in health_routes(registry)]
    routes += [
        Route("/", service_root, methods=["GET"]),
        Route("/v1/events", ingest_events, methods=["POST"]),
        Route("/v1/s2s/events", ingest_s2s, methods=["POST"]),
        Route("/v1/deeplink/resolve", resolve_deferred, methods=["POST"]),
        Route("/v1/skan/conversion-values", conversion_values, methods=["GET"]),
        Route(
            "/.well-known/skadnetwork/report-attribution",
            receive_postback,
            methods=["POST"],
        ),
        Route("/c/{tracking_code}", redirect_click, methods=["GET", "HEAD"]),
        Route("/internal/stats", stats, methods=["GET"]),
        # Sampled at scrape time rather than maintained per message: keeping the
        # backlog gauge accurate on every event would put a Redis round trip on
        # the ingest path in order to measure the ingest path.
        Route("/metrics", metrics_endpoint(_sample), methods=["GET"]),
    ]

    application = Starlette(
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
    app_holder["app"] = application
    return application


app = create_app()
