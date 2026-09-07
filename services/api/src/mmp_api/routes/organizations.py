"""Organisations, membership and role management."""

from __future__ import annotations

import re
import unicodedata
import uuid
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from mmp_core.ids import uuid7
from mmp_core.logging import get_logger

from mmp_api.context import AppContext
from mmp_api.deps import Principal, current_principal, get_context, require_role
from mmp_api.schemas import MemberInvite, MemberOut, OrganizationCreate, OrganizationOut

router = APIRouter(prefix="/organizations", tags=["organizations"])
log = get_logger(__name__)


def _slugify(name: str) -> str:
    normalised = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return (re.sub(r"[^a-z0-9]+", "-", normalised.lower()).strip("-") or "org")[:60]


@router.get("")
async def list_organizations(
    principal: Annotated[Principal, Depends(current_principal)],
    context: Annotated[AppContext, Depends(get_context)],
) -> list[OrganizationOut]:
    """Every organisation this user belongs to.

    Scoped by membership rather than by RLS: the caller has no active tenant yet
    at this point, which is exactly the state this endpoint exists to resolve.
    """
    async with context.database.system_connection() as conn:
        rows = await conn.fetch(
            """SELECT o.id, o.name, o.slug, o.timezone, m.role
               FROM organizations o
               JOIN organization_members m ON m.organization_id = o.id
               WHERE m.user_id = $1
               ORDER BY o.name""",
            principal.user_id,
        )
    return [OrganizationOut(**dict(row)) for row in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_organization(
    body: OrganizationCreate,
    principal: Annotated[Principal, Depends(current_principal)],
    context: Annotated[AppContext, Depends(get_context)],
) -> OrganizationOut:
    org_id = uuid7()
    slug = f"{_slugify(body.name)}-{uuid.uuid4().hex[:6]}"
    async with context.database.system_connection() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO organizations (id, name, slug, timezone) VALUES ($1, $2, $3, $4)",
            org_id,
            body.name,
            slug,
            body.timezone,
        )
        await conn.execute(
            "INSERT INTO organization_members (id, organization_id, user_id, role) "
            "VALUES ($1, $2, $3, 'owner')",
            uuid7(),
            org_id,
            principal.user_id,
        )
    return OrganizationOut(
        id=org_id, name=body.name, slug=slug, timezone=body.timezone, role="owner"
    )


@router.post("/{organization_id}/switch")
async def switch_organization(
    organization_id: uuid.UUID,
    principal: Annotated[Principal, Depends(current_principal)],
    context: Annotated[AppContext, Depends(get_context)],
) -> OrganizationOut:
    """Change the active organisation on the current session.

    Membership is re-checked here rather than trusted from the request: without
    this check, switching would be a self-service grant of access to any
    organisation whose ID the caller can guess.
    """
    async with context.database.system_connection() as conn:
        row = await conn.fetchrow(
            """SELECT o.id, o.name, o.slug, o.timezone, m.role
               FROM organizations o
               JOIN organization_members m ON m.organization_id = o.id
               WHERE o.id = $1 AND m.user_id = $2""",
            organization_id,
            principal.user_id,
        )
    if row is None:
        # 404 rather than 403: confirming that an organisation exists but is
        # off-limits is itself information.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "organization not found")

    await context.sessions.set_active_organization(principal.session_token, organization_id)
    return OrganizationOut(**dict(row))


@router.get("/members")
async def list_members(
    principal: Annotated[Principal, Depends(require_role("viewer"))],
    context: Annotated[AppContext, Depends(get_context)],
) -> list[MemberOut]:
    async with context.database.system_connection() as conn:
        rows = await conn.fetch(
            """SELECT u.id AS user_id, u.email, u.name, m.role
               FROM organization_members m
               JOIN users u ON u.id = m.user_id
               WHERE m.organization_id = $1
               ORDER BY u.email""",
            principal.organization_id,
        )
    return [MemberOut(**dict(row)) for row in rows]


@router.post("/members", status_code=status.HTTP_201_CREATED)
async def add_member(
    body: MemberInvite,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    context: Annotated[AppContext, Depends(get_context)],
) -> MemberOut:
    """Add an existing user to this organisation.

    An admin cannot grant a role above their own — otherwise "admin" is
    functionally "owner" via one extra call.
    """
    from mmp_api.deps import ROLE_RANK

    if ROLE_RANK[body.role] > ROLE_RANK[principal.role or "viewer"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "cannot grant a role higher than your own")

    async with context.database.system_connection() as conn:
        user = await conn.fetchrow(
            "SELECT id, email, name FROM users WHERE email = $1", body.email.lower()
        )
        if user is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user")
        try:
            await conn.execute(
                "INSERT INTO organization_members (id, organization_id, user_id, role) "
                "VALUES ($1, $2, $3, $4)",
                uuid7(),
                principal.organization_id,
                user["id"],
                body.role,
            )
        except asyncpg.exceptions.UniqueViolationError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "already a member of this organization"
            ) from exc

    log.info(
        "member_added",
        organization_id=str(principal.organization_id),
        role=body.role,
        actor=str(principal.user_id),
    )
    return MemberOut(user_id=user["id"], email=user["email"], name=user["name"], role=body.role)


@router.delete("/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    user_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    context: Annotated[AppContext, Depends(get_context)],
) -> None:
    async with context.database.system_connection() as conn:
        target_role = await conn.fetchval(
            "SELECT role FROM organization_members WHERE organization_id = $1 AND user_id = $2",
            principal.organization_id,
            user_id,
        )
        if target_role is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "not a member")

        if target_role == "owner":
            remaining = await conn.fetchval(
                "SELECT count(*) FROM organization_members "
                "WHERE organization_id = $1 AND role = 'owner'",
                principal.organization_id,
            )
            # An organisation with no owner cannot grant anyone access to it
            # again — an unrecoverable state, reachable by one careless click.
            if remaining <= 1:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "cannot remove the last owner of an organization",
                )

        await conn.execute(
            "DELETE FROM organization_members WHERE organization_id = $1 AND user_id = $2",
            principal.organization_id,
            user_id,
        )

    log.info(
        "member_removed",
        organization_id=str(principal.organization_id),
        actor=str(principal.user_id),
    )
