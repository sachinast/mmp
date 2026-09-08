"""Session lifecycle.

A session is a run of activity by one device with no gap longer than the app's
timeout. The SDK does not decide this — it reports events, and the platform
decides where the boundaries fall. That has to be server-side or two devices with
different clock behaviour would produce incomparable session counts.

State lives in Redis and is genuinely ephemeral: the current session id for a
device, with a TTL equal to the timeout. Expiry *is* the session ending. Nothing
scans for stale sessions, because a scan over every active device is exactly the
operation that stops working at scale.

The durable record is the events themselves — ``session_start``, the events
carrying the session id, and ``session_end``. Redis holds only what is needed to
decide the next event's session, and it can be lost: the cost of losing it is
some sessions counted as two, not data loss.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from mmp_core.ids import uuid7
from redis.asyncio import Redis

SESSION_PREFIX = "sess:"
DEFAULT_TIMEOUT = dt.timedelta(minutes=30)

# Events that do not extend a session.
#
# A background push receipt or a silent sync should not keep a session alive for
# another thirty minutes — the person is not using the app. Counting those as
# engagement inflates session length, which is a metric advertisers optimise
# against.
NON_ENGAGEMENT_EVENTS = frozenset({"session_end", "push_received", "background_sync"})


@dataclass(frozen=True)
class SessionDecision:
    session_id: uuid.UUID
    started: bool
    previous_session_id: uuid.UUID | None = None


def _key(app_id: str, anonymous_id: str) -> str:
    return f"{SESSION_PREFIX}{app_id}:{anonymous_id}"


class SessionTracker:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def resolve(
        self,
        *,
        app_id: str,
        anonymous_id: str,
        event_name: str,
        session_timeout: dt.timedelta = DEFAULT_TIMEOUT,
    ) -> SessionDecision:
        """Return the session this event belongs to, starting one if needed.

        A single Redis round trip in the common case: SET NX creates a session
        if none exists, and its return value tells us whether we created it. The
        obvious GET-then-SET has a race between the two where two events from the
        same device create two sessions.
        """
        key = _key(app_id, anonymous_id)
        seconds = int(session_timeout.total_seconds())

        if event_name in NON_ENGAGEMENT_EVENTS:
            existing = await self._redis.get(key)
            if existing:
                return SessionDecision(session_id=self._to_uuid(existing), started=False)
            # No live session and nothing here should start one.
            return SessionDecision(session_id=uuid7(), started=False)

        candidate = uuid7()
        created = await self._redis.set(key, str(candidate), ex=seconds, nx=True)
        if created:
            return SessionDecision(session_id=candidate, started=True)

        # A session was already live: adopt it and push its expiry out.
        existing = await self._redis.get(key)
        if existing is None:
            # It expired in the gap between SET NX and GET. Rare, and the right
            # answer is a new session — the previous one had already ended.
            await self._redis.set(key, str(candidate), ex=seconds)
            return SessionDecision(session_id=candidate, started=True)

        session_id = uuid.UUID(existing.decode() if isinstance(existing, bytes) else existing)
        await self._redis.expire(key, seconds)
        return SessionDecision(session_id=session_id, started=False)

    @staticmethod
    def _to_uuid(raw: bytes | str) -> uuid.UUID:
        return uuid.UUID(raw.decode() if isinstance(raw, bytes) else raw)

    async def resolve_many(
        self,
        *,
        app_id: str,
        devices: Sequence[tuple[str, str]],
        session_timeout: dt.timedelta = DEFAULT_TIMEOUT,
    ) -> dict[str, SessionDecision]:
        """Resolve one session per distinct device, in a single round trip.

        The per-event version costs a Redis round trip each, and a batch is
        twenty events — which measured as ingest p50 going from 2.4 ms to
        6.8 ms, nearly tripling the endpoint's latency. Since a batch is almost
        always one device, and always a handful, the whole batch is one
        pipelined SET NX followed by one pipelined read of whatever already
        existed.

        ``devices`` is (anonymous_id, event_name) pairs, deduplicated by the
        caller on anonymous_id.
        """
        if not devices:
            return {}

        seconds = int(session_timeout.total_seconds())
        engaged = [
            (anonymous_id, name)
            for anonymous_id, name in devices
            if name not in NON_ENGAGEMENT_EVENTS
        ]
        idle = [anonymous_id for anonymous_id, name in devices if name in NON_ENGAGEMENT_EVENTS]

        decisions: dict[str, SessionDecision] = {}
        candidates = {anonymous_id: uuid7() for anonymous_id, _ in engaged}

        if engaged:
            pipe = self._redis.pipeline(transaction=False)
            for anonymous_id, _ in engaged:
                pipe.set(
                    _key(app_id, anonymous_id),
                    str(candidates[anonymous_id]),
                    ex=seconds,
                    nx=True,
                )
            created = await pipe.execute()

            # Whoever lost the SET NX already had a session; read them all back
            # together and push their expiry out in the same trip.
            adopted = [
                anonymous_id
                for (anonymous_id, _), was_created in zip(engaged, created, strict=True)
                if not was_created
            ]
            existing_values: list[bytes | str | None] = []
            if adopted:
                pipe = self._redis.pipeline(transaction=False)
                for anonymous_id in adopted:
                    pipe.get(_key(app_id, anonymous_id))
                    pipe.expire(_key(app_id, anonymous_id), seconds)
                results = await pipe.execute()
                existing_values = results[0::2]

            adopted_iter = iter(zip(adopted, existing_values, strict=True))
            for (anonymous_id, _), was_created in zip(engaged, created, strict=True):
                if was_created:
                    decisions[anonymous_id] = SessionDecision(
                        session_id=candidates[anonymous_id], started=True
                    )
            for anonymous_id, raw in adopted_iter:
                if raw is None:
                    # Expired between the SET NX and the GET. Rare, and a new
                    # session is the right answer — the old one had ended.
                    decisions[anonymous_id] = SessionDecision(
                        session_id=candidates[anonymous_id], started=True
                    )
                    continue
                decisions[anonymous_id] = SessionDecision(
                    session_id=uuid.UUID(raw.decode() if isinstance(raw, bytes) else raw),
                    started=False,
                )

        if idle:
            pipe = self._redis.pipeline(transaction=False)
            for anonymous_id in idle:
                pipe.get(_key(app_id, anonymous_id))
            for anonymous_id, raw in zip(idle, await pipe.execute(), strict=True):
                decisions[anonymous_id] = SessionDecision(
                    session_id=self._to_uuid(raw) if raw else uuid7(),
                    started=False,
                )

        return decisions

    async def end(self, *, app_id: str, anonymous_id: str) -> uuid.UUID | None:
        """Close a session explicitly, when the SDK reports the app backgrounded."""
        key = _key(app_id, anonymous_id)
        existing = await self._redis.get(key)
        if existing is None:
            return None
        await self._redis.delete(key)
        return self._to_uuid(existing)

    async def current(self, *, app_id: str, anonymous_id: str) -> uuid.UUID | None:
        existing = await self._redis.get(_key(app_id, anonymous_id))
        if existing is None:
            return None
        return self._to_uuid(existing)
