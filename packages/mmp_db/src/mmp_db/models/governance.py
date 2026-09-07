"""Consent, audit, usage metering, and pipeline reconciliation."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import BYTEA, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from mmp_db.base import Base, OrgScopedMixin, TimestampMixin, one_of, org_fk, uuid_pk

CONSENT_PURPOSES = ("analytics", "attribution", "advertising")
CONSENT_STATES = ("unknown", "granted", "denied")


class ConsentState(Base, OrgScopedMixin, TimestampMixin):
    """Per-device, per-purpose consent.

    Default is ``unknown``, not ``granted``. Processing code asks this table
    before it acts, and absence of a record means no permission — consent is
    something we are given, not something we assume until told otherwise.
    """

    __tablename__ = "consent_states"
    __table_args__ = (
        UniqueConstraint("app_id", "anonymous_id", "purpose"),
        CheckConstraint(one_of("purpose", *CONSENT_PURPOSES), name="purpose_valid"),
        CheckConstraint(one_of("state", *CONSENT_STATES), name="state_valid"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    app_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("apps.id", ondelete="CASCADE"), index=True
    )
    anonymous_id: Mapped[str] = mapped_column(String(255), nullable=False)
    purpose: Mapped[str] = mapped_column(String(20), nullable=False)
    state: Mapped[str] = mapped_column(String(10), nullable=False, default="unknown")
    source: Mapped[str | None] = mapped_column(String(40))
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base, OrgScopedMixin):
    """Append-only, hash-chained.

    Each row carries the hash of its predecessor, so removing or editing a row
    breaks the chain for everything after it. Tamper-evident rather than
    tamper-proof — which is what an audit log can actually promise.
    """

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_org_time", "organization_id", "created_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(60), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(255))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    previous_hash: Mapped[bytes | None] = mapped_column(BYTEA)
    entry_hash: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )


class UsageRollup(Base, OrgScopedMixin):
    """Billable volume, aggregated hourly.

    Exists from Phase 1 rather than from whenever billing gets built, because
    metering data cannot be reconstructed retroactively — you can only start
    counting from the day you decide to.
    """

    __tablename__ = "usage_rollup"
    __table_args__ = (UniqueConstraint("organization_id", "app_id", "bucket_hour", "metric"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    app_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("apps.id", ondelete="CASCADE"), index=True
    )
    bucket_hour: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    metric: Mapped[str] = mapped_column(String(40), nullable=False)
    count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class PipelineAudit(Base):
    """Counts from both ends of the pipeline, for reconciliation.

    Silent loss is the failure that destroys trust in a measurement product, and
    it is only detectable if the number accepted at the edge is compared against
    the number that reached storage. Not org-scoped: this is our own operational
    data, read by the platform team, not by tenants.
    """

    __tablename__ = "pipeline_audit"
    __table_args__ = (UniqueConstraint("app_id", "bucket_hour", "stage"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    app_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    bucket_hour: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    stage: Mapped[str] = mapped_column(String(40), nullable=False)
    count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    recorded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )
