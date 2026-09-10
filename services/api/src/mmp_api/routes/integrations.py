"""Provider integrations.

Configuring a network: which adapter, what credentials, what settings. Two
things this does that a raw postback rule cannot:

* **It validates against the adapter.** A missing credential, an https-less
  endpoint, an event map naming an event we never emit — all rejected when the
  integration is saved rather than discovered as a delivery that never arrives.
* **It publishes what a provider can do.** The dashboard can refuse to attach a
  refund rule to a provider that does not accept refunds, instead of letting
  someone configure something that will be silently discarded.

Credentials are write-only, like every other stored secret here: encrypted at
rest, and only the *names* of the supplied fields are ever returned.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

import asyncpg
import msgspec
from fastapi import APIRouter, Depends, HTTPException, status
from mmp_core.ids import uuid7
from mmp_core.logging import get_logger
from mmp_crypto.envelope import organization_aad, seal
from mmp_db.jsonfields import decode
from mmp_db.types import DbConn
from mmp_providers.base import Capability, ProviderConfig
from pydantic import BaseModel, Field

from mmp_api.context import AppContext
from mmp_api.deps import Principal, get_context, require_role, tenant_db
from mmp_db import audit, sql
from mmp_providers import registry

router = APIRouter(tags=["integrations"])
log = get_logger(__name__)

INTEGRATION_COLUMNS = (
    "id",
    "provider",
    "name",
    "configuration",
    "status",
    "last_health_check_at",
    "created_at",
)


class ProviderFieldInfo(BaseModel):
    """What an adapter needs configured, so a form can be built from the adapter
    instead of from a second copy of its requirements."""

    name: str
    label: str
    secret: bool
    required: bool
    hint: str


class ProviderInfo(BaseModel):
    name: str
    display_name: str
    auth_style: str
    capabilities: list[str]
    fields: list[ProviderFieldInfo]
    # So a dashboard can show what will be sent under what name, rather than
    # leaving someone to discover the translation from a network's report.
    event_map: dict[str, str]


class IntegrationCreate(BaseModel):
    provider: Annotated[str, Field(min_length=1, max_length=60)]
    name: Annotated[str, Field(min_length=1, max_length=120)]
    credentials: dict[str, str] = Field(default_factory=dict)
    configuration: dict[str, Any] = Field(default_factory=dict)


class IntegrationUpdate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    credentials: dict[str, str] | None = None
    configuration: dict[str, Any] | None = None
    status: Annotated[str, Field(pattern="^(active|paused)$")] | None = None


class IntegrationOut(BaseModel):
    id: uuid.UUID
    provider: str
    name: str
    configuration: dict[str, Any]
    status: str
    last_health_check_at: dt.datetime | None
    created_at: dt.datetime
    # Names only. A credential value is never returned, to anyone, for any role.
    credential_names: list[str] = Field(default_factory=list)


@router.get("/providers")
async def list_providers(
    _principal: Annotated[Principal, Depends(require_role("viewer"))],
) -> list[ProviderInfo]:
    """The adapters this build has.

    Read from the registry rather than a hard-coded list, so a deployment cannot
    advertise an integration it cannot actually perform.
    """
    registry.load_builtin_once()
    return [
        ProviderInfo(
            name=provider.name,
            display_name=provider.display_name,
            auth_style=str(provider.auth_style),
            capabilities=sorted(str(c) for c in provider.capabilities),
            event_map=dict(provider.event_map),
            fields=[
                ProviderFieldInfo(
                    name=field.name,
                    label=field.label,
                    secret=field.secret,
                    required=field.required,
                    hint=field.hint,
                )
                for field in provider.fields
            ],
        )
        for provider in registry.available()
    ]


def _validate(
    provider_name: str,
    body_credentials: dict[str, Any],
    configuration: dict[str, Any],
) -> None:
    registry.load_builtin_once()
    try:
        provider = registry.get(provider_name)
    except registry.UnknownProvider as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    problems = provider.validate(
        ProviderConfig(credentials=body_credentials, settings=configuration)
    )
    if problems:
        # Every problem at once. One per submission is a form nobody finishes.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"provider": provider_name, "problems": problems},
        )


@router.get("/integrations")
async def list_integrations(
    _principal: Annotated[Principal, Depends(require_role("admin"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> list[IntegrationOut]:
    """Admin-only: an integration list is a list of which networks an advertiser
    works with, which is commercially sensitive even without the credentials."""
    rows = await conn.fetch(
        sql.select("provider_integrations", INTEGRATION_COLUMNS, suffix="ORDER BY created_at DESC")
    )
    return [
        IntegrationOut(
            **{k: (decode(v) if k == "configuration" else v) for k, v in dict(row).items()},
            credential_names=[],
        )
        for row in rows
    ]


@router.post("/integrations", status_code=status.HTTP_201_CREATED)
async def create_integration(
    body: IntegrationCreate,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    context: Annotated[AppContext, Depends(get_context)],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> IntegrationOut:
    _validate(body.provider, body.credentials, body.configuration)

    sealed = seal(
        msgspec.json.encode(body.credentials),
        provider=context.master_keys,
        aad=organization_aad(principal.org_id),
    )

    integration_id = uuid7()
    try:
        row = await conn.fetchrow(
            sql.with_returning(
                """INSERT INTO provider_integrations (
                       id, organization_id, provider, name, credentials_ciphertext,
                       credentials_nonce, wrapped_dek, key_version, configuration,
                       status, created_at, updated_at)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, 1, $8::jsonb, 'active',
                           now(), now())""",
                INTEGRATION_COLUMNS,
            ),
            integration_id,
            principal.org_id,
            body.provider,
            body.name,
            sealed.ciphertext,
            sealed.nonce,
            sealed.wrapped_dek,
            msgspec.json.encode(body.configuration).decode(),
        )
    except asyncpg.exceptions.UniqueViolationError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "an integration with that provider and name already exists",
        ) from exc

    await audit.record(
        conn,
        organization_id=principal.org_id,
        action="integration.created",
        resource_type="provider_integration",
        resource_id=str(integration_id),
        actor_user_id=principal.user_id,
        detail={
            "provider": body.provider,
            "name": body.name,
            "credential_names": sorted(body.credentials),
        },
    )
    log.info("integration_created", provider=body.provider, integration_id=str(integration_id))
    return IntegrationOut(
        **{k: (decode(v) if k == "configuration" else v) for k, v in dict(row).items()},
        credential_names=sorted(body.credentials),
    )


@router.patch("/integrations/{integration_id}")
async def update_integration(
    integration_id: uuid.UUID,
    body: IntegrationUpdate,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    context: Annotated[AppContext, Depends(get_context)],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> IntegrationOut:
    existing = await conn.fetchrow(
        "SELECT provider, configuration FROM provider_integrations WHERE id = $1",
        integration_id,
    )
    if existing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "integration not found")

    changes: dict[str, Any] = {}
    if body.name is not None:
        changes["name"] = body.name
    if body.status is not None:
        changes["status"] = body.status

    configuration = (
        body.configuration
        if body.configuration is not None
        else decode(existing["configuration"]) or {}
    )
    if body.configuration is not None:
        changes["configuration"] = msgspec.json.encode(configuration).decode()

    if body.credentials is not None:
        # Credentials are replaced wholesale, never merged. A partial update
        # would let a caller change one field of a credential set they cannot
        # read, which is a confusing way to end up with a half-rotated secret.
        sealed = seal(
            msgspec.json.encode(body.credentials),
            provider=context.master_keys,
            aad=organization_aad(principal.org_id),
        )
        changes["credentials_ciphertext"] = sealed.ciphertext
        changes["credentials_nonce"] = sealed.nonce
        changes["wrapped_dek"] = sealed.wrapped_dek

    if not changes:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "nothing to update")

    # Validate the integration as it will be, not as it was: a configuration
    # change that breaks it must fail here rather than at the next conversion.
    if body.credentials is not None or body.configuration is not None:
        _validate(existing["provider"], body.credentials or {}, configuration)

    row = await conn.fetchrow(
        sql.update(
            "provider_integrations", list(changes), where="id = $1", returning=INTEGRATION_COLUMNS
        ),
        integration_id,
        *changes.values(),
    )
    return IntegrationOut(
        **{k: (decode(v) if k == "configuration" else v) for k, v in dict(row).items()},
        credential_names=sorted(body.credentials or {}),
    )


@router.delete("/integrations/{integration_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_integration(
    integration_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> None:
    """Deleting sets any rule using it back to the plain template path.

    The foreign key is ON DELETE SET NULL rather than CASCADE, deliberately:
    removing an integration should not silently delete the postback rules an
    advertiser configured, and a rule with no provider still works.
    """
    result = await conn.execute("DELETE FROM provider_integrations WHERE id = $1", integration_id)
    if result == "DELETE 0":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "integration not found")

    await audit.record(
        conn,
        organization_id=principal.org_id,
        action="integration.deleted",
        resource_type="provider_integration",
        resource_id=str(integration_id),
        actor_user_id=principal.user_id,
    )


@router.get("/integrations/{integration_id}/capabilities")
async def integration_capabilities(
    integration_id: uuid.UUID,
    _principal: Annotated[Principal, Depends(require_role("viewer"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> dict[str, Any]:
    """What this integration will accept.

    Lets a dashboard refuse to attach a refund rule to a provider that does not
    take refunds, rather than letting someone configure a delivery the network
    will silently discard.
    """
    row = await conn.fetchrow(
        "SELECT provider FROM provider_integrations WHERE id = $1", integration_id
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "integration not found")

    registry.load_builtin_once()
    provider = registry.get(row["provider"])
    return {
        "provider": provider.name,
        "capabilities": sorted(str(c) for c in provider.capabilities),
        "accepts_events": sorted(provider.event_map) or "all",
        "requires_attribution": Capability.REQUIRES_ATTRIBUTION in provider.capabilities,
    }
