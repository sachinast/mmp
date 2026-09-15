"""What the live view needs that the database does not keep: rejections.

Everything else the live view shows — clicks, installs, events, postbacks — is
already persisted and is read back from the tables. A rejected request is not.
The SDK receives a 4xx, a metric increments, and nothing records which app it
came from or why. For someone integrating, that is the failure that matters
most: they fire an event, it never appears, and there is nothing to look at.

So the tracker keeps the last few rejections per app in a capped Redis list.

* **Written only on the failure path.** An accepted event pays nothing for this.
* **Bounded.** ``LTRIM`` caps the list and a TTL removes it; an app that stops
  sending costs nothing an hour later.
* **The reason, never the payload.** The body of a rejected request is exactly
  the data that failed validation — unknown size, unknown content, possibly
  personal data sent by mistake. What is kept is our own message about it.
* **Never raises.** Recording a rejection must not turn a 422 into a 500; a
  Redis failure here is logged and forgotten.

Both the tracker (which writes) and the API (which reads) use the key defined
here, and nowhere else. Several cache keys in this codebase were once written
out independently in each service; they agreed by inspection and nothing more.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import msgspec
from mmp_core.logging import get_logger
from redis.asyncio import Redis

log = get_logger(__name__)

REJECTIONS_PREFIX = "live:rejected:"
REJECTIONS_MAX = 50
REJECTIONS_TTL = dt.timedelta(hours=1)

# A client being rate limited sends a lot of requests — that is why it is being
# rate limited. Recording every one would turn an abusive burst into a Redis
# write storm, so rate limiting is noted at most once per window per app.
RATE_LIMIT_NOTE_WINDOW = dt.timedelta(seconds=10)

# Our own validation messages are short. Anything longer is truncated rather
# than trusted, since a decoder's message can quote the input it choked on.
MAX_DETAIL = 300


def rejections_key(app_id: str) -> str:
    return f"{REJECTIONS_PREFIX}{app_id}"


def _rate_limit_note_key(app_id: str) -> str:
    return f"{REJECTIONS_PREFIX}{app_id}:rate-limit-noted"


class Rejection(msgspec.Struct, frozen=True):
    at: str
    status: int
    reason: str
    detail: str
    events_in_batch: int | None = None
    source: str = "sdk"


_encoder = msgspec.json.Encoder()
_decoder = msgspec.json.Decoder(Rejection)


async def record_rejection(
    redis: Redis,
    app_id: str,
    *,
    status: int,
    reason: str,
    detail: str = "",
    events_in_batch: int | None = None,
    source: str = "sdk",
) -> None:
    """Keep a note that a request for this app was refused, and why."""
    try:
        if reason == "rate_limited":
            noted = await redis.set(
                _rate_limit_note_key(app_id),
                b"1",
                nx=True,
                ex=int(RATE_LIMIT_NOTE_WINDOW.total_seconds()),
            )
            if not noted:
                return

        entry = Rejection(
            at=dt.datetime.now(dt.UTC).isoformat(),
            status=status,
            reason=reason,
            detail=detail[:MAX_DETAIL],
            events_in_batch=events_in_batch,
            source=source,
        )
        key = rejections_key(app_id)
        async with redis.pipeline(transaction=False) as pipe:
            pipe.lpush(key, _encoder.encode(entry))
            pipe.ltrim(key, 0, REJECTIONS_MAX - 1)
            pipe.expire(key, int(REJECTIONS_TTL.total_seconds()))
            await pipe.execute()
    except Exception:
        log.warning("live_rejection_not_recorded", app_id=app_id, reason=reason, exc_info=True)


async def recent_rejections(
    redis: Redis, app_id: str, *, since: dt.datetime, limit: int = REJECTIONS_MAX
) -> list[dict[str, Any]]:
    """Rejections for an app at or after ``since``, newest first.

    An unreadable entry is skipped rather than failing the whole read: the live
    view is a debugging aid, and one bad entry should not blank it.
    """
    # redis-py types list commands as "awaitable or list" to serve both clients;
    # this is the asyncio client, so it is always the awaitable.
    raw: list[bytes] = await redis.lrange(rejections_key(app_id), 0, limit - 1)  # type: ignore[misc]
    entries: list[dict[str, Any]] = []
    for item in raw:
        try:
            entry = _decoder.decode(item)
        except msgspec.DecodeError:
            log.debug("live_rejection_unreadable", app_id=app_id)
            continue
        if dt.datetime.fromisoformat(entry.at) >= since:
            entries.append(msgspec.structs.asdict(entry))
    return entries
