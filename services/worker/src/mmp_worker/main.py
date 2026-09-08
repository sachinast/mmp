"""Worker entrypoint.

Runs the stream consumer alongside the scheduled jobs, under one supervisor with
one shutdown path. SIGTERM stops the consumer, which finishes its current batch,
drains what is already queued, and exits — so a deploy costs latency rather than
events.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
from collections.abc import Awaitable, Callable

from mmp_core.settings import Settings
from mmp_db.pool import Database
from redis.asyncio import Redis

from mmp_core import configure_logging, get_logger, load_settings
from mmp_worker.attribution import AttributionConsumer
from mmp_worker.consumers import ClickConsumer, EventConsumer
from mmp_worker.jobs import maintain_partitions, refresh_usage
from mmp_worker.postbacks import PostbackConsumer, retry_due
from mmp_worker.rollups import refresh_late_arrivals, refresh_trailing

log = get_logger(__name__)

PARTITION_INTERVAL = 3600.0
USAGE_INTERVAL = 60.0
ROLLUP_INTERVAL = 60.0
# Nightly in effect. Recomputing a week of buckets is not something to do every
# minute, and the events it catches are days old by definition.
LATE_ARRIVAL_INTERVAL = 6 * 3600.0
# The shortest backoff is ~30s, so sweeping every 15s means a retry fires close
# to when it was due rather than up to a full interval late.
RETRY_INTERVAL = 15.0


def consumer_name() -> str:
    """Stable per process, unique per instance.

    Redis tracks pending messages per consumer name. A name that changes on
    every restart would orphan the previous instance's pending entries, leaving
    them to be reclaimed only by the stalled-message sweep.
    """
    return f"{socket.gethostname()}:{os.getpid()}"


async def _every(
    interval: float, task: Callable[[], Awaitable[object]], *, name: str, stop: asyncio.Event
) -> None:
    while not stop.is_set():
        try:
            await task()
        except Exception:
            log.exception("scheduled_job_failed", job=name)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)


async def run(settings: Settings | None = None) -> None:
    settings = settings or load_settings(service_name="worker")
    configure_logging(service="worker", level=settings.log_level, json_output=settings.log_json)

    database = await Database.connect(settings, role="mmp_worker")
    redis = Redis.from_url(str(settings.redis_url), decode_responses=False)
    events = EventConsumer(redis=redis, database=database, consumer_name=consumer_name())
    # A separate consumer, so a slow or failing event batch cannot delay click
    # persistence. A late click is a missed attribution for every conversion
    # that follows it.
    clicks = ClickConsumer(redis=redis, database=database, consumer_name=consumer_name())
    # A third consumer group on the events stream. Independent cursor, so
    # attribution neither waits for persistence nor holds it up.
    attribution = AttributionConsumer(redis=redis, database=database, consumer_name=consumer_name())
    # Its own consumer group again: a conversion must reach an ad network
    # promptly, and networks optimise spend on these signals — a late postback
    # is spend misallocated.
    postbacks = PostbackConsumer(redis=redis, database=database, consumer_name=consumer_name())

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    # Ensure today's partitions exist before the first write, not on the first
    # failure.
    await maintain_partitions(database)

    log.info("worker_started", consumer=consumer_name(), version=settings.version)
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(events.run(), name="event-consumer")
        tasks.create_task(clicks.run(), name="click-consumer")
        tasks.create_task(attribution.run(), name="attribution-consumer")
        tasks.create_task(postbacks.run(), name="postback-consumer")
        tasks.create_task(
            _every(
                PARTITION_INTERVAL,
                lambda: maintain_partitions(database),
                name="partitions",
                stop=stop,
            ),
            name="partition-maintenance",
        )
        tasks.create_task(
            _every(USAGE_INTERVAL, lambda: refresh_usage(database), name="usage", stop=stop),
            name="usage-rollup",
        )
        tasks.create_task(
            _every(
                ROLLUP_INTERVAL,
                lambda: refresh_trailing(database),
                name="rollups",
                stop=stop,
            ),
            name="rollup-refresh",
        )
        tasks.create_task(
            _every(
                RETRY_INTERVAL,
                lambda: retry_due(database),
                name="postback-retries",
                stop=stop,
            ),
            name="postback-retry",
        )
        tasks.create_task(
            _every(
                LATE_ARRIVAL_INTERVAL,
                lambda: refresh_late_arrivals(database),
                name="late-arrivals",
                stop=stop,
            ),
            name="late-arrival-refresh",
        )

        await stop.wait()
        log.info("worker_draining")
        await events.stop()
        await clicks.stop()
        await attribution.stop()
        await postbacks.stop()

    await redis.aclose()
    await database.close()
    log.info(
        "worker_stopped",
        events=events.metrics.as_dict(),
        clicks=clicks.metrics.as_dict(),
        attribution=attribution.metrics.as_dict(),
        postbacks=postbacks.metrics.as_dict(),
    )


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
