"""Deep link registry.

The advertiser's list of pre-approved destinations. A link can then name a code
instead of carrying a path, which matters because a destination in a query
string is supplied by whoever wrote the link — see ``mmp_core.deeplinks`` for
what that means and why raw paths are validated so narrowly.

Registering a code buys two things a raw path cannot: a destination in any form
the app understands, including a custom scheme, because it came from the
advertiser rather than from the URL; and a web fallback for the people who tap
the link and do not install.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from mmp_core.deeplinks import MAX_DESTINATION, is_registered_code
from mmp_core.ids import uuid7
from mmp_db.notify import notify_deep_links_changed
from mmp_db.types import DbConn
from pydantic import BaseModel, Field

from mmp_api.deps import Principal, require_role, tenant_db
from mmp_db import audit

router = APIRouter(tags=["deeplinks"])


class DeepLinkIn(BaseModel):
    app_id: str
    code: str = Field(min_length=1, max_length=32)
    # Not validated as a relative path, deliberately: this came from the
    # authenticated advertiser, not from a URL, so a custom scheme is theirs to
    # choose. Length-bounded because it still ends up in a Play referrer.
    destination: str = Field(min_length=1, max_length=MAX_DESTINATION)
    fallback_url: str = Field(min_length=1, max_length=2048)


class DeepLinkOut(BaseModel):
    id: str
    app_id: str
    code: str
    destination: str
    fallback_url: str
    created_at: dt.datetime


@router.get("/deep-links")
async def list_deep_links(
    app_id: str,
    _principal: Annotated[Principal, Depends(require_role("member"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> list[DeepLinkOut]:
    rows = await conn.fetch(
        "SELECT id, app_id, code, destination, fallback_url, created_at "
        "FROM deep_links WHERE app_id = $1 ORDER BY code",
        app_id,
    )
    return [
        DeepLinkOut(
            id=str(row["id"]),
            app_id=str(row["app_id"]),
            code=row["code"],
            destination=row["destination"],
            fallback_url=row["fallback_url"],
            created_at=row["created_at"],
        )
        for row in rows
    ]


@router.post("/deep-links", status_code=status.HTTP_201_CREATED)
async def create_deep_link(
    body: DeepLinkIn,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> DeepLinkOut:
    if not is_registered_code(body.code):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "code may contain only letters, digits, hyphen and underscore",
        )
    if "\n" in body.destination or "\r" in body.destination:
        # It is echoed into a redirect's Location further down the path.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "destination must be a single line"
        )

    owned = await conn.fetchval("SELECT 1 FROM apps WHERE id = $1", body.app_id)
    if not owned:
        # RLS already scopes this to the tenant, so a missing row means either
        # no such app or another tenant's — and those must be indistinguishable.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "app not found")

    new_id = uuid7()
    try:
        row = await conn.fetchrow(
            """INSERT INTO deep_links (id, organization_id, app_id, code, destination,
                                       fallback_url, created_at, updated_at)
               VALUES ($1, $2, $3, $4, $5, $6, now(), now())
               RETURNING id, app_id, code, destination, fallback_url, created_at""",
            new_id,
            principal.organization_id,
            body.app_id,
            body.code,
            body.destination,
            body.fallback_url,
        )
    except Exception as exc:  # unique (app_id, code)
        if "ix_deep_links_app_code" not in str(exc) and "uq_deep_links_code" not in str(exc):
            raise
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"code {body.code!r} already exists for this app"
        ) from exc

    await audit.record(
        conn,
        organization_id=principal.org_id,
        action="deep_link.created",
        resource_type="deep_link",
        resource_id=str(new_id),
        actor_user_id=principal.user_id,
        detail={"code": body.code, "app_id": body.app_id},
    )
    # Wake the trackers. Without this the code exists in the database and is
    # invisible to every redirect until the next full resync — up to five
    # minutes of an advertiser testing their own link and getting nothing.
    await notify_deep_links_changed(conn, body.app_id)
    return DeepLinkOut(
        id=str(row["id"]),
        app_id=str(row["app_id"]),
        code=row["code"],
        destination=row["destination"],
        fallback_url=row["fallback_url"],
        created_at=row["created_at"],
    )


@router.delete("/deep-links/{deep_link_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_deep_link(
    deep_link_id: str,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    conn: Annotated[DbConn, Depends(tenant_db)],
) -> None:
    # app_id as well as id: the tracker is told which app's codes to reload, and
    # a delete has to invalidate an entry whose code it is no longer being sent.
    removed = await conn.fetchrow(
        "DELETE FROM deep_links WHERE id = $1 RETURNING id, app_id", deep_link_id
    )
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "deep link not found")
    await audit.record(
        conn,
        organization_id=principal.org_id,
        action="deep_link.deleted",
        resource_type="deep_link",
        resource_id=str(deep_link_id),
        actor_user_id=principal.user_id,
    )
    await notify_deep_links_changed(conn, str(removed["app_id"]))
