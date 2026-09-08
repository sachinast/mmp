"""The traffic-level fraud sweep.

Runs periodically over a trailing window and scores each tracking link against
the rules that need a population rather than a single install — flooding, click
farms, one device credited with many installs. The per-install rules already ran
inline at attribution; this is the half that cannot be judged one row at a time.

**One query, not one per link.** The statistics are computed in the database and
come back as a handful of rows per app. Pulling installs into Python to count
them is how a fraud job becomes the reason the database is slow, and this job
reads the same tables the ingest path writes.

**Idempotent.** A sweep is exactly the kind of job that gets re-run after a
crash or a deploy, and its output is shown to customers. Findings upsert on
(app, link, rule, window), so a re-run corrects a finding instead of duplicating
it — and a link that has since gone quiet has its stale finding removed rather
than left standing as an accusation nobody rechecked.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field

from mmp_attrib.fraud import TrafficWindow, assess_traffic
from mmp_core.ids import uuid7
from mmp_core.logging import get_logger
from mmp_core.metrics import fraud_findings as fraud_findings_metric
from mmp_db.pool import Database

log = get_logger(__name__)

# A week, because the rules ask about behaviour over a week — the late-conversion
# threshold is seven days, so a shorter window could not observe one.
SWEEP_WINDOW = dt.timedelta(days=7)

# Statistics per tracking link. The joins are to `clicks` on the winning click,
# which is indexed by click_id, so this reads attributions for the window and
# looks up each one's click rather than scanning the click partitions.
STATS_SQL = """
WITH scored AS (
    SELECT
        a.organization_id,
        a.app_id,
        a.tracking_link_id,
        a.campaign_id,
        c.device_hash,
        c.ip_hash,
        (a.installed_at - c.clicked_at) AS gap
    FROM attributions a
    JOIN clicks c ON c.click_id = a.click_id
    WHERE a.installed_at >= $1
      AND a.installed_at < $2
      AND a.superseded_by IS NULL
      AND a.click_id IS NOT NULL
),
totals AS (
    SELECT
        organization_id,
        app_id,
        tracking_link_id,
        -- No min(uuid) in PostgreSQL; every row for a link carries the same
        -- campaign anyway, so any one of them is the answer.
        (array_agg(campaign_id))[1] AS campaign_id,
        count(*) AS attributed_installs,
        count(*) FILTER (WHERE gap > $3) AS late_installs
    FROM scored
    GROUP BY organization_id, app_id, tracking_link_id
),
-- Each of these collapses to one row per link BEFORE being joined. Joining the
-- per-device counts straight onto `scored` would multiply every install by the
-- number of distinct devices on its link and inflate the totals enormously —
-- which is a way to invent a fraud finding out of arithmetic.
per_device AS (
    SELECT tracking_link_id, max(installs) AS max_installs_per_device
    FROM (
        SELECT tracking_link_id, device_hash, count(*) AS installs
        FROM scored
        WHERE device_hash IS NOT NULL
        GROUP BY tracking_link_id, device_hash
    ) d
    GROUP BY tracking_link_id
),
per_ip AS (
    SELECT tracking_link_id, max(devices) AS max_devices_per_ip
    FROM (
        SELECT tracking_link_id, ip_hash, count(DISTINCT device_hash) AS devices
        FROM scored
        WHERE ip_hash IS NOT NULL
        GROUP BY tracking_link_id, ip_hash
    ) p
    GROUP BY tracking_link_id
)
SELECT
    t.organization_id,
    t.app_id,
    t.tracking_link_id,
    t.campaign_id,
    t.attributed_installs,
    t.late_installs,
    COALESCE(d.max_installs_per_device, 0) AS max_installs_per_device,
    COALESCE(p.max_devices_per_ip, 0) AS max_devices_per_ip
FROM totals t
LEFT JOIN per_device d ON d.tracking_link_id = t.tracking_link_id
LEFT JOIN per_ip p ON p.tracking_link_id = t.tracking_link_id
"""

UPSERT_SQL = """
INSERT INTO fraud_findings (
    id, organization_id, app_id, tracking_link_id, campaign_id,
    window_start, window_end, rule, severity, detail, evidence, created_at
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, now())
ON CONFLICT (app_id, tracking_link_id, rule, window_start)
DO UPDATE SET
    severity = EXCLUDED.severity,
    detail = EXCLUDED.detail,
    evidence = EXCLUDED.evidence,
    window_end = EXCLUDED.window_end
"""

# A link that stopped misbehaving must stop being accused. Without this a
# finding written once would stand for as long as the row was retained, and the
# first thing a customer would learn is that our fraud reporting is stale.
CLEAR_SQL = """
DELETE FROM fraud_findings
WHERE window_start = $1
  AND app_id = $2
  AND tracking_link_id IS NOT DISTINCT FROM $3
  AND rule <> ALL($4::text[])
"""


@dataclass
class SweepResult:
    links_examined: int = 0
    findings_written: int = 0
    by_rule: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "links_examined": self.links_examined,
            "findings_written": self.findings_written,
            "by_rule": dict(self.by_rule),
        }


async def sweep(
    database: Database,
    *,
    now: dt.datetime,
    window: dt.timedelta = SWEEP_WINDOW,
    late_threshold: dt.timedelta | None = None,
) -> SweepResult:
    """Score every tracking link with attributed traffic in the window.

    ``now`` is a parameter, not a call to the clock, so the sweep can be run
    over a past window to reproduce a finding someone is disputing.
    """
    from mmp_attrib.fraud import LATE_CONVERSION_THRESHOLD

    late = late_threshold if late_threshold is not None else LATE_CONVERSION_THRESHOLD
    window_start = now - window
    result = SweepResult()

    async with database.acquire_raw() as conn:
        rows = await conn.fetch(STATS_SQL, window_start, now, late)

        for row in rows:
            result.links_examined += 1
            assessment = assess_traffic(
                TrafficWindow(
                    attributed_installs=row["attributed_installs"],
                    late_installs=row["late_installs"],
                    max_devices_per_ip=row["max_devices_per_ip"],
                    max_installs_per_device=row["max_installs_per_device"],
                )
            )

            evidence = json.dumps(
                {
                    "attributed_installs": row["attributed_installs"],
                    "late_installs": row["late_installs"],
                    "max_devices_per_ip": row["max_devices_per_ip"],
                    "max_installs_per_device": row["max_installs_per_device"],
                }
            )

            for signal in assessment.signals:
                await conn.execute(
                    UPSERT_SQL,
                    uuid7(),
                    row["organization_id"],
                    row["app_id"],
                    row["tracking_link_id"],
                    row["campaign_id"],
                    window_start,
                    now,
                    str(signal.rule),
                    int(signal.severity),
                    signal.detail,
                    evidence,
                )
                result.findings_written += 1
                rule = str(signal.rule)
                result.by_rule[rule] = result.by_rule.get(rule, 0) + 1
                fraud_findings_metric.labels(rule=rule).inc()

            await conn.execute(
                CLEAR_SQL,
                window_start,
                row["app_id"],
                row["tracking_link_id"],
                assessment.rules,
            )

    log.info("fraud_sweep_complete", **result.as_dict())
    return result
