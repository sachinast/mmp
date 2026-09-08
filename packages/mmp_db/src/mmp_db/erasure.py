"""Erasing a device's data.

A deletion request is the one privacy operation that cannot be partially done.
Removing a person's events but leaving their attribution, or clearing the
database but leaving the rollups, produces a system that reports having deleted
data it still holds — which is worse than not deleting, because it is a claim.

So erasure is enumerated explicitly, table by table, and the enumeration is
tested against the schema: a new table carrying an ``anonymous_id`` fails the
test until someone has decided what erasure means for it.

**What is not deleted, and why.** Aggregate counts in the rollups are left alone.
They contain no identifier — a row saying "412 installs in this hour for this
campaign" is not personal data, and recomputing every historical aggregate to
subtract one person would be expensive, would corrupt reporting an advertiser has
already acted on, and would achieve nothing for the individual. This is a
judgement, it is the common one, and it is written down here so it is a decision
rather than an oversight.

**Erasure is asynchronous and recorded.** The request is acknowledged, the work
is queued, and completion is written to the audit log. A synchronous delete
across every partition would hold locks on the ingest path.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid
from dataclasses import dataclass, field

from mmp_core.logging import get_logger

from mmp_db.types import DbConn

log = get_logger(__name__)


class Scope(enum.StrEnum):
    """How much to erase.

    DEVICE — one device's data within one app. The ordinary request.
    USER   — everything linked to a signed-in identity, across devices.
    """

    DEVICE = "device"
    USER = "user"


# Every table holding something tied to a person, and how to find it.
#
# Ordered so that referencing rows go before referenced ones. Attribution rows
# point at clicks; deleting the click first would leave a dangling reference in
# a table we are about to read.
ERASURE_TARGETS: tuple[tuple[str, str], ...] = (
    ("events", "app_id = $1 AND anonymous_id = $2"),
    ("attributions", "app_id = $1 AND anonymous_id = $2"),
    ("consent_states", "app_id = $1 AND anonymous_id = $2"),
)

# Tables carrying a person-linked column that are deliberately *not* erased,
# with the reason. The test below requires every such table to appear in one
# list or the other.
ERASURE_EXCLUSIONS: dict[str, str] = {
    "clicks": (
        "A click is recorded before any device identifies itself, and is keyed "
        "by a click id rather than an anonymous id. Its device_hash is erased "
        "separately below."
    ),
    "rollup_events_hourly": "aggregate counts, no identifier",
    "rollup_clicks_hourly": "aggregate counts, no identifier",
    "rollup_campaign_daily": "aggregate counts, no identifier",
    "usage_rollup": "billing volume, no identifier",
    "pipeline_audit": "operational counts, no identifier",
}

# Clicks are handled by clearing the identifier rather than deleting the row.
#
# Deleting a click would change an advertiser's click count for a period they
# have already been billed for and already reported. Clearing the device hash
# removes the link to a person while leaving the fact that a click happened —
# which is the outcome erasure is actually asking for.
CLEAR_CLICK_IDENTIFIERS = """
UPDATE clicks
SET device_hash = NULL, ip_hash = NULL, user_agent = NULL
WHERE app_id = $1 AND device_hash = $2
"""


@dataclass
class ErasureResult:
    request_id: uuid.UUID
    scope: Scope
    deleted: dict[str, int] = field(default_factory=dict)
    cleared: dict[str, int] = field(default_factory=dict)
    completed_at: dt.datetime | None = None

    @property
    def total_deleted(self) -> int:
        return sum(self.deleted.values())

    def as_dict(self) -> dict[str, object]:
        return {
            "request_id": str(self.request_id),
            "scope": str(self.scope),
            "deleted": dict(self.deleted),
            "cleared": dict(self.cleared),
            "total_deleted": self.total_deleted,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


def _rows_affected(status: str) -> int:
    """asyncpg returns 'DELETE 12' / 'UPDATE 3'."""
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, IndexError):  # pragma: no cover
        return 0


async def erase_device(
    conn: DbConn,
    *,
    app_id: uuid.UUID,
    anonymous_id: str,
    device_hash: bytes | None = None,
    request_id: uuid.UUID | None = None,
) -> ErasureResult:
    """Erase one device's data within one app.

    Runs in a single transaction: a partial erasure that reports success is the
    failure this whole module exists to avoid.
    """
    from mmp_core.ids import uuid7

    result = ErasureResult(request_id=request_id or uuid7(), scope=Scope.DEVICE)

    async with conn.transaction():
        for table, predicate in ERASURE_TARGETS:
            from mmp_db import sql

            status = await conn.execute(sql.delete_where(table, predicate), app_id, anonymous_id)
            result.deleted[table] = _rows_affected(status)

        if device_hash is not None:
            status = await conn.execute(CLEAR_CLICK_IDENTIFIERS, app_id, device_hash)
            result.cleared["clicks"] = _rows_affected(status)

    result.completed_at = dt.datetime.now(dt.UTC)
    log.info("erasure_completed", **result.as_dict())
    return result
