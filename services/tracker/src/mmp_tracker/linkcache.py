"""The tracking-link cache.

The redirect handler must not query Postgres. A tracking link is read on every
click and changes perhaps monthly, so a database round trip per redirect would
be the single largest item in the latency budget, and it would tie the
availability of every advertiser's campaign to the availability of our database.

So the whole active link set lives in a dict in each tracker process. It is
small — a few hundred bytes per link, so a hundred thousand links is tens of
megabytes — and it is kept fresh two ways:

* **``LISTEN``/``NOTIFY``.** The API calls ``pg_notify`` when a link, a deep
  link, or an app changes — an application call, not a database trigger; this schema has
  none. Propagation is typically milliseconds, which is what makes "I disabled
  that link" mean something.
* **A periodic full resync.** Notifications are fire-and-forget: a process that
  was disconnected when one was sent never learns about it. The resync bounds
  how long a missed notification can matter, and is the reason this design is
  safe rather than merely fast.

A miss falls through to one indexed lookup and populates the cache, so a link
created a second ago still works before the notification arrives.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import uuid
from dataclasses import dataclass

import asyncpg
from mmp_core.logging import get_logger
from mmp_db.notify import APPS_CHANNEL, DEEP_LINKS_CHANNEL, TRACKING_LINKS_CHANNEL
from mmp_db.pool import Database

log = get_logger(__name__)

CHANNEL = TRACKING_LINKS_CHANNEL
RESYNC_INTERVAL = dt.timedelta(minutes=5)
# A negative entry, so a flood of requests for a code that does not exist cannot
# be turned into a flood of database lookups. Short, because a link created a
# moment ago must start working quickly.
NEGATIVE_TTL = dt.timedelta(seconds=30)

# Written out in full rather than composed from a shared prefix. The repo bans
# f-string SQL outside mmp_db's reviewed builders, and appending a WHERE clause
# to a shared string is exactly the habit that ban exists to prevent — even
# here, where nothing interpolated comes from a request.
LOAD_ONE_SQL = """
SELECT l.id, l.tracking_code, l.organization_id, l.app_id, l.campaign_id,
       l.android_url, l.ios_url, l.fallback_url, l.deep_link_path, l.status,
       a.status AS app_status
FROM tracking_links l
JOIN apps a ON a.id = l.app_id
WHERE l.tracking_code = $1
"""

# Both statuses, not just the link's. A link on a disabled app is inactive:
# otherwise disabling an app would stop its ingestion while leaving its links
# quietly sending traffic to a store listing nobody is measuring.
LOAD_ACTIVE_SQL = """
SELECT l.id, l.tracking_code, l.organization_id, l.app_id, l.campaign_id,
       l.android_url, l.ios_url, l.fallback_url, l.deep_link_path, l.status,
       a.status AS app_status
FROM tracking_links l
JOIN apps a ON a.id = l.app_id
WHERE l.status = 'active' AND a.status = 'active'
"""


# The advertiser's registry of pre-approved destinations. Loaded whole, for the
# same reason the links are: the redirect must not query Postgres, and a code
# that is not in this dict is simply not honoured — there is no fallback lookup,
# so a flood of invented codes costs nothing.
LOAD_APP_LINKS_SQL = """
SELECT l.id, l.tracking_code, l.organization_id, l.app_id, l.campaign_id,
       l.android_url, l.ios_url, l.fallback_url, l.deep_link_path, l.status,
       a.status AS app_status
