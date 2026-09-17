"""Loading candidate clicks for one install.

The engine takes a list of candidates and does not fetch. That separation keeps
it pure and testable, but it puts a sharp obligation here: "every click for this
app inside the window" could be tens of millions of rows for a large advertiser,
and loading them to attribute one install would be a self-inflicted outage.

So candidates are fetched by the *specific* signal being tested, each query
hitting an index that exists for it:

* by click id      -> ``ix_clicks_<partition>_click_id``
* by device hash   -> ``ix_clicks_<partition>_device``

Both are bounded by the attribution window, which bounds the partitions scanned.
An install with no click id and no device hash issues no query at all: there is
nothing deterministic to match, so the answer is organic without asking.
"""

from __future__ import annotations

import datetime as dt
import uuid

from mmp_db.types import DbConn

from mmp_attrib.engine import Click

# One device can legitimately produce several clicks in a window — a user
# clicking the same ad twice, or several ads. A cap keeps a pathological case
# (or a click-spamming network) from turning one attribution into a large scan;
# last-click only ever needs the most recent few.
MAX_DEVICE_CANDIDATES = 50

BY_CLICK_ID_SQL = """
SELECT click_id, clicked_at, campaign_id, tracking_link_id, device_hash, is_bot,
       deep_link, sub1, sub2, sub3
FROM clicks
WHERE click_id = $1
  AND app_id = $2
  AND clicked_at >= $3
  AND clicked_at <= $4
"""

BY_DEVICE_SQL = """
SELECT click_id, clicked_at, campaign_id, tracking_link_id, device_hash, is_bot,
       deep_link, sub1, sub2, sub3
FROM clicks
WHERE app_id = $1
  AND device_hash = $2
  AND clicked_at >= $3
  AND clicked_at <= $4
ORDER BY clicked_at DESC
LIMIT $5
"""


def _to_click(row: object) -> Click:
    return Click(
        click_id=row["click_id"],  # type: ignore[index]
        clicked_at=row["clicked_at"],  # type: ignore[index]
        campaign_id=row["campaign_id"],  # type: ignore[index]
        tracking_link_id=row["tracking_link_id"],  # type: ignore[index]
        device_hash=bytes(row["device_hash"]) if row["device_hash"] else None,  # type: ignore[index]
        is_bot=row["is_bot"],  # type: ignore[index]
        deep_link=row["deep_link"],  # type: ignore[index]
        sub1=row["sub1"],  # type: ignore[index]
        sub2=row["sub2"],  # type: ignore[index]
        sub3=row["sub3"],  # type: ignore[index]
    )


async def load_candidates(
    conn: DbConn,
    *,
    app_id: uuid.UUID,
    installed_at: dt.datetime,
    window_days: int,
    click_ids: list[uuid.UUID],
    device_hash: bytes | None,
) -> list[Click]:
    """Fetch only the clicks that could possibly win.

    ``click_ids`` holds whatever ids we have (from the referrer, from the SDK);
    each is looked up directly. ``device_hash`` triggers the device query. If
    neither is present the install is organic and nothing is queried.
    """
    window_start = installed_at - dt.timedelta(days=window_days)
    found: dict[uuid.UUID, Click] = {}

    for click_id in click_ids:
        row = await conn.fetchrow(BY_CLICK_ID_SQL, click_id, app_id, window_start, installed_at)
        if row is not None:
            found[row["click_id"]] = _to_click(row)

    if device_hash is not None:
        rows = await conn.fetch(
            BY_DEVICE_SQL,
            app_id,
            device_hash,
            window_start,
            installed_at,
            MAX_DEVICE_CANDIDATES,
        )
        for row in rows:
            found.setdefault(row["click_id"], _to_click(row))

    return list(found.values())
