"""The audit log.

Hash-chained: each entry carries the hash of its predecessor, so removing or
editing one breaks the chain for everything after it. That makes the log
**tamper-evident**, which is what an audit log can honestly promise — it is not
tamper-proof, because anyone who can write to the table can also rewrite the
chain from the point they altered. Detecting that requires the chain head to be
recorded somewhere they cannot reach, which is a deployment concern and is not
done here.

Stating the limit matters more than the mechanism. A log described as
tamper-proof when it is only tamper-evident is worse than a plain log, because
someone will rely on it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

import msgspec
from mmp_core.ids import uuid7
from mmp_core.logging import get_logger

from mmp_db.types import DbConn

log = get_logger(__name__)

_encoder = msgspec.json.Encoder()

# Actions worth recording. A closed set, because an audit log that accepts any
# string becomes an unqueryable pile within a year.
ACTIONS = frozenset(
    {
        "api_key.created",
        "api_key.revoked",
        "api_key.rotated",
        "app.created",
        "app.disabled",
        "member.added",
        "member.removed",
        "postback_rule.created",
        "postback_rule.disabled",
        "webhook.created",
        "webhook.deleted",
        "webhook.secret_rotated",
        "export.requested",
        "deep_link.created",
        "deep_link.deleted",
        "integration.created",
        "integration.deleted",
        "consent.recorded",
        "erasure.requested",
        "erasure.completed",
        "retention.applied",
    }
)

LAST_ENTRY_SQL = """
SELECT entry_hash FROM audit_log
WHERE organization_id = $1
ORDER BY created_at DESC, id DESC
LIMIT 1
"""

INSERT_SQL = """
INSERT INTO audit_log (id, organization_id, actor_user_id, action, resource_type,
                       resource_id, detail, previous_hash, entry_hash, created_at)
VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, now())
"""


@dataclass(frozen=True)
class AuditEntry:
    id: uuid.UUID
    action: str
    entry_hash: bytes


def compute_hash(
    *,
    entry_id: uuid.UUID,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    action: str,
    resource_type: str,
    resource_id: str | None,
    detail: dict[str, Any],
    previous_hash: bytes | None,
) -> bytes:
    """Hash an entry together with its predecessor.

    Every field is included. Hashing only a summary would let someone change the
    detail of an entry without breaking the chain, which is the one thing the
    chain is for.
    """
    payload = b"\n".join(
        [
            str(entry_id).encode(),
            str(organization_id).encode(),
            str(actor_user_id or "").encode(),
            action.encode(),
            resource_type.encode(),
            (resource_id or "").encode(),
            # Sorted keys, so the same detail always hashes the same way
            # regardless of how the dict was built.
            _encoder.encode(dict(sorted(detail.items()))),
            previous_hash or b"",
        ]
    )
    return sha256(payload).digest()


async def record(
    conn: DbConn,
    *,
    organization_id: uuid.UUID,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    detail: dict[str, Any] | None = None,
) -> AuditEntry:
    """Append one entry.

    Unknown actions are rejected rather than recorded: an audit log that accepts
    any string is one nobody can query, and the failure shows up when someone
    needs it most.
    """
    if action not in ACTIONS:
        raise ValueError(f"unknown audit action: {action!r}")

    detail = detail or {}
    entry_id = uuid7()

    # Read-then-write under the caller's transaction. Two concurrent writers can
    # read the same predecessor and produce a fork — the chain still detects
    # *edits*, which is what it is for, but a fork means the ordering is not
    # total. Serialising every audit write would put a lock on every mutation in
    # the platform; the trade is deliberate and the limit is stated.
    previous = await conn.fetchval(LAST_ENTRY_SQL, organization_id)
    previous_hash = bytes(previous) if previous else None

    entry_hash = compute_hash(
        entry_id=entry_id,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        detail=detail,
        previous_hash=previous_hash,
    )

    await conn.execute(
        INSERT_SQL,
        entry_id,
        organization_id,
        actor_user_id,
        action,
        resource_type,
        resource_id,
        _encoder.encode(detail).decode(),
        previous_hash,
        entry_hash,
    )
    return AuditEntry(id=entry_id, action=action, entry_hash=entry_hash)


@dataclass(frozen=True)
class VerificationResult:
    entries: int
    intact: bool
    broken_at: uuid.UUID | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "entries": self.entries,
            "intact": self.intact,
            "broken_at": str(self.broken_at) if self.broken_at else None,
        }


async def verify(conn: DbConn, *, organization_id: uuid.UUID) -> VerificationResult:
    """Walk the chain and report the first entry that does not match.

    A chain nobody verifies is a chain nobody would notice was broken, so this
    exists to be run — on a schedule, and by hand when something is disputed.
    """
    import json

    rows = await conn.fetch(
        """
        SELECT id, organization_id, actor_user_id, action, resource_type,
               resource_id, detail, previous_hash, entry_hash
        FROM audit_log
        WHERE organization_id = $1
        ORDER BY created_at, id
        """,
        organization_id,
    )

    previous_hash: bytes | None = None
    for row in rows:
        detail = row["detail"]
        if isinstance(detail, str):
            detail = json.loads(detail)

        expected = compute_hash(
            entry_id=row["id"],
            organization_id=row["organization_id"],
            actor_user_id=row["actor_user_id"],
            action=row["action"],
            resource_type=row["resource_type"],
            resource_id=row["resource_id"],
            detail=detail or {},
            previous_hash=previous_hash,
        )
        if expected != bytes(row["entry_hash"]):
            log.error("audit_chain_broken", entry_id=str(row["id"]))
            return VerificationResult(entries=len(rows), intact=False, broken_at=row["id"])
        previous_hash = bytes(row["entry_hash"])

    return VerificationResult(entries=len(rows), intact=True)
