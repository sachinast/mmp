"""The analytics read path.

Every endpoint here reads a **rollup**, never a raw partition. That is the rule
this stack lives or dies by: a dashboard querying raw events is fine at ten
million rows and unusable at a billion, and nobody notices the transition until
an advertiser does.

Three constraints are enforced on every query, and all three exist because the
alternative is a query that looks fine in development and takes the database
down in production:

* **A date range is required**, never defaulted. A default range is a default
  scan, discovered by whoever opens the page on the largest account.
* **The range is capped** at 90 days. Longer periods go through export.
* **The result is cached** in Redis for five minutes, keyed by organisation —
  the key includes the tenant so a cache hit can never cross one.

The event explorer is the single exception: it reads raw partitions, and it
carries a mandatory range and a row cap for exactly that reason.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from typing import Annotated, Any

import msgspec
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from mmp_db.types import DbConn
from pydantic import BaseModel

from mmp_api.context import AppContext
from mmp_api.deps import Principal, get_context, require_role, tenant_db

router = APIRouter(prefix="/analytics", tags=["analytics"])

MAX_RANGE_DAYS = 90
CACHE_TTL = 300

# Bump when the shape of any cached response changes.
#
# Cached values outlive deploys. Renaming a field and shipping it means the new
# code reads old entries and fails validation on every request until the TTL
# expires — five minutes of 500s across the dashboard, caused by a rename that
# looked entirely safe. Including a version in the key makes a shape change miss
# the old entries instead of choking on them.
CACHE_SCHEMA_VERSION = 2
MAX_EXPLORER_ROWS = 1000


class Totals(BaseModel):
    clicks: int
    installs: int
    sessions: int
    events: int
    # Named for what the rollup can actually answer. A period-level distinct
    # count is not derivable from per-hour distinct counts.
    peak_hourly_devices: int
    revenue_minor: int
    conversions: int
    install_rate: float | None


class Overview(BaseModel):
    from_date: dt.date
    to_date: dt.date
    totals: Totals
    series: list[dict[str, Any]]


class CampaignRow(BaseModel):
    campaign_id: uuid.UUID
    campaign_name: str | None
    clicks: int
    installs: int
    revenue_minor: int
    conversions: int
    install_rate: float | None


class EventRow(BaseModel):
    event_name: str
    event_count: int
    peak_hourly_devices: int
    revenue_minor: int


def _validate_range(from_date: dt.date, to_date: dt.date) -> tuple[dt.datetime, dt.datetime]:
    if to_date < from_date:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "to is before from")
    if (to_date - from_date).days > MAX_RANGE_DAYS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"range exceeds {MAX_RANGE_DAYS} days; use the export API for longer periods",
        )
    start = dt.datetime.combine(from_date, dt.time.min, tzinfo=dt.UTC)
    end = dt.datetime.combine(to_date + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC)
    return start, end


def _cache_key(organization_id: uuid.UUID, name: str, *parts: object) -> str:
    """Tenant-scoped by construction.

    The organisation id is part of the key rather than part of the value, so a
    cache hit cannot return another tenant's numbers even if a query above it
    forgot its filter.
    """
    digest = hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:32]
    return f"analytics:v{CACHE_SCHEMA_VERSION}:{organization_id}:{name}:{digest}"


async def _cached(
    context: AppContext,
    key: str,
    compute: Any,
) -> tuple[Any, bool]:
    raw = await context.redis.get(key)
    if raw is not None:
        return msgspec.json.decode(raw), True
    value = await compute()
    await context.redis.set(key, msgspec.json.encode(value), ex=CACHE_TTL)
    return value, False


OVERVIEW_EVENTS_SQL = """
SELECT
    coalesce(sum(event_count), 0)::bigint AS events,
    coalesce(sum(event_count) FILTER (WHERE event_name = 'install'), 0)::bigint AS installs,
    coalesce(sum(event_count) FILTER (WHERE event_name = 'session_start'), 0)::bigint
        AS sessions,
    -- The busiest hour's distinct devices, not the period's. Distinct counts
    -- cannot be aggregated across buckets; named for what it actually is.
    coalesce(max(unique_devices), 0)::bigint AS peak_hourly_devices,
    coalesce(sum(revenue_minor), 0)::bigint AS revenue_minor,
    coalesce(sum(event_count) FILTER (WHERE revenue_minor > 0), 0)::bigint AS conversions
FROM rollup_events_hourly
WHERE app_id = $1 AND bucket_hour >= $2 AND bucket_hour < $3
"""

OVERVIEW_CLICKS_SQL = """
SELECT coalesce(sum(click_count) - sum(bot_count), 0)::bigint AS clicks
FROM rollup_clicks_hourly
WHERE app_id = $1 AND bucket_hour >= $2 AND bucket_hour < $3
"""

SERIES_SQL = """
SELECT
    date_trunc('day', bucket_hour)::date AS day,
    coalesce(sum(event_count), 0)::bigint AS events,
    coalesce(sum(event_count) FILTER (WHERE event_name = 'install'), 0)::bigint AS installs,
    coalesce(sum(revenue_minor), 0)::bigint AS revenue_minor
FROM rollup_events_hourly
WHERE app_id = $1 AND bucket_hour >= $2 AND bucket_hour < $3
GROUP BY 1
ORDER BY 1
"""

CAMPAIGNS_SQL = """
SELECT
    r.campaign_id,
    c.name AS campaign_name,
    sum(r.clicks)::bigint AS clicks,
    sum(r.installs)::bigint AS installs,
    sum(r.revenue_minor)::bigint AS revenue_minor,
    sum(r.conversions)::bigint AS conversions
