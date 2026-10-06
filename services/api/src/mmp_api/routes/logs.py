"""Logs: the raw rows, one page at a time.

The live view answers "did it arrive just now"; this answers "what happened on
Tuesday". Three logs — events, clicks, installs — each read straight from its
table with the filters someone verifying an integration actually uses: an event
name, a device, a user, a platform, a campaign.

Two bounds keep this from becoming the way someone takes the database down:

* **A date range of at most 31 days.** Every query carries a time bound, so the
  partitioned tables prune to the partitions in range rather than scanning a
  year.
* **Keyset pagination, never OFFSET.** A cursor names the last row seen
  ``(time, id)`` and the next page starts strictly after it. OFFSET 50000
  reads and discards fifty thousand rows on every page; a keyset page costs
  the same whether it is the first or the thousandth.

Every filter is a bound parameter against a fixed statement — ``($4::text IS
NULL OR event_name = $4)`` — so there is no query construction at all, and
nothing a request supplies can reach the SQL text.

As in the live view, device and IP hashes are never returned. They are stable
pseudonyms for a person, and nobody reading a log needs one.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from mmp_db.jsonfields import decode_list
from mmp_db.types import DbConn
from mmp_ingest.schema import PLATFORM_CODES

from mmp_api.deps import Principal, require_role, tenant_db

router = APIRouter(prefix="/logs", tags=["logs"])

MAX_RANGE = dt.timedelta(days=31)
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
PLATFORM_NAMES = {code: name for name, code in PLATFORM_CODES.items()}

EVENTS_SQL = """
SELECT event_id, received_at, occurred_at, event_name, anonymous_id, user_id, session_id,
       platform, os_version, app_version, device_model, country, click_id,
       revenue_minor, currency, clock_skew_ms, properties
FROM events
WHERE app_id = $1 AND received_at >= $2 AND received_at < $3
  AND ($4::text IS NULL OR event_name = $4)
  AND ($5::smallint IS NULL OR platform = $5)
  AND ($6::text IS NULL OR anonymous_id = $6)
  AND ($7::text IS NULL OR user_id = $7)
  AND ($8::timestamptz IS NULL OR (received_at, event_id) < ($8, $9::uuid))
ORDER BY received_at DESC, event_id DESC
LIMIT $10
"""

CLICKS_SQL = """
SELECT c.click_id, c.clicked_at, c.campaign_id, c.tracking_link_id, c.platform, c.country,
       c.os_version, c.device_model, c.is_bot, c.deep_link, c.sub1, c.sub2, c.sub3,
       l.name AS link_name, l.tracking_code, cp.name AS campaign_name
FROM clicks c
LEFT JOIN tracking_links l ON l.id = c.tracking_link_id
LEFT JOIN campaigns cp ON cp.id = c.campaign_id
WHERE c.app_id = $1 AND c.clicked_at >= $2 AND c.clicked_at < $3
  AND ($4::uuid IS NULL OR c.campaign_id = $4)
  AND ($5::smallint IS NULL OR c.platform = $5)
  AND ($6::char(2) IS NULL OR c.country = $6)
  AND ($7::timestamptz IS NULL OR (c.clicked_at, c.click_id) < ($7, $8::uuid))
ORDER BY c.clicked_at DESC, c.click_id DESC
LIMIT $9
"""

INSTALLS_SQL = """
SELECT a.id, a.attributed_at, a.installed_at, a.anonymous_id, a.method, a.fraud_verdict,
       a.fraud_rules, a.deep_link, a.superseded_by, a.sub1, a.sub2, a.sub3, a.click_id,
       cp.name AS campaign_name, l.name AS link_name
FROM attributions a
LEFT JOIN campaigns cp ON cp.id = a.campaign_id
LEFT JOIN tracking_links l ON l.id = a.tracking_link_id
WHERE a.app_id = $1 AND a.attributed_at >= $2 AND a.attributed_at < $3
  AND ($4::text IS NULL OR a.method = $4)
  AND ($5::text IS NULL OR a.fraud_verdict = $5)
  AND ($6::text IS NULL OR a.anonymous_id = $6)
  AND ($7::timestamptz IS NULL OR (a.attributed_at, a.id) < ($7, $8::uuid))
