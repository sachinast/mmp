"""Attribution records and the identity cache backing conversion resolution."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from mmp_db.base import Base, OrgScopedMixin, TimestampMixin, one_of, org_fk, uuid_pk

# Ordered by fidelity. `referrer` is ground truth on Android; `probabilistic`
# exists so that if it is ever switched on it is labelled at the row level and
# can be excluded from a report — it is never blended into deterministic totals.
METHODS = ("referrer", "click_id", "device_match", "organic", "probabilistic")


class Attribution(Base, OrgScopedMixin, TimestampMixin):
    """One install, one attribution.

    The partial unique index below is the invariant the entire product's numbers
    rest on. Enforcing it in worker code would mean enforcing it across several
    processes consuming a queue that redelivers on timeout; enforcing it here
    means the database refuses the duplicate and the losing worker's
    ``ON CONFLICT DO NOTHING`` turns a race into a no-op.

    Rows are immutable. A re-attribution inserts a new row and points the old
    one at it through ``superseded_by``, so a number already reported to an ad
    network can always be reconstructed.
    """

    __tablename__ = "attributions"
    __table_args__ = (
        Index(
            "uq_attributions_install_key_current",
            "app_id",
            "install_key",
            unique=True,
            postgresql_where="superseded_by IS NULL",
        ),
        Index("ix_attributions_click_id", "click_id", postgresql_where="click_id IS NOT NULL"),
        CheckConstraint(one_of("method", *METHODS), name="method_valid"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    app_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("apps.id", ondelete="CASCADE"), index=True
    )
    # app_id + anonymous_id. Denormalised into one column so the uniqueness
    # constraint is a single index rather than a composite over a nullable set.
    install_key: Mapped[str] = mapped_column(String(255), nullable=False)
    anonymous_id: Mapped[str] = mapped_column(String(255), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(255), index=True)

    click_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    tracking_link_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    source: Mapped[str | None] = mapped_column(String(120))
    medium: Mapped[str | None] = mapped_column(String(120))

    method: Mapped[str] = mapped_column(String(20), nullable=False)
    installed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attributed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Copied from the app at attribution time. The window that produced this row
    # stays readable even after the app's configuration changes.
    window_days: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    # DEFERRABLE INITIALLY DEFERRED: superseding is a two-statement swap — point
    # the old row at the new one, then insert the new one — and the partial
    # unique index forbids doing it in the other order. Deferring the check to
    # commit makes the whole correction atomic instead of impossible.
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "attributions.id",
            ondelete="SET NULL",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    # The fraud assessment of this row. Deliberately not a filter: every read
    # path returns flagged attributions unless a caller asks otherwise, so a
    # verdict cannot silently move anybody's numbers.
    fraud_score: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    fraud_verdict: Mapped[str] = mapped_column(String(16), nullable=False, server_default="clean")
    # The rules that fired, kept so "why was this flagged" can be answered
    # later without re-running the assessment against inputs that may since
    # have been erased by a deletion request.
    fraud_rules: Mapped[list[str] | None] = mapped_column(JSONB)


class FraudFinding(Base, OrgScopedMixin, TimestampMixin):
    """One rule firing on one tracking link over one window.

    Separate from the per-install columns above because this is a property of a
    population — flooding is not visible in any single install — and because
    keeping it separate stops a table that grows per install from also carrying
    per-campaign analysis.
    """

    __tablename__ = "fraud_findings"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    app_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("apps.id", ondelete="CASCADE"), index=True
    )
    tracking_link_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    window_start: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rule: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # Stored, not regenerated: the thresholds this sentence quotes may have
    # changed by the time someone reads it, and a finding has to say what it
    # said when it was made.
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict[str, object] | None] = mapped_column(JSONB)


class ConversionMapping(Base, OrgScopedMixin, TimestampMixin):
    """Event name to SKAdNetwork conversion value, per platform."""

    __tablename__ = "conversion_mappings"
    __table_args__ = (
        UniqueConstraint("app_id", "platform", "event_name"),
        CheckConstraint("conversion_value BETWEEN 0 AND 63", name="conversion_value_range"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    app_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("apps.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(20), nullable=False)
    event_name: Mapped[str] = mapped_column(String(120), nullable=False)
    conversion_value: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    coarse_value: Mapped[str | None] = mapped_column(String(10))
