"""Campaigns, tracking links, and deep links."""

from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from mmp_db.base import Base, OrgScopedMixin, TimestampMixin, one_of, org_fk, uuid_pk

CAMPAIGN_STATUSES = ("active", "paused", "archived")
LINK_STATUSES = ("active", "disabled")


class Campaign(Base, OrgScopedMixin, TimestampMixin):
    __tablename__ = "campaigns"
    __table_args__ = (
        CheckConstraint(one_of("status", *CAMPAIGN_STATUSES), name="status_valid"),
        UniqueConstraint("app_id", "name"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    app_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("apps.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str | None] = mapped_column(String(120), index=True)
    medium: Mapped[str | None] = mapped_column(String(120))
    # The network's own campaign identifier, kept so postbacks and reconciliation
    # against their reporting can be matched without a manual mapping table.
    external_campaign_id: Mapped[str | None] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")


class TrackingLink(Base, OrgScopedMixin, TimestampMixin):
    """A redirectable campaign link.

    ``tracking_code`` is generated with ``secrets.token_urlsafe`` — random, not
    sequential. A guessable code lets anyone enumerate a client's campaign
    structure by walking the space, and lets a competitor fabricate clicks
    against a known link.
    """

    __tablename__ = "tracking_links"
    __table_args__ = (CheckConstraint(one_of("status", *LINK_STATUSES), name="status_valid"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    app_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("apps.id", ondelete="CASCADE"), index=True
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), index=True
    )
    tracking_code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    android_url: Mapped[str | None] = mapped_column(Text)
    ios_url: Mapped[str | None] = mapped_column(Text)
    fallback_url: Mapped[str] = mapped_column(Text, nullable=False)
    deep_link_path: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")


class DeepLink(Base, OrgScopedMixin, TimestampMixin):
    __tablename__ = "deep_links"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    app_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("apps.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    destination: Mapped[str] = mapped_column(Text, nullable=False)
    fallback_url: Mapped[str] = mapped_column(Text, nullable=False)
