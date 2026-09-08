"""Counting both ends of the pipeline.

Silent loss is the failure a measurement platform can least afford and least
easily detect. Everything else announces itself: a crashed worker pages, a
failing postback shows in a delivery log, a slow query shows in latency. Events
that are accepted and never stored produce **no signal at all** — the customer's
numbers are simply lower than they should be, and nobody knows until an
advertiser reconciles against their ad network and finds a gap.

The only defence is to count at both ends and compare. This job writes what the
edge accepted and what reached storage into ``pipeline_audit``, hour by hour, and
reports the difference.

**Drift is not expected to be zero**, and a job that alerted on any difference
would be turned off within a week. Two sources of legitimate difference:

* **Deduplication.** The edge counts an accepted request; the writer drops
  redelivered and retried events. This is the large one and it is healthy.
* **The boundary.** An event accepted at 10:59:59.9 may be written at 11:00:00.1
  and land in the next hour's bucket.

So the job compares only completed hours, allows a tolerance, and reports the
rate rather than the raw count — a hundred missing events out of a hundred is an
outage, and out of ten million is a rounding difference at the boundary.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from mmp_core.logging import get_logger
from mmp_core.metrics import pipeline_drift
from mmp_db.pool import Database

log = get_logger(__name__)

# Only completed hours, and not the one that just ended: a batch accepted at
# 10:59:59 may still be in a buffer, in a stream, or in a worker's current
# transaction. Comparing the hour that ended a minute ago reports the pipeline's
# own latency as loss.
SETTLING_PERIOD = dt.timedelta(minutes=10)

# Below this the difference is noise: a handful of events either side of an hour
# boundary. Above it, on a meaningful volume, something is wrong.
DRIFT_TOLERANCE = 0.01  # 1%
MINIMUM_VOLUME = 100  # below this, a rate is not meaningful

RECORD_PERSISTED_SQL = """
INSERT INTO pipeline_audit (id, app_id, bucket_hour, stage, count)
SELECT gen_random_uuid(), app_id, date_trunc('hour', received_at), 'persisted', count(*)
FROM events
WHERE received_at >= $1 AND received_at < $2
GROUP BY app_id, date_trunc('hour', received_at)
ON CONFLICT (app_id, bucket_hour, stage)
DO UPDATE SET count = EXCLUDED.count, recorded_at = now()
"""

RECORD_CLICKS_SQL = """
INSERT INTO pipeline_audit (id, app_id, bucket_hour, stage, count)
SELECT gen_random_uuid(), app_id, date_trunc('hour', clicked_at), 'clicks_persisted',
       count(*)
FROM clicks
WHERE clicked_at >= $1 AND clicked_at < $2
GROUP BY app_id, date_trunc('hour', clicked_at)
ON CONFLICT (app_id, bucket_hour, stage)
DO UPDATE SET count = EXCLUDED.count, recorded_at = now()
"""

COMPARE_SQL = """
SELECT
    a.app_id,
    a.bucket_hour,
    max(a.count) FILTER (WHERE a.stage = 'accepted')  AS accepted,
    max(a.count) FILTER (WHERE a.stage = 'persisted') AS persisted
FROM pipeline_audit a
WHERE a.bucket_hour >= $1 AND a.bucket_hour < $2
  AND a.stage IN ('accepted', 'persisted')
GROUP BY a.app_id, a.bucket_hour
HAVING max(a.count) FILTER (WHERE a.stage = 'accepted') IS NOT NULL
"""


@dataclass(frozen=True)
class Discrepancy:
    app_id: object
    bucket_hour: dt.datetime
    accepted: int
    persisted: int

    @property
    def missing(self) -> int:
        return self.accepted - self.persisted

    @property
    def rate(self) -> float:
        return self.missing / self.accepted if self.accepted else 0.0

    @property
    def significant(self) -> bool:
        """Worth waking someone for.

        Both conditions, not either: a 50% rate on four events is two events,
        and a hundred missing out of ten million is the hour boundary.
        """
        return self.accepted >= MINIMUM_VOLUME and self.rate > DRIFT_TOLERANCE


@dataclass(frozen=True)
class ReconciliationResult:
    hours_checked: int
    discrepancies: list[Discrepancy]
    worst_rate: float

    def as_dict(self) -> dict[str, object]:
        return {
            "hours_checked": self.hours_checked,
            "discrepancies": len(self.discrepancies),
            "worst_rate": round(self.worst_rate, 5),
        }


async def record_accepted(
    database: Database, *, app_id: object, bucket_hour: dt.datetime, count: int
) -> None:
    """Record what the edge accepted for one app-hour.

    Called by the tracker's periodic flush rather than per request: an audit
    write on the ingest path would be an audit that changes what it measures.
    """
    async with database.system_connection() as conn:
        await conn.execute(
            """
            INSERT INTO pipeline_audit (id, app_id, bucket_hour, stage, count)
            VALUES (gen_random_uuid(), $1, $2, 'accepted', $3)
            ON CONFLICT (app_id, bucket_hour, stage)
            DO UPDATE SET count = EXCLUDED.count, recorded_at = now()
            """,
            app_id,
            bucket_hour,
            count,
        )


async def reconcile(
    database: Database,
    redis: object | None = None,
    *,
    now: dt.datetime | None = None,
    lookback: dt.timedelta = dt.timedelta(hours=6),
) -> ReconciliationResult:
    """Count both ends over the recent completed hours and report the gap.

    The accepted side is drained from Redis, where the tracker leaves it: the
    tracker holds INSERT on two tables and SELECT on three, and widening that so
    it could write an audit row would trade a real privilege boundary for a
    bookkeeping convenience.
    """
    now = now or dt.datetime.now(dt.UTC)

    if redis is not None:
        from mmp_ingest.audit import drain

        for (app_id, bucket), count in (await drain(redis)).items():  # type: ignore[arg-type]
            await record_accepted(
                database, app_id=uuid.UUID(app_id), bucket_hour=bucket, count=count
            )
    end = (now - SETTLING_PERIOD).replace(minute=0, second=0, microsecond=0)
    start = end - lookback

    async with database.system_connection() as conn:
        async with conn.transaction():
            await conn.execute(RECORD_PERSISTED_SQL, start, end)
            await conn.execute(RECORD_CLICKS_SQL, start, end)
        rows = await conn.fetch(COMPARE_SQL, start, end)

    discrepancies = []
    worst = 0.0
    for row in rows:
        item = Discrepancy(
            app_id=row["app_id"],
            bucket_hour=row["bucket_hour"],
            accepted=row["accepted"] or 0,
            persisted=row["persisted"] or 0,
        )
        worst = max(worst, item.rate)
        if item.significant:
            discrepancies.append(item)
            log.error(
                "pipeline_drift_detected",
                app_id=str(item.app_id),
                bucket_hour=item.bucket_hour.isoformat(),
                accepted=item.accepted,
                persisted=item.persisted,
                missing=item.missing,
                rate=round(item.rate, 4),
            )

    pipeline_drift.labels(stage="events").set(worst)

    result = ReconciliationResult(
        hours_checked=len(rows), discrepancies=discrepancies, worst_rate=worst
    )
    log.info("reconciliation_complete", **result.as_dict())
    return result
