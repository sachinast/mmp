"""Redis Streams as the ingest buffer.

Chosen over a plain list or a task queue for three properties that matter when
the thing in the queue is a customer's revenue data:

* **Consumer groups with explicit acknowledgement.** A worker that dies
  mid-batch leaves its messages pending, and another worker claims them. Nothing
  is lost because a process was rescheduled.
* **A visible backlog.** ``XLEN`` and the pending-entries list answer "are we
  behind, and by how much" without instrumentation of our own.
* **Replay.** The stream retains delivered messages, so a bug in the writer can
  be fixed and the affected window reprocessed — as long as it is caught inside
  the retention window.

Delivery is at-least-once. Exactly-once *counting* is produced at the database,
not hoped for here; see ``mmp_ingest.writer``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import msgspec
from mmp_core.logging import get_logger
from redis.asyncio import Redis

log = get_logger(__name__)

EVENTS_STREAM = "stream:events"
CLICKS_STREAM = "stream:clicks"
EVENTS_GROUP = "events-writer"
CLICKS_GROUP = "clicks-writer"

# A hard ceiling on stream memory. Redis is configured noeviction, so without a
# cap a stalled consumer would grow the stream until writes start failing —
# which is the correct failure, but a cap plus alerting is a better one.
# ~2M events at roughly 500 bytes is about 1 GB.
DEFAULT_MAXLEN = 2_000_000

_encoder = msgspec.msgpack.Encoder()

# msgpack rather than JSON: about 30% smaller on this shape, and meaningfully
# faster to decode in the worker's hot loop. Not pickle — a queue payload is
# attacker-adjacent data, and pickle would make it a code-execution path.
PAYLOAD_FIELD = b"d"


class StreamProducer:
    """Appends to a stream, pipelining a whole batch into one round trip."""

    def __init__(self, redis: Redis, *, stream: str, maxlen: int = DEFAULT_MAXLEN) -> None:
        self._redis = redis
        self._stream = stream
        self._maxlen = maxlen

    async def publish(self, payloads: Sequence[msgspec.Struct]) -> int:
        if not payloads:
            return 0
        pipe = self._redis.pipeline(transaction=False)
        for payload in payloads:
            pipe.xadd(
                self._stream,
                {PAYLOAD_FIELD: _encoder.encode(payload)},
                # approximate trimming: exact trimming makes XADD O(n) in the
                # number of entries removed, on the hot write path.
                maxlen=self._maxlen,
                approximate=True,
            )
        await pipe.execute()
        return len(payloads)

    async def depth(self) -> int:
        return int(await self._redis.xlen(self._stream))


class StreamConsumer[T]:
    """One member of a consumer group.

    Messages are acknowledged **after** the database transaction commits. The
    reverse order — ack then write — turns any crash between the two into
    silently lost events, which is the failure mode a measurement platform can
    least afford and least easily detect.
    """

    def __init__(
        self,
        redis: Redis,
        *,
        stream: str,
        group: str,
        consumer: str,
        decoder_type: type[T],
    ) -> None:
        self._redis = redis
        self._stream = stream
        self._group = group
        self._consumer = consumer
        self._decoder: msgspec.msgpack.Decoder[T] = msgspec.msgpack.Decoder(decoder_type)

    async def ensure_group(self) -> None:
        try:
            # mkstream so a fresh deployment does not need the stream to exist
            # first; id="0" so a group created after messages have arrived still
            # sees them rather than skipping to the tail.
            await self._redis.xgroup_create(self._stream, self._group, id="0", mkstream=True)
            log.info("consumer_group_created", stream=self._stream, group=self._group)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def read(self, *, count: int) -> list[tuple[str, T]]:
        """Poll for new messages. Returns immediately, empty if none.

        Deliberately non-blocking, i.e. no ``BLOCK`` argument.

        The natural implementation is a blocking read, which parks on the server
        until a message arrives or the timeout expires — one round trip instead
        of a polling loop. It is not used here because on Redis 8.10
        ``XREADGROUP ... BLOCK n`` does not return when the timeout expires: it
        blocks indefinitely. This reproduces in ``redis-cli``, so it is server
        behaviour rather than a client quirk, and a consumer built on it stalls
        the moment its stream goes quiet.

        Polling costs one extra round trip per idle interval — a few operations
        a second per worker — and behaves identically on every server version.
        The caller owns the idle backoff; see ``EventConsumer.run``.
        """
        response = await self._redis.xreadgroup(
            self._group,
            self._consumer,
            {self._stream: ">"},
            count=count,
        )
        return self._decode(response)

    async def claim_stalled(self, *, min_idle_ms: int, count: int) -> list[tuple[str, T]]:
        """Take over messages a dead consumer never acknowledged.

        Without this, a worker killed mid-batch leaves its messages pending
        forever: delivered, unacknowledged, and invisible to ``XREADGROUP``'s
        ">" cursor. The events would be in Redis and in no report.
        """
        _cursor, messages, _deleted = await self._redis.xautoclaim(
            self._stream,
            self._group,
            self._consumer,
            min_idle_time=min_idle_ms,
            count=count,
        )
        if messages:
            log.warning("claimed_stalled_messages", count=len(messages), group=self._group)
        return self._decode([(self._stream, messages)])

    def _decode(self, response: Any) -> list[tuple[str, T]]:
        decoded: list[tuple[str, T]] = []
        if not response:
            return decoded
        for _stream, messages in response:
            for message_id, fields in messages:
                raw = fields.get(PAYLOAD_FIELD) or fields.get(PAYLOAD_FIELD.decode())
                if raw is None:
                    log.warning("stream_message_missing_payload", message_id=message_id)
                    continue
                message_id_str = (
                    message_id.decode() if isinstance(message_id, bytes) else message_id
                )
                try:
                    decoded.append((message_id_str, self._decoder.decode(raw)))
                except msgspec.ValidationError:
                    # Undecodable means no future attempt will succeed either.
                    # Retrying forever would block the group's head.
                    log.exception("stream_message_undecodable", message_id=message_id_str)
        return decoded

    async def ack(self, message_ids: Sequence[str]) -> None:
        if message_ids:
            await self._redis.xack(self._stream, self._group, *message_ids)

    async def pending_count(self) -> int:
        summary = await self._redis.xpending(self._stream, self._group)
        return int(summary.get("pending", 0)) if isinstance(summary, dict) else 0
