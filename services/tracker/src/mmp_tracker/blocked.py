"""Blocked event names, resolved on the ingest path.

Budget: nothing, usually. The names an app has blocked are held in process for
thirty seconds, so a batch costs a dictionary lookup. A miss costs one Redis GET,
where the API publishes the set whenever a definition changes; only if Redis has
no entry at all — a cold cache, or a flush — does this read Postgres, through
the three-column grant the tracker holds on ``event_definitions``.

Matching is on the folded name, like the SDK's reserved-name check: blocking
``Add To Cart`` blocks ``add_to_cart``. A block that depended on exact
capitalisation would be a block someone could step around by accident.
"""

from __future__ import annotations

import datetime as dt
import time

import msgspec
from mmp_db.pool import Database
from mmp_ingest.catalogue import blocked_events_cache_key
from mmp_ingest.schema import canonical_event_name
from redis.asyncio import Redis

IN_PROCESS_TTL = 30.0
# Long, because the API rewrites the entry on every change; this only bounds
# how long a stale entry survives if the API's write was lost.
REDIS_TTL = dt.timedelta(hours=1)

BLOCKED_SQL = "SELECT name FROM event_definitions WHERE app_id = $1 AND status = 'blocked'"

_encoder = msgspec.json.Encoder()
_decoder = msgspec.json.Decoder(list[str])


class BlockedEvents:
    def __init__(self, database: Database, redis: Redis) -> None:
        self._database = database
        self._redis = redis
        self._cache: dict[str, tuple[frozenset[str], float]] = {}

    async def names(self, app_id: str) -> frozenset[str]:
        """The folded names blocked for an app. Empty when none are."""
        cached = self._cache.get(app_id)
        now = time.monotonic()
        if cached and cached[1] > now:
            return cached[0]

        raw = await self._redis.get(blocked_events_cache_key(app_id))
        if raw is None:
            names = await self._load(app_id)
        else:
            names = _decoder.decode(raw if isinstance(raw, bytes) else raw.encode())

        folded = frozenset(canonical_event_name(name) for name in names)
        self._cache[app_id] = (folded, now + IN_PROCESS_TTL)
        return folded

    async def _load(self, app_id: str) -> list[str]:
        async with self._database.acquire_raw() as conn:
            rows = await conn.fetch(BLOCKED_SQL, app_id)
        names = [row["name"] for row in rows]
        await self._redis.set(
            blocked_events_cache_key(app_id),
            _encoder.encode(names),
            ex=int(REDIS_TTL.total_seconds()),
        )
        return names

    def forget(self, app_id: str) -> None:
        """Drop the in-process entry. For tests; production relies on the TTL."""
        self._cache.pop(app_id, None)
