"""The wire format for an ingested event.

msgspec Structs rather than Pydantic models. Pydantic's ergonomics are worth
their cost on the business API, where a request is a form submission by a human;
here a request is a batch of twenty events arriving thousands of times a second,
and validation is a measurable fraction of the latency budget.

The same struct is used for HTTP decoding and for the queue payload, so an event
is parsed once at the edge and never re-parsed downstream.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import msgspec

# Event names are a dimension in every aggregate query. Left unbounded, one
# misconfigured SDK generating names from a user ID would explode the rollup
# tables' cardinality — a failure that is very hard to undo after the fact.
MAX_EVENT_NAME_LENGTH = 120
MAX_ID_LENGTH = 255
MAX_PROPERTIES_BYTES = 16 * 1024
MAX_EVENTS_PER_BATCH = 100

# Clock skew tolerance. A device whose clock is wrong by a month would otherwise
# write into a partition nobody is querying, and quietly vanish from reports.
MAX_CLOCK_SKEW = dt.timedelta(hours=24)

PLATFORM_CODES = {"android": 1, "ios": 2, "web": 3, "server": 4, "unknown": 0}

# Reserved names carry defined semantics through attribution and rollups.
SYSTEM_EVENTS = frozenset(
    {
        "install",
        "app_open",
        "session_start",
        "session_end",
        "signup",
        "login",
        "purchase",
        "subscription_start",
        "subscription_renew",
        "refund",
    }
)


class IncomingEvent(msgspec.Struct, omit_defaults=True):
    """One event as the SDK sends it."""

    event_name: str
    anonymous_id: str
    # Client-minted UUIDv7. The anchor for idempotency: the SDK removes an event
    # from its offline queue only after a 202, so a lost acknowledgement means
    # the same event_id arrives twice by design.
    event_id: str | None = None
    occurred_at: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    platform: str | None = None
    os_version: str | None = None
    app_version: str | None = None
    device_model: str | None = None
    revenue_minor: int | None = None
    currency: str | None = None
    click_id: str | None = None
    properties: dict[str, Any] = msgspec.field(default_factory=dict)


class EventBatch(msgspec.Struct):
    events: list[IncomingEvent]


class ValidationFailure(Exception):
    """A payload we will not accept. Always a 4xx, never a retry."""

    def __init__(self, reason: str, *, index: int | None = None) -> None:
        self.reason = reason
        self.index = index
        super().__init__(reason if index is None else f"events[{index}]: {reason}")


class QueuedEvent(msgspec.Struct):
    """An event after edge validation, as it travels through the stream.

    ``received_at`` is stamped here, at the edge, and carried through the queue
    rather than being taken at write time. That is what makes a stream
    redelivery land on the same partition with the same primary key, so the
    database itself rejects the duplicate. Taking the timestamp in the worker
    would give a redelivered message a new key and silently double-count it.
    """

    event_id: str
    received_at: str
    occurred_at: str
    organization_id: str
    app_id: str
    event_name: str
    anonymous_id: str
    user_id: str | None
    session_id: str | None
    platform: int
    os_version: str | None
    app_version: str | None
    device_model: str | None
    country: str | None
    ip_hash: bytes | None
    click_id: str | None
    revenue_minor: int | None
    currency: str | None
    clock_skew_ms: int | None
    properties: dict[str, Any]
    correlation_id: str | None = None


def _parse_timestamp(value: str | None, *, now: dt.datetime) -> tuple[dt.datetime, int | None]:
    """Return the client timestamp and its skew from server time.

    Both are kept. ``occurred_at`` is reported to the customer as their event's
    time; ``received_at`` is what the platform partitions and bills on. Blending
    them would make one wrong device clock a reporting incident.
    """
    if value is None:
        return now, None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationFailure("occurred_at is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)

    skew = parsed - now
    if abs(skew) > MAX_CLOCK_SKEW:
        # Clamped rather than rejected: the event is real and the customer paid
        # for it. Recording the skew keeps the distortion visible instead of
        # burying it in a partition nobody queries.
        return now, int(skew.total_seconds() * 1000)
    return parsed, int(skew.total_seconds() * 1000)


def validate_event(
    event: IncomingEvent,
    *,
    index: int,
    organization_id: uuid.UUID,
    app_id: uuid.UUID,
    now: dt.datetime,
    default_event_id: str,
) -> QueuedEvent:
    if not event.event_name or len(event.event_name) > MAX_EVENT_NAME_LENGTH:
        raise ValidationFailure(
            f"event_name must be 1-{MAX_EVENT_NAME_LENGTH} characters", index=index
        )
    if not event.anonymous_id or len(event.anonymous_id) > MAX_ID_LENGTH:
        raise ValidationFailure("anonymous_id is required", index=index)
    if event.user_id is not None and len(event.user_id) > MAX_ID_LENGTH:
        raise ValidationFailure("user_id is too long", index=index)
    if event.revenue_minor is not None and event.currency is None:
        raise ValidationFailure("revenue_minor requires a currency", index=index)
    if event.currency is not None and len(event.currency) != 3:
        raise ValidationFailure("currency must be a 3-letter ISO code", index=index)

    properties_size = len(msgspec.json.encode(event.properties))
    if properties_size > MAX_PROPERTIES_BYTES:
        raise ValidationFailure(f"properties exceed {MAX_PROPERTIES_BYTES} bytes", index=index)

    event_id = event.event_id or default_event_id
    try:
        uuid.UUID(event_id)
    except ValueError as exc:
        raise ValidationFailure("event_id must be a UUID", index=index) from exc

    for name, value in (("session_id", event.session_id), ("click_id", event.click_id)):
        if value is not None:
            try:
                uuid.UUID(value)
            except ValueError as exc:
                raise ValidationFailure(f"{name} must be a UUID", index=index) from exc

    occurred_at, skew = _parse_timestamp(event.occurred_at, now=now)

    return QueuedEvent(
        event_id=event_id,
        received_at=now.isoformat(),
        occurred_at=occurred_at.isoformat(),
        organization_id=str(organization_id),
        app_id=str(app_id),
        event_name=event.event_name,
        anonymous_id=event.anonymous_id,
        user_id=event.user_id,
        session_id=event.session_id,
        platform=PLATFORM_CODES.get((event.platform or "unknown").lower(), 0),
        os_version=event.os_version,
        app_version=event.app_version,
        device_model=event.device_model,
        country=None,
        ip_hash=None,
        click_id=event.click_id,
        revenue_minor=event.revenue_minor,
        currency=event.currency.upper() if event.currency else None,
        clock_skew_ms=skew,
        properties=event.properties,
    )
