"""Fraud findings and flagged installs.

Read-only. Nothing here changes a verdict, because a verdict that a customer
could edit is not evidence of anything — and the argument these endpoints exist
to settle is usually between an advertiser and the network they are paying.

Two views, matching the two shapes the assessment takes:

* ``/fraud/findings`` — traffic-level findings from the sweep, one per link per
  rule per window, each carrying the sentence it was written with.
* ``/fraud/installs`` — the individual attributions that were flagged, so a
  finding can be drilled into rather than merely asserted.

The install view returns flagged rows only. That is a filter on a fraud
endpoint, not on reporting: every analytics path continues to count flagged
installs, so nobody's totals move because a rule fired.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from mmp_db.jsonfields import decode_list
from mmp_db.types import DbConn
from pydantic import BaseModel

from mmp_api.deps import Principal, require_role, tenant_db

router = APIRouter(tags=["fraud"])

# A bounded range, for the same reason every other range endpoint here has one:
# an unbounded scan of a partitioned table is a denial of service that looks
# like a report.
MAX_RANGE = dt.timedelta(days=90)
MAX_ROWS = 500

FINDINGS_SQL = """
SELECT f.id, f.app_id, f.tracking_link_id, f.campaign_id, f.rule, f.severity,
       f.detail, f.window_start, f.window_end, f.created_at,
       l.tracking_code, c.name AS campaign_name
FROM fraud_findings f
LEFT JOIN tracking_links l ON l.id = f.tracking_link_id
LEFT JOIN campaigns c ON c.id = f.campaign_id
WHERE f.app_id = $1 AND f.window_start >= $2 AND f.window_start < $3
ORDER BY f.severity DESC, f.window_start DESC
LIMIT $4
"""

FLAGGED_SQL = """
SELECT id, app_id, click_id, campaign_id, tracking_link_id, method,
       installed_at, fraud_score, fraud_verdict, fraud_rules
FROM attributions
WHERE app_id = $1
  AND installed_at >= $2 AND installed_at < $3
  AND fraud_verdict <> 'clean'
  AND superseded_by IS NULL
ORDER BY fraud_score DESC, installed_at DESC
LIMIT $4
"""


class Finding(BaseModel):
    id: str
    rule: str
    severity: int
    detail: str
    tracking_code: str | None
    campaign_name: str | None
    window_start: dt.datetime
    window_end: dt.datetime


class FlaggedInstall(BaseModel):
    attribution_id: str
    method: str
    installed_at: dt.datetime
    fraud_score: int
    fraud_verdict: str
    rules: list[str]


def _validated_range(since: dt.datetime, until: dt.datetime) -> None:
    if until <= since:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "until must be after since")
    if until - since > MAX_RANGE:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"range must not exceed {MAX_RANGE.days} days",
        )


@router.get("/fraud/findings")
async def list_findings(
    app_id: str,
    since: Annotated[dt.datetime, Query()],
    until: Annotated[dt.datetime, Query()],
    _principal: Annotated[Principal, Depends(require_role("member"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> list[Finding]:
    _validated_range(since, until)
    rows = await conn.fetch(FINDINGS_SQL, app_id, since, until, MAX_ROWS)
    return [
        Finding(
            id=str(row["id"]),
            rule=row["rule"],
            severity=row["severity"],
            detail=row["detail"],
            tracking_code=row["tracking_code"],
            campaign_name=row["campaign_name"],
            window_start=row["window_start"],
            window_end=row["window_end"],
        )
        for row in rows
    ]


@router.get("/fraud/installs")
async def list_flagged_installs(
    app_id: str,
    since: Annotated[dt.datetime, Query()],
    until: Annotated[dt.datetime, Query()],
    _principal: Annotated[Principal, Depends(require_role("member"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> list[FlaggedInstall]:
    _validated_range(since, until)
    rows = await conn.fetch(FLAGGED_SQL, app_id, since, until, MAX_ROWS)
    return [
        FlaggedInstall(
            attribution_id=str(row["id"]),
            method=row["method"],
            installed_at=row["installed_at"],
            fraud_score=row["fraud_score"],
            fraud_verdict=row["fraud_verdict"],
            rules=decode_list(row["fraud_rules"]),
        )
        for row in rows
    ]
