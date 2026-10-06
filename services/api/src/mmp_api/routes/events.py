"""The event catalogue: what an app sends, what each name means, and what is blocked.

Three sources are merged into one list per app:

* **Standard events** from ``mmp_ingest.catalogue`` — the vocabulary every
  integration starts from, with the properties each expects.
* **Definitions** in ``event_definitions`` — the advertiser's own names, and
  any standard name they have annotated or blocked.
* **Discovered names** — whatever has actually arrived in the last thirty days,
  read from the hourly rollup. An event the app sends that nobody has defined is
  shown rather than hidden, because the most useful thing a catalogue can tell
  someone integrating an app is "this is arriving and you have not named it".

Blocking is write-through to Redis, where the tracker reads it. The database
row is the truth and the Redis entry is a published copy; the tracker falls back
to the row when the copy is missing.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Literal

import asyncpg
import msgspec
from fastapi import APIRouter, Depends, HTTPException, status
from mmp_core.ids import uuid7
from mmp_core.logging import get_logger
from mmp_db.types import DbConn
from mmp_ingest.catalogue import (
    CATEGORIES,
    STANDARD_EVENTS,
    blocked_events_cache_key,
    standard_event,
)
from mmp_ingest.schema import MAX_EVENT_NAME_LENGTH
from pydantic import BaseModel, Field

from mmp_api.context import AppContext
from mmp_api.deps import Principal, get_context, require_role, tenant_db
from mmp_db import audit, sql

router = APIRouter(tags=["events"])
log = get_logger(__name__)

DISCOVERY_WINDOW = dt.timedelta(days=30)
TREND_DAYS = 14

_blocked_encoder = msgspec.json.Encoder()


class EventPropertyOut(BaseModel):
    name: str
    type: str
    description: str


class StandardEventOut(BaseModel):
    name: str
    display_name: str
    category: str
    description: str
    properties: list[EventPropertyOut]
    revenue: bool
    sdk_owned: bool


class EventDefinitionIn(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=MAX_EVENT_NAME_LENGTH)]
    display_name: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    description: Annotated[str, Field(max_length=2000)] | None = None
    category: Literal[
        "lifecycle", "account", "commerce", "engagement", "content", "gaming", "custom"
    ] = "custom"
    revenue: bool = False


class EventDefinitionUpdate(BaseModel):
    display_name: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    description: Annotated[str, Field(max_length=2000)] | None = None
    category: (
        Literal["lifecycle", "account", "commerce", "engagement", "content", "gaming", "custom"]
        | None
    ) = None
    revenue: bool | None = None
    status: Literal["active", "blocked"] | None = None


class AppEventOut(BaseModel):
    """One row of an app's catalogue, whichever source it came from."""

    name: str
    display_name: str
    description: str | None
    category: str
    # standard: in the platform catalogue. custom: defined by the advertiser.
    # discovered: arriving, and defined by nobody.
    kind: Literal["standard", "custom", "discovered"]
    status: Literal["active", "blocked"]
    revenue: bool
    sdk_owned: bool
    defined: bool
    id: uuid.UUID | None
    properties: list[EventPropertyOut]
    count_30d: int
    revenue_minor_30d: int
    first_seen_at: dt.datetime | None
    last_seen_at: dt.datetime | None
    # Daily counts for the trailing fortnight, oldest first, zero-filled.
    trend: list[int]


DEFINITION_COLUMNS = (
    "id",
    "app_id",
    "name",
    "display_name",
    "description",
    "category",
    "kind",
    "revenue",
    "status",
    "blocked_at",
    "created_at",
)
# Only these may be changed through PATCH, and the allowlist is the only source
# of column names in the generated SQL.
UPDATABLE = ("display_name", "description", "category", "revenue", "status", "blocked_at")

STATS_SQL = """
SELECT event_name,
       sum(event_count)::bigint AS count_30d,
       sum(revenue_minor)::bigint AS revenue_minor_30d,
       min(bucket_hour) AS first_seen_at,
       max(bucket_hour) AS last_seen_at
FROM rollup_events_hourly
WHERE app_id = $1 AND bucket_hour >= $2
GROUP BY event_name
"""

TREND_SQL = """
SELECT event_name, date_trunc('day', bucket_hour)::date AS day, sum(event_count)::bigint AS n
FROM rollup_events_hourly
WHERE app_id = $1 AND bucket_hour >= $2
GROUP BY 1, 2
"""


async def _require_app(conn: DbConn, app_id: uuid.UUID) -> None:
    if not await conn.fetchval("SELECT 1 FROM apps WHERE id = $1", app_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "app not found")


