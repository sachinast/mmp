"""Apps, API credentials, and per-app measurement configuration."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, SmallInteger, String
from sqlalchemy.dialects.postgresql import BYTEA, UUID
from sqlalchemy.orm import Mapped, mapped_column

from mmp_db.base import Base, OrgScopedMixin, TimestampMixin, one_of, org_fk, uuid_pk

PLATFORMS = ("android", "ios", "cross_platform")
APP_STATUSES = ("active", "paused", "disabled")
CONSENT_MODES = ("permissive", "strict")
KEY_ENVIRONMENTS = ("dev", "prod")
KEY_STATUSES = ("active", "revoked")
KEY_KINDS = ("sdk", "s2s")


class App(Base, OrgScopedMixin, TimestampMixin):
    __tablename__ = "apps"
    __table_args__ = (
        CheckConstraint(one_of("platform", *PLATFORMS), name="platform_valid"),
        CheckConstraint(one_of("status", *APP_STATUSES), name="status_valid"),
        CheckConstraint(one_of("consent_mode", *CONSENT_MODES), name="consent_mode_valid"),
        # Attribution windows are bounded here rather than trusted from the API,
        # because an unbounded window turns every install into a full-history
        # click scan.
        CheckConstraint(
            "install_window_days BETWEEN 1 AND 30 AND event_window_days BETWEEN 1 AND 90",
            name="attribution_windows_sane",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    platform: Mapped[str] = mapped_column(String(20), nullable=False)
    android_package_name: Mapped[str | None] = mapped_column(String(255), index=True)
    ios_bundle_id: Mapped[str | None] = mapped_column(String(255), index=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")

    # Attribution configuration. Stored per app and copied onto every
    # attribution row, so changing the window never rewrites history.
    install_window_days: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=7)
    event_window_days: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=30)
    session_timeout_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=30)

    # How the absence of a consent record is read. An explicit denial is always
    # honoured; this decides only what "we have not been told" means. Defaults
    # to permissive so that shipping consent support does not silently stop
    # attributing every existing advertiser's installs — see mmp_ingest.consent
    # for the full argument.
    consent_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="permissive", server_default="permissive"
    )


class ApiKey(Base, OrgScopedMixin, TimestampMixin):
    """An app credential.

    Only ``key_hash`` is stored — an HMAC-SHA256 of the raw key under a pepper
    held in KMS, never in this database. ``key_prefix`` is the plaintext first
    characters, indexed, so authentication is one indexed lookup followed by one
    constant-time comparison rather than a scan-and-verify over every key.
    """

    __tablename__ = "api_keys"
    __table_args__ = (
        CheckConstraint(one_of("environment", *KEY_ENVIRONMENTS), name="environment_valid"),
        CheckConstraint(one_of("status", *KEY_STATUSES), name="status_valid"),
        CheckConstraint(one_of("kind", *KEY_KINDS), name="kind_valid"),
        Index("ix_api_keys_prefix_active", "key_prefix", postgresql_where="status = 'active'"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    app_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("apps.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False, default="default")
    kind: Mapped[str] = mapped_column(String(10), nullable=False, default="sdk")
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    key_hash: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    pepper_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    environment: Mapped[str] = mapped_column(String(10), nullable=False, default="dev")
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="active")
    # Written by a periodic flush, not on every request — updating a row on each
    # authenticated ingest would add a write to the hot path for a field nobody
    # reads in real time.
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
