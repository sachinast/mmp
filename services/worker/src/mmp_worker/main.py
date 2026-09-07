"""Worker entrypoint.

Two kinds of background work live here and they are kept apart on purpose:

* **Stream consumers** (Phase 3+) read Redis Streams consumer groups and write
  batches to Postgres. They are throughput work, and they ack after commit.
* **arq jobs** (Phase 9+) are outbound IO — postbacks, webhooks — where the
  retry schedule and per-job state matter more than throughput.

Phase 0 runs neither. It proves the process starts, exposes a health port, and
exits cleanly on SIGTERM.
"""

from __future__ import annotations

import asyncio
import signal

from mmp_core import HealthRegistry, configure_logging, get_logger, load_settings

log = get_logger(__name__)


async def run() -> None:
    settings = load_settings(service_name="worker")
    configure_logging(service="worker", level=settings.log_level, json_output=settings.log_json)
    registry = HealthRegistry(service="worker", version=settings.version)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    log.info("worker_started", version=settings.version)
    await stop.wait()

    registry.start_draining()
    log.info("worker_draining")
    # Phase 3 drains in-flight batches here before returning.
    log.info("worker_stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
