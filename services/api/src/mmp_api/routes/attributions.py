"""Reading attributions.

Two very different readers, so two endpoints:

* A **support engineer** answering "why was this install credited to that
  campaign?" needs one record with its reason, its method, and the click that
  won. An attribution nobody can explain is one nobody can defend when a network
  disputes it.
* A **dashboard** needs counts by method and campaign, which is an aggregate and
  must be bounded — this is the read path that would otherwise become a full
  table scan the first time someone opens it on a large advertiser.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from mmp_db.types import DbConn
from pydantic import BaseModel

from mmp_api.deps import Principal, require_role, tenant_db

router = APIRouter(prefix="/attributions", tags=["attribution"])

# The same ceiling the analytics API will use. An unbounded range here is the
# difference between a fast dashboard and a query that scans every partition.
MAX_RANGE_DAYS = 90


class AttributionOut(BaseModel):
    id: uuid.UUID
    app_id: uuid.UUID
    anonymous_id: str
    user_id: str | None
    method: str
    click_id: uuid.UUID | None
    campaign_id: uuid.UUID | None
    tracking_link_id: uuid.UUID | None
    source: str | None
    medium: str | None
    installed_at: dt.datetime
    attributed_at: dt.datetime
    window_days: int
    expires_at: dt.datetime
    superseded_by: uuid.UUID | None


class MethodBreakdown(BaseModel):
    method: str
    count: int


class AttributionSummary(BaseModel):
    from_date: dt.date
    to_date: dt.date
    total: int
    attributed: int
    organic: int
    match_rate: float | None
    by_method: list[MethodBreakdown]


DETAIL_SQL = """
SELECT id, app_id, anonymous_id, user_id, method, click_id, campaign_id,
       tracking_link_id, source, medium, installed_at, attributed_at,
       window_days, expires_at, superseded_by
FROM attributions
WHERE app_id = $1 AND anonymous_id = $2
ORDER BY created_at DESC
"""

SUMMARY_SQL = """
SELECT method, count(*) AS count
FROM attributions
WHERE app_id = $1
  AND installed_at >= $2
  AND installed_at < $3
  AND superseded_by IS NULL
GROUP BY method
"""


@router.get("/summary")
async def summary(
    _principal: Annotated[Principal, Depends(require_role("viewer"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
    app_id: uuid.UUID,
    from_date: Annotated[dt.date, Query(alias="from")],
    to_date: Annotated[dt.date, Query(alias="to")],
) -> AttributionSummary:
    """Attribution counts by method over a bounded range.

    The date range is required rather than defaulted. A default would be a
    default *scan*, and the first person to open this on a large advertiser
    would discover how large by waiting for it.
    """
    if to_date < from_date:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "to is before from")
    if (to_date - from_date).days > MAX_RANGE_DAYS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"range exceeds {MAX_RANGE_DAYS} days; use the export API for longer periods",
        )
    if not await conn.fetchval("SELECT 1 FROM apps WHERE id = $1", app_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "app not found")

    rows = await conn.fetch(
        SUMMARY_SQL,
        app_id,
        dt.datetime.combine(from_date, dt.time.min, tzinfo=dt.UTC),
        dt.datetime.combine(to_date + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC),
    )
    counts = {row["method"]: row["count"] for row in rows}
    total = sum(counts.values())
    organic = counts.get("organic", 0)
    attributed = total - organic

    return AttributionSummary(
        from_date=from_date,
        to_date=to_date,
        total=total,
        attributed=attributed,
        organic=organic,
        # The number an advertiser looks at first, and the one that tells us
        # something is wrong before they do.
        match_rate=round(attributed / total, 4) if total else None,
        by_method=[
            MethodBreakdown(method=method, count=count)
            for method, count in sorted(counts.items(), key=lambda kv: -kv[1])
        ],
    )


@router.get("/lookup")
async def lookup_device(
    _principal: Annotated[Principal, Depends(require_role("viewer"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
    app_id: uuid.UUID,
    anonymous_id: str,
) -> list[AttributionOut]:
    """Every attribution ever recorded for one device, newest first.

    Includes superseded rows deliberately. "It used to say organic and now it
    says Meta" is a question we must be able to answer with the record rather
    than with an explanation, and the superseded chain is that record.
    """
    rows = await conn.fetch(DETAIL_SQL, app_id, anonymous_id)
    return [AttributionOut(**dict(row)) for row in rows]
