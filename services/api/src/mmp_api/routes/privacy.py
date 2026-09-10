"""Consent records and erasure requests.

The endpoints an operator uses to answer a data subject: what has this device
consented to, and please delete it. Both write to the audit log, because "we
deleted it" is a claim that has to be evidenced later.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from mmp_core.ids import uuid7
from mmp_core.logging import get_logger
from mmp_db.erasure import erase_device
from mmp_db.types import DbConn
from mmp_ingest.consent import consent_cache_key
from pydantic import BaseModel, Field

from mmp_api.context import AppContext
from mmp_api.deps import Principal, get_context, require_role, tenant_db
from mmp_db import audit, sql

router = APIRouter(prefix="/privacy", tags=["privacy"])
log = get_logger(__name__)

CONSENT_COLUMNS = (
    "id",
    "app_id",
    "anonymous_id",
    "purpose",
    "state",
    "source",
    "expires_at",
    "created_at",
)


class ConsentRecord(BaseModel):
    app_id: uuid.UUID
    anonymous_id: Annotated[str, Field(min_length=1, max_length=255)]
    purpose: Literal["analytics", "attribution", "advertising"]
    state: Literal["unknown", "granted", "denied"]
    source: Annotated[str, Field(max_length=40)] | None = None
    expires_at: dt.datetime | None = None


class ConsentOut(BaseModel):
    id: uuid.UUID
    app_id: uuid.UUID
    anonymous_id: str
    purpose: str
    state: str
    source: str | None
    expires_at: dt.datetime | None
    created_at: dt.datetime


class ErasureRequest(BaseModel):
    app_id: uuid.UUID
    anonymous_id: Annotated[str, Field(min_length=1, max_length=255)]
    # Supplied by the caller because we cannot derive it: the hash depends on a
    # pepper the operator does not hold, and the raw advertising id is never
    # stored. Without it the device's clicks stay identified.
    device_hash_hex: Annotated[str, Field(max_length=64)] | None = None


class ErasureOut(BaseModel):
    request_id: uuid.UUID
    scope: str
    deleted: dict[str, int]
    cleared: dict[str, int]
    total_deleted: int
    completed_at: dt.datetime | None


class AuditVerification(BaseModel):
    entries: int
    intact: bool
    broken_at: uuid.UUID | None
    note: str = (
        "Tamper-evident, not tamper-proof: anyone who can write to the table can "
        "also rewrite the chain from the point they altered."
    )


@router.get("/consent")
async def list_consent(
    _principal: Annotated[Principal, Depends(require_role("viewer"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
    app_id: uuid.UUID,
    anonymous_id: str,
) -> list[ConsentOut]:
    """Everything a device has told this app, purpose by purpose.

    Both parameters required: an unfiltered listing of consent records is a
    listing of every device in the app, which is neither useful nor something
    this endpoint should make easy.
    """
    # Check the app is visible first. Without it a request for another tenant's
    # app returns 200 with an empty list, which reads as "this device has given
    # no consent" rather than "this app is not yours" — and confirms the id is
    # real.
    if not await conn.fetchval("SELECT 1 FROM apps WHERE id = $1", app_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "app not found")

    rows = await conn.fetch(
        sql.select(
            "consent_states",
            CONSENT_COLUMNS,
            where="app_id = $1 AND anonymous_id = $2",
            suffix="ORDER BY created_at DESC",
        ),
        app_id,
        anonymous_id,
    )
    return [ConsentOut(**dict(row)) for row in rows]


@router.post("/consent", status_code=status.HTTP_201_CREATED)
async def record_consent(
    body: ConsentRecord,
    principal: Annotated[Principal, Depends(require_role("member"))],
    context: Annotated[AppContext, Depends(get_context)],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> ConsentOut:
    """Record a decision made outside the SDK.

    A consent management platform, a support agent acting on a written request,
    a backfill. The SDK's own path goes through the tracker as an ordinary
    event; this is for everything else.
    """
    if not await conn.fetchval("SELECT 1 FROM apps WHERE id = $1", body.app_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "app not found")

    row = await conn.fetchrow(
        sql.with_returning(
            """INSERT INTO consent_states (id, organization_id, app_id, anonymous_id,
                                           purpose, state, source, expires_at,
                                           created_at, updated_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now(), now())
               ON CONFLICT (app_id, anonymous_id, purpose)
               DO UPDATE SET state = EXCLUDED.state, source = EXCLUDED.source,
                             expires_at = EXCLUDED.expires_at, updated_at = now()""",
            CONSENT_COLUMNS,
        ),
        uuid7(),
        principal.org_id,
        body.app_id,
        body.anonymous_id,
        body.purpose,
        body.state,
        body.source,
        body.expires_at,
    )

    # The tracker caches consent for five minutes. A withdrawal recorded here
    # must take effect now, not when that expires — which is exactly the delay a
    # person exercising their rights does not expect.
    await context.redis.delete(consent_cache_key(str(body.app_id), body.anonymous_id))

    await audit.record(
        conn,
        organization_id=principal.org_id,
        action="consent.recorded",
        resource_type="consent_state",
        resource_id=body.anonymous_id,
        actor_user_id=principal.user_id,
        detail={"purpose": body.purpose, "state": body.state, "source": body.source},
    )
    return ConsentOut(**dict(row))


@router.post("/erasure", status_code=status.HTTP_200_OK)
async def request_erasure(
    body: ErasureRequest,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    context: Annotated[AppContext, Depends(get_context)],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> ErasureOut:
    """Erase a device's data, and record that we did.

    Admin-only: this is destructive and irreversible, and a viewer's job is to
    read reports.

    Performed synchronously here because the volume for one device is small and
    a caller answering a regulator wants a completed result, not a ticket. A
    bulk erasure — a whole app, a whole organisation — would need the async path
    described in mmp_db.erasure, and does not exist yet.
    """
    if not await conn.fetchval("SELECT 1 FROM apps WHERE id = $1", body.app_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "app not found")

    device_hash = None
    if body.device_hash_hex:
        try:
            device_hash = bytes.fromhex(body.device_hash_hex)
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "device_hash_hex is not hex"
            ) from exc

    request_id = uuid7()
    await audit.record(
        conn,
        organization_id=principal.org_id,
        action="erasure.requested",
        resource_type="device",
        resource_id=body.anonymous_id,
        actor_user_id=principal.user_id,
        detail={"request_id": str(request_id), "app_id": str(body.app_id)},
    )

    result = await erase_device(
        conn,
        app_id=body.app_id,
        anonymous_id=body.anonymous_id,
        device_hash=device_hash,
        request_id=request_id,
    )

    await audit.record(
        conn,
        organization_id=principal.org_id,
        action="erasure.completed",
        resource_type="device",
        resource_id=body.anonymous_id,
        actor_user_id=principal.user_id,
        detail={
            "request_id": str(request_id),
            "deleted": result.deleted,
            "cleared": result.cleared,
        },
    )

    await context.redis.delete(consent_cache_key(str(body.app_id), body.anonymous_id))
    await context.redis.delete(f"attr:{body.app_id}:{body.anonymous_id}")

    log.info("erasure_served", **result.as_dict(), actor=str(principal.user_id))
    return ErasureOut(**result.as_dict())  # type: ignore[arg-type]


@router.get("/audit/verify")
async def verify_audit_chain(
    principal: Annotated[Principal, Depends(require_role("owner"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> AuditVerification:
    """Walk the audit chain and report the first entry that does not match.

    A chain nobody verifies is a chain nobody would notice was broken.
    """
    result = await audit.verify(conn, organization_id=principal.org_id)
    return AuditVerification(**result.as_dict())  # type: ignore[arg-type]
