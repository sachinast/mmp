"""Webhook configuration.

A webhook's signing secret is **recoverable by design** — we must reproduce it
to sign every delivery — which makes it the one credential in this system that
cannot be hashed. It is encrypted under the envelope scheme and returned exactly
once, at creation, in the same way an API key is: shown, then unavailable
forever. Someone who loses it rotates it.

The auto-disable behaviour is worth stating plainly because it surprises people:
after enough consecutive failures we stop delivering and mark the webhook
disabled. That is not punitive. An endpoint returning 500s for a day is not
recovering on its own, and continuing to hammer it generates load on a broken
system and a backlog we would have to drain later anyway.
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from mmp_core.ids import uuid7
from mmp_core.logging import get_logger
from mmp_core.outbound import is_permitted
from mmp_crypto.envelope import SealedSecret, open_sealed, organization_aad, seal
from mmp_db.jsonfields import decode
from mmp_db.types import DbConn
from mmp_providers.webhooks import (
    MAX_CONSECUTIVE_FAILURES,
    WEBHOOK_EVENTS,
    verification_snippet,
)
from pydantic import BaseModel, Field, HttpUrl, field_validator

from mmp_api.context import AppContext
from mmp_api.deps import Principal, get_context, require_role, tenant_db
from mmp_db import sql

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
log = get_logger(__name__)

WEBHOOK_COLUMNS = (
    "id",
    "url",
    "events",
    "enabled",
    "consecutive_failures",
    "disabled_at",
    "created_at",
)
DELIVERY_COLUMNS = (
    "id",
    "webhook_id",
    "event_id",
    "event_type",
    "status",
    "attempt_count",
    "response_status",
    "response_body",
    "error",
    "created_at",
    "delivered_at",
    "next_retry_at",
)

# 32 bytes, prefixed so it is recognisable in a customer's configuration and
# greppable in their secret scanner.
# Not a secret: a public prefix so the value is recognisable in a customer's
# configuration and greppable by their secret scanner.
SECRET_PREFIX = "whsec_"  # noqa: S105 # nosec B105


class WebhookCreate(BaseModel):
    url: HttpUrl
    events: list[str] = Field(min_length=1, max_length=len(WEBHOOK_EVENTS))

    @field_validator("url")
    @classmethod
    def _https_only(cls, v: HttpUrl) -> HttpUrl:
        # We post conversion data here. Over http, anyone on the path reads a
        # customer's revenue and can forge deliveries into their systems.
        if v.scheme != "https":
            raise ValueError("webhook URLs must use https")
        return v

    @field_validator("events")
    @classmethod
    def _known_events(cls, v: list[str]) -> list[str]:
        unknown = set(v) - WEBHOOK_EVENTS
        if unknown:
            raise ValueError(
                f"unknown events: {', '.join(sorted(unknown))}. "
                f"Available: {', '.join(sorted(WEBHOOK_EVENTS))}"
            )
        return sorted(set(v))


class WebhookUpdate(BaseModel):
    url: HttpUrl | None = None
    events: list[str] | None = None
    enabled: bool | None = None

    @field_validator("url")
    @classmethod
    def _https_only(cls, v: HttpUrl | None) -> HttpUrl | None:
        if v is not None and v.scheme != "https":
            raise ValueError("webhook URLs must use https")
        return v

    @field_validator("events")
    @classmethod
    def _known_events(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        unknown = set(v) - WEBHOOK_EVENTS
        if unknown:
            raise ValueError(f"unknown events: {', '.join(sorted(unknown))}")
        return sorted(set(v))


class WebhookOut(BaseModel):
    @field_validator("events", mode="before")
    @classmethod
    def _decode_json(cls, v: object) -> object:
        return decode(v)

    id: uuid.UUID
    url: str
    events: list[str]
    enabled: bool
    consecutive_failures: int
    disabled_at: dt.datetime | None
    created_at: dt.datetime
    # So the dashboard can warn before the endpoint is switched off, rather than
    # after.
    failures_before_disable: int = MAX_CONSECUTIVE_FAILURES


class WebhookCreated(WebhookOut):
    """Returned once, from creation and rotation only."""

    signing_secret: str
    verification_example: str
    warning: str = "Store this secret now. It cannot be retrieved again."


class WebhookDeliveryOut(BaseModel):
    id: uuid.UUID
    webhook_id: uuid.UUID
    event_id: uuid.UUID
    event_type: str
    status: str
    attempt_count: int
    response_status: int | None
    response_body: str | None
    error: str | None
    created_at: dt.datetime
    delivered_at: dt.datetime | None
    next_retry_at: dt.datetime | None


def _check_destination(url: str) -> None:
    permitted, reason = is_permitted(url)
    if not permitted:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"destination is not permitted: {reason}",
        )


def _seal_secret(
    context: AppContext, organization_id: uuid.UUID, secret: str
) -> tuple[bytes, bytes, bytes]:
    sealed = seal(
        secret.encode("utf-8"),
        provider=context.master_keys,
        aad=organization_aad(organization_id),
    )
    return sealed.ciphertext, sealed.nonce, sealed.wrapped_dek


def open_webhook_secret(context: AppContext, organization_id: uuid.UUID, row: object) -> str:
    """Recover a signing secret in order to sign a delivery.

    The only reason this function exists. It is not reachable from any endpoint
    that returns data to a user.
    """
    return open_sealed(
        SealedSecret(
            ciphertext=bytes(row["secret_ciphertext"]),  # type: ignore[index]
            nonce=bytes(row["secret_nonce"]),  # type: ignore[index]
            wrapped_dek=bytes(row["wrapped_dek"]),  # type: ignore[index]
            key_version=1,
        ),
        provider=context.master_keys,
        aad=organization_aad(organization_id),
    ).decode("utf-8")


@router.get("")
async def list_webhooks(
    _principal: Annotated[Principal, Depends(require_role("viewer"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> list[WebhookOut]:
    rows = await conn.fetch(
        sql.select("webhooks", WEBHOOK_COLUMNS, suffix="ORDER BY created_at DESC")
    )
    return [WebhookOut(**dict(row)) for row in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_webhook(
    body: WebhookCreate,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    context: Annotated[AppContext, Depends(get_context)],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> WebhookCreated:
    url = str(body.url)
    _check_destination(url)

    secret = SECRET_PREFIX + secrets.token_urlsafe(32)
    ciphertext, nonce, wrapped = _seal_secret(context, principal.org_id, secret)
    webhook_id = uuid7()

    row = await conn.fetchrow(
        sql.with_returning(
            """INSERT INTO webhooks (id, organization_id, url, secret_ciphertext,
                                     secret_nonce, wrapped_dek, events, enabled,
                                     consecutive_failures, created_at, updated_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, true, 0, now(), now())""",
            WEBHOOK_COLUMNS,
        ),
        webhook_id,
        principal.org_id,
        url,
        ciphertext,
        nonce,
        wrapped,
        json.dumps(body.events),
    )

    log.info(
        "webhook_created",
        webhook_id=str(webhook_id),
        events=body.events,
        actor=str(principal.user_id),
    )
    return WebhookCreated(
        **dict(row),
        signing_secret=secret,
        # Shipped with the secret so verification is written correctly the first
        # time. A signature nobody verifies is a signature that does nothing.
        verification_example=verification_snippet(secret),
    )


@router.post("/{webhook_id}/rotate")
async def rotate_secret(
    webhook_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    context: Annotated[AppContext, Depends(get_context)],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> WebhookCreated:
    """Issue a new signing secret.

    Deliveries are signed with the new secret immediately, so the receiver must
    be updated first — there is no dual-signing period. That is a deliberate
    simplification: accepting two secrets at once doubles the window in which a
    leaked one still works, and rotation is rare enough to coordinate.
    """
    secret = SECRET_PREFIX + secrets.token_urlsafe(32)
    ciphertext, nonce, wrapped = _seal_secret(context, principal.org_id, secret)

    row = await conn.fetchrow(
        sql.with_returning(
            """UPDATE webhooks
               SET secret_ciphertext = $2, secret_nonce = $3, wrapped_dek = $4,
                   updated_at = now()
               WHERE id = $1""",
            WEBHOOK_COLUMNS,
        ),
        webhook_id,
        ciphertext,
        nonce,
        wrapped,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "webhook not found")

    log.info("webhook_secret_rotated", webhook_id=str(webhook_id), actor=str(principal.user_id))
    return WebhookCreated(
        **dict(row), signing_secret=secret, verification_example=verification_snippet(secret)
    )


@router.patch("/{webhook_id}")
async def update_webhook(
    webhook_id: uuid.UUID,
    body: WebhookUpdate,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> WebhookOut:
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "nothing to update")
    if "url" in changes:
        changes["url"] = str(changes["url"])
        _check_destination(changes["url"])
    if "events" in changes:
        changes["events"] = json.dumps(changes["events"])

    # Re-enabling resets the failure count. Without this, an endpoint that was
    # fixed would be disabled again by the next single failure, since it would
    # still be sitting at the threshold.
    if changes.get("enabled") is True:
        changes["consecutive_failures"] = 0
        changes["disabled_at"] = None

    row = await conn.fetchrow(
        sql.update("webhooks", list(changes), where="id = $1", returning=WEBHOOK_COLUMNS),
        webhook_id,
        *changes.values(),
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "webhook not found")
    log.info("webhook_updated", webhook_id=str(webhook_id), actor=str(principal.user_id))
    return WebhookOut(**dict(row))


@router.delete("/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    webhook_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> None:
    """Genuinely deleted, unlike a postback rule.

    A webhook is the customer's own integration, not a record of what an ad
    network was told. Nothing downstream needs to explain it later, and leaving
    a disabled row holding an encrypted secret serves nobody.
    """
    result = await conn.execute("DELETE FROM webhooks WHERE id = $1", webhook_id)
    if result == "DELETE 0":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "webhook not found")
    log.info("webhook_deleted", webhook_id=str(webhook_id), actor=str(principal.user_id))


@router.get("/events")
async def available_events(
    _principal: Annotated[Principal, Depends(require_role("viewer"))],
) -> dict[str, list[str]]:
    return {"events": sorted(WEBHOOK_EVENTS)}


@router.get("/{webhook_id}/deliveries")
async def list_deliveries(
    webhook_id: uuid.UUID,
    _principal: Annotated[Principal, Depends(require_role("viewer"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
    delivery_status: Annotated[
        Literal["pending", "in_flight", "delivered", "failed", "abandoned"] | None,
        Query(alias="status"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[WebhookDeliveryOut]:
    if delivery_status is not None:
        rows = await conn.fetch(
            sql.select(
                "webhook_deliveries",
                DELIVERY_COLUMNS,
                where="webhook_id = $1 AND status = $2",
                suffix="ORDER BY created_at DESC LIMIT $3",
            ),
            webhook_id,
            delivery_status,
            limit,
        )
    else:
        rows = await conn.fetch(
            sql.select(
                "webhook_deliveries",
                DELIVERY_COLUMNS,
                where="webhook_id = $1",
                suffix="ORDER BY created_at DESC LIMIT $2",
            ),
            webhook_id,
            limit,
        )
    return [WebhookDeliveryOut(**dict(row)) for row in rows]
