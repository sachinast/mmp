"""Outbound conversion distribution: integrations, postbacks, webhooks."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import BYTEA, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from mmp_db.base import Base, OrgScopedMixin, TimestampMixin, one_of, org_fk, uuid_pk

DELIVERY_STATUSES = ("pending", "in_flight", "delivered", "failed", "abandoned")
HTTP_METHODS = ("GET", "POST")
INTEGRATION_STATUSES = ("active", "paused", "error")


class ProviderIntegration(Base, OrgScopedMixin, TimestampMixin):
    """A partner account, with its credentials under envelope encryption.

    The credential is encrypted with AES-256-GCM under a data key that is itself
    wrapped by KMS. ``key_version`` lets the wrapping key rotate without
    re-encrypting payloads, and ``aad`` binds the ciphertext to the organisation
    that owns it — a row copied into another tenant fails to decrypt rather than
    silently working.
    """

    __tablename__ = "provider_integrations"
    __table_args__ = (
        UniqueConstraint("organization_id", "provider", "name"),
        CheckConstraint(one_of("status", *INTEGRATION_STATUSES), name="status_valid"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    provider: Mapped[str] = mapped_column(String(60), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)

    credentials_ciphertext: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    credentials_nonce: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    wrapped_dek: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    key_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)

    configuration: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    last_health_check_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class PostbackRule(Base, OrgScopedMixin, TimestampMixin):
    """A conversion forwarding rule.

    ``url_template`` uses ``{{variable}}`` placeholders substituted from a fixed
    allowlist — never a template engine. Rendering user-supplied templates with
    Jinja would hand anyone who can create a rule server-side template injection
    and, from there, code execution.
    """

    __tablename__ = "postback_rules"
    __table_args__ = (
        CheckConstraint(one_of("method", *HTTP_METHODS), name="method_valid"),
        Index("ix_postback_rules_app_event", "app_id", "trigger_event"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    app_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("apps.id", ondelete="CASCADE"), index=True
    )
    provider_integration_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("provider_integrations.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    trigger_event: Mapped[str] = mapped_column(String(120), nullable=False)
    method: Mapped[str] = mapped_column(String(4), nullable=False, default="GET")
    url_template: Mapped[str] = mapped_column(Text, nullable=False)
    body_template: Mapped[str | None] = mapped_column(Text)

    # Header values may carry a partner's bearer token, so they are encrypted
    # with the same envelope scheme and never returned by the API after save.
    headers_ciphertext: Mapped[bytes | None] = mapped_column(BYTEA)
    headers_nonce: Mapped[bytes | None] = mapped_column(BYTEA)
    wrapped_dek: Mapped[bytes | None] = mapped_column(BYTEA)
    # As on webhooks: which master key wrapped the data key. Without it a
    # header set sealed after a rotation cannot be opened at all.
    key_version: Mapped[int] = mapped_column(nullable=False, default=1, server_default="1")

    success_status_codes: Mapped[list[int]] = mapped_column(
        JSONB, nullable=False, default=lambda: [200, 201, 202, 204]
    )
    requires_attribution: Mapped[bool] = mapped_column(nullable=False, default=True)
    is_sandbox: Mapped[bool] = mapped_column(nullable=False, default=False)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)


class PostbackDelivery(Base, OrgScopedMixin):
    """One attempt-tracked delivery of one event to one rule.

    The unique constraint on ``(postback_rule_id, event_id)`` is what makes
    delivery idempotent: a worker claims the row before sending, so a redelivered
    queue message cannot fire a second conversion at an ad network.
    """

    __tablename__ = "postback_deliveries"
    __table_args__ = (
        UniqueConstraint("postback_rule_id", "event_id"),
        CheckConstraint(one_of("status", *DELIVERY_STATUSES), name="status_valid"),
        Index(
            "ix_postback_deliveries_retry",
            "next_retry_at",
            postgresql_where="status = 'failed'",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    postback_rule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("postback_rules.id", ondelete="CASCADE"), index=True
    )
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)

    request_url: Mapped[str | None] = mapped_column(Text)
    response_status: Mapped[int | None] = mapped_column(Integer)
    # Truncated on write. An unbounded partner response body is an unbounded
    # write amplification on our busiest outbound table.
    response_body: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )
    delivered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class WebhookDelivery(Base, OrgScopedMixin):
    """One attempt to deliver one event to one webhook.

    Separate from postback_deliveries rather than sharing a table. They look
    similar and answer to different people: a postback's shape is dictated by an
    ad network, a webhook's by us. Sharing storage would mean every schema change
    for a network's quirk touching the customer-facing contract too.

    The unique constraint carries the same weight as the postback one: an
    at-least-once queue will redeliver, and a duplicated purchase notification
    is a duplicated order in whatever system is listening.
    """

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("webhook_id", "event_id"),
        CheckConstraint(one_of("status", *DELIVERY_STATUSES), name="status_valid"),
        Index(
            "ix_webhook_deliveries_retry",
            "next_retry_at",
            postgresql_where="status = 'failed'",
        ),
        Index("ix_webhook_deliveries_recent", "webhook_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    webhook_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("webhooks.id", ondelete="CASCADE"), index=True
    )
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)

    request_url: Mapped[str | None] = mapped_column(Text)
    request_body: Mapped[str | None] = mapped_column(Text)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )
    delivered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class Webhook(Base, OrgScopedMixin, TimestampMixin):
    __tablename__ = "webhooks"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    url: Mapped[str] = mapped_column(Text, nullable=False)
    # Recoverable, unlike an API key: we must reproduce the secret to sign each
    # delivery. Encrypted rather than hashed, and never returned after creation.
    secret_ciphertext: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    secret_nonce: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    wrapped_dek: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    # Which master key wrapped the data key. Without it a secret sealed after a
    # rotation is read back under the old key and cannot be opened at all.
    key_version: Mapped[int] = mapped_column(nullable=False, default=1, server_default="1")
    events: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    consecutive_failures: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    disabled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
