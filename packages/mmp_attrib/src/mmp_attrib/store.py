"""Persisting attributions, and the identity cache that makes them useful.

Two writes, for two different readers:

* **Postgres** is the source of truth. The partial unique index on
  ``(app_id, install_key) WHERE superseded_by IS NULL`` is what makes "one
  install, one attribution" true even with several workers racing on a
  redelivered message — the loser's ``ON CONFLICT DO NOTHING`` turns the race
  into a no-op rather than a duplicate.

* **Redis** is a lookup cache keyed by identity. When a purchase arrives three
  weeks later, the postback engine needs to know which campaign to credit, and
  it needs to know in milliseconds. Without this it would be a query against the
  attributions table on every conversion.

The cache is derived state: it can be lost and rebuilt, and every read falls
back to Postgres. It is never the thing that decides an attribution.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass

import msgspec
from mmp_core.ids import uuid7
from mmp_core.logging import get_logger
from mmp_db.types import DbConn
from redis.asyncio import Redis

from mmp_attrib.engine import Decision, Method, should_supersede
from mmp_attrib.fraud import Assessment

log = get_logger(__name__)

IDENTITY_PREFIX = "attr:"

INSERT_SQL = """
INSERT INTO attributions (
    id, organization_id, app_id, install_key, anonymous_id, user_id,
    click_id, campaign_id, tracking_link_id, source, medium,
    method, installed_at, attributed_at, window_days, expires_at,
    fraud_score, fraud_verdict, fraud_rules, deep_link,
    created_at, updated_at
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, now(), $14, $15,
        $16, $17, $18, $19,
        now(), now())
ON CONFLICT DO NOTHING
RETURNING id
"""

CURRENT_SQL = """
SELECT id, method, click_id, campaign_id, tracking_link_id, source, medium,
       installed_at, attributed_at, window_days, expires_at, user_id
