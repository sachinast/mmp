"""Postback rule management and delivery logs.

Two things govern everything here:

* **A bad rule must fail at save time, not at delivery time.** A URL that points
  somewhere we refuse to reach, or a template that references a variable that
  does not exist, produces an error the user sees while they are looking at the
  form. The alternative is a rule that appears to work and quietly delivers
  nothing, discovered when an ad network asks why conversions stopped.
* **Headers are write-only.** A postback header commonly carries a partner's
  bearer token. It is encrypted at rest and never returned — not in a listing,
  not in a detail view, not to an owner. Someone who needs to change it sets a
  new value; nobody needs to read it back, and every path that could return it
  is a path that could leak it.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Annotated, Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from mmp_core.ids import uuid7
from mmp_core.logging import get_logger
from mmp_core.outbound import is_permitted
from mmp_crypto.envelope import SealedSecret, open_sealed, organization_aad, seal
from mmp_db.jsonfields import decode
from mmp_db.types import DbConn
from mmp_providers.templates import (
    ALLOWED_VARIABLES,
    SUB_PARAMETERS,
    TemplateError,
    inspect,
    uses_sub_parameters,
)
from pydantic import BaseModel, Field, field_validator

from mmp_api.context import AppContext
from mmp_api.deps import Principal, get_context, require_role, tenant_db
from mmp_db import sql

router = APIRouter(tags=["postbacks"])
log = get_logger(__name__)

RULE_COLUMNS = (
    "id",
    "app_id",
    "campaign_id",
    "provider_integration_id",
    "name",
    "trigger_event",
    "method",
    "url_template",
    "body_template",
    "success_status_codes",
    "requires_attribution",
    "is_sandbox",
    "enabled",
    "created_at",
)
DELIVERY_COLUMNS = (
    "id",
    "postback_rule_id",
    "event_id",
    "status",
    "attempt_count",
    "request_url",
    "response_status",
    "response_body",
    "error",
    "created_at",
    "delivered_at",
    "next_retry_at",
)
RULE_UPDATABLE = (
    "name",
    "campaign_id",
    "trigger_event",
    "method",
    "url_template",
    "body_template",
    "requires_attribution",
    "is_sandbox",
    "enabled",
)

MAX_HEADERS = 10
MAX_HEADER_VALUE = 1024

# Headers a rule may not set. These are ours to control: a rule that could
# override Host would defeat the destination pinning in mmp_core.outbound, and
# one that could set Content-Length could desynchronise the request.
FORBIDDEN_HEADERS = frozenset(
    {"host", "content-length", "transfer-encoding", "connection", "expect", "upgrade"}
)


class PostbackRuleCreate(BaseModel):
    app_id: uuid.UUID
    name: Annotated[str, Field(min_length=1, max_length=255)]
    trigger_event: Annotated[str, Field(min_length=1, max_length=120)]
    url_template: Annotated[str, Field(min_length=1, max_length=4096)]
    method: Literal["GET", "POST"] = "GET"
    body_template: Annotated[str, Field(max_length=4096)] | None = None
    headers: dict[str, str] | None = None
    success_status_codes: list[int] = Field(default_factory=lambda: [200, 201, 202, 204])
    requires_attribution: bool = True
    is_sandbox: bool = False
    # One campaign's installs only. Required to use {{sub1}}, {{sub2}} or {{sub3}}.
    campaign_id: uuid.UUID | None = None

    @field_validator("success_status_codes")
    @classmethod
    def _sane_codes(cls, v: list[int]) -> list[int]:
        if not v or len(v) > 10:
            raise ValueError("between 1 and 10 status codes")
        if any(code < 100 or code > 599 for code in v):
            raise ValueError("status codes must be between 100 and 599")
        return v

    @field_validator("headers")
    @classmethod
    def _sane_headers(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return None
        if len(v) > MAX_HEADERS:
            raise ValueError(f"at most {MAX_HEADERS} headers")
        for name, value in v.items():
            if name.lower() in FORBIDDEN_HEADERS:
                raise ValueError(f"{name} is set by the platform and cannot be overridden")
            if len(value) > MAX_HEADER_VALUE:
                raise ValueError(f"{name} value is too long")
            # A newline in a header value is request splitting: everything after
            # it is read by the partner as a separate header, or a separate
            # request.
            if "\n" in value or "\r" in value or "\n" in name or "\r" in name:
                raise ValueError("header names and values may not contain newlines")
        return v


class PostbackRuleUpdate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    trigger_event: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    method: Literal["GET", "POST"] | None = None
    url_template: Annotated[str, Field(min_length=1, max_length=4096)] | None = None
    body_template: Annotated[str, Field(max_length=4096)] | None = None
    requires_attribution: bool | None = None
    is_sandbox: bool | None = None
    enabled: bool | None = None
    # Explicitly null widens the rule to every install of the app — refused if
    # its templates still use a sub parameter.
    campaign_id: uuid.UUID | None = None


class PostbackRuleOut(BaseModel):
    # asyncpg returns jsonb as a string; see mmp_db.jsonfields for why a pool
    # codec is not an option.
    @field_validator("success_status_codes", mode="before")
    @classmethod
    def _decode_json(cls, v: object) -> object:
        return decode(v)

    id: uuid.UUID
    app_id: uuid.UUID
    campaign_id: uuid.UUID | None
    provider_integration_id: uuid.UUID | None
    name: str
    trigger_event: str
    method: str
    url_template: str
    body_template: str | None
    success_status_codes: list[int]
    requires_attribution: bool
    is_sandbox: bool
    enabled: bool
    created_at: dt.datetime
    # The names only. A rule's header *values* commonly carry a partner's bearer
    # token and are never returned — see the module docstring.
    header_names: list[str] = Field(default_factory=list)


class DeliveryOut(BaseModel):
    id: uuid.UUID
    postback_rule_id: uuid.UUID
    event_id: uuid.UUID
    status: str
    attempt_count: int
    request_url: str | None
    response_status: int | None
    response_body: str | None
    error: str | None
    created_at: dt.datetime
    delivered_at: dt.datetime | None
    next_retry_at: dt.datetime | None


class TemplatePreview(BaseModel):
    url: str
    body: str | None
    variables_used: list[str]
    available_variables: list[str]


def _validate_templates(url_template: str, body_template: str | None) -> None:
    """Reject an unusable template while the user is still looking at the form."""
    try:
        check = inspect(url_template)
        if not check.ok:
            raise TemplateError(
                "url_template: " + ", ".join(sorted(check.unknown | check.malformed))
            )
        if body_template:
            body_check = inspect(body_template)
            if not body_check.ok:
                raise TemplateError(
                    "body_template: " + ", ".join(sorted(body_check.unknown | body_check.malformed))
                )
    except TemplateError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


async def _validate_scope(
    conn: DbConn,
    *,
    app_id: uuid.UUID,
    campaign_id: uuid.UUID | None,
    url_template: str,
    body_template: str | None,
) -> None:
    """A rule may only use a partner's sub parameters if it is scoped to one campaign.

    sub1 is typically a partner's own click id. An app-wide rule fires for every
    install whichever partner earned it, so {{sub1}} in one would send partner
    A's click ids to wherever partner B's rule points. Refused here, at save
    time, with the reason — rather than discovered by a partner reading another
    partner's ids in its logs.
    """
    if campaign_id is not None and not await conn.fetchval(
        "SELECT 1 FROM campaigns WHERE id = $1 AND app_id = $2", campaign_id, app_id
    ):
        # Scoped by RLS too, so another tenant's campaign is equally "not found".
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "campaign not found for this app"
        )

    used = uses_sub_parameters(url_template, body_template)
    if used and campaign_id is None:
        names = ", ".join("{{" + name + "}}" for name in sorted(used))
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{names} can only be used in a rule scoped to one campaign: they carry the "
            "partner's own click ids, and a rule for every campaign would send them to "
            "whichever partner it points at",
        )


def _validate_destination(url_template: str, *, is_sandbox: bool) -> None:
    """Check where the rule points, before it is saved.

    The template is stripped of its placeholders first: the destination is
    determined by the literal part of the URL, and a placeholder is not a valid
    hostname character anyway.

    Skipped for sandbox rules, whose traffic goes to our own echo endpoint
    rather than to the template's destination.
    """
    if is_sandbox:
        return
    import re

    concrete = re.sub(r"\{\{[^}]*\}\}", "x", url_template)
    permitted, reason = is_permitted(concrete)
    if not permitted:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"destination is not permitted: {reason}",
        )


def _seal_headers(
    context: AppContext, organization_id: uuid.UUID, headers: dict[str, str] | None
) -> tuple[bytes | None, bytes | None, bytes | None, int]:
    """Seal a rule's custom headers, keeping the key version they were sealed
    under.

    A rule with no headers still gets a version: the column is NOT NULL, and 1
    is what an unsealed row would have meant anyway.
    """
    if not headers:
        return None, None, None, 1
    import msgspec

    sealed = seal(
        msgspec.json.encode(headers),
        provider=context.master_keys,
        aad=organization_aad(organization_id),
    )
    return sealed.ciphertext, sealed.nonce, sealed.wrapped_dek, sealed.key_version


def _header_names(
    context: AppContext, organization_id: uuid.UUID, row: asyncpg.Record
) -> list[str]:
    """Decrypt only far enough to list the names.

    A user needs to know an Authorization header exists on their rule; they do
    not need its value, and neither does any code path that could return it.
    """
    if not row["headers_ciphertext"]:
        return []
    import msgspec

    try:
        plaintext = open_sealed(
            SealedSecret(
                ciphertext=bytes(row["headers_ciphertext"]),
                nonce=bytes(row["headers_nonce"]),
                wrapped_dek=bytes(row["wrapped_dek"]),
                key_version=int(row["key_version"]),
            ),
            provider=context.master_keys,
            aad=organization_aad(organization_id),
        )
        return sorted(msgspec.json.decode(plaintext, type=dict[str, str]))
    except Exception:
        log.warning("postback_headers_undecryptable", rule_id=str(row["id"]))
        return []


# ---------------------------------------------------------------- rules
@router.get("/postback-rules")
async def list_rules(
    principal: Annotated[Principal, Depends(require_role("viewer"))],
    context: Annotated[AppContext, Depends(get_context)],
    conn: Annotated[DbConn, Depends(tenant_db)],
    app_id: uuid.UUID | None = None,
) -> list[PostbackRuleOut]:
    columns = (*RULE_COLUMNS, "headers_ciphertext", "headers_nonce", "wrapped_dek", "key_version")
    if app_id is not None:
        rows = await conn.fetch(
            sql.select(
                "postback_rules", columns, where="app_id = $1", suffix="ORDER BY created_at DESC"
            ),
            app_id,
        )
    else:
        rows = await conn.fetch(
            sql.select("postback_rules", columns, suffix="ORDER BY created_at DESC")
        )
    return [
        PostbackRuleOut(
            **{key: row[key] for key in RULE_COLUMNS},
            header_names=_header_names(context, principal.org_id, row),
        )
        for row in rows
    ]


@router.post("/postback-rules", status_code=status.HTTP_201_CREATED)
async def create_rule(
    body: PostbackRuleCreate,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    context: Annotated[AppContext, Depends(get_context)],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> PostbackRuleOut:
    if not await conn.fetchval("SELECT 1 FROM apps WHERE id = $1", body.app_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "app not found")

    _validate_templates(body.url_template, body.body_template)
    await _validate_scope(
        conn,
        app_id=body.app_id,
        campaign_id=body.campaign_id,
        url_template=body.url_template,
        body_template=body.body_template,
    )
    _validate_destination(body.url_template, is_sandbox=body.is_sandbox)

    ciphertext, nonce, wrapped, key_version = _seal_headers(context, principal.org_id, body.headers)
    rule_id = uuid7()
    row = await conn.fetchrow(
        sql.with_returning(
            """INSERT INTO postback_rules (
                   id, organization_id, app_id, name, trigger_event, method,
                   url_template, body_template, headers_ciphertext, headers_nonce,
                   wrapped_dek, key_version, success_status_codes, requires_attribution,
                   is_sandbox, campaign_id, enabled, created_at, updated_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                       $13, $14, $15, $16, true, now(), now())""",
            RULE_COLUMNS,
        ),
        rule_id,
        principal.org_id,
        body.app_id,
        body.name,
        body.trigger_event,
        body.method,
        body.url_template,
        body.body_template,
        ciphertext,
        nonce,
        wrapped,
        key_version,
        # asyncpg takes jsonb as a string, and hands it back as one.
        json.dumps(body.success_status_codes),
        body.requires_attribution,
        body.is_sandbox,
        body.campaign_id,
    )

    log.info(
        "postback_rule_created",
        rule_id=str(rule_id),
        trigger_event=body.trigger_event,
        sandbox=body.is_sandbox,
        actor=str(principal.user_id),
    )
    return PostbackRuleOut(**dict(row), header_names=sorted(body.headers or {}))


@router.patch("/postback-rules/{rule_id}")
async def update_rule(
    rule_id: uuid.UUID,
    body: PostbackRuleUpdate,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    context: Annotated[AppContext, Depends(get_context)],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> PostbackRuleOut:
    changes = body.model_dump(exclude_unset=True)
    unknown = set(changes) - set(RULE_UPDATABLE)
    if unknown:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"cannot update: {unknown}")
    if not changes:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "nothing to update")

    existing = await conn.fetchrow(
        "SELECT app_id, campaign_id, url_template, body_template, is_sandbox"
        " FROM postback_rules WHERE id = $1",
        rule_id,
    )
    if existing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "rule not found")

    # Validate the rule as it will be after the change, not as it was: a patch
    # that only flips is_sandbox off must revalidate a destination that was
    # never checked while the rule was in sandbox mode.
    url_template = changes.get("url_template", existing["url_template"])
    body_template = changes.get("body_template", existing["body_template"])
    is_sandbox = changes.get("is_sandbox", existing["is_sandbox"])
    _validate_templates(url_template, body_template)
    # The merged rule again: a patch that clears campaign_id on a rule whose
    # template still says {{sub1}} is the unscoped case arrived at in two steps.
    await _validate_scope(
        conn,
        app_id=existing["app_id"],
        campaign_id=changes.get("campaign_id", existing["campaign_id"]),
        url_template=url_template,
        body_template=body_template,
    )
    _validate_destination(url_template, is_sandbox=is_sandbox)

    row = await conn.fetchrow(
        sql.update(
            "postback_rules",
            list(changes),
            where="id = $1",
            returning=(
                *RULE_COLUMNS,
                "headers_ciphertext",
                "headers_nonce",
                "wrapped_dek",
                "key_version",
            ),
        ),
        rule_id,
        *changes.values(),
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "rule not found")
    return PostbackRuleOut(
        **{key: row[key] for key in RULE_COLUMNS},
        header_names=_header_names(context, principal.org_id, row),
    )


@router.delete("/postback-rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disable_rule(
    rule_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> None:
    """Disable, not delete.

    Delivery rows reference this rule, and a deleted rule would leave an
    advertiser unable to explain conversions their network already received.
    """
    result = await conn.execute(
        "UPDATE postback_rules SET enabled = false, updated_at = now() WHERE id = $1",
        rule_id,
    )
    if result == "UPDATE 0":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "rule not found")
    log.info("postback_rule_disabled", rule_id=str(rule_id), actor=str(principal.user_id))


@router.get("/postback-rules/variables")
async def postback_variables(
    _principal: Annotated[Principal, Depends(require_role("viewer"))],
) -> dict[str, list[str]]:
    """What a template may reference, so a form can list it from the source of
    truth rather than from a copy that drifts."""
    return {
        "variables": sorted(ALLOWED_VARIABLES),
        "campaign_scoped_only": sorted(SUB_PARAMETERS),
    }


@router.get("/postback-rules/{rule_id}/preview")
async def preview_rule(
    rule_id: uuid.UUID,
    _principal: Annotated[Principal, Depends(require_role("viewer"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> TemplatePreview:
    """Render the templates against sample values.

    Lets someone see the URL their partner will actually receive before any real
    conversion depends on it — which is the difference between finding a
    misplaced parameter now and finding it in a reconciliation meeting.
    """
    from mmp_providers.templates import render

    row = await conn.fetchrow(
        "SELECT url_template, body_template FROM postback_rules WHERE id = $1", rule_id
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "rule not found")

    sample: dict[str, object] = {
        "event_id": "01a07d15-839d-79fe-bb9c-e427a9b16652",
        "event_name": "purchase",
        "event_timestamp": "2026-09-08T12:00:00+00:00",
        "app_id": "01a07d15-0000-7000-8000-000000000001",
        "user_id": "user-1234",
        "anonymous_id": "device-abcdef",
        "click_id": "01a07d15-0000-7000-8000-0000000000c1",
        "campaign_id": "01a07d15-0000-7000-8000-0000000000ca",
        "campaign_name": "Meta US — Q3",
        "source": "meta",
        "medium": "cpi",
        "revenue": "19.99",
        "currency": "USD",
        "platform": 1,
        "country": "US",
        "attribution_method": "referrer",
        "install_timestamp": "2026-09-01T09:15:00+00:00",
        "sub1": "partner-click-7f3a9c",
        "sub2": "publisher-42",
        "sub3": "creative-b",
    }
    check = inspect(row["url_template"])
    return TemplatePreview(
        url=render(row["url_template"], sample),
        body=render(row["body_template"], sample, encode=False) if row["body_template"] else None,
        variables_used=sorted(check.variables),
        available_variables=sorted(ALLOWED_VARIABLES),
    )


# ------------------------------------------------------------- deliveries
@router.get("/postback-deliveries")
async def list_deliveries(
    _principal: Annotated[Principal, Depends(require_role("viewer"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
    rule_id: uuid.UUID | None = None,
    delivery_status: Annotated[
        Literal["pending", "in_flight", "delivered", "failed", "abandoned"] | None,
        Query(alias="status"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[DeliveryOut]:
    """Recent delivery attempts, newest first.

    Bounded and never open-ended: this table is the busiest write target in the
    system, and an unpaginated listing of it is a query that gets slower every
    day until someone notices.
    """
    clauses = []
    args: list[Any] = []
    if rule_id is not None:
        args.append(rule_id)
        clauses.append(f"postback_rule_id = ${len(args)}")
    if delivery_status is not None:
        args.append(delivery_status)
        clauses.append(f"status = ${len(args)}")
    args.append(limit)

    where = " AND ".join(clauses) if clauses else ""
    rows = await conn.fetch(
        sql.select(
            "postback_deliveries",
            DELIVERY_COLUMNS,
            where=where,
            suffix=f"ORDER BY created_at DESC LIMIT ${len(args)}",
        ),
        *args,
    )
    return [DeliveryOut(**dict(row)) for row in rows]


@router.post("/postback-deliveries/{delivery_id}/retry")
async def retry_delivery(
    delivery_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> DeliveryOut:
    """Queue one failed delivery for another attempt.

    Only from a terminal failed state. Re-queueing something already in flight
    would let an impatient click produce the duplicate conversion the whole
    delivery design exists to prevent.
    """
    row = await conn.fetchrow(
        sql.with_returning(
            """UPDATE postback_deliveries
               SET status = 'failed', next_retry_at = now(), error = NULL
               WHERE id = $1 AND status IN ('failed', 'abandoned')""",
            DELIVERY_COLUMNS,
        ),
        delivery_id,
    )
    if row is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "only a failed or abandoned delivery can be retried",
        )
    log.info(
        "postback_delivery_retry_requested",
        delivery_id=str(delivery_id),
        actor=str(principal.user_id),
    )
    return DeliveryOut(**dict(row))
