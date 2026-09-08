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
from mmp_worker.consumers import ClickConsumer, EventConsumer
from mmp_worker.jobs import maintain_partitions, refresh_usage

log = get_logger(__name__)

PARTITION_INTERVAL = 3600.0
USAGE_INTERVAL = 60.0


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

        await stop.wait()
        log.info("worker_draining")
        await events.stop()
        await clicks.stop()

    await redis.aclose()
    await database.close()
    log.info(
        "worker_stopped",
        events=events.metrics.as_dict(),
        clicks=clicks.metrics.as_dict(),
    )


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
