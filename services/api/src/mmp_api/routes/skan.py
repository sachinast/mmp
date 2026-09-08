"""SKAdNetwork: conversion value configuration and postback reporting.

Two halves of the same feature.

**Configuration.** A conversion value is six bits — 0 to 63 — that the app sets
before Apple's timer expires, and it is the *only* thing an advertiser learns
about what a user did after installing. There is no event stream, no revenue
figure, no session count: one small integer, once. So deciding what those 64
values mean is a real modelling decision, and it belongs to the advertiser
rather than to us. This API stores their decision; ``conversion-values`` maps an
event name to the value it should raise the counter to.

**Reporting.** What Apple actually sent back, with the caveats attached. SKAdNetwork
numbers do not reconcile with the deterministic attribution in the rest of this
platform and should not be expected to: they are delayed by up to several days,
they are subject to Apple's privacy thresholds — which silently null out the
campaign identifier and the conversion value when volume is low — and a
non-winning postback means another network was credited, not that nothing
happened. The endpoint reports them as their own series rather than blending
them into install counts, because blending is how a number nobody can explain
ends up in a board deck.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from mmp_core.ids import uuid7
from mmp_db.types import DbConn
from pydantic import BaseModel, Field

from mmp_api.deps import Principal, require_role, tenant_db
from mmp_db import audit

router = APIRouter(tags=["skadnetwork"])

MAX_RANGE = dt.timedelta(days=90)
COARSE_VALUES = ("low", "medium", "high")


class ConversionValueIn(BaseModel):
    app_id: str
    event_name: str = Field(min_length=1, max_length=120)
    # Six bits. The constraint is Apple's, not ours.
    conversion_value: int = Field(ge=0, le=63)
    # SKAdNetwork 4 only, and only what a low-volume campaign receives instead
    # of the fine value.
    coarse_value: str | None = None
    platform: str = "ios"


class ConversionValueOut(BaseModel):
    id: str
    app_id: str
    event_name: str
    conversion_value: int
    coarse_value: str | None
    platform: str


class PostbackOut(BaseModel):
    id: str
    received_at: dt.datetime
    version: str
    ad_network_id: str
    source_identifier: str | None
    did_win: bool
    redownload: bool
    conversion_value: int | None
    coarse_value: str | None
    postback_sequence_index: int | None


@router.get("/skan/conversion-values")
async def list_conversion_values(
    app_id: str,
    _principal: Annotated[Principal, Depends(require_role("member"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> list[ConversionValueOut]:
    rows = await conn.fetch(
        "SELECT id, app_id, event_name, conversion_value, coarse_value, platform "
        "FROM conversion_mappings WHERE app_id = $1 ORDER BY conversion_value",
        app_id,
    )
    return [
        ConversionValueOut(
            id=str(row["id"]),
            app_id=str(row["app_id"]),
            event_name=row["event_name"],
            conversion_value=row["conversion_value"],
            coarse_value=row["coarse_value"],
            platform=row["platform"],
        )
        for row in rows
    ]


@router.put("/skan/conversion-values", status_code=status.HTTP_200_OK)
async def set_conversion_value(
    body: ConversionValueIn,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> ConversionValueOut:
    """Upsert, not create.

    An advertiser tunes this mapping repeatedly while working out what their 64
    values should mean, and a create-only endpoint would make every adjustment a
    delete followed by a create — with a window in between where the event maps
    to nothing at all.
    """
    if body.coarse_value is not None and body.coarse_value not in COARSE_VALUES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"coarse_value must be one of {', '.join(COARSE_VALUES)}",
        )

    owned = await conn.fetchval("SELECT 1 FROM apps WHERE id = $1", body.app_id)
    if not owned:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "app not found")

    row = await conn.fetchrow(
        """INSERT INTO conversion_mappings (id, organization_id, app_id, platform,
                                            event_name, conversion_value, coarse_value,
                                            created_at, updated_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, now(), now())
           ON CONFLICT (app_id, platform, event_name)
           DO UPDATE SET conversion_value = EXCLUDED.conversion_value,
                         coarse_value = EXCLUDED.coarse_value,
                         updated_at = now()
           RETURNING id, app_id, event_name, conversion_value, coarse_value, platform""",
        uuid7(),
        principal.org_id,
        body.app_id,
        body.platform,
        body.event_name,
        body.conversion_value,
        body.coarse_value,
    )

    await audit.record(
        conn,
        organization_id=principal.org_id,
        action="conversion_value.set",
        resource_type="conversion_mapping",
        resource_id=str(row["id"]),
        actor_user_id=principal.user_id,
        detail={"event_name": body.event_name, "conversion_value": body.conversion_value},
    )
    return ConversionValueOut(
        id=str(row["id"]),
        app_id=str(row["app_id"]),
        event_name=row["event_name"],
        conversion_value=row["conversion_value"],
        coarse_value=row["coarse_value"],
        platform=row["platform"],
    )


@router.delete("/skan/conversion-values/{mapping_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversion_value(
    mapping_id: str,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> None:
    deleted = await conn.fetchval(
        "DELETE FROM conversion_mappings WHERE id = $1 RETURNING id", mapping_id
    )
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "mapping not found")
    await audit.record(
        conn,
        organization_id=principal.org_id,
        action="conversion_value.deleted",
        resource_type="conversion_mapping",
        resource_id=str(mapping_id),
        actor_user_id=principal.user_id,
    )


@router.get("/skan/postbacks")
async def list_postbacks(
    app_id: str,
    since: Annotated[dt.datetime, Query()],
    until: Annotated[dt.datetime, Query()],
    _principal: Annotated[Principal, Depends(require_role("member"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> list[PostbackOut]:
    if until <= since:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "until must be after since")
    if until - since > MAX_RANGE:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"range must not exceed {MAX_RANGE.days} days"
        )

    rows = await conn.fetch(
        """SELECT id, received_at, version, ad_network_id, source_identifier, did_win,
                  redownload, conversion_value, coarse_value, postback_sequence_index
           FROM skadnetwork_postbacks
           WHERE app_id = $1 AND received_at >= $2 AND received_at < $3
           ORDER BY received_at DESC
           LIMIT 1000""",
        app_id,
        since,
        until,
    )
    return [
        PostbackOut(
            id=str(row["id"]),
            received_at=row["received_at"],
            version=row["version"],
            ad_network_id=row["ad_network_id"],
            source_identifier=row["source_identifier"],
            did_win=row["did_win"],
            redownload=row["redownload"],
            conversion_value=row["conversion_value"],
            coarse_value=row["coarse_value"],
            postback_sequence_index=row["postback_sequence_index"],
        )
        for row in rows
    ]


class SkanSummaryRow(BaseModel):
    ad_network_id: str
    source_identifier: str | None
    winning_postbacks: int
    non_winning_postbacks: int
    redownloads: int
    # Null for every postback Apple withheld a value from — see `suppressed`.
    average_conversion_value: float | None
    suppressed: int


class SkanSummary(BaseModel):
    rows: list[SkanSummaryRow]
    # Stated in the payload rather than left to a dashboard to remember.
    caveat: str


SUPPRESSION_NOTE = (
    "SKAdNetwork counts are not comparable with deterministic install counts. "
    "Postbacks arrive days late, Apple nulls the campaign identifier and the "
    "conversion value below its privacy thresholds, and a non-winning postback "
    "means another network was credited rather than that nothing happened."
)


@router.get("/skan/summary")
async def summarise(
    app_id: str,
    since: Annotated[dt.datetime, Query()],
    until: Annotated[dt.datetime, Query()],
    _principal: Annotated[Principal, Depends(require_role("member"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> SkanSummary:
    """Grouped by network and campaign, with the withheld values counted.

    ``suppressed`` is reported rather than hidden: a campaign whose conversion
    values were mostly withheld has an average computed from the few that were
    not, and an average over an unstated fraction of the data is exactly the
    kind of number that gets quoted without its denominator.
    """
    if until <= since or until - since > MAX_RANGE:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid range")

    rows = await conn.fetch(
        """SELECT ad_network_id,
                  source_identifier,
                  count(*) FILTER (WHERE did_win) AS winning,
                  count(*) FILTER (WHERE NOT did_win) AS non_winning,
                  count(*) FILTER (WHERE redownload) AS redownloads,
                  avg(conversion_value) FILTER (WHERE did_win) AS avg_value,
                  count(*) FILTER (WHERE did_win AND conversion_value IS NULL) AS suppressed
           FROM skadnetwork_postbacks
           WHERE app_id = $1 AND received_at >= $2 AND received_at < $3
           GROUP BY ad_network_id, source_identifier
           ORDER BY winning DESC""",
        app_id,
        since,
        until,
    )
    return SkanSummary(
        rows=[
            SkanSummaryRow(
                ad_network_id=row["ad_network_id"],
                source_identifier=row["source_identifier"],
                winning_postbacks=row["winning"],
                non_winning_postbacks=row["non_winning"],
                redownloads=row["redownloads"],
                average_conversion_value=(
                    float(row["avg_value"]) if row["avg_value"] is not None else None
                ),
                suppressed=row["suppressed"],
            )
            for row in rows
        ],
        caveat=SUPPRESSION_NOTE,
    )
