"""Accepted-count bookkeeping for reconciliation.

The tracker knows how many events it accepted; the worker is the only service
that may write ``pipeline_audit``. Rather than widen the tracker's grants — it
holds INSERT on exactly two tables and SELECT on three, and that narrowness is
the point — the count travels through Redis.

Counters are incremented in memory and flushed periodically. A Redis round trip
per request to record that a request happened would be an audit that changes what
it measures.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Awaitable
from typing import Any, cast

from mmp_core.logging import get_logger
from redis.asyncio import Redis

log = get_logger(__name__)

KEY_PREFIX = "audit:accepted:"
# Long enough that the hourly reconciliation job cannot miss a bucket even after
# a lengthy outage, short enough that the keys do not accumulate.
TTL = dt.timedelta(days=2)


def _key(bucket_hour: dt.datetime) -> str:
    return f"{KEY_PREFIX}{bucket_hour:%Y%m%d%H}"


class AcceptedCounter:
    """In-memory counts, flushed to Redis on a timer."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._pending: dict[tuple[str, dt.datetime], int] = defaultdict(int)

    def record(self, app_id: str, count: int, *, now: dt.datetime | None = None) -> None:
        if count <= 0:
            return
        moment = (now or dt.datetime.now(dt.UTC)).replace(minute=0, second=0, microsecond=0)
        self._pending[(app_id, moment)] += count

    async def flush(self) -> int:
        """Push the accumulated counts. Safe to call when there is nothing.

        Counts are moved out of the buffer *before* the network call and put back
        on failure — the alternative loses them silently, which would make the
        reconciliation job report loss it caused itself.
        """
        if not self._pending:
            return 0
        batch = dict(self._pending)
        self._pending.clear()

        try:
            pipe = self._redis.pipeline(transaction=False)
            for (app_id, bucket), count in batch.items():
                pipe.hincrby(_key(bucket), app_id, count)
                pipe.expire(_key(bucket), int(TTL.total_seconds()))
            await pipe.execute()
        except Exception:
            for key, count in batch.items():
                self._pending[key] += count
            log.warning("accepted_counter_flush_failed", buckets=len(batch))
            return 0
        return sum(batch.values())


async def drain(redis: Redis, *, hours: int = 6) -> dict[tuple[str, dt.datetime], int]:
    """Read the counts the tracker recorded, for the worker to persist."""
    now = dt.datetime.now(dt.UTC).replace(minute=0, second=0, microsecond=0)
    counts: dict[tuple[str, dt.datetime], int] = {}
    for offset in range(hours + 1):
        bucket = now - dt.timedelta(hours=offset)
        # redis-py types every call for both sync and async modes, so the
        # return is a union the async client never actually produces.
        raw = await cast("Awaitable[dict[Any, Any]]", redis.hgetall(_key(bucket)))
        for app_id, value in (raw or {}).items():
            key = app_id.decode() if isinstance(app_id, bytes) else app_id
            counts[(key, bucket)] = int(value)
    return counts
