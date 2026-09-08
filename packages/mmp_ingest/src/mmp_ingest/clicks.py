"""Click records: the wire format and the batch writer.

A click is smaller and simpler than an event, but its write path has the same
shape and the same rule: the edge stamps ``clicked_at`` and the queue carries
it, so a stream redelivery reproduces the primary key and the database drops the
duplicate.
"""

from __future__ import annotations

import datetime as dt
import uuid

import msgspec
from mmp_core.logging import get_logger
from mmp_db.types import DbConn

from mmp_db import sql

log = get_logger(__name__)

STAGING_TABLE = "staging_clicks"

CLICK_COLUMNS = (
    "click_id",
    "clicked_at",
    "organization_id",
    "app_id",
    "campaign_id",
    "tracking_link_id",
    "device_hash",
    "ip_hash",
    "country",
    "platform",
    "os_version",
    "device_model",
    "user_agent",
    "sub1",
    "sub2",
    "sub3",
    "is_bot",
)

CREATE_STAGING = sql.create_temp_like(STAGING_TABLE, like="clicks")
TRUNCATE_STAGING = sql.truncate(STAGING_TABLE)
INSERT_FROM_STAGING = sql.insert_select(
    "clicks", STAGING_TABLE, CLICK_COLUMNS, on_conflict="ON CONFLICT DO NOTHING"
)

# The user agent is stored for bot analysis and nothing else. Truncated because
# a crafted 8 KB header would otherwise be written verbatim on every click.
MAX_USER_AGENT = 512
MAX_SUB_PARAM = 255


class QueuedClick(msgspec.Struct):
    click_id: str
    clicked_at: str
    organization_id: str
    app_id: str
    campaign_id: str | None
    tracking_link_id: str
    device_hash: bytes | None
    ip_hash: bytes | None
    country: str | None
    platform: int
    os_version: str | None
    device_model: str | None
    user_agent: str | None
    sub1: str | None
    sub2: str | None
    sub3: str | None
    is_bot: bool
    correlation_id: str | None = None


def _to_record(click: QueuedClick) -> tuple[object, ...]:
    return (
        uuid.UUID(click.click_id),
        dt.datetime.fromisoformat(click.clicked_at),
        uuid.UUID(click.organization_id),
        uuid.UUID(click.app_id),
        uuid.UUID(click.campaign_id) if click.campaign_id else None,
        uuid.UUID(click.tracking_link_id),
        click.device_hash,
        click.ip_hash,
        click.country,
        click.platform,
        click.os_version,
        click.device_model,
        click.user_agent[:MAX_USER_AGENT] if click.user_agent else None,
        click.sub1,
        click.sub2,
        click.sub3,
        click.is_bot,
    )


class ClickWriter:
    def __init__(self) -> None:
        self._prepared: set[int] = set()

    async def ensure_staging(self, conn: DbConn) -> None:
        key = id(conn)
        if key in self._prepared:
            return
        await conn.execute(CREATE_STAGING)
        self._prepared.add(key)

    async def write(self, conn: DbConn, clicks: list[QueuedClick]) -> tuple[int, int]:
        if not clicks:
            return 0, 0
        await self.ensure_staging(conn)
        records = [_to_record(click) for click in clicks]
        async with conn.transaction():
            await conn.execute(TRUNCATE_STAGING)
            await conn.copy_records_to_table(
                STAGING_TABLE, records=records, columns=list(CLICK_COLUMNS)
            )
            status = await conn.execute(INSERT_FROM_STAGING)
        inserted = int(status.rsplit(" ", 1)[-1]) if status.startswith("INSERT") else 0
        return len(clicks), inserted
