"""Campaigns and tracking links."""

from __future__ import annotations

import secrets
import uuid
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from mmp_core.ids import uuid7
from mmp_core.logging import get_logger
from mmp_db.notify import notify_tracking_link_changed
from mmp_db.types import DbConn

from mmp_api.deps import Principal, require_role, tenant_db
from mmp_api.schemas import (
    CampaignCreate,
    CampaignOut,
    TrackingLinkCreate,
    TrackingLinkOut,
    TrackingLinkUpdate,
)
from mmp_db import sql

router = APIRouter(tags=["campaigns"])
log = get_logger(__name__)

CAMPAIGN_COLUMNS = (
    "id",
    "app_id",
    "name",
    "source",
    "medium",
    "external_campaign_id",
    "status",
    "created_at",
)
LINK_COLUMNS = (
    "id",
    "app_id",
    "campaign_id",
    "tracking_code",
    "name",
    "android_url",
    "ios_url",
    "fallback_url",
    "deep_link_path",
    "status",
    "created_at",
)
LINK_UPDATABLE = ("name", "android_url", "ios_url", "fallback_url", "deep_link_path", "status")

# 22 characters of base62 is ~131 bits. Far more than uniqueness needs, and
# that is the point: a tracking code is effectively public — it appears in ad
# creative and in browser history — so it must be unguessable. A short or
# sequential code lets a competitor enumerate an advertiser's entire campaign
# structure, and lets anyone fabricate clicks against a known link.
CODE_LENGTH = 22
CODE_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def generate_tracking_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


# ---------------------------------------------------------------- campaigns
@router.get("/campaigns")
async def list_campaigns(
    _principal: Annotated[Principal, Depends(require_role("viewer"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
    app_id: uuid.UUID | None = None,
) -> list[CampaignOut]:
    if app_id is not None:
        rows = await conn.fetch(
            sql.select(
                "campaigns",
                CAMPAIGN_COLUMNS,
                where="app_id = $1",
                suffix="ORDER BY created_at DESC",
            ),
            app_id,
        )
    else:
        rows = await conn.fetch(
            sql.select("campaigns", CAMPAIGN_COLUMNS, suffix="ORDER BY created_at DESC")
        )
    return [CampaignOut(**dict(row)) for row in rows]


@router.post("/campaigns", status_code=status.HTTP_201_CREATED)
async def create_campaign(
    body: CampaignCreate,
    principal: Annotated[Principal, Depends(require_role("member"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> CampaignOut:
    if not await conn.fetchval("SELECT 1 FROM apps WHERE id = $1", body.app_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "app not found")
    try:
        row = await conn.fetchrow(
            sql.with_returning(
                """INSERT INTO campaigns (id, organization_id, app_id, name, source, medium,
                                          external_campaign_id, status)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, 'active')""",
                CAMPAIGN_COLUMNS,
            ),
            uuid7(),
            principal.organization_id,
            body.app_id,
            body.name,
            body.source,
            body.medium,
            body.external_campaign_id,
        )
    except asyncpg.exceptions.UniqueViolationError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "a campaign with that name already exists for this app"
        ) from exc
    return CampaignOut(**dict(row))


# ------------------------------------------------------------ tracking links
@router.get("/tracking-links")
async def list_links(
    _principal: Annotated[Principal, Depends(require_role("viewer"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
    campaign_id: uuid.UUID | None = None,
) -> list[TrackingLinkOut]:
    if campaign_id is not None:
        rows = await conn.fetch(
            sql.select(
                "tracking_links",
                LINK_COLUMNS,
                where="campaign_id = $1",
                suffix="ORDER BY created_at DESC",
            ),
            campaign_id,
        )
    else:
        rows = await conn.fetch(
            sql.select("tracking_links", LINK_COLUMNS, suffix="ORDER BY created_at DESC")
        )
    return [TrackingLinkOut(**dict(row)) for row in rows]


@router.post("/tracking-links", status_code=status.HTTP_201_CREATED)
async def create_link(
    body: TrackingLinkCreate,
    principal: Annotated[Principal, Depends(require_role("member"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> TrackingLinkOut:
    campaign = await conn.fetchrow("SELECT app_id FROM campaigns WHERE id = $1", body.campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "campaign not found")

    # Retried rather than assumed unique: 131 bits makes a collision
    # vanishingly unlikely, but "vanishingly unlikely" and "handled" are
    # different things, and the handling costs one loop.
    for _ in range(5):
        code = generate_tracking_code()
        try:
            row = await conn.fetchrow(
                sql.with_returning(
                    """INSERT INTO tracking_links (id, organization_id, app_id, campaign_id,
                                                   tracking_code, name, android_url, ios_url,
                                                   fallback_url, deep_link_path, status)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'active')""",
                    LINK_COLUMNS,
                ),
                uuid7(),
                principal.organization_id,
                campaign["app_id"],
                body.campaign_id,
                code,
                body.name,
                str(body.android_url) if body.android_url else None,
                str(body.ios_url) if body.ios_url else None,
                str(body.fallback_url),
                body.deep_link_path,
            )
            break
        except asyncpg.exceptions.UniqueViolationError:
            # At 131 bits this should never happen; if it starts happening, the
            # code generator is broken and that is worth knowing loudly.
            log.warning("tracking_code_collision", tracking_code=code)
            continue
    else:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "could not allocate a tracking code"
        )

    await _notify_trackers(conn, code)
    log.info("tracking_link_created", link_id=str(row["id"]), tracking_code=code)
    return TrackingLinkOut(**dict(row))


@router.patch("/tracking-links/{link_id}")
async def update_link(
    link_id: uuid.UUID,
    body: TrackingLinkUpdate,
    _principal: Annotated[Principal, Depends(require_role("member"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> TrackingLinkOut:
    changes = body.model_dump(exclude_unset=True)
    unknown = set(changes) - set(LINK_UPDATABLE)
    if unknown:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"cannot update: {unknown}")
    if not changes:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "nothing to update")

    # Pydantic URL types are not database values.
    changes = {
        k: (str(v) if v is not None and k.endswith("_url") else v) for k, v in changes.items()
    }

    row = await conn.fetchrow(
        sql.update("tracking_links", list(changes), where="id = $1", returning=LINK_COLUMNS),
        link_id,
        *changes.values(),
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tracking link not found")

    # Without this the change would take up to a full resync interval to reach
    # the tracker processes — so "I disabled that link" would not be true for
    # another five minutes.
    await _notify_trackers(conn, row["tracking_code"])
    return TrackingLinkOut(**dict(row))


@router.delete("/tracking-links/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disable_link(
    link_id: uuid.UUID,
    _principal: Annotated[Principal, Depends(require_role("member"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> None:
    """Disable, never delete.

    Clicks already recorded reference this link, and an advertiser's historical
    reporting must not develop holes because someone tidied up.
    """
    row = await conn.fetchrow(
        "UPDATE tracking_links SET status = 'disabled', updated_at = now() "
        "WHERE id = $1 RETURNING tracking_code",
        link_id,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tracking link not found")
    await _notify_trackers(conn, row["tracking_code"])


async def _notify_trackers(conn: DbConn, tracking_code: str) -> None:
    """Tell every tracker process to reload this link.

    Fire-and-forget by nature: a process that was disconnected when this was
    sent never sees it, which is why the cache also resyncs on a timer. This
    makes the common case fast, not the guarantee.
    """
    await notify_tracking_link_changed(conn, tracking_code)
