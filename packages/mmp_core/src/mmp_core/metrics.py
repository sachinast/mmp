"""Metrics.

The rule that decides what goes in here: **a metric exists to answer a question
someone will ask at 3am.** "Is ingestion stopped?" "Are we behind?" "Did the
match rate drop?" Anything that does not answer such a question is cardinality
and cost.

That is why there are no per-app or per-organisation labels below. It is the
first thing anyone reaches for and it is how a metrics bill becomes a surprise:
a counter labelled by app_id has as many series as there are apps, and a
histogram labelled by app_id has that times its buckets. Per-tenant numbers
belong in the rollups, which are built for exactly that and are queried on
demand rather than scraped every fifteen seconds.

Labels here are bounded by construction — a fixed set of outcomes, a fixed set
of streams — and each one is chosen because an operator would filter by it.
"""

from __future__ import annotations

from collections.abc import Callable

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client.core import CollectorRegistry as _Registry

from mmp_core.logging import get_logger

log = get_logger(__name__)

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# A registry per process rather than the global default, so a test can build one
# in isolation and two services in one interpreter do not collide.
REGISTRY: _Registry = CollectorRegistry()

# --- ingest -------------------------------------------------------------
events_accepted = Counter(
    "mmp_events_accepted_total",
    "Events accepted at the edge and queued.",
    ["source"],  # sdk | s2s — bounded, and the two behave differently enough to split
    registry=REGISTRY,
)
events_rejected = Counter(
    "mmp_events_rejected_total",
    "Events refused at the edge.",
    ["reason"],  # a fixed vocabulary; see REJECTION_REASONS
    registry=REGISTRY,
)
events_written = Counter(
    "mmp_events_written_total",
    "Event rows committed to Postgres.",
    registry=REGISTRY,
)
events_duplicate = Counter(
    "mmp_events_duplicate_total",
    "Events dropped by the deduplication key. Expected to be non-zero.",
    registry=REGISTRY,
)

# The number that tells you ingestion has stopped, and the one worth alerting
# on. A rising backlog with a healthy write rate is a capacity problem; a rising
# backlog with a zero write rate is an outage.
stream_backlog = Gauge(
    "mmp_stream_backlog",
    "Messages in a stream awaiting a consumer.",
    ["stream"],
    registry=REGISTRY,
)
stream_pending = Gauge(
    "mmp_stream_pending",
    "Messages delivered to a consumer but not yet acknowledged.",
    ["stream"],
    registry=REGISTRY,
)

# --- redirect -----------------------------------------------------------
redirects = Counter(
    "mmp_redirects_total",
    "Click redirects served.",
    ["outcome"],  # redirected | unknown_code
    registry=REGISTRY,
)
redirect_latency = Histogram(
    "mmp_redirect_duration_seconds",
    "Time to serve a click redirect.",
    # Buckets chosen around the published SLO rather than the library defaults:
    # the interesting region is 1-100ms, and the defaults put most of their
    # resolution above a second where nothing of ours should ever be.
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
    registry=REGISTRY,
)

# --- attribution --------------------------------------------------------
attributions = Counter(
    "mmp_attributions_total",
    "Installs attributed, by method.",
    ["method"],  # referrer | click_id | device_match | organic — a closed set
    registry=REGISTRY,
)

# --- outbound -----------------------------------------------------------
deliveries = Counter(
    "mmp_deliveries_total",
    "Outbound delivery attempts.",
    ["kind", "outcome"],  # postback|webhook by delivered|failed|abandoned|blocked
    registry=REGISTRY,
)
delivery_latency = Histogram(
    "mmp_delivery_duration_seconds",
    "Time for an outbound delivery to complete.",
    ["kind"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)

# --- reconciliation -----------------------------------------------------
# The gap between what the edge accepted and what reached storage. Silent loss
# is the failure a measurement platform can least afford and least easily
# detect, and this is the only number that shows it.
pipeline_drift = Gauge(
    "mmp_pipeline_drift",
    "Accepted at the edge minus persisted, for the last completed hour.",
    ["stage"],
    registry=REGISTRY,
)

REJECTION_REASONS = (
    "unauthorized",
    "rate_limited",
    "payload_too_large",
    "invalid_json",
    "invalid_event",
    "batch_too_large",
    "replayed",
    "shed",
)


def render() -> bytes:
    return generate_latest(REGISTRY)


def observe_rejection(reason: str) -> None:
    """Count a rejection against a fixed vocabulary.

    Unknown reasons collapse into "other" rather than creating a new series.
    A label taken from an exception message is an unbounded label, and an
    unbounded label is a metrics outage waiting for the right bad request.
    """
    events_rejected.labels(reason=reason if reason in REJECTION_REASONS else "other").inc()


async def sample_stream_depths(redis: object, streams: dict[str, str]) -> None:
    """Refresh the backlog gauges.

    Sampled on scrape rather than maintained on every message: keeping a gauge
    accurate per message would put a Redis round trip on the ingest path to
    measure the ingest path.
    """
    for stream, group in streams.items():
        try:
            length = await redis.xlen(stream)  # type: ignore[attr-defined]
            stream_backlog.labels(stream=stream).set(length)
            summary = await redis.xpending(stream, group)  # type: ignore[attr-defined]
            pending = summary.get("pending", 0) if isinstance(summary, dict) else 0
            stream_pending.labels(stream=stream).set(pending)
        except Exception:
            # A metrics failure that takes down the thing being measured is a
            # famous own goal. Left at its last value, and logged at debug: at
            # scrape frequency a warning per failure would be its own incident.
            log.debug("stream_depth_sample_failed", stream=stream, exc_info=True)
            continue


def metrics_endpoint(before_render: Callable[[], object] | None = None):  # type: ignore[no-untyped-def]
    """A Starlette handler that renders the registry."""
    from starlette.requests import Request
    from starlette.responses import Response

    async def handler(_request: Request) -> Response:
        if before_render is not None:
            result = before_render()
            if hasattr(result, "__await__"):
                await result
        return Response(render(), media_type=CONTENT_TYPE)

    return handler