async def _publish_blocked(context: AppContext, conn: DbConn, app_id: uuid.UUID) -> None:
    """Copy the blocked set to Redis for the tracker. The row is the truth."""
    rows = await conn.fetch(
        "SELECT name FROM event_definitions WHERE app_id = $1 AND status = 'blocked'", app_id
    )
    await context.redis.set(
        blocked_events_cache_key(str(app_id)),
        _blocked_encoder.encode([row["name"] for row in rows]),
        ex=3600,
    )


def _standard_out(event: Any) -> StandardEventOut:
    return StandardEventOut(
        name=event.name,
        display_name=event.display_name,
        category=event.category,
        description=event.description,
        properties=[
            EventPropertyOut(name=p.name, type=p.type, description=p.description)
            for p in event.properties
        ],
        revenue=event.revenue,
        sdk_owned=event.sdk_owned,
    )


@router.get("/events/catalogue")
async def catalogue(
    _principal: Annotated[Principal, Depends(require_role("viewer"))],
) -> list[StandardEventOut]:
    """The standard vocabulary. The same for every app; defined once in code."""
    return [_standard_out(event) for event in STANDARD_EVENTS]


@router.get("/apps/{app_id}/events")
async def list_app_events(
    app_id: uuid.UUID,
    _principal: Annotated[Principal, Depends(require_role("viewer"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> list[AppEventOut]:
    await _require_app(conn, app_id)
    now = dt.datetime.now(dt.UTC)
    since = now - DISCOVERY_WINDOW
    trend_since = (now - dt.timedelta(days=TREND_DAYS - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    definitions = {
        row["name"]: dict(row)
        for row in await conn.fetch(
            sql.select("event_definitions", DEFINITION_COLUMNS, where="app_id = $1"), app_id
        )
    }
    stats = {row["event_name"]: dict(row) for row in await conn.fetch(STATS_SQL, app_id, since)}
    trends: dict[str, dict[dt.date, int]] = {}
    for row in await conn.fetch(TREND_SQL, app_id, trend_since):
        trends.setdefault(row["event_name"], {})[row["day"]] = row["n"]
    days = [(trend_since + dt.timedelta(days=i)).date() for i in range(TREND_DAYS)]

    # Which standard events have an arriving or defined name, matched on the
    # folded form so "Add To Cart" is recognised as add_to_cart. Each standard
    # event is listed once, under whichever name is actually in use.
    seen_standard: set[str] = set()
    out: list[AppEventOut] = []

    def row_for(name: str) -> AppEventOut:
        definition = definitions.get(name)
        standard = standard_event(name)
        if standard is not None:
            seen_standard.add(standard.name)
        stat = stats.get(name, {})
        trend = trends.get(name, {})
        kind: Literal["standard", "custom", "discovered"]
        if definition is not None:
            kind = "standard" if definition["kind"] == "standard" else "custom"
        elif standard is not None:
            kind = "standard"
        else:
            kind = "discovered"
        return AppEventOut(
            name=name,
            display_name=(
                definition["display_name"]
                if definition
                else (standard.display_name if standard else name)
            ),
            description=(
                definition["description"]
                if definition and definition["description"]
                else (standard.description if standard else None)
            ),
            category=definition["category"]
            if definition
            else (standard.category if standard else "custom"),
            kind=kind,
            status=definition["status"] if definition else "active",
            revenue=definition["revenue"]
            if definition
            else (standard.revenue if standard else False),
            sdk_owned=standard.sdk_owned if standard else False,
            defined=definition is not None,
            id=definition["id"] if definition else None,
            properties=[
                EventPropertyOut(name=p.name, type=p.type, description=p.description)
                for p in (standard.properties if standard else ())
            ],
            count_30d=int(stat.get("count_30d") or 0),
            revenue_minor_30d=int(stat.get("revenue_minor_30d") or 0),
            first_seen_at=stat.get("first_seen_at"),
            last_seen_at=stat.get("last_seen_at"),
            trend=[int(trend.get(day, 0)) for day in days],
        )

    # Names in use first — defined or arriving — then the standard events that
    # are neither, so the list reads as "what you have" followed by "what you
    # could send".
    for name in sorted(
        set(definitions) | set(stats), key=lambda n: -int(stats.get(n, {}).get("count_30d") or 0)
    ):
        out.append(row_for(name))
    for standard in STANDARD_EVENTS:
        if standard.name not in seen_standard:
            out.append(row_for(standard.name))
    return out


@router.post("/apps/{app_id}/events", status_code=status.HTTP_201_CREATED)
async def define_event(
    app_id: uuid.UUID,
    body: EventDefinitionIn,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> dict[str, Any]:
    await _require_app(conn, app_id)
    name = body.name.strip()
    if not name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "name is required")
    standard = standard_event(name)
    kind = "standard" if standard else "custom"
    # A standard event keeps the catalogue's own category and revenue flag; what
    # an advertiser may add is their display name and description. Letting
    # `purchase` be redefined as non-revenue would make reports disagree with
    # what the worker does with it.
    category = standard.category if standard else body.category
    revenue = standard.revenue if standard else body.revenue
    display_name = (body.display_name or (standard.display_name if standard else name)).strip()

    definition_id = uuid7()
    try:
        row = await conn.fetchrow(
            sql.with_returning(
                """INSERT INTO event_definitions
                       (id, organization_id, app_id, name, display_name, description,
                        category, kind, revenue, status)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'active')""",
                DEFINITION_COLUMNS,
            ),
            definition_id,
            principal.organization_id,
            app_id,
            name,
            display_name,
            body.description,
            category,
            kind,
            revenue,
        )
    except asyncpg.exceptions.UniqueViolationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "that event is already defined") from exc

    await audit.record(
        conn,
        organization_id=principal.org_id,
        action="event_definition.created",
        resource_type="event_definition",
        resource_id=str(definition_id),
        actor_user_id=principal.user_id,
        detail={"app_id": str(app_id), "name": name, "kind": kind},
    )
    assert row is not None
    return dict(row)


@router.patch("/apps/{app_id}/events/{definition_id}")
async def update_event(
    app_id: uuid.UUID,
    definition_id: uuid.UUID,
    body: EventDefinitionUpdate,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    context: Annotated[AppContext, Depends(get_context)],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> dict[str, Any]:
    await _require_app(conn, app_id)
    current = await conn.fetchrow(
        sql.select("event_definitions", DEFINITION_COLUMNS, where="id = $1 AND app_id = $2"),
        definition_id,
        app_id,
    )
    if current is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event definition not found")

    changes = body.model_dump(exclude_unset=True)
    if current["kind"] == "standard":
        # See define_event: the catalogue decides these for standard names.
        changes.pop("category", None)
        changes.pop("revenue", None)
    if not changes:
        return dict(current)

    status_change = changes.get("status")
    if status_change == "blocked" and current["status"] != "blocked":
        changes["blocked_at"] = dt.datetime.now(dt.UTC)
    elif status_change == "active":
        changes["blocked_at"] = None
    unknown = set(changes) - set(UPDATABLE)
    if unknown:  # pragma: no cover — the schema already forbids this
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"cannot update: {unknown}")

    # Column names come from UPDATABLE and are validated again inside
    # sql.update; values are always bound. A request can influence what is
    # written, never which column is written to.
    row = await conn.fetchrow(
        sql.update(
            "event_definitions",
            list(changes),
            where="id = $1 AND app_id = $2",
            returning=DEFINITION_COLUMNS,
            start=3,
        ),
        definition_id,
        app_id,
        *changes.values(),
    )
    assert row is not None

    if status_change and status_change != current["status"]:
        await _publish_blocked(context, conn, app_id)
        await audit.record(
            conn,
            organization_id=principal.org_id,
            action=(
                "event_definition.blocked"
                if status_change == "blocked"
                else "event_definition.unblocked"
            ),
            resource_type="event_definition",
            resource_id=str(definition_id),
            actor_user_id=principal.user_id,
            detail={"app_id": str(app_id), "name": current["name"]},
        )
        log.info(
            "event_definition_status_changed",
            app_id=str(app_id),
            name=current["name"],
            status=status_change,
        )
    return dict(row)


@router.delete("/apps/{app_id}/events/{definition_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_event_definition(
    app_id: uuid.UUID,
    definition_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    context: Annotated[AppContext, Depends(get_context)],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> None:
    """Remove the definition. The events it described are untouched: a
    catalogue entry is documentation, and deleting documentation must never
    delete data."""
    await _require_app(conn, app_id)
    row = await conn.fetchrow(
        "DELETE FROM event_definitions WHERE id = $1 AND app_id = $2 RETURNING name, status",
        definition_id,
        app_id,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event definition not found")
    if row["status"] == "blocked":
        await _publish_blocked(context, conn, app_id)
    await audit.record(
        conn,
        organization_id=principal.org_id,
        action="event_definition.deleted",
        resource_type="event_definition",
        resource_id=str(definition_id),
        actor_user_id=principal.user_id,
        detail={"app_id": str(app_id), "name": row["name"]},
    )


# Exported for the dashboard's "add event" form, which offers the categories.
EVENT_CATEGORIES = (*CATEGORIES, "custom")
