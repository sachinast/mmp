"""Raw data export.

An advertiser's own event, click and attribution rows, streamed as CSV, so they
can join our numbers against their own warehouse. It is also the answer to
"can we get our data out", which is a fair question to ask before trusting a
measurement vendor with it.

**Streamed, never assembled.** Rows go out through a server-side cursor as they
are read. Building the file in memory would mean a single export deciding how
much RAM the API needs, and the person asking for a month of events has no idea
they are the one who sets that number.

**Columns are an allowlist, not the table.** ``SELECT *`` would export whatever
a future migration adds, which is how internal identifiers escape. What is
deliberately excluded:

* ``device_hash`` and ``ip_hash`` — these are ours, not the advertiser's. They
  are derived from an advertising ID and an IP address under a system-wide
  pepper, which makes them stable pseudonyms for a person. Handing them out
  turns a hash we keep for attribution into a cross-dataset join key someone
  else can re-identify against. The advertiser's own ``anonymous_id`` and
  ``user_id`` are exported, because those are theirs.
* ``user_agent`` — a fingerprinting surface with no reporting use.
* ``properties`` — advertiser-supplied JSON that may contain anything at all,
  including data they never told us was personal. It is exported only from the
  events dataset, where it is the point, and never joined into another.

**An export leaves the erasure boundary.** Once a file is downloaded, a later
deletion request cannot reach it. That is inherent, not a defect to be fixed
here, and it is why every export is recorded in the audit log with who asked,
for what, and over which range. See docs/SECURITY.md.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from mmp_core.logging import get_logger

from mmp_api.context import AppContext
from mmp_api.deps import Principal, get_context, require_role
from mmp_db import audit, sql

router = APIRouter(tags=["exports"])
log = get_logger(__name__)

# A month. Long enough for the reporting periods people actually work in, short
# enough that one request cannot ask for the whole retention window.
MAX_RANGE = dt.timedelta(days=31)

# The cap exists so a single export cannot hold a connection open indefinitely.
# It is a refusal rather than a truncation: a silently short file is worse than
# no file, because the recipient has no way to tell.
MAX_ROWS = 1_000_000

# Rows fetched per round trip inside the cursor.
BATCH = 2_000


class Dataset:
    def __init__(self, table: str, time_column: str, columns: tuple[str, ...]) -> None:
        self.table = table
        self.time_column = time_column
        self.columns = columns


# Written out per dataset. The repo bans f-string SQL outside mmp_db's reviewed
# builders, and these strings are assembled from this dict alone — no request
# value reaches them; the dataset name is looked up, never interpolated.
DATASETS: dict[str, Dataset] = {
    "events": Dataset(
        "events",
        "received_at",
        (
            "event_id",
            "received_at",
            "occurred_at",
            "app_id",
            "event_name",
            "anonymous_id",
            "user_id",
            "session_id",
            "platform",
            "os_version",
            "app_version",
            "device_model",
            "country",
            "click_id",
            "revenue_minor",
            "currency",
            "properties",
        ),
    ),
    "clicks": Dataset(
        "clicks",
        "clicked_at",
        (
            "click_id",
            "clicked_at",
            "app_id",
            "campaign_id",
            "tracking_link_id",
            "country",
            "platform",
            "os_version",
            "device_model",
            "sub1",
            "sub2",
            "sub3",
            "is_bot",
            "deep_link",
        ),
    ),
    "attributions": Dataset(
        "attributions",
        "installed_at",
        (
            "id",
            "app_id",
            "anonymous_id",
            "user_id",
            "click_id",
            "campaign_id",
            "tracking_link_id",
            "source",
            "medium",
            "method",
            "installed_at",
            "attributed_at",
            "window_days",
            "fraud_score",
            "fraud_verdict",
            "deep_link",
            # A partner's own click id and publisher, as it put them on the link.
            # Exported because they are what a partner reconciles by — and what
            # an advertiser needs in front of them when a partner disputes a count.
            "sub1",
            "sub2",
            "sub3",
        ),
    ),
}


def _query(dataset: Dataset) -> str:
    """Built through ``mmp_db.sql``, which validates every identifier.

    Nothing here comes from the request: the dataset is looked up in ``DATASETS``
    by name and the name itself never reaches the statement. The table, the
    columns and the time column are all values this module wrote. Going through
    the builder anyway is the point — it is the one place identifiers become
    SQL, and an exception for "this one is obviously safe" is how the next one
    gets waved through too.
    """
    return sql.select(
        dataset.table,
        dataset.columns,
        where=(
            f"app_id = $1 AND {sql.identifier(dataset.time_column)} >= $2 "
            f"AND {sql.identifier(dataset.time_column)} < $3"
        ),
        suffix=f"ORDER BY {sql.identifier(dataset.time_column)} LIMIT {MAX_ROWS + 1}",
    )


async def _rows(
    context: AppContext,
    organization_id: uuid.UUID,
    dataset: Dataset,
    app_id: str,
    since: dt.datetime,
    until: dt.datetime,
) -> AsyncIterator[bytes]:
    """Stream the CSV.

    The connection is acquired here rather than through the ``tenant_db``
    dependency on purpose: a dependency that yields is closed before the
    response body finishes streaming, so the transaction — and with it the
    ``SET LOCAL`` that scopes RLS to this tenant — would be gone by the time the
    first row was read.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)

    def drain() -> bytes:
        chunk = buffer.getvalue().encode()
        buffer.seek(0)
        buffer.truncate(0)
        return chunk

    writer.writerow(dataset.columns)
    yield drain()

    sent = 0
    async with context.database.tenant_connection(organization_id) as conn:
        cursor = await conn.cursor(_query(dataset), app_id, since, until)
        while batch := await cursor.fetch(BATCH):
            for row in batch:
                sent += 1
                if sent > MAX_ROWS:
                    # Refuse loudly rather than truncate. The stream has already
                    # started so this cannot be a 4xx, and a CSV that simply
                    # stopped would be silently wrong in the recipient's
                    # warehouse — which is worse than a file that fails to parse.
                    yield b'"EXPORT ABORTED","row limit exceeded, narrow the range"\n'
                    log.warning("export_row_limit", app_id=app_id, limit=MAX_ROWS)
                    return
                writer.writerow(["" if value is None else value for value in row])
            yield drain()


