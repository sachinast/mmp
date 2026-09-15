"""The live view: what has happened to an app in the last few minutes.

Built for the moment someone integrates the SDK, fires an event from a test
device, and needs to know whether it arrived — and if not, why. So it shows five
things interleaved by time: clicks, installs as attributed, in-app events,
postback deliveries, and requests the tracker refused.

It reads what was **persisted**, not what was received. An event on this list has
been validated, queued, consumed and written — the whole pipeline — which is the
thing being verified. The cost is that it appears a moment after it was sent
rather than the instant the request landed.

Bounded in every direction, because it is polled:

* a fifteen-minute window, whatever ``since`` asks for;
* a row limit per kind;
* indexes that let each query read only that window.

What it does not show, deliberately:

* **device and IP hashes.** They are stable pseudonyms for a person, derived
  under a system-wide pepper, and nobody verifying an integration needs one.
* **full postback URLs.** Advertisers put partner tokens in postback query
  strings; the host and the outcome are what a delivery problem needs.

Tenancy is checked explicitly rather than left to row-level security. The rows
from Postgres are scoped by RLS, but rejections come from Redis, which has no
notion of a tenant — without the ownership check, any member of any
organisation could read another's rejections by naming its app id.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from mmp_db.jsonfields import decode_list
from mmp_db.types import DbConn
from mmp_ingest.live import recent_rejections
from mmp_ingest.schema import PLATFORM_CODES

from mmp_api.context import AppContext
from mmp_api.deps import Principal, get_context, require_role, tenant_db

router = APIRouter(tags=["live"])

WINDOW = dt.timedelta(minutes=15)
# How far before ``since`` each poll reads again.
#
# Events and clicks carry the time the tracker received them, but only become
# visible once the worker has written them. Without a margin, an event received
# half a second before a poll and written just after it falls between two polls
# and is never shown — permanently, and in the one view whose purpose is to show
# that an event arrived. Re-reading the last minute catches anything written late
# by up to that long; the client de-duplicates what it has already seen.
#
# A pipeline more than a minute behind can still drop items from this view. That
# is a backlog, which the stream metrics report, and past this margin the live
# view is the wrong instrument for it.
LATE_WRITE_MARGIN = dt.timedelta(seconds=60)
PER_KIND = 100
MAX_ITEMS = 250
# An event's own properties are shown, because "did my parameters arrive" is half
# of what people check. Past this size they are summarised by their keys.
MAX_PROPERTIES_BYTES = 4096
MAX_ERROR_CHARS = 300

CLICKS_SQL = """
SELECT c.click_id, c.clicked_at, c.platform, c.country, c.os_version, c.device_model,
       c.is_bot, c.deep_link, c.sub1, c.sub2, c.sub3,
       l.name AS link_name, l.tracking_code, cp.name AS campaign_name
FROM clicks c
LEFT JOIN tracking_links l ON l.id = c.tracking_link_id
LEFT JOIN campaigns cp ON cp.id = c.campaign_id
WHERE c.app_id = $1 AND c.clicked_at >= $2
ORDER BY c.clicked_at DESC
LIMIT $3
"""

EVENTS_SQL = """
SELECT event_id, received_at, occurred_at, event_name, anonymous_id, user_id,
       platform, app_version, os_version, device_model, country,
       revenue_minor, currency, clock_skew_ms, properties
FROM events
WHERE app_id = $1 AND received_at >= $2
ORDER BY received_at DESC
LIMIT $3
"""

INSTALLS_SQL = """
SELECT a.id, a.attributed_at, a.installed_at, a.anonymous_id, a.method,
       a.fraud_verdict, a.fraud_rules, a.deep_link, a.superseded_by,
       cp.name AS campaign_name, l.name AS link_name
FROM attributions a
LEFT JOIN campaigns cp ON cp.id = a.campaign_id
LEFT JOIN tracking_links l ON l.id = a.tracking_link_id
WHERE a.app_id = $1 AND a.attributed_at >= $2
ORDER BY a.attributed_at DESC
LIMIT $3
"""

POSTBACKS_SQL = """
SELECT d.id, d.created_at, d.delivered_at, d.status, d.attempt_count,
       d.response_status, d.error, d.request_url, d.event_id, r.name AS rule_name
