"""The idempotency window at the edge.

The database primary key exactly deduplicates a *stream redelivery*, because the
edge stamps ``received_at`` and the queue carries it. It cannot deduplicate an
SDK that retries after a lost acknowledgement: that is a new HTTP request with a
new ``received_at``, and the same ``event_id``.

That case is real and common. The SDK removes an event from its offline queue
only after a 202, so any dropped response guarantees a retry — by design.

This module is the layer that catches it: a Redis set-if-absent per
``(app_id, event_id)`` with a TTL covering the SDK's own event-expiry window. It
is deliberately *not* presented as exact. Redis can lose a key to a failover or
a flush, and an event resent after the TTL will be counted twice. The honest
description is: exact for redelivery, best-effort over a bounded window for
client retries, with the residual rate measured by the reconciliation job rather
than assumed to be zero.
"""

from __future__ import annotations

import datetime as dt

from redis.asyncio import Redis

# Matches the SDK's default event expiry: an event older than this is dropped
# by the client, so it can never arrive to be deduplicated.
DEFAULT_WINDOW = dt.timedelta(days=7)

KEY_PREFIX = "seen:"


class IdempotencyWindow:
    def __init__(self, redis: Redis, *, window: dt.timedelta = DEFAULT_WINDOW) -> None:
        self._redis = redis
        self._ttl = int(window.total_seconds())

    @staticmethod
    def _key(app_id: str, event_id: str) -> str:
        return f"{KEY_PREFIX}{app_id}:{event_id}"

    async def filter_new(self, app_id: str, event_ids: list[str]) -> set[str]:
        """Return the subset not seen before, marking them as seen.

        One pipelined round trip for the whole batch. Doing this per event would
        add a network hop per event to the hot path — the cost the batching
        exists to avoid.
        """
        if not event_ids:
            return set()

        pipe = self._redis.pipeline(transaction=False)
        for event_id in event_ids:
            pipe.set(self._key(app_id, event_id), 1, ex=self._ttl, nx=True)
        results = await pipe.execute()

        # SET NX returns True when the key was absent, i.e. this is the first
        # time we have seen the event.
        return {event_id for event_id, was_new in zip(event_ids, results, strict=True) if was_new}

    async def forget(self, app_id: str, event_ids: list[str]) -> None:
        """Undo the marks for a batch that failed to enqueue.

        Without this, an event marked seen but never queued would be silently
        dropped on the SDK's retry — the one failure mode worse than a
        duplicate, because it is invisible.
        """
        if not event_ids:
            return
        await self._redis.delete(*[self._key(app_id, event_id) for event_id in event_ids])
