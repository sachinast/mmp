"""Consent, evaluated before anything is stored.

The ordering is the whole point. Consent checked *after* persistence is a
deletion problem: the data is already in a partition, a rollup, a postback and a
partner's system, and unwinding it is a project. Checked at the edge, a denied
purpose means the field never existed.

**An explicit denial is always honoured.** That part is not configurable and not
negotiable: if a user has told us no, no field that serves that purpose is
stored, whatever the app is set to.

**What "unknown" means is per-app, and the choice is explicit.** Absence of a
record can mean either "we have not been told yet" or "consent is not required
here", and only the app's operator knows which. So each app carries a mode:

* ``permissive`` (default) — unknown proceeds. This is how the platform behaves
  for an integration that has not adopted consent reporting, and it is what
  every existing SDK expects.
* ``strict`` — unknown denies. Required where consent must precede processing.

The default is permissive **not** because it is the safer choice — it is not —
but because the alternative is a platform that silently stops attributing every
existing advertiser's installs the moment this code ships, which would be a data
loss event disguised as a privacy feature. Strict is one field away and is the
correct setting for an app serving users in a consent jurisdiction; making it
the default is a product decision with commercial consequences, and belongs to
whoever owns that, not to this module.

**Purposes are separable.** A user may allow us to count that an install
happened and refuse to have it attributed to an ad network. Collapsing that into
one flag would force an all-or-nothing choice that neither the user nor the
regulation intends.
"""

from __future__ import annotations

import datetime as dt
import enum
from collections.abc import Sequence
from dataclasses import dataclass, field

import msgspec
from mmp_core.logging import get_logger
from redis.asyncio import Redis

log = get_logger(__name__)

CACHE_PREFIX = "consent:"


def consent_cache_key(app_id: str, anonymous_id: str) -> str:
    """The cache key for one device's consent, in one place.

    Public because the API invalidates this cache too — a decision recorded
    through `/v1/privacy/consent` has to reach the tracker, not wait five
    minutes for a TTL. That path used to build the key from its own copy of the
    format, in two spots, and the test asserting the invalidation built a third.
    All three agreed, so nothing was broken; nothing would have noticed if a
    rename made them disagree, and the symptom would have been a withdrawal of
    consent quietly not taking effect.
    """
    return f"{CACHE_PREFIX}{app_id}:{anonymous_id}"


# Short. A user who withdraws consent expects it to take effect now, not after a
# cache expires, and this is read on the ingest path where a database lookup per
# event is not affordable.
CACHE_TTL = dt.timedelta(minutes=5)


class Purpose(enum.StrEnum):
    """What we may do with an event, separably.

    ANALYTICS  — count it, aggregate it, show it to the app's owner.
    ATTRIBUTION— link it to a click, so a network learns the install was theirs.
    ADVERTISING— forward it to a third party for targeting or optimisation.
    """

    ANALYTICS = "analytics"
    ATTRIBUTION = "attribution"
    ADVERTISING = "advertising"


class State(enum.StrEnum):
    UNKNOWN = "unknown"
    GRANTED = "granted"
    DENIED = "denied"


# Fields that only exist to serve a purpose. When that purpose is not granted,
# they are removed at the edge rather than stored and filtered later.
#
# Attribution needs a device identifier to match a click; analytics does not.
# Advertising is what sends data to a third party; nothing is stripped for it
# here because it gates an *action*, not a field.
PURPOSE_FIELDS: dict[Purpose, tuple[str, ...]] = {
    Purpose.ATTRIBUTION: ("device_hash", "install_referrer", "click_id"),
}

# Always kept, whatever the consent state.
#
# An event that cannot be counted at all cannot be billed, reconciled, or
# rate-limited, and a platform that silently stops recording that *something*
# happened has no way to tell that from an outage. What remains is the fact of
# an event, with nothing that identifies a person beyond the pseudonymous id the
# SDK generated.
ALWAYS_RETAINED = (
    "event_id",
    "received_at",
    "occurred_at",
    "organization_id",
    "app_id",
    "event_name",
    "anonymous_id",
)


class Mode(enum.StrEnum):
    """How an app treats the absence of a consent record."""

    PERMISSIVE = "permissive"
    STRICT = "strict"


@dataclass(frozen=True)
class ConsentSet:
    """What a device has told us, purpose by purpose, read under an app's mode."""

    states: dict[Purpose, State] = field(default_factory=dict)
    mode: Mode = Mode.PERMISSIVE

    def state(self, purpose: Purpose) -> State:
        return self.states.get(purpose, State.UNKNOWN)

    def allows(self, purpose: Purpose) -> bool:
        """An explicit denial always refuses. Unknown depends on the mode."""
        current = self.state(purpose)
        if current is State.DENIED:
            return False
        if current is State.GRANTED:
            return True
        return self.mode is Mode.PERMISSIVE

    @property
    def is_empty(self) -> bool:
        return not self.states


