"""Registration, login, logout, current user."""

from __future__ import annotations

import re
import unicodedata
import uuid
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from mmp_core.ids import uuid7
from mmp_core.logging import get_logger
from mmp_core.ratelimit import LOGIN_PER_ACCOUNT, LOGIN_PER_ADDRESS
from mmp_crypto.passwords import (
    PasswordPolicyError,
    hash_password,
    needs_rehash,
    verify_password_constant_work,
)
from mmp_crypto.pii import hash_ip

from mmp_api.context import AppContext
from mmp_api.deps import Principal, current_principal, current_session, get_context
from mmp_api.schemas import Login, OrganizationOut, Registration, UserOut
from mmp_api.sessions import COOKIE_NAME, CSRF_COOKIE_NAME, Session

router = APIRouter(tags=["auth"])
log = get_logger(__name__)


def _slugify(name: str) -> str:
    normalised = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", normalised.lower()).strip("-")
    return (slug or "org")[:60]


def _client_ip(request: Request) -> str:
    # X-Forwarded-For is only trustworthy behind our own proxy, which is the
    # only deployment shape. The left-most entry is the client; the rest are
    # hops. Never trust it for authorisation — only for rate limiting and geo.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    # A literal rather than an address: this only keys a rate limiter, and a
    # made-up IP would silently pool every client with no peer address together.
    return request.client.host if request.client else "unknown"


def _set_session_cookies(response: Response, token: str, session: Session, *, secure: bool) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,  # unreadable from JavaScript, so XSS cannot exfiltrate it
        secure=secure,
        samesite="lax",
        max_age=7 * 24 * 3600,
        path="/",
    )
    # Readable by design: the dashboard's JS must echo it in a header. Its
    # security comes from the same-origin policy, not from secrecy.
    response.set_cookie(
        CSRF_COOKIE_NAME,
        session.csrf_token,
        httponly=False,
        secure=secure,
        samesite="lax",
        max_age=7 * 24 * 3600,
        path="/",
    )


@router.post("/auth/register", status_code=status.HTTP_201_CREATED)
async def register(
    body: Registration,
    request: Request,
    response: Response,
    context: Annotated[AppContext, Depends(get_context)],
) -> OrganizationOut:
    """Create a user, their first organisation, and their owner membership."""
    try:
        password_hash = hash_password(body.password)
    except PasswordPolicyError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    user_id, org_id = uuid7(), uuid7()
    slug = f"{_slugify(body.organization_name)}-{uuid.uuid4().hex[:6]}"

    async with context.database.system_connection() as conn, conn.transaction():
        try:
            await conn.execute(
                "INSERT INTO users (id, email, password_hash, name, is_active) "
                "VALUES ($1, $2, $3, $4, true)",
                user_id,
                body.email.lower(),
                password_hash,
                body.name,
            )
        except asyncpg.exceptions.UniqueViolationError as exc:
            # Deliberately the same message a duplicate would produce elsewhere:
            # a distinct "email already registered" turns this endpoint into an
            # account-existence oracle.
            raise HTTPException(
                status.HTTP_409_CONFLICT, "could not create account with those details"
            ) from exc

        await conn.execute(
            "INSERT INTO organizations (id, name, slug, timezone) VALUES ($1, $2, $3, 'UTC')",
            org_id,
            body.organization_name,
            slug,
        )
        await conn.execute(
            "INSERT INTO organization_members (id, organization_id, user_id, role) "
            "VALUES ($1, $2, $3, 'owner')",
            uuid7(),
            org_id,
            user_id,
        )

    token, session = await context.sessions.create(
        user_id=user_id,
        organization_id=org_id,
        ip_hash=hash_ip(_client_ip(request), pepper=context.settings.ip_hash_pepper),
    )
    _set_session_cookies(response, token, session, secure=context.settings.is_prod)
    log.info("user_registered", user_id=str(user_id), organization_id=str(org_id))
    return OrganizationOut(
        id=org_id, name=body.organization_name, slug=slug, timezone="UTC", role="owner"
    )


@router.post("/auth/login")
async def login(
    body: Login,
    request: Request,
    response: Response,
    context: Annotated[AppContext, Depends(get_context)],
) -> UserOut:
    email = body.email.lower()
    address = _client_ip(request)

    # Two limits: per account so one user cannot be brute-forced, per address so
    # one attacker cannot spray many accounts at one guess each.
    for key, limit in (
        (f"login:account:{email}", LOGIN_PER_ACCOUNT),
        (f"login:ip:{address}", LOGIN_PER_ADDRESS),
    ):
        result = await context.limiter.check(key, limit)
        if not result.allowed:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "too many attempts",
                headers={"retry-after": str(max(1, result.retry_after_ms // 1000))},
            )

    async with context.database.system_connection() as conn:
        row = await conn.fetchrow(
            "SELECT id, email, name, password_hash, is_active, created_at "
            "FROM users WHERE email = $1",
            email,
        )

    stored_hash = row["password_hash"] if row else None
    # Runs the hash even when there is no such user, so the response time does
    # not reveal which emails have accounts.
    if not verify_password_constant_work(stored_hash, body.password) or not row["is_active"]:
        log.info("login_failed", email_domain=email.rpartition("@")[2])
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid email or password")

    if needs_rehash(row["password_hash"]):
        async with context.database.system_connection() as conn:
            await conn.execute(
                "UPDATE users SET password_hash = $1 WHERE id = $2",
                hash_password(body.password),
                row["id"],
            )

    async with context.database.system_connection() as conn:
        organization_id = await conn.fetchval(
            "SELECT organization_id FROM organization_members WHERE user_id = $1 "
            "ORDER BY created_at LIMIT 1",
            row["id"],
        )
        await conn.execute("UPDATE users SET last_login_at = now() WHERE id = $1", row["id"])

    token, session = await context.sessions.create(
        user_id=row["id"],
        organization_id=organization_id,
        ip_hash=hash_ip(address, pepper=context.settings.ip_hash_pepper),
    )
    _set_session_cookies(response, token, session, secure=context.settings.is_prod)
    log.info("login_succeeded", user_id=str(row["id"]))
    return UserOut(id=row["id"], email=row["email"], name=row["name"], created_at=row["created_at"])


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    session_pair: Annotated[tuple[str, Session], Depends(current_session)],
    context: Annotated[AppContext, Depends(get_context)],
) -> None:
    token, _ = session_pair
    await context.sessions.revoke(token)
    response.delete_cookie(COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")


@router.post("/auth/logout-everywhere", status_code=status.HTTP_204_NO_CONTENT)
async def logout_everywhere(
    response: Response,
    principal: Annotated[Principal, Depends(current_principal)],
    context: Annotated[AppContext, Depends(get_context)],
) -> None:
    """Revoke every session for this user.

    Real revocation is the reason sessions are server-side. This is what makes
    "I think my laptop was stolen" an action rather than a wait.
    """
    revoked = await context.sessions.revoke_all_for_user(principal.user_id)
    response.delete_cookie(COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")
    log.info("sessions_revoked", user_id=str(principal.user_id), count=revoked)


@router.get("/auth/me")
async def me(
    principal: Annotated[Principal, Depends(current_principal)],
    context: Annotated[AppContext, Depends(get_context)],
) -> UserOut:
    async with context.database.system_connection() as conn:
        row = await conn.fetchrow(
            "SELECT id, email, name, created_at FROM users WHERE id = $1", principal.user_id
        )
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user no longer exists")
    return UserOut(**dict(row))
