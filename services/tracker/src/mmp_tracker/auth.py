"""API key authentication on the hot path.

Budget: this runs on every ingest request, so the target is a Redis GET and one
HMAC — no Postgres query in the common case.

The cache holds the *resolved* record (organisation, app, environment), keyed by
the key prefix, never the key itself or its hash. A cache miss falls through to
one indexed lookup. Revocation deletes the cache entry directly, so a revoked
key stops working immediately rather than at the end of a TTL — revocation that
waits for a cache to expire is not revocation.
"""

from __future__ import annotations

import datetime as dt

import msgspec
from mmp_core.logging import get_logger
from mmp_crypto.keys import parse_key, verify_key
from mmp_db.pool import Database
from redis.asyncio import Redis

log = get_logger(__name__)

CACHE_PREFIX = "apikey:"
CACHE_TTL = dt.timedelta(minutes=10)
# A negative cache, so a flood of invalid keys cannot be turned into a flood of
# database lookups. Short, because a key created a moment ago should work.
NEGATIVE_CACHE_TTL = dt.timedelta(seconds=30)
NEGATIVE_MARKER = "-"


class AuthRecord(msgspec.Struct):
    app_id: str
    organization_id: str
    environment: str
    kind: str
    key_hash: bytes
    app_status: str
    consent_mode: str = "permissive"


class AuthError(Exception):
    """Authentication failed. Never carries a reason to the caller.

    "unknown key" versus "wrong secret" versus "revoked" would let anyone probe
    which prefixes are real.
    """


_encoder = msgspec.msgpack.Encoder()
_decoder = msgspec.msgpack.Decoder(AuthRecord)

LOOKUP_SQL = """
SELECT k.app_id, k.organization_id, k.environment, k.kind, k.key_hash,
       a.status AS app_status, a.consent_mode
FROM api_keys k
JOIN apps a ON a.id = k.app_id
WHERE k.key_prefix = $1 AND k.status = 'active'
"""


class KeyAuthenticator:
    def __init__(self, database: Database, redis: Redis, *, pepper: str) -> None:
        self._database = database
        self._redis = redis
        self._pepper = pepper

    async def authenticate(self, presented: str) -> AuthRecord:
        parsed = parse_key(presented)
        if parsed is None:
            raise AuthError("malformed key")

        cache_key = CACHE_PREFIX + parsed.prefix
        cached = await self._redis.get(cache_key)

        if cached == NEGATIVE_MARKER or cached == NEGATIVE_MARKER.encode():
            raise AuthError("known-bad prefix")

        if cached:
            record = _decoder.decode(cached if isinstance(cached, bytes) else cached.encode())
        else:
            record = await self._load(parsed.prefix, cache_key)

        # Constant-time comparison, and only after the record is in hand. An
        # early return on a prefix miss versus a secret miss is a timing oracle.
        if not verify_key(
            presented_secret=parsed.secret, stored_hash=record.key_hash, pepper=self._pepper
        ):
            raise AuthError("secret mismatch")

        if record.environment != parsed.environment:
            # The environment is encoded in the key text; a mismatch means the
            # key was edited, which is never legitimate.
            raise AuthError("environment mismatch")
        if record.app_status != "active":
            raise AuthError("app is not active")

        return record

    async def _load(self, prefix: str, cache_key: str) -> AuthRecord:
        async with self._database.acquire_raw() as conn:
            row = await conn.fetchrow(LOOKUP_SQL, prefix)

        if row is None:
            await self._redis.set(
                cache_key, NEGATIVE_MARKER, ex=int(NEGATIVE_CACHE_TTL.total_seconds())
            )
            raise AuthError("no such key")

        record = AuthRecord(
            app_id=str(row["app_id"]),
            organization_id=str(row["organization_id"]),
            environment=row["environment"],
            kind=row["kind"],
            key_hash=bytes(row["key_hash"]),
            app_status=row["app_status"],
            consent_mode=row["consent_mode"],
        )
        await self._redis.set(cache_key, _encoder.encode(record), ex=int(CACHE_TTL.total_seconds()))
        return record
