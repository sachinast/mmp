"""The bounded buffer between the request handler and Redis.

The request handler does not await Redis. It appends to a deque and returns; a
background task ships the buffer in pipelined batches. Two reasons, and the
second is the important one:

* **Latency.** A round trip to Redis per request is a round trip we do not need
  to be inside the response.
* **Availability.** If Redis is slow or gone, awaiting it inside the handler
  makes every request slow or failed. With a buffer, ingestion degrades: the
  buffer fills, and only then do we shed. A tracking endpoint that returns 500
  because a queue is unavailable turns our incident into the customer's.

Shedding drops the **oldest** entries. Under sustained overload the newest data
is the data still worth having, and the oldest is closest to being stale anyway.
Every drop is counted, and the counter is what the reconciliation job compares
against — a shed event is a known loss, not a silent one.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Sequence

import msgspec
from mmp_core.logging import get_logger
from mmp_ingest.stream import StreamProducer

log = get_logger(__name__)

DEFAULT_CAPACITY = 50_000
DEFAULT_FLUSH_INTERVAL = 0.05  # 50ms
DEFAULT_FLUSH_SIZE = 500


class ShippingBuffer:
    def __init__(
        self,
        producer: StreamProducer,
        *,
        capacity: int = DEFAULT_CAPACITY,
        flush_interval: float = DEFAULT_FLUSH_INTERVAL,
        flush_size: int = DEFAULT_FLUSH_SIZE,
    ) -> None:
        self._producer = producer
        self._queue: deque[msgspec.Struct] = deque(maxlen=capacity)
        self._capacity = capacity
        self._flush_interval = flush_interval
        self._flush_size = flush_size
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._stopping = False

        self.dropped = 0
        self.shipped = 0
        self.failed_flushes = 0

    def append(self, payloads: Sequence[msgspec.Struct]) -> int:
        """Enqueue without awaiting. Returns how many were dropped."""
        dropped = 0
        for payload in payloads:
            if len(self._queue) >= self._capacity:
                # deque(maxlen) would drop silently; count it instead.
                self._queue.popleft()
                dropped += 1
            self._queue.append(payload)

        if dropped:
            self.dropped += dropped
            log.warning("ingest_buffer_shedding", dropped=dropped, capacity=self._capacity)
        if len(self._queue) >= self._flush_size:
            self._wake.set()
        return dropped

    @property
    def depth(self) -> int:
        return len(self._queue)

    @property
    def capacity(self) -> int:
        return self._capacity

    def snapshot(self) -> tuple[msgspec.Struct, ...]:
        """A copy of what is currently queued. For tests and diagnostics."""
        return tuple(self._queue)

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="ingest-buffer-flush")

    async def stop(self) -> None:
        """Drain before exiting.

        The last flush is what makes a rolling deploy lossless: without it, every
        instance discards up to a full buffer of accepted events on shutdown.
        """
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            await self._task
            self._task = None
        await self._flush_once()

    async def _run(self) -> None:
        while not self._stopping:
            # A timeout here is the normal case: it means the interval elapsed
            # without the buffer filling, which is exactly when a time-based
            # flush should happen.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._flush_interval)
            self._wake.clear()
            await self._flush_once()

    async def _flush_once(self) -> None:
        """Ship at most one chunk, then yield.

        The chunk is capped at ``flush_size`` rather than draining the whole
        buffer. A pipeline of a few thousand XADDs is a single long stretch of
        serialisation and one large write on a single-threaded event loop, and
        it showed up directly in the ingest p99: 2.3 ms median against a 17 ms
        tail, entirely from requests unlucky enough to arrive mid-flush.
        Chunking turns that into several short stalls the loop can interleave
        around. If more remains, the loop is woken again immediately.
        """
        if not self._queue:
            return
        batch = [self._queue.popleft() for _ in range(min(len(self._queue), self._flush_size))]
        try:
            await self._producer.publish(batch)
            self.shipped += len(batch)
            if self._queue:
                # More waiting: come back at once rather than after the timer.
                self._wake.set()
        except Exception:
            self.failed_flushes += 1
            # Put them back at the front, in order, so a transient Redis blip
            # costs latency rather than data.
            self._queue.extendleft(reversed(batch))
            log.exception("ingest_buffer_flush_failed", queued=len(self._queue))
