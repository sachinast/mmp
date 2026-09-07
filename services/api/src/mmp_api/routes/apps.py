"""App registration and configuration."""

from __future__ import annotations

import uuid
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from mmp_core.ids import uuid7
from mmp_core.logging import get_logger
from mmp_db.types import DbConn

from mmp_api.deps import Principal, require_role, tenant_db
from mmp_api.schemas import AppCreate, AppOut, AppUpdate
from mmp_db import sql

router = APIRouter(prefix="/apps", tags=["apps"])
log = get_logger(__name__)

APP_COLUMNS = (
    "id",
    "name",
    "platform",
    "android_package_name",
    "ios_bundle_id",
    "timezone",
    "status",
    "install_window_days",
    "event_window_days",
    "created_at",
)

# Only these columns may be updated, and the allowlist is the *only* source of
# column names in the generated SQL. Building an UPDATE from request keys is how
# an endpoint that looks like a partial update becomes a way to set any column.
UPDATABLE = ("name", "timezone", "status", "install_window_days", "event_window_days")


@router.get("")
async def list_apps(
    _principal: Annotated[Principal, Depends(require_role("viewer"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> list[AppOut]:
    rows = await conn.fetch(sql.select("apps", APP_COLUMNS, suffix="ORDER BY created_at DESC"))
    return [AppOut(**dict(row)) for row in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_app(
    body: AppCreate,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> AppOut:
    if body.platform in ("android", "cross_platform") and not body.android_package_name:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "android_package_name is required for this platform",
        )
    if body.platform in ("ios", "cross_platform") and not body.ios_bundle_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "ios_bundle_id is required for this platform",
        )

    app_id = uuid7()
    try:
        row = await conn.fetchrow(
            sql.with_returning(
                """INSERT INTO apps (id, organization_id, name, platform,
                                     android_package_name, ios_bundle_id, timezone, status,
                                     install_window_days, event_window_days,
                                     session_timeout_minutes)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, 'active', $8, $9, 30)""",
                APP_COLUMNS,
            ),
            app_id,
            principal.organization_id,
            body.name,
            body.platform,
            body.android_package_name,
            body.ios_bundle_id,
            body.timezone,
            body.install_window_days,
            body.event_window_days,
        )
    except asyncpg.exceptions.CheckViolationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid app configuration"
        ) from exc

    log.info("app_created", app_id=str(app_id), organization_id=str(principal.organization_id))
    return AppOut(**dict(row))


@router.get("/{app_id}")
async def get_app(
    app_id: uuid.UUID,
    _principal: Annotated[Principal, Depends(require_role("viewer"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> AppOut:
    # No organization_id filter in this query, on purpose: RLS supplies it. An
    # app belonging to another tenant is simply not visible on this connection.
    row = await conn.fetchrow(sql.select("apps", APP_COLUMNS, where="id = $1"), app_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "app not found")
    return AppOut(**dict(row))


@router.patch("/{app_id}")
async def update_app(
    app_id: uuid.UUID,
    body: AppUpdate,
    _principal: Annotated[Principal, Depends(require_role("admin"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> AppOut:
    changes = body.model_dump(exclude_unset=True)
    unknown = set(changes) - set(UPDATABLE)
    if unknown:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"cannot update: {unknown}")
    if not changes:
        return await get_app(app_id, _principal, conn)

    # Column names come from UPDATABLE above and are validated again inside
    # sql.update; values are always bound. A request can influence what is
    # written, never which column is written to.
    row = await conn.fetchrow(
        sql.update("apps", list(changes), where="id = $1", returning=APP_COLUMNS),
        app_id,
        *changes.values(),
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "app not found")
    return AppOut(**dict(row))


@router.delete("/{app_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disable_app(
    app_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> None:
    """Disable, never delete.

    Deleting an app would cascade to its campaigns and attribution history —
    numbers a client may have already reported to an ad network. Disabling stops
    ingestion and leaves the record intact.
    """
    result = await conn.execute(
        "UPDATE apps SET status = 'disabled', updated_at = now() WHERE id = $1", app_id
    )
    if result == "UPDATE 0":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "app not found")
    log.info("app_disabled", app_id=str(app_id), actor=str(principal.user_id))