FROM rollup_campaign_daily r
LEFT JOIN campaigns c ON c.id = r.campaign_id
WHERE r.app_id = $1 AND r.bucket_day >= $2 AND r.bucket_day < $3
GROUP BY r.campaign_id, c.name
ORDER BY installs DESC, clicks DESC
LIMIT $4
"""

EVENTS_SQL = """
SELECT
    event_name,
    sum(event_count)::bigint AS event_count,
    max(unique_devices)::bigint AS peak_hourly_devices,
    sum(revenue_minor)::bigint AS revenue_minor
FROM rollup_events_hourly
WHERE app_id = $1 AND bucket_hour >= $2 AND bucket_hour < $3
GROUP BY event_name
ORDER BY event_count DESC
LIMIT $4
"""


async def _require_app(conn: DbConn, app_id: uuid.UUID) -> None:
    # RLS makes this a tenancy check as well as an existence one: another
    # tenant's app is invisible on this connection, so it 404s rather than
    # returning an empty report that reads as "no data".
    if not await conn.fetchval("SELECT 1 FROM apps WHERE id = $1", app_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "app not found")


@router.get("/overview")
async def overview(
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(require_role("viewer"))],
    context: Annotated[AppContext, Depends(get_context)],
    conn: Annotated[DbConn, Depends(tenant_db)],
    app_id: uuid.UUID,
    from_date: Annotated[dt.date, Query(alias="from")],
    to_date: Annotated[dt.date, Query(alias="to")],
) -> Overview:
    start, end = _validate_range(from_date, to_date)
    await _require_app(conn, app_id)

    async def compute() -> dict[str, Any]:
        events = await conn.fetchrow(OVERVIEW_EVENTS_SQL, app_id, start, end)
        clicks = await conn.fetchrow(OVERVIEW_CLICKS_SQL, app_id, start, end)
        series = await conn.fetch(SERIES_SQL, app_id, start, end)

        installs = events["installs"]
        click_count = clicks["clicks"]
        return {
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "totals": {
                "clicks": click_count,
                "installs": installs,
                "sessions": events["sessions"],
                "events": events["events"],
                "peak_hourly_devices": events["peak_hourly_devices"],
                "revenue_minor": events["revenue_minor"],
                "conversions": events["conversions"],
                # None rather than zero when there were no clicks: a rate with
                # no denominator is undefined, and showing 0% would read as
                # "nobody converted" rather than "nobody clicked".
                "install_rate": (round(installs / click_count, 4) if click_count else None),
            },
            "series": [
                {
                    "day": row["day"].isoformat(),
                    "events": row["events"],
                    "installs": row["installs"],
                    "revenue_minor": row["revenue_minor"],
                }
                for row in series
            ],
        }

    key = _cache_key(principal.org_id, "overview", app_id, start, end)
    value, hit = await _cached(context, key, compute)
    # Surfaced so an expensive screen is visible in a browser's network tab
    # before it is visible as a database incident.
    response.headers["x-cache"] = "hit" if hit else "miss"
    response.headers["x-query-source"] = "rollup"
    return Overview(**value)


@router.get("/campaigns")
async def campaigns(
    response: Response,
    principal: Annotated[Principal, Depends(require_role("viewer"))],
    context: Annotated[AppContext, Depends(get_context)],
    conn: Annotated[DbConn, Depends(tenant_db)],
    app_id: uuid.UUID,
    from_date: Annotated[dt.date, Query(alias="from")],
    to_date: Annotated[dt.date, Query(alias="to")],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[CampaignRow]:
    start, end = _validate_range(from_date, to_date)
    await _require_app(conn, app_id)

    async def compute() -> list[dict[str, Any]]:
        rows = await conn.fetch(CAMPAIGNS_SQL, app_id, start.date(), end.date(), limit)
        return [
            {
                "campaign_id": str(row["campaign_id"]),
                "campaign_name": row["campaign_name"],
                "clicks": row["clicks"],
                "installs": row["installs"],
                "revenue_minor": row["revenue_minor"],
                "conversions": row["conversions"],
                "install_rate": (
                    round(row["installs"] / row["clicks"], 4) if row["clicks"] else None
                ),
            }
            for row in rows
        ]

    key = _cache_key(principal.org_id, "campaigns", app_id, start, end, limit)
    value, hit = await _cached(context, key, compute)
    response.headers["x-cache"] = "hit" if hit else "miss"
    response.headers["x-query-source"] = "rollup"
    return [CampaignRow(**row) for row in value]


@router.get("/events")
async def events(
    response: Response,
    principal: Annotated[Principal, Depends(require_role("viewer"))],
    context: Annotated[AppContext, Depends(get_context)],
    conn: Annotated[DbConn, Depends(tenant_db)],
    app_id: uuid.UUID,
    from_date: Annotated[dt.date, Query(alias="from")],
    to_date: Annotated[dt.date, Query(alias="to")],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[EventRow]:
    start, end = _validate_range(from_date, to_date)
    await _require_app(conn, app_id)

    async def compute() -> list[dict[str, Any]]:
        rows = await conn.fetch(EVENTS_SQL, app_id, start, end, limit)
        return [dict(row) for row in rows]

    key = _cache_key(principal.org_id, "events", app_id, start, end, limit)
    value, hit = await _cached(context, key, compute)
    response.headers["x-cache"] = "hit" if hit else "miss"
    response.headers["x-query-source"] = "rollup"
    return [EventRow(**row) for row in value]