FROM postback_deliveries d
JOIN postback_rules r ON r.id = d.postback_rule_id
WHERE r.app_id = $1 AND d.created_at >= $2
ORDER BY d.created_at DESC
LIMIT $3
"""


def _properties(raw: object) -> dict[str, Any]:
    if raw is None:
        return {}
    text = raw if isinstance(raw, str) else json.dumps(raw)
    value = json.loads(text)
    if not isinstance(value, dict):
        return {}
    size = len(text.encode())
    if size <= MAX_PROPERTIES_BYTES:
        return value
    return {"_truncated": True, "_bytes": size, "_keys": sorted(value)[:50]}


# Stored as a smallint. Translated from the ingest schema's own table rather than
# a copy of it: the first version of this view showed clicks as "Campaign · 1".
PLATFORM_NAMES = {code: name for name, code in PLATFORM_CODES.items()}


def _platform(code: int | None) -> str | None:
    if code is None:
        return None
    return PLATFORM_NAMES.get(code, "unknown")


def _host(url: str | None) -> str | None:
    if not url:
        return None
    return urlsplit(url).hostname


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None


@router.get("/live")
async def live_feed(
    app_id: str,
    response: Response,
    _principal: Annotated[Principal, Depends(require_role("member"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
    context: Annotated[AppContext, Depends(get_context)],
    since: Annotated[dt.datetime | None, Query()] = None,
) -> dict[str, Any]:
    """Recent activity for one app, newest first.

    Poll with the ``server_time`` from the previous response as ``since``. Each
    poll deliberately re-reads the minute before it (see ``LATE_WRITE_MARGIN``),
    so items repeat across polls: de-duplicate on ``kind`` and ``id``, and expect
    an occasional item older than the newest one already shown.
    """
    # Scoped by RLS, so another tenant's app is simply not found — and that is
    # the check that makes the Redis read below safe.
    try:
        app_uuid = uuid.UUID(app_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "app not found") from None
    if not await conn.fetchval("SELECT 1 FROM apps WHERE id = $1", app_uuid):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "app not found")

    now = dt.datetime.now(dt.UTC)
    floor = now - WINDOW
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=dt.UTC)
    # One expression, so there is exactly one place the bound is enforced: re-read
    # the late-write margin, never reach further back than the window, never ask
    # for the future.
    since = floor if since is None else min(now, max(floor, since - LATE_WRITE_MARGIN))

    items: list[dict[str, Any]] = []

    for row in await conn.fetch(CLICKS_SQL, app_uuid, since, PER_KIND):
        items.append(
            {
                "kind": "click",
                "id": str(row["click_id"]),
                "at": _iso(row["clicked_at"]),
                "title": row["link_name"] or row["tracking_code"] or "click",
                "campaign": row["campaign_name"],
                "platform": _platform(row["platform"]),
                "country": row["country"],
                "is_bot": row["is_bot"],
                "details": {
                    "click_id": str(row["click_id"]),
                    "tracking_code": row["tracking_code"],
                    "deep_link": row["deep_link"],
                    "os_version": row["os_version"],
                    "device_model": row["device_model"],
                    "sub1": row["sub1"],
                    "sub2": row["sub2"],
                    "sub3": row["sub3"],
                },
            }
        )

    for row in await conn.fetch(EVENTS_SQL, app_uuid, since, PER_KIND):
        items.append(
            {
                "kind": "event",
                "id": str(row["event_id"]),
                "at": _iso(row["received_at"]),
                "title": row["event_name"],
                "device": row["anonymous_id"],
                "user_id": row["user_id"],
                "platform": _platform(row["platform"]),
                "revenue_minor": row["revenue_minor"],
                "currency": row["currency"],
                "details": {
                    "event_id": str(row["event_id"]),
                    "occurred_at": _iso(row["occurred_at"]),
                    "clock_skew_ms": row["clock_skew_ms"],
                    "app_version": row["app_version"],
                    "os_version": row["os_version"],
                    "device_model": row["device_model"],
                    "country": row["country"],
                    "properties": _properties(row["properties"]),
                },
            }
        )

    for row in await conn.fetch(INSTALLS_SQL, app_uuid, since, PER_KIND):
        items.append(
            {
                "kind": "install",
                "id": str(row["id"]),
                "at": _iso(row["attributed_at"]),
                "title": row["method"],
                "device": row["anonymous_id"],
                "campaign": row["campaign_name"],
                "fraud_verdict": row["fraud_verdict"],
                "details": {
                    "attribution_id": str(row["id"]),
                    "installed_at": _iso(row["installed_at"]),
                    "tracking_link": row["link_name"],
                    "deep_link": row["deep_link"],
                    "fraud_rules": decode_list(row["fraud_rules"]),
                    "superseded": row["superseded_by"] is not None,
                },
            }
        )

    for row in await conn.fetch(POSTBACKS_SQL, app_uuid, since, PER_KIND):
        items.append(
            {
                "kind": "postback",
                "id": str(row["id"]),
                "at": _iso(row["created_at"]),
                "title": row["rule_name"],
                "status": row["status"],
                "response_status": row["response_status"],
                "details": {
                    "destination_host": _host(row["request_url"]),
                    "attempts": row["attempt_count"],
                    "delivered_at": _iso(row["delivered_at"]),
                    "error": (row["error"] or "")[:MAX_ERROR_CHARS] or None,
                    "event_id": str(row["event_id"]) if row["event_id"] else None,
                },
            }
        )

    for entry in await recent_rejections(context.redis, str(app_uuid), since=since):
        items.append(
            {
                "kind": "rejected",
                # Rejections have no identity of their own. Built from what the
                # entry says rather than where it sits: new rejections are pushed
                # to the front, so a positional id would change between polls
                # and the same rejection would appear twice.
                "id": f"{entry['at']}:{entry['reason']}:{entry['status']}",
                "at": entry["at"],
                "title": entry["reason"],
                "status": entry["status"],
                "source": entry["source"],
                "details": {
                    "detail": entry["detail"],
                    "events_in_batch": entry["events_in_batch"],
                },
            }
        )

    items.sort(key=lambda item: item["at"] or "", reverse=True)
    response.headers["cache-control"] = "no-store"
    return {
        "items": items[:MAX_ITEMS],
        "server_time": now.isoformat(),
        "since": since.isoformat(),
        "window_seconds": int(WINDOW.total_seconds()),
    }
