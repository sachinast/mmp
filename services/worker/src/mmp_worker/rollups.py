"""Rollup refresh jobs.

Two cadences, for two different problems:

* **The trailing refresh** runs every minute over the last few hours. This is
  what makes the dashboard feel live.
* **The late-arrival refresh** runs nightly over the last three days. The SDK's
  offline queue holds events for up to seven days, so a device that was on a
  plane arrives long after its bucket was first computed. Without this pass those
  events would be in the raw table and in no report — present in the data,
  absent from every number anyone looks at, which is the worst possible place
  for them to be.

Both passes recompute rather than increment, so running them over the same
window twice is a no-op rather than a doubling.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from mmp_core.logging import get_logger
from mmp_db.pool import Database
from mmp_db.rollups import (
    REFRESH_CAMPAIGN_DAILY,
    REFRESH_CLICKS_HOURLY,
    REFRESH_EVENTS_HOURLY,
)

log = get_logger(__name__)

# Wide enough that an event arriving a few minutes late still lands in a bucket
# that gets recomputed before anyone looks at it.
TRAILING_WINDOW = dt.timedelta(hours=3)

# Matches the SDK's offline event expiry, plus a day of slack. An event older
# than its own client-side expiry can never arrive, so there is nothing beyond
# this worth recomputing.
LATE_ARRIVAL_WINDOW = dt.timedelta(days=8)

# The campaign rollup pays for a join between events and attributions, so it is
# refreshed less often than the cheap ones.
CAMPAIGN_WINDOW = dt.timedelta(days=2)


@dataclass(frozen=True)
class RefreshResult:
    window_start: dt.datetime
    window_end: dt.datetime
    duration_ms: int

    def as_dict(self) -> dict[str, object]:
        return {
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "duration_ms": self.duration_ms,
        }


async def refresh_trailing(
    database: Database,
    *,
    now: dt.datetime | None = None,
    window: dt.timedelta = TRAILING_WINDOW,
    campaign_window: dt.timedelta = CAMPAIGN_WINDOW,
) -> RefreshResult:
    """Recompute the recent past. Runs every minute."""
    import time

    now = now or dt.datetime.now(dt.UTC)
    # Truncated to the hour so a bucket is always recomputed in full. A window
    # starting mid-hour would write a partial count for that bucket and leave it
    # there until the next run happened to cover the whole hour.
    start = (now - window).replace(minute=0, second=0, microsecond=0)
    end = now + dt.timedelta(minutes=1)

    started = time.perf_counter()
    async with database.system_connection() as conn:
        async with conn.transaction():
            await conn.execute(REFRESH_EVENTS_HOURLY, start, end)
            await conn.execute(REFRESH_CLICKS_HOURLY, start, end)
        # Separate transaction: the campaign rollup is the expensive one, and
        # holding the cheap refreshes open while it runs would extend their
        # locks for no reason.
        campaign_start = (now - campaign_window).replace(hour=0, minute=0, second=0, microsecond=0)
        async with conn.transaction():
            await conn.execute(REFRESH_CAMPAIGN_DAILY, campaign_start, end)

    duration_ms = int((time.perf_counter() - started) * 1000)
    result = RefreshResult(window_start=start, window_end=end, duration_ms=duration_ms)

    # The number to watch as volume grows. When this stops fitting comfortably
    # inside the refresh interval, that is the signal to add a columnar sink —
    # not a signal to add another index.
    if duration_ms > 30_000:
        log.warning("rollup_refresh_slow", **result.as_dict())
    else:
        log.info("rollup_refresh", **result.as_dict())
    return result


async def refresh_late_arrivals(
    database: Database,
    *,
    now: dt.datetime | None = None,
    window: dt.timedelta = LATE_ARRIVAL_WINDOW,
) -> RefreshResult:
    """Recompute the window an offline SDK queue can still deliver into.

    Runs nightly. Without it, a week of events from a device that was offline
    would sit in the raw partitions and appear in no report — present in the
    data and absent from every number anyone looks at.
    """
    import time

    now = now or dt.datetime.now(dt.UTC)
    start = (now - window).replace(hour=0, minute=0, second=0, microsecond=0)
    end = now + dt.timedelta(minutes=1)

    started = time.perf_counter()
    async with database.system_connection() as conn:
        async with conn.transaction():
            await conn.execute(REFRESH_EVENTS_HOURLY, start, end)
            await conn.execute(REFRESH_CLICKS_HOURLY, start, end)
        async with conn.transaction():
            await conn.execute(REFRESH_CAMPAIGN_DAILY, start, end)

    result = RefreshResult(
        window_start=start,
        window_end=end,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
    log.info("rollup_late_arrival_refresh", **result.as_dict())
    return result
