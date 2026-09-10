"""Channels the tracker listens on, and the calls that wake it.

The tracker keeps the active link set in process memory and refreshes it two
ways: a notification when something changes, and a periodic full resync that
bounds how long a *missed* notification can matter. Both halves are needed —
notifications are fire-and-forget, so a process that was disconnected when one
was sent never learns about it.

These live in `mmp_db` because both the API (which changes links) and the
tracker (which caches them) need the same channel names, and services must not
import each other. Before this they did not share: the channel name was a
literal in the API and a constant in the tracker, and a helper in the tracker
that nothing called. Deep links had no notification at all, so a freshly
registered code was silently ignored until the next resync — up to five minutes
of an advertiser testing their own link and getting nothing, with no error to
explain it.
"""

from __future__ import annotations

from mmp_db.types import DbConn

TRACKING_LINKS_CHANNEL = "tracking_links_changed"
DEEP_LINKS_CHANNEL = "deep_links_changed"


async def notify_tracking_link_changed(conn: DbConn, tracking_code: str) -> None:
    """Tell the tracker one link changed, so it reloads that one rather than all."""
    await conn.execute("SELECT pg_notify($1, $2)", TRACKING_LINKS_CHANNEL, tracking_code)


async def notify_deep_links_changed(conn: DbConn, app_id: str) -> None:
    """Tell the tracker an app's deep links changed.

    The payload is an app id rather than a code because the tracker keys deep
    links by (app_id, code) and a delete has to invalidate an entry whose code
    it is no longer being told. Reloading one app's codes is cheap; there are
    few of them and they are small.
    """
    await conn.execute("SELECT pg_notify($1, $2)", DEEP_LINKS_CHANNEL, app_id)