ORDER BY a.attributed_at DESC, a.id DESC
LIMIT $9
"""


def _range(from_date: dt.date, to_date: dt.date) -> tuple[dt.datetime, dt.datetime]:
    if to_date < from_date:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "to is before from")
    if to_date - from_date > MAX_RANGE:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"range exceeds {MAX_RANGE.days} days"
        )
    start = dt.datetime.combine(from_date, dt.time.min, tzinfo=dt.UTC)
    end = dt.datetime.combine(to_date + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC)
    return start, end


def _encode_cursor(at: dt.datetime, row_id: uuid.UUID) -> str:
    raw = f"{at.isoformat()}|{row_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[dt.datetime | None, uuid.UUID | None]:
    if not cursor:
        return None, None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        at_text, id_text = base64.urlsafe_b64decode(padded).decode().split("|", 1)
        at = dt.datetime.fromisoformat(at_text)
        if at.tzinfo is None:
            at = at.replace(tzinfo=dt.UTC)
        return at, uuid.UUID(id_text)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "malformed cursor") from exc


def _platform_code(name: str | None) -> int | None:
    if name is None:
        return None
    code = PLATFORM_CODES.get(name.lower())
    if code is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "unknown platform")
    return code


def _properties(raw: object) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str | bytes):
        try:
            decoded = json.loads(raw)
        except ValueError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None


async def _require_app(conn: DbConn, app_id: uuid.UUID) -> None:
    if not await conn.fetchval("SELECT 1 FROM apps WHERE id = $1", app_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "app not found")


def _limit(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


def _page(rows: list[Any], limit: int, at_key: str, id_key: str) -> tuple[list[Any], str | None]:
    """Fetched one more than asked for: the extra row says whether there is a
    next page, without a COUNT over the range."""
    more = len(rows) > limit
    rows = rows[:limit]
    cursor = _encode_cursor(rows[-1][at_key], rows[-1][id_key]) if more and rows else None
    return rows, cursor


@router.get("/events")
async def event_log(
    response: Response,
    _principal: Annotated[Principal, Depends(require_role("member"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
    app_id: uuid.UUID,
    from_date: Annotated[dt.date, Query(alias="from")],
    to_date: Annotated[dt.date, Query(alias="to")],
    event_name: str | None = None,
    platform: str | None = None,
    anonymous_id: str | None = None,
    user_id: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    await _require_app(conn, app_id)
    start, end = _range(from_date, to_date)
    cursor_at, cursor_id = _decode_cursor(cursor)
    size = _limit(limit)
    rows = await conn.fetch(
        EVENTS_SQL,
        app_id,
        start,
        end,
        event_name or None,
        _platform_code(platform),
        anonymous_id or None,
        user_id or None,
        cursor_at,
        cursor_id,
        size + 1,
    )
    rows, next_cursor = _page(rows, size, "received_at", "event_id")
    response.headers["cache-control"] = "no-store"
    return {
        "items": [
            {
                "event_id": str(row["event_id"]),
                "received_at": _iso(row["received_at"]),
                "occurred_at": _iso(row["occurred_at"]),
                "event_name": row["event_name"],
                "anonymous_id": row["anonymous_id"],
                "user_id": row["user_id"],
                "session_id": str(row["session_id"]) if row["session_id"] else None,
                "platform": PLATFORM_NAMES.get(row["platform"] or 0, "unknown"),
                "os_version": row["os_version"],
                "app_version": row["app_version"],
                "device_model": row["device_model"],
                "country": row["country"],
                "click_id": str(row["click_id"]) if row["click_id"] else None,
                "revenue_minor": row["revenue_minor"],
                "currency": row["currency"],
                "clock_skew_ms": row["clock_skew_ms"],
                "properties": _properties(row["properties"]),
            }
            for row in rows
        ],
        "next_cursor": next_cursor,
    }


@router.get("/clicks")
async def click_log(
    response: Response,
    _principal: Annotated[Principal, Depends(require_role("member"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
    app_id: uuid.UUID,
    from_date: Annotated[dt.date, Query(alias="from")],
    to_date: Annotated[dt.date, Query(alias="to")],
    campaign_id: uuid.UUID | None = None,
    platform: str | None = None,
    country: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    await _require_app(conn, app_id)
    start, end = _range(from_date, to_date)
    cursor_at, cursor_id = _decode_cursor(cursor)
    size = _limit(limit)
    rows = await conn.fetch(
        CLICKS_SQL,
        app_id,
        start,
        end,
        campaign_id,
        _platform_code(platform),
        country.upper()[:2] if country else None,
        cursor_at,
        cursor_id,
        size + 1,
    )
    rows, next_cursor = _page(rows, size, "clicked_at", "click_id")
    response.headers["cache-control"] = "no-store"
    return {
        "items": [
            {
                "click_id": str(row["click_id"]),
                "clicked_at": _iso(row["clicked_at"]),
                "campaign_id": str(row["campaign_id"]) if row["campaign_id"] else None,
                "campaign": row["campaign_name"],
                "link": row["link_name"],
                "tracking_code": row["tracking_code"],
                "platform": PLATFORM_NAMES.get(row["platform"] or 0, "unknown"),
                "country": row["country"],
                "os_version": row["os_version"],
                "device_model": row["device_model"],
                "is_bot": row["is_bot"],
                "deep_link": row["deep_link"],
                "sub1": row["sub1"],
                "sub2": row["sub2"],
                "sub3": row["sub3"],
            }
            for row in rows
        ],
        "next_cursor": next_cursor,
    }


@router.get("/installs")
async def install_log(
    response: Response,
    _principal: Annotated[Principal, Depends(require_role("member"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
    app_id: uuid.UUID,
    from_date: Annotated[dt.date, Query(alias="from")],
    to_date: Annotated[dt.date, Query(alias="to")],
    method: str | None = None,
    verdict: str | None = None,
    anonymous_id: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    await _require_app(conn, app_id)
    start, end = _range(from_date, to_date)
    cursor_at, cursor_id = _decode_cursor(cursor)
    size = _limit(limit)
    rows = await conn.fetch(
        INSTALLS_SQL,
        app_id,
        start,
        end,
        method or None,
        verdict or None,
        anonymous_id or None,
        cursor_at,
        cursor_id,
        size + 1,
    )
    rows, next_cursor = _page(rows, size, "attributed_at", "id")
    response.headers["cache-control"] = "no-store"
    return {
        "items": [
            {
                "attribution_id": str(row["id"]),
                "attributed_at": _iso(row["attributed_at"]),
                "installed_at": _iso(row["installed_at"]),
                "anonymous_id": row["anonymous_id"],
                "method": row["method"],
                "fraud_verdict": row["fraud_verdict"],
                "fraud_rules": decode_list(row["fraud_rules"]),
                "campaign": row["campaign_name"],
                "link": row["link_name"],
                "click_id": str(row["click_id"]) if row["click_id"] else None,
                "deep_link": row["deep_link"],
                "superseded": row["superseded_by"] is not None,
                "sub1": row["sub1"],
                "sub2": row["sub2"],
                "sub3": row["sub3"],
            }
            for row in rows
        ],
        "next_cursor": next_cursor,
    }