@router.get("/exports/{dataset_name}")
async def export_dataset(
    dataset_name: str,
    app_id: str,
    since: Annotated[dt.datetime, Query()],
    until: Annotated[dt.datetime, Query()],
    principal: Annotated[Principal, Depends(require_role("admin"))],
    context: Annotated[AppContext, Depends(get_context)],
) -> StreamingResponse:
    """Admin-only. An export is the whole dataset leaving the system, which is a
    different act from reading a report, and should need a different level of
    authority."""
    dataset = DATASETS.get(dataset_name)
    if dataset is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"unknown dataset; available: {', '.join(sorted(DATASETS))}",
        )
    if until <= since:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "until must be after since")
    if until - since > MAX_RANGE:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"range must not exceed {MAX_RANGE.days} days"
        )

    async with context.database.tenant_connection(principal.org_id) as conn:
        owned = await conn.fetchval("SELECT 1 FROM apps WHERE id = $1", app_id)
        if not owned:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "app not found")
        # Recorded before a single row leaves, so a request that dies mid-stream
        # still leaves evidence that it was made.
        await audit.record(
            conn,
            organization_id=principal.org_id,
            action="export.requested",
            resource_type="export",
            resource_id=dataset_name,
            actor_user_id=principal.user_id,
            detail={
                "app_id": app_id,
                "since": since.isoformat(),
                "until": until.isoformat(),
            },
        )

    filename = f"{dataset_name}-{since.date()}-{until.date()}.csv"
    return StreamingResponse(
        _rows(context, principal.org_id, dataset, app_id, since, until),
        media_type="text/csv",
        headers={
            "content-disposition": f'attachment; filename="{filename}"',
            # Never cached: this is tenant data going through whatever sits in
            # front of the API.
            "cache-control": "no-store",
        },
    )