FROM tracking_links l
JOIN apps a ON a.id = l.app_id
WHERE l.app_id = $1
"""

LOAD_DEEP_LINKS_FOR_APP_SQL = """
SELECT d.app_id, d.code, d.destination, d.fallback_url
FROM deep_links d
WHERE d.app_id = $1
"""

LOAD_DEEP_LINKS_SQL = """
SELECT d.app_id, d.code, d.destination, d.fallback_url
FROM deep_links d
JOIN apps a ON a.id = d.app_id
WHERE a.status = 'active'
"""


@dataclass(frozen=True, slots=True)
class DeepLinkTarget:
    destination: str
    fallback_url: str


@dataclass(frozen=True, slots=True)
class CachedLink:
    """Everything the redirect needs, resolved. Frozen so a handler cannot
    accidentally mutate shared state, and slotted because there may be a lot
    of these."""

    id: str
    organization_id: str
    app_id: str
    campaign_id: str | None
    android_url: str | None
    ios_url: str | None
    fallback_url: str
    deep_link_path: str | None
    active: bool


def _to_link(row: asyncpg.Record) -> CachedLink:
    return CachedLink(
        id=str(row["id"]),
        organization_id=str(row["organization_id"]),
        app_id=str(row["app_id"]),
        campaign_id=str(row["campaign_id"]) if row["campaign_id"] else None,
        android_url=row["android_url"],
        ios_url=row["ios_url"],
        fallback_url=row["fallback_url"],
        deep_link_path=row["deep_link_path"],
        # A link on a disabled app is treated as disabled. Otherwise turning off
        # an app would stop its ingestion but leave its links quietly sending
        # traffic to a store listing nobody is measuring.
        active=row["status"] == "active" and row["app_status"] == "active",
    )


class LinkCache:
    def __init__(self, database: Database, *, resync: dt.timedelta = RESYNC_INTERVAL) -> None:
        self._database = database
        self._links: dict[str, CachedLink] = {}
        # Keyed by app, because two advertisers may both register "summer" and
        # one must never resolve to the other's destination.
        self._deep: dict[tuple[str, str], DeepLinkTarget] = {}
        self._missing: dict[str, dt.datetime] = {}
        self._resync = resync
        self._listener: asyncpg.Connection | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False
        self.loads = 0
        self.notifications = 0
        self.resyncs = 0

    # --- lifecycle ------------------------------------------------------
    async def start(self) -> None:
        await self.resync()
        with contextlib.suppress(Exception):
            await self._start_listening()
        self._tasks.append(asyncio.create_task(self._resync_loop(), name="linkcache-resync"))

    async def stop(self) -> None:
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        if self._listener is not None:
            with contextlib.suppress(Exception):
                await self._listener.close()
            self._listener = None

    async def _start_listening(self) -> None:
        """A dedicated connection, held for the process's lifetime.

        LISTEN is connection-scoped, so this cannot come from the shared pool —
        a pooled connection would be handed to another caller and the
        subscription lost with it.
        """
        self._listener = await asyncpg.connect(self._database.dsn, statement_cache_size=0)
        await self._listener.add_listener(CHANNEL, self._on_notify)
        await self._listener.add_listener(DEEP_LINKS_CHANNEL, self._on_deep_notify)
        await self._listener.add_listener(APPS_CHANNEL, self._on_app_notify)
        log.info(
            "linkcache_listening",
            channels=[CHANNEL, DEEP_LINKS_CHANNEL, APPS_CHANNEL],
        )

    def _on_notify(self, _conn: object, _pid: int, _channel: str, payload: str) -> None:
        """Callback from asyncpg's listener. Must not block.

        The payload is the tracking code that changed; reloading just that one
        keeps a busy dashboard from triggering a full resync per edit.
        """
        self.notifications += 1
        self._missing.pop(payload, None)
        task = asyncio.create_task(self._reload_one(payload), name="linkcache-reload")
        self._tasks.append(task)
        task.add_done_callback(lambda t: self._tasks.remove(t) if t in self._tasks else None)

    def _on_deep_notify(self, _conn: object, _pid: int, _channel: str, payload: str) -> None:
        """An app's deep links changed. The payload is the app id.

        Deep links had no notification at all before this, so a freshly
        registered code did nothing until the next full resync — up to five
        minutes of an advertiser testing their own link, getting no deep link,
        and no error to explain it. `deep_link()` deliberately does not fall
        through to the database on a miss, which is the right defence against
        someone probing codes, but it leaves this as the only way a new code
        arrives promptly.
        """
        self.notifications += 1
        task = asyncio.create_task(self._reload_deep_links(payload), name="linkcache-reload-deep")
        self._tasks.append(task)
        task.add_done_callback(lambda t: self._tasks.remove(t) if t in self._tasks else None)

    def _on_app_notify(self, _conn: object, _pid: int, _channel: str, payload: str) -> None:
        """An app changed. The payload is the app id.

        A link on a disabled app is inactive — the queries below have always
        joined on app status — but nothing announced an app changing, so
        disabling one left its links redirecting until the next full resync.
        Disabling a single link took effect in milliseconds, which made the
        inconsistency easy to miss: "I turned that off" was true in one case and
        not the other.
        """
        self.notifications += 1
        task = asyncio.create_task(self._reload_app_links(payload), name="linkcache-reload-app")
        self._tasks.append(task)
        task.add_done_callback(lambda t: self._tasks.remove(t) if t in self._tasks else None)

    async def _reload_app_links(self, app_id: str) -> None:
        """Replace every cached link belonging to one app.

        Dropped first, then reloaded from a query that already excludes links on
        a disabled app — so disabling the app removes them and re-enabling it
        brings them back, without either case needing its own branch.
        """
        async with self._database.acquire_raw() as conn:
            rows = await conn.fetch(LOAD_APP_LINKS_SQL, uuid.UUID(app_id))
        remaining = {code: link for code, link in self._links.items() if str(link.app_id) != app_id}
        for row in rows:
            link = _to_link(row)
            if link.active:
                remaining[row["tracking_code"]] = link
        self._links = remaining

    async def _reload_deep_links(self, app_id: str) -> None:
        """Replace one app's deep links.

        Every code for the app is dropped first, so a delete takes effect: the
        notification carries the app, not the code that went away.
        """
        async with self._database.acquire_raw() as conn:
            rows = await conn.fetch(LOAD_DEEP_LINKS_FOR_APP_SQL, uuid.UUID(app_id))
        remaining = {key: value for key, value in self._deep.items() if key[0] != app_id}
        for row in rows:
            remaining[(str(row["app_id"]), row["code"])] = DeepLinkTarget(
                destination=row["destination"], fallback_url=row["fallback_url"]
            )
        # Swapped in one assignment, like resync(): a redirect must never see a
        # half-rebuilt map.
        self._deep = remaining

    async def _resync_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self._resync.total_seconds())
            try:
                await self.resync()
            except Exception:
                log.exception("linkcache_resync_failed")

    # --- loading --------------------------------------------------------
    async def resync(self) -> int:
        """Replace the cache wholesale.

        Built into a new dict and swapped in one assignment: mutating the live
        dict would let a redirect observe a half-loaded cache and 404 a link
        that exists.
        """
        async with self._database.acquire_raw() as conn:
            rows = await conn.fetch(LOAD_ACTIVE_SQL)
            deep_rows = await conn.fetch(LOAD_DEEP_LINKS_SQL)
        # Filtered again on `active`, deliberately: the query already excludes
        # disabled links and disabled apps, and this repeats the rule in Python.
        # The redundancy is cheap and it is the layer that caught the bug —
        # when the query filtered only on the link's own status, a disabled app
        # kept its links live, and this filter is what made the test pass while
        # the query was still wrong.
        loaded = {}
        for row in rows:
            link = _to_link(row)
            if link.active:
                loaded[row["tracking_code"]] = link
        self._links = loaded
        self._deep = {
            (str(row["app_id"]), row["code"]): DeepLinkTarget(
                destination=row["destination"], fallback_url=row["fallback_url"]
            )
            for row in deep_rows
        }
        self._missing.clear()
        self.resyncs += 1
        log.info("linkcache_resynced", links=len(self._links))
        return len(self._links)

    async def _reload_one(self, tracking_code: str) -> None:
        async with self._database.acquire_raw() as conn:
            row = await conn.fetchrow(LOAD_ONE_SQL, tracking_code)
        if row is None:
            self._links.pop(tracking_code, None)
            return
        link = _to_link(row)
        if link.active:
            self._links[tracking_code] = link
        else:
            # Disabled links are dropped rather than kept with a flag: the
            # redirect's fast path is a dict lookup, and every branch it does
            # not have to take is budget it does not spend.
            self._links.pop(tracking_code, None)

    # --- reads ----------------------------------------------------------
    def get(self, tracking_code: str) -> CachedLink | None:
        """The hot path. A dict lookup, nothing else."""
        return self._links.get(tracking_code)

    def deep_link(self, app_id: str, code: str) -> DeepLinkTarget | None:
        """Resolve a registered code, or nothing.

        No fall-through to the database on a miss, unlike ``get``. A tracking
        code that is missing is probably a link created seconds ago and worth
        one query; a deep link code that is missing is far more likely to be
        someone trying codes, and honouring it would put an attacker in charge
        of how often we query.
        """
        return self._deep.get((app_id, code))

    def is_known_missing(self, tracking_code: str, *, now: dt.datetime | None = None) -> bool:
        expiry = self._missing.get(tracking_code)
        if expiry is None:
            return False
        if (now or dt.datetime.now(dt.UTC)) > expiry:
            self._missing.pop(tracking_code, None)
            return False
        return True

    async def load_missing(self, tracking_code: str) -> CachedLink | None:
        """Fall through to the database for a code the cache does not hold.

        Reached when a link was created moments ago, or when this process
        started after it was created but before the first resync.
        """
        async with self._database.acquire_raw() as conn:
            row = await conn.fetchrow(LOAD_ONE_SQL, tracking_code)
        self.loads += 1
        if row is None:
            self._missing[tracking_code] = dt.datetime.now(dt.UTC) + NEGATIVE_TTL
            return None
        link = _to_link(row)
        if link.active:
            self._links[tracking_code] = link
            return link
        self._missing[tracking_code] = dt.datetime.now(dt.UTC) + NEGATIVE_TTL
        return None

    @property
    def size(self) -> int:
        return len(self._links)

    @property
    def deep_link_count(self) -> int:
        return len(self._deep)
