"""Scheduled background jobs.

Partition maintenance and usage metering. Both are boring by design and both
would be noticed only by their absence — which is the argument for running them
from the same supervised process as the consumer rather than from cron on some
box nobody remembers.
"""

from __future__ import annotations

import datetime as dt

from mmp_core.logging import get_logger
from mmp_db.maintenance import ensure_partitions
from mmp_db.pool import Database

log = get_logger(__name__)

# Roll up the trailing window every minute. Wide enough that an event arriving a
# little late still lands in a bucket that gets recomputed.
USAGE_LOOKBACK = dt.timedelta(hours=3)

USAGE_ROLLUP_SQL = """
INSERT INTO usage_rollup (id, organization_id, app_id, bucket_hour, metric, count)
SELECT
    gen_random_uuid(),
    organization_id,
    app_id,
    date_trunc('hour', received_at) AS bucket_hour,
    'events' AS metric,
    count(*) AS count
FROM events
WHERE received_at >= $1 AND received_at < $2
GROUP BY organization_id, app_id, date_trunc('hour', received_at)
ON CONFLICT (organization_id, app_id, bucket_hour, metric)
DO UPDATE SET count = EXCLUDED.count
"""

PIPELINE_AUDIT_SQL = """
INSERT INTO pipeline_audit (id, app_id, bucket_hour, stage, count)
SELECT
    gen_random_uuid(),
    app_id,
    date_trunc('hour', received_at),
    'persisted',
    count(*)
FROM events
WHERE received_at >= $1 AND received_at < $2
GROUP BY app_id, date_trunc('hour', received_at)
ON CONFLICT (app_id, bucket_hour, stage)
DO UPDATE SET count = EXCLUDED.count, recorded_at = now()
"""


async def maintain_partitions(database: Database, *, lead_days: int = 7) -> list[str]:
    async with database.system_connection() as conn:
        created = await ensure_partitions(conn, lead_days=lead_days)
    return created


async def refresh_usage(database: Database, *, lookback: dt.timedelta = USAGE_LOOKBACK) -> int:
    """Recompute billable volume for the trailing window.

    Recomputed rather than incremented: an idempotent recompute over a bounded
    window is safe to run twice, and safe to run after a worker crash. An
    incrementing counter is neither, and metering that double-counts after a
    restart becomes an invoice dispute.
    """
    now = dt.datetime.now(dt.UTC)
    start = (now - lookback).replace(minute=0, second=0, microsecond=0)
    async with database.system_connection() as conn, conn.transaction():
        await conn.execute(USAGE_ROLLUP_SQL, start, now)
        await conn.execute(PIPELINE_AUDIT_SQL, start, now)
    return 1
