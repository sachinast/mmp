"""API key lifecycle.

The security property this module exists to hold: **the raw key is returned
exactly once, at creation, and is otherwise unrecoverable from this system.**

Every function below is written so that violating that would require a
deliberate change, not an oversight. ``ApiKeyOut`` has no field that could carry
it; only the creation endpoint returns ``ApiKeyCreated``; nothing logs the
``GeneratedKey``, whose ``__repr__`` redacts itself anyway.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from mmp_core.ids import uuid7
from mmp_core.logging import get_logger
from mmp_crypto.keys import generate_key
from mmp_db.types import DbConn

from mmp_api.context import AppContext
from mmp_api.deps import Principal, get_context, require_role, tenant_db
from mmp_api.schemas import ApiKeyCreate, ApiKeyCreated, ApiKeyOut
from mmp_db import sql

router = APIRouter(prefix="/apps/{app_id}/keys", tags=["api-keys"])
log = get_logger(__name__)

KEY_COLUMNS = (
    "id",
    "app_id",
    "name",
    "kind",
    "key_prefix",
    "environment",
    "status",
    "last_used_at",
    "created_at",
)
MAX_ACTIVE_KEYS_PER_APP = 20


@router.get("")
async def list_keys(
    app_id: uuid.UUID,
    _principal: Annotated[Principal, Depends(require_role("admin"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> list[ApiKeyOut]:
    """Keys are admin-visible only.

    A viewer can read analytics; a viewer who can enumerate key prefixes and
    revoke credentials can take an advertiser's tracking offline.
    """
    # Check the app is visible on this connection first. Without it, a request
    # for another tenant's app returns 200 with an empty list — which reads to a
    # caller as "this app has no keys" rather than "this app is not yours", and
    # confirms the ID is valid.
    if not await conn.fetchval("SELECT 1 FROM apps WHERE id = $1", app_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "app not found")

    rows = await conn.fetch(
        sql.select("api_keys", KEY_COLUMNS, where="app_id = $1", suffix="ORDER BY created_at DESC"),
        app_id,
    )
    return [ApiKeyOut(**dict(row)) for row in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_key(
    app_id: uuid.UUID,
    body: ApiKeyCreate,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    context: Annotated[AppContext, Depends(get_context)],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> ApiKeyCreated:
    # RLS makes this a tenant-scoped existence check as well as a validity one.
    exists = await conn.fetchval("SELECT 1 FROM apps WHERE id = $1", app_id)
    if not exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "app not found")

    active = await conn.fetchval(
        "SELECT count(*) FROM api_keys WHERE app_id = $1 AND status = 'active'", app_id
    )
    if active >= MAX_ACTIVE_KEYS_PER_APP:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"an app may have at most {MAX_ACTIVE_KEYS_PER_APP} active keys; "
            "revoke one before creating another",
        )

    generated = generate_key(environment=body.environment, pepper=context.settings.api_key_pepper)
    key_id = uuid7()
    row = await conn.fetchrow(
        sql.with_returning(
            """INSERT INTO api_keys (id, organization_id, app_id, name, kind, key_prefix,
                                     key_hash, pepper_version, environment, status)
               VALUES ($1, $2, $3, $4, $5, $6, $7, 1, $8, 'active')""",
            KEY_COLUMNS,
        ),
        key_id,
        principal.organization_id,
        app_id,
        body.name,
        body.kind,
        generated.prefix,
        generated.key_hash,
        body.environment,
    )

    # Logged by prefix only. The prefix identifies the key in an audit trail
    # without being usable as one.
    log.info(
        "api_key_created",
        key_id=str(key_id),
        key_prefix=generated.prefix,
        app_id=str(app_id),
        environment=body.environment,
        actor=str(principal.user_id),
    )
    return ApiKeyCreated(**dict(row), api_key=generated.raw)


@router.post("/{key_id}/rotate")
async def rotate_key(
    app_id: uuid.UUID,
    key_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    context: Annotated[AppContext, Depends(get_context)],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> ApiKeyCreated:
    """Issue a replacement key and revoke the old one.

    Rotation creates the new key *before* revoking the old one so that the
    caller never holds zero working credentials. Overlap is the point: an app
    with a live SDK cannot swap credentials atomically.
    """
    existing = await conn.fetchrow(
        "SELECT name, kind, environment FROM api_keys WHERE id = $1 AND app_id = $2",
        key_id,
        app_id,
    )
    if existing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "key not found")

    generated = generate_key(
        environment=existing["environment"], pepper=context.settings.api_key_pepper
    )
    new_id = uuid7()
    async with conn.transaction():
        row = await conn.fetchrow(
            sql.with_returning(
                """INSERT INTO api_keys (id, organization_id, app_id, name, kind, key_prefix,
                                         key_hash, pepper_version, environment, status)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, 1, $8, 'active')""",
                KEY_COLUMNS,
            ),
            new_id,
            principal.organization_id,
            app_id,
            existing["name"],
            existing["kind"],
            generated.prefix,
            generated.key_hash,
            existing["environment"],
        )
        await conn.execute(
            "UPDATE api_keys SET status = 'revoked', revoked_at = now() WHERE id = $1", key_id
        )

    log.info(
        "api_key_rotated",
        old_key_id=str(key_id),
        new_key_prefix=generated.prefix,
        actor=str(principal.user_id),
    )
    return ApiKeyCreated(**dict(row), api_key=generated.raw)


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_key(
    app_id: uuid.UUID,
    key_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    context: Annotated[AppContext, Depends(get_context)],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> None:
    row = await conn.fetchrow(
        "UPDATE api_keys SET status = 'revoked', revoked_at = now() "
        "WHERE id = $1 AND app_id = $2 AND status = 'active' RETURNING key_prefix",
        key_id,
        app_id,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "active key not found")

    # The tracker caches authenticated keys; without this the revoked key would
    # keep working until the cache expired. Revocation has to be immediate or it
    # is not revocation.
    await context.redis.delete(f"apikey:{row['key_prefix']}")
    log.info("api_key_revoked", key_id=str(key_id), actor=str(principal.user_id))