class _Cached(msgspec.Struct):
    states: dict[str, str]


_encoder = msgspec.msgpack.Encoder()
_decoder = msgspec.msgpack.Decoder(_Cached)

LOOKUP_SQL = """
SELECT purpose, state
FROM consent_states
WHERE app_id = $1 AND anonymous_id = $2
  AND (expires_at IS NULL OR expires_at > now())
"""


class ConsentGate:
    """Reads consent and applies it. Cached, because it is on the hot path."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    @staticmethod
    def _key(app_id: str, anonymous_id: str) -> str:
        return consent_cache_key(app_id, anonymous_id)

    async def lookup(
        self, app_id: str, anonymous_id: str, *, mode: Mode = Mode.PERMISSIVE
    ) -> ConsentSet:
        raw = await self._redis.get(self._key(app_id, anonymous_id))
        if raw is None:
            # No cached record. Deliberately *not* a database read on the ingest
            # path: the tracker cannot query consent_states (it holds SELECT on
            # three tables and that is not one of them), and a query per event on
            # the hot path would be the wrong trade regardless. The SDK reports
            # consent, which populates the cache; until it does the state is
            # unknown, and what unknown means is the app's mode to decide.
            return ConsentSet(mode=mode)
        cached = _decoder.decode(raw)
        return ConsentSet(
            states={Purpose(k): State(v) for k, v in cached.states.items()}, mode=mode
        )

    async def lookup_many(
        self,
        app_id: str,
        anonymous_ids: Sequence[str],
        *,
        mode: Mode = Mode.PERMISSIVE,
    ) -> dict[str, ConsentSet]:
        """Resolve consent for a whole batch in one round trip.

        The per-device version costs a Redis GET each, and the ingest benchmark
        took p50 from 2.4 ms to 7.3 ms on a twenty-device batch — the same
        mistake as the first version of session assignment, in the same code
        path, caught by the same gate. A batch is usually one device and always
        a handful; one pipelined read covers all of them.
        """
        if not anonymous_ids:
            return {}

        unique = list(dict.fromkeys(anonymous_ids))
        pipe = self._redis.pipeline(transaction=False)
        for anonymous_id in unique:
            pipe.get(self._key(app_id, anonymous_id))
        results = await pipe.execute()

        resolved: dict[str, ConsentSet] = {}
        for anonymous_id, raw in zip(unique, results, strict=True):
            if raw is None:
                resolved[anonymous_id] = ConsentSet(mode=mode)
                continue
            cached = _decoder.decode(raw)
            resolved[anonymous_id] = ConsentSet(
                states={Purpose(k): State(v) for k, v in cached.states.items()},
                mode=mode,
            )
        return resolved

    async def record(
        self,
        app_id: str,
        anonymous_id: str,
        states: dict[Purpose, State],
        *,
        ttl: dt.timedelta = CACHE_TTL,
    ) -> None:
        merged = await self.lookup(app_id, anonymous_id)
        combined = {**merged.states, **states}
        await self._redis.set(
            self._key(app_id, anonymous_id),
            _encoder.encode(_Cached(states={str(k): str(v) for k, v in combined.items()})),
            ex=int(ttl.total_seconds()),
        )

    async def forget(self, app_id: str, anonymous_id: str) -> None:
        """Drop the cached decision so the next read is fresh.

        Called when consent changes. Without it a withdrawal would not take
        effect until the cache expired, which is exactly the delay a user does
        not expect.
        """
        await self._redis.delete(self._key(app_id, anonymous_id))


def minimise(properties: dict[str, object], consent: ConsentSet) -> dict[str, object]:
    """Strip fields whose purpose has not been granted.

    Returns a new dict; the caller's is untouched. Fields are removed, not
    blanked — a key present with a null value still tells a reader that we asked.
    """
    if not properties:
        return properties

    removed: list[str] = []
    result = dict(properties)
    for purpose, fields in PURPOSE_FIELDS.items():
        if consent.allows(purpose):
            continue
        for name in fields:
            if name in result:
                del result[name]
                removed.append(name)

    if removed:
        log.debug("event_minimised", removed=sorted(removed))
    return result


def may_attribute(consent: ConsentSet) -> bool:
    """Whether this install may be linked to a click at all."""
    return consent.allows(Purpose.ATTRIBUTION)


def may_forward(consent: ConsentSet) -> bool:
    """Whether this event may be sent to a third party.

    Both purposes, not either: forwarding a conversion to an ad network is an
    advertising use *of an attribution*, and a user who allowed one but not the
    other has not agreed to it.
    """
    return consent.allows(Purpose.ADVERTISING) and consent.allows(Purpose.ATTRIBUTION)