FROM attributions
WHERE app_id = $1 AND install_key = $2 AND superseded_by IS NULL
"""

SUPERSEDE_SQL = "UPDATE attributions SET superseded_by = $1 WHERE id = $2"


class CachedAttribution(msgspec.Struct):
    attribution_id: str
    click_id: str | None
    campaign_id: str | None
    tracking_link_id: str | None
    method: str
    attributed_at: str
    expires_at: str


_encoder = msgspec.msgpack.Encoder()
_decoder = msgspec.msgpack.Decoder(CachedAttribution)


def install_key(app_id: uuid.UUID, anonymous_id: str) -> str:
    """The uniqueness key for "one install".

    An install is identified by the device's anonymous id within an app. Not by
    user id: the user may sign in later, or not at all, and an install that
    becomes attributable only after login would be attributed late and
    inconsistently.
    """
    return f"{app_id}:{anonymous_id}"


@dataclass(frozen=True)
class StoredAttribution:
    attribution_id: uuid.UUID
    method: Method
    created: bool
    superseded: uuid.UUID | None = None


async def record(
    conn: DbConn,
    redis: Redis,
    *,
    organization_id: uuid.UUID,
    app_id: uuid.UUID,
    anonymous_id: str,
    user_id: str | None,
    installed_at: dt.datetime,
    decision: Decision,
    event_window_days: int,
    assessment: Assessment | None = None,
) -> StoredAttribution:
    """Write an attribution, upgrading an existing one if this is better evidence.

    Runs in one transaction. The supersede-then-insert order is only possible
    because the foreign key is DEFERRABLE — the partial unique index forbids two
    current rows, so the replacement cannot exist before the original steps
    aside.
    """
    key = install_key(app_id, anonymous_id)
    expires_at = installed_at + dt.timedelta(days=event_window_days)
    # An unassessed install is recorded as clean rather than as unknown. There
    # is no third state in the schema on purpose: a nullable verdict would mean
    # every reader had to decide what "not assessed" means, and they would not
    # all decide the same way.
    assessment = assessment or Assessment()

    async with conn.transaction():
        existing = await conn.fetchrow(CURRENT_SQL, app_id, key)

        if existing is not None:
            if not should_supersede(Method(existing["method"]), decision.method):
                # Already attributed at equal or better fidelity. Not an error:
                # this is the normal path for a redelivered install event.
                return StoredAttribution(
                    attribution_id=existing["id"],
                    method=Method(existing["method"]),
                    created=False,
                )
            replacement_id = uuid7()
            await conn.execute(SUPERSEDE_SQL, replacement_id, existing["id"])
            log.info(
                "attribution_superseded",
                install_key=key,
                previous_method=existing["method"],
                new_method=str(decision.method),
            )
        else:
            replacement_id = uuid7()

        inserted = await conn.fetchval(
            INSERT_SQL,
            replacement_id,
            organization_id,
            app_id,
            key,
            anonymous_id,
            user_id,
            decision.click_id,
            decision.campaign_id,
            decision.click.tracking_link_id if decision.click else None,
            decision.click.source if decision.click else None,
            decision.click.medium if decision.click else None,
            str(decision.method),
            installed_at,
            decision.window_days,
            expires_at,
            assessment.score,
            str(assessment.verdict),
            json.dumps(assessment.rules) if assessment.signals else None,
            decision.click.deep_link if decision.click else None,
        )

    if inserted is None:
        # Another worker won the race on the same install. Its row is equally
        # valid — this is exactly what the unique index is for.
        current = await conn.fetchrow(CURRENT_SQL, app_id, key)
        return StoredAttribution(
            attribution_id=current["id"],
            method=Method(current["method"]),
            created=False,
        )

    await cache(
        redis,
        app_id=app_id,
        anonymous_id=anonymous_id,
        attribution_id=replacement_id,
        decision=decision,
        expires_at=expires_at,
    )
    return StoredAttribution(
        attribution_id=replacement_id,
        method=decision.method,
        created=True,
        superseded=existing["id"] if existing is not None else None,
    )


def _cache_key(app_id: uuid.UUID, identity: str) -> str:
    return f"{IDENTITY_PREFIX}{app_id}:{identity}"


async def cache(
    redis: Redis,
    *,
    app_id: uuid.UUID,
    anonymous_id: str,
    attribution_id: uuid.UUID,
    decision: Decision,
    expires_at: dt.datetime,
) -> None:
    """Cache by identity, with a TTL matching the attribution's own expiry.

    Nothing outlives the window it was attributed under, so a stale entry cannot
    credit a campaign for a conversion that arrived after the window closed.
    """
    ttl = int((expires_at - dt.datetime.now(dt.UTC)).total_seconds())
    if ttl <= 0:
        return
    payload = _encoder.encode(
        CachedAttribution(
            attribution_id=str(attribution_id),
            click_id=str(decision.click_id) if decision.click_id else None,
            campaign_id=str(decision.campaign_id) if decision.campaign_id else None,
            tracking_link_id=(str(decision.click.tracking_link_id) if decision.click else None),
            method=str(decision.method),
            attributed_at=dt.datetime.now(dt.UTC).isoformat(),
            expires_at=expires_at.isoformat(),
        )
    )
    await redis.set(_cache_key(app_id, anonymous_id), payload, ex=ttl)


async def lookup(
    redis: Redis,
    conn: DbConn,
    *,
    app_id: uuid.UUID,
    anonymous_id: str,
) -> CachedAttribution | None:
    """Resolve a conversion to its attribution. Cache first, Postgres second.

    The fallback is not optional. Redis can lose keys to a failover or an
    eviction, and an attribution that silently disappears would send a purchase
    to the wrong campaign — or to none.
    """
    cached = await redis.get(_cache_key(app_id, anonymous_id))
    if cached:
        return _decoder.decode(cached)

    row = await conn.fetchrow(CURRENT_SQL, app_id, install_key(app_id, anonymous_id))
    if row is None:
        return None

    rebuilt = CachedAttribution(
        attribution_id=str(row["id"]),
        click_id=str(row["click_id"]) if row["click_id"] else None,
        campaign_id=str(row["campaign_id"]) if row["campaign_id"] else None,
        tracking_link_id=(str(row["tracking_link_id"]) if row["tracking_link_id"] else None),
        method=row["method"],
        attributed_at=row["attributed_at"].isoformat(),
        expires_at=row["expires_at"].isoformat(),
    )
    ttl = int((row["expires_at"] - dt.datetime.now(dt.UTC)).total_seconds())
    if ttl > 0:
        await redis.set(_cache_key(app_id, anonymous_id), _encoder.encode(rebuilt), ex=ttl)
    return rebuilt


async def link_user(
    redis: Redis,
    *,
    app_id: uuid.UUID,
    anonymous_id: str,
    user_id: str,
) -> None:
    """Mirror the cache entry under the user id after a login.

    Once a person signs in, conversions may arrive carrying only their user id —
    from a server-to-server call that never sees the device. Without this alias
    those conversions would resolve to nothing and be reported organic.
    """
    payload = await redis.get(_cache_key(app_id, anonymous_id))
    if payload is None:
        return
    ttl = await redis.ttl(_cache_key(app_id, anonymous_id))
    if ttl and ttl > 0:
        await redis.set(_cache_key(app_id, f"user:{user_id}"), payload, ex=ttl)
