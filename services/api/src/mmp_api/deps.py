"""Request-scoped dependencies: who is calling, as which organisation, with what role.

The chain is deliberate and each link fails closed:

    session cookie -> session -> user -> active organisation -> membership role

``require_role`` is the only authorisation primitive. There is no "check the
role inline" path, because the inline version is the one that gets forgotten.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status
from mmp_db.types import DbConn

from mmp_api.context import AppContext
from mmp_api.sessions import COOKIE_NAME, CSRF_HEADER, Session

# Ordered by privilege. A requirement of "admin" is satisfied by "owner".
ROLE_RANK = {"viewer": 0, "member": 1, "admin": 2, "owner": 3}

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def get_context(request: Request) -> AppContext:
    context: AppContext = request.app.state.context
    return context


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    session_token: str
    session: Session
    organization_id: uuid.UUID | None
    role: str | None

    @property
    def org_id(self) -> uuid.UUID:
        """The active organisation, for code reached through require_role.

        require_organization has already rejected the request if there is none,
        so this narrows the Optional without an assert — asserts vanish under -O,
        and this one guards a tenancy boundary.
        """
        if self.organization_id is None:  # pragma: no cover — require_role guarantees it
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "no active organization selected")
        return self.organization_id

    def at_least(self, role: str) -> bool:
        if self.role is None:
            return False
        return ROLE_RANK[self.role] >= ROLE_RANK[role]


async def current_session(
    request: Request, context: Annotated[AppContext, Depends(get_context)]
) -> tuple[str, Session]:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
    session = await context.sessions.get(token)
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session expired")

    # CSRF: the session cookie is SameSite=Lax, which stops the common cases but
    # not everything. A state-changing request must also echo the per-session
    # token, which a cross-origin page cannot read.
    if request.method in UNSAFE_METHODS:
        presented = request.headers.get(CSRF_HEADER)
        import hmac

        if not presented or not hmac.compare_digest(presented, session.csrf_token):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "csrf token missing or invalid")

    return token, session


async def current_principal(
    session_pair: Annotated[tuple[str, Session], Depends(current_session)],
    context: Annotated[AppContext, Depends(get_context)],
) -> Principal:
    token, session = session_pair
    user_id = uuid.UUID(session.user_id)
    organization_id = uuid.UUID(session.organization_id) if session.organization_id else None

    role: str | None = None
    if organization_id is not None:
        async with context.database.system_connection() as conn:
            role = await conn.fetchval(
                "SELECT role FROM organization_members WHERE user_id = $1 AND organization_id = $2",
                user_id,
                organization_id,
            )
        if role is None:
            # Membership was revoked while the session was live. The session
            # keeps its identity but loses the organisation immediately —
            # authorisation is re-derived per request, never cached in the token.
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "no longer a member of the active organization"
            )

    return Principal(
        user_id=user_id,
        session_token=token,
        session=session,
        organization_id=organization_id,
        role=role,
    )


async def require_organization(
    principal: Annotated[Principal, Depends(current_principal)],
) -> Principal:
    if principal.organization_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no active organization selected")
    return principal


def require_role(minimum: str) -> Any:
    """Authorisation dependency factory.

    Usage: ``principal: Annotated[Principal, Depends(require_role("admin"))]``.
    """
    if minimum not in ROLE_RANK:
        raise ValueError(f"unknown role: {minimum!r}")

    async def dependency(
        principal: Annotated[Principal, Depends(require_organization)],
    ) -> Principal:
        if not principal.at_least(minimum):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"requires {minimum} role or higher",
            )
        return principal

    return dependency


async def tenant_db(
    principal: Annotated[Principal, Depends(require_organization)],
    context: Annotated[AppContext, Depends(get_context)],
) -> AsyncIterator[DbConn]:
    """A connection scoped to the caller's organisation for one transaction.

    Every tenant-data endpoint takes this rather than a raw pool. RLS then makes
    a forgotten filter return nothing instead of returning someone else's rows.
    """
    if principal.organization_id is None:  # pragma: no cover — require_organization guarantees it
        # Not an assert: asserts vanish under -O, and this one is the guard that
        # stops an unscoped connection reaching a tenant-data query.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no active organization selected")
    async with context.database.tenant_connection(principal.organization_id) as conn:
        yield conn
