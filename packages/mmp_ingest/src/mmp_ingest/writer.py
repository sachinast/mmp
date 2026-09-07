"""Batch persistence of events into Postgres.

The obvious implementation — one parameterised INSERT per event with
``ON CONFLICT DO NOTHING`` — is correct and roughly twenty times too slow. The
fast implementation, binary ``COPY``, has no ``ON CONFLICT``: COPY is a bulk
loader, not a statement, and a single duplicate aborts the whole batch.

So: COPY into an unlogged staging table, then one
``INSERT ... SELECT ... ON CONFLICT DO NOTHING`` from staging into the real
table. That keeps COPY's throughput and gets exact deduplication, at the cost of
writing each row twice into memory. The staging table is TEMP and lives for the
connection's lifetime, so the per-batch cost is a TRUNCATE.

**Why dedup works.** ``received_at`` is stamped at the edge and carried in the
queue message, so a stream redelivery reproduces the exact primary key
``(received_at, app_id, event_id)`` and the conflict clause drops it. That
covers redelivery precisely. It does *not* cover an SDK retrying a genuinely new
HTTP request after a lost acknowledgement — a different ``received_at``, same
``event_id`` — which is handled one layer up by the Redis idempotency window in
``mmp_ingest.dedup``. Two mechanisms, because they defend against two different
failures.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

import msgspec
from mmp_core.logging import get_logger
from mmp_db.types import DbConn

from mmp_db import sql
from mmp_ingest.schema import QueuedEvent

log = get_logger(__name__)

STAGING_TABLE = "staging_events"

EVENT_COLUMNS = (
    "event_id",
    "received_at",
    "occurred_at",
    "organization_id",
    "app_id",
    "event_name",
    "anonymous_id",
    "user_id",
    "session_id",
    "platform",
    "os_version",
    "app_version",
    "device_model",
    "country",
    "ip_hash",
    "click_id",
    "revenue_minor",
    "currency",
    "clock_skew_ms",
    "properties",
)

# TEMP: this data is written, read once and truncated. A durable table would
# pay WAL for every ingested row twice, and the staging copy never needs to
# survive a crash — if the connection dies the batch is unacknowledged and gets
# redelivered anyway.
#
# All three statements are assembled once at import, through the validated
# builders in mmp_db.sql, so no identifier is constructed per request.
CREATE_STAGING = sql.create_temp_like(STAGING_TABLE, like="events")
TRUNCATE_STAGING = sql.truncate(STAGING_TABLE)
INSERT_FROM_STAGING = sql.insert_select(
    "events", STAGING_TABLE, EVENT_COLUMNS, on_conflict="ON CONFLICT DO NOTHING"
)

_json_encoder = msgspec.json.Encoder()


@dataclass(frozen=True)
class WriteResult:
    received: int
    inserted: int

    @property
    def duplicates(self) -> int:
        return self.received - self.inserted


def _to_record(event: QueuedEvent) -> tuple[object, ...]:
    return (
        uuid.UUID(event.event_id),
        dt.datetime.fromisoformat(event.received_at),
        dt.datetime.fromisoformat(event.occurred_at),
        uuid.UUID(event.organization_id),
        uuid.UUID(event.app_id),
        event.event_name,
        event.anonymous_id,
        event.user_id,
        uuid.UUID(event.session_id) if event.session_id else None,
        event.platform,
        event.os_version,
        event.app_version,
        event.device_model,
        event.country,
        event.ip_hash,
        uuid.UUID(event.click_id) if event.click_id else None,
        event.revenue_minor,
        event.currency,
        event.clock_skew_ms,
        _json_encoder.encode(event.properties).decode(),
    )


class EventWriter:
    def __init__(self) -> None:
        self._prepared: set[int] = set()

    async def ensure_staging(self, conn: DbConn) -> None:
        """Create the staging table once per connection.

        Tracked by connection identity rather than re-issued per batch: the
        CREATE is cheap but not free, and this runs thousands of times an hour.
        """
        key = id(conn)
        if key in self._prepared:
            return
        await conn.execute(CREATE_STAGING)
        self._prepared.add(key)

    async def write(self, conn: DbConn, events: list[QueuedEvent]) -> WriteResult:
        if not events:
            return WriteResult(received=0, inserted=0)

        await self.ensure_staging(conn)
        records = [_to_record(event) for event in events]

        async with conn.transaction():
            await conn.execute(f"TRUNCATE {STAGING_TABLE}")
            await conn.copy_records_to_table(
                STAGING_TABLE, records=records, columns=list(EVENT_COLUMNS)
            )
            status = await conn.execute(INSERT_FROM_STAGING)

        # asyncpg returns "INSERT 0 <count>" — the second number is what was
        # actually written after conflicts were dropped.
        inserted = int(status.rsplit(" ", 1)[-1]) if status.startswith("INSERT") else 0
        result = WriteResult(received=len(events), inserted=inserted)
        if result.duplicates:
            log.info(
                "batch_written_with_duplicates",
                received=result.received,
                inserted=result.inserted,
                duplicates=result.duplicates,
            )
        return result
