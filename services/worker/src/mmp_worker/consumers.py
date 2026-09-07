"""The event stream consumer.

The loop is: read a batch, write it in one transaction, then acknowledge. That
order is the whole design. Acknowledging first would make any crash between ack
and commit a silent loss of a customer's events — the failure a measurement
platform can least afford and least easily detect.

A message that fails repeatedly is moved to a dead-letter stream rather than
retried forever. One poison message at the head of a consumer group otherwise
stalls every event behind it, turning a single bad row into a total outage.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field

import msgspec
from mmp_core.logging import get_logger
from mmp_db.pool import Database
from mmp_ingest.schema import QueuedEvent
from mmp_ingest.stream import EVENTS_GROUP, EVENTS_STREAM, StreamConsumer
from mmp_ingest.writer import EventWriter
from redis.asyncio import Redis

log = get_logger(__name__)

DEAD_LETTER_STREAM = "stream:events:dead"
# Long enough that a slow batch is not stolen from a healthy worker, short
# enough that a crashed worker's messages are picked up within a minute.
STALLED_AFTER_MS = 60_000
MAX_ATTEMPTS = 5
DEAD_LETTER_MAXLEN = 100_000

_encoder = msgspec.msgpack.Encoder()


@dataclass
class ConsumerMetrics:
    batches: int = 0
    events_received: int = 0
    events_written: int = 0
    duplicates: int = 0
    dead_lettered: int = 0
    failures: int = 0
    last_batch_at: dt.datetime | None = field(default=None)

    def as_dict(self) -> dict[str, object]:
        return {
            "batches": self.batches,
            "events_received": self.events_received,
            "events_written": self.events_written,
            "duplicates": self.duplicates,
            "dead_lettered": self.dead_lettered,
            "failures": self.failures,
            "last_batch_at": self.last_batch_at.isoformat() if self.last_batch_at else None,
        }


class EventConsumer:
    def __init__(
        self,
        *,
        redis: Redis,
        database: Database,
        consumer_name: str,
        batch_size: int = 5000,
        idle_sleep: float = 0.1,
    ) -> None:
        self._redis = redis
        self._database = database
        self._batch_size = batch_size
        # How long to wait after an empty poll. Blocking reads are not used —
        # see StreamConsumer.read for why — so this is the queue's idle latency.
        self._idle_sleep = idle_sleep
        self._consumer: StreamConsumer[QueuedEvent] = StreamConsumer(
            redis,
            stream=EVENTS_STREAM,
            group=EVENTS_GROUP,
            consumer=consumer_name,
            decoder_type=QueuedEvent,
        )
        self._writer = EventWriter()
        self._attempts: dict[str, int] = {}
        self.metrics = ConsumerMetrics()
        self._stopping = False

    async def start(self) -> None:
        await self._consumer.ensure_group()

    async def stop(self) -> None:
        self._stopping = True

    async def run(self) -> None:
        """Consume until stopped, draining what is already queued on the way out."""
        await self.start()
        while not self._stopping:
            processed = await self.run_once()
            if processed == 0:
                # Nothing pending: reclaim anything a dead worker left behind,
                # then back off rather than spinning on an empty stream.
                await self._reclaim()
                await asyncio.sleep(self._idle_sleep)
        # Drain: whatever arrived while we were shutting down is still ours.
        while await self.run_once():
            pass
        log.info("event_consumer_drained", **self.metrics.as_dict())

    async def run_once(self) -> int:
        messages = await self._consumer.read(count=self._batch_size)
        if not messages:
            return 0
        return await self._process(messages)

    async def _reclaim(self) -> None:
        messages = await self._consumer.claim_stalled(
            min_idle_ms=STALLED_AFTER_MS, count=self._batch_size
        )
        if messages:
            await self._process(messages)

    async def _process(self, messages: Sequence[tuple[str, QueuedEvent]]) -> int:
        message_ids = [message_id for message_id, _ in messages]
        events = [event for _, event in messages]

        try:
            async with self._database.acquire_raw() as conn:
                result = await self._writer.write(conn, events)
        except Exception:
            self.metrics.failures += 1
            log.exception("event_batch_write_failed", batch_size=len(events))
            await self._handle_failure(message_ids, events)
            return len(messages)

        # Acknowledged only now — after the transaction committed.
        await self._consumer.ack(message_ids)
        for message_id in message_ids:
            self._attempts.pop(message_id, None)

        self.metrics.batches += 1
        self.metrics.events_received += result.received
        self.metrics.events_written += result.inserted
        self.metrics.duplicates += result.duplicates
        self.metrics.last_batch_at = dt.datetime.now(dt.UTC)
        return len(messages)

    async def _handle_failure(self, message_ids: list[str], events: list[QueuedEvent]) -> None:
        """Retry, then dead-letter.

        The batch is left unacknowledged so it is redelivered. Once a message
        has failed MAX_ATTEMPTS times it is moved aside: one undigestible row
        must not be able to stop every event behind it.
        """
        exhausted: list[str] = []
        for message_id, event in zip(message_ids, events, strict=True):
            attempts = self._attempts.get(message_id, 0) + 1
            self._attempts[message_id] = attempts
            if attempts >= MAX_ATTEMPTS:
                await self._redis.xadd(
                    DEAD_LETTER_STREAM,
                    {
                        b"d": _encoder.encode(event),
                        b"attempts": str(attempts).encode(),
                        b"failed_at": dt.datetime.now(dt.UTC).isoformat().encode(),
                    },
                    maxlen=DEAD_LETTER_MAXLEN,
                    approximate=True,
                )
                exhausted.append(message_id)
                self._attempts.pop(message_id, None)

        if exhausted:
            # Acknowledged so the group moves on; the events are preserved in
            # the dead-letter stream for inspection and manual replay.
            await self._consumer.ack(exhausted)
            self.metrics.dead_lettered += len(exhausted)
            log.error(
                "events_dead_lettered",
                count=len(exhausted),
                stream=DEAD_LETTER_STREAM,
                max_attempts=MAX_ATTEMPTS,
            )
        else:
            # Back off before the redelivery so a database outage is not turned
            # into a tight retry loop against a database that is already unwell.
            await asyncio.sleep(min(2.0, 0.2 * max(self._attempts.values(), default=1)))
