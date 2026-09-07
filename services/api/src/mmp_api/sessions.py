"""Server-side sessions in Redis.

A JWT would avoid this round trip, and would also make logout a lie: a signed
token stays valid until it expires no matter what the user clicks. For a
platform where a compromised dashboard session can exfiltrate an organisation's
campaign data and rotate its API keys, revocation has to be real.

So sessions are opaque random identifiers, stored server-side, deletable. The
cost is one Redis GET per dashboard request — on the API tier, not the tracking
tier, where it would matter.
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
import uuid
from collections.abc import Awaitable
from dataclasses import asdict, dataclass
from typing import Any, cast

from redis.asyncio import Redis


def _await[T](result: Awaitable[T] | T) -> Awaitable[T]:
    """Narrow redis-py's sync/async union.

    The client is typed for both modes, so every call returns ``Awaitable[T] | T``
    even though the async client always returns the awaitable. Casting once here
    beats an ignore comment on every call site.
    """
    return cast("Awaitable[T]", result)


SESSION_BYTES = 32
SESSION_PREFIX = "session:"
USER_SESSIONS_PREFIX = "user_sessions:"

DEFAULT_TTL = dt.timedelta(days=7)
# Sliding expiry, but capped: an always-open tab must not confer an
# indefinitely valid session.
ABSOLUTE_TTL = dt.timedelta(days=30)

COOKIE_NAME = "mmp_session"
CSRF_COOKIE_NAME = "mmp_csrf"
CSRF_HEADER = "x-csrf-token"


@dataclass(frozen=True)
class Session:
    user_id: str
    organization_id: str | None
    csrf_token: str
    created_at: str
    ip_hash: str | None = None

    @property
    def age(self) -> dt.timedelta:
        return dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(self.created_at)


class SessionStore:
    def __init__(self, redis: Redis, *, ttl: dt.timedelta = DEFAULT_TTL) -> None:
        self._redis = redis
        self._ttl = ttl

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        organization_id: uuid.UUID | None = None,
        ip_hash: bytes | None = None,
    ) -> tuple[str, Session]:
        token = secrets.token_urlsafe(SESSION_BYTES)
        session = Session(
            user_id=str(user_id),
            organization_id=str(organization_id) if organization_id else None,
            csrf_token=secrets.token_urlsafe(32),
            created_at=dt.datetime.now(dt.UTC).isoformat(),
            ip_hash=ip_hash.hex() if ip_hash else None,
        )
        await self._redis.setex(
            SESSION_PREFIX + token, int(self._ttl.total_seconds()), json.dumps(asdict(session))
        )
        # Indexed by user so that "log out everywhere" and "revoke on password
        # change" are one operation rather than a scan.
        await _await(self._redis.sadd(USER_SESSIONS_PREFIX + str(user_id), token))
        await _await(
            self._redis.expire(
                USER_SESSIONS_PREFIX + str(user_id), int(ABSOLUTE_TTL.total_seconds())
            )
        )
        return token, session

    async def get(self, token: str) -> Session | None:
        raw = await self._redis.get(SESSION_PREFIX + token)
        if raw is None:
            return None
        session = Session(**json.loads(raw))
        if session.age > ABSOLUTE_TTL:
            await self.revoke(token)
            return None
        # Sliding window: active use extends the session, up to the absolute cap.
        await _await(self._redis.expire(SESSION_PREFIX + token, int(self._ttl.total_seconds())))
        return session

    async def set_active_organization(self, token: str, organization_id: uuid.UUID) -> None:
        session = await self.get(token)
        if session is None:
            return
        updated = Session(
            user_id=session.user_id,
            organization_id=str(organization_id),
            csrf_token=session.csrf_token,
            created_at=session.created_at,
            ip_hash=session.ip_hash,
        )
        ttl = await _await(self._redis.ttl(SESSION_PREFIX + token))
        await self._redis.setex(
            SESSION_PREFIX + token,
            ttl if ttl > 0 else int(self._ttl.total_seconds()),
            json.dumps(asdict(updated)),
        )

    async def revoke(self, token: str) -> None:
        session = await self.get_raw(token)
        await self._redis.delete(SESSION_PREFIX + token)
        if session is not None:
            await _await(self._redis.srem(USER_SESSIONS_PREFIX + session.user_id, token))

    async def get_raw(self, token: str) -> Session | None:
        """Read without extending the TTL — used by revocation paths."""
        raw = await self._redis.get(SESSION_PREFIX + token)
        return Session(**json.loads(raw)) if raw is not None else None

    async def revoke_all_for_user(self, user_id: uuid.UUID) -> int:
        """Called on password change and on role removal.

        A password reset that leaves the attacker's existing session alive has
        not actually locked anyone out.
        """
        key = USER_SESSIONS_PREFIX + str(user_id)
        tokens: set[Any] = await _await(self._redis.smembers(key))
        if tokens:
            await self._redis.delete(*[SESSION_PREFIX + t for t in tokens])
        await self._redis.delete(key)
        return len(tokens)
