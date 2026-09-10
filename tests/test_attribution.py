"""Attribution end to end: click, install, one row, correct campaign."""

from __future__ import annotations

import asyncio
import datetime as dt
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from mmp_db.jsonfields import decode_list

from tests.conftest_ingest import ANDROID_UA, flush_tracker, sample_event


async def _drain(*consumers, rounds: int = 5) -> None:
    for _ in range(rounds):
        moved = 0
        for consumer in consumers:
            moved += await consumer.run_once()
        if moved == 0:
            break


async def _click_then_install(
    tracker, click_consumer, attribution_consumer, seeded_app, *, referrer=True
):
    """Simulate the real sequence: a click, then an install carrying the
    Play referrer that click produced."""
    response = await tracker.get(
        f"/c/{seeded_app['tracking_code']}",
        headers={"user-agent": ANDROID_UA},
        follow_redirects=False,
    )
    play_referrer = unquote(parse_qs(urlparse(response.headers["location"]).query)["referrer"][0])
    click_id = parse_qs(play_referrer)["utm_content"][0]

    await flush_tracker(tracker)
    await _drain(click_consumer)

    properties = {"install_referrer": play_referrer} if referrer else {}
    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="install", anonymous_id="device-attr", properties=properties
                )
            ]
        },
    )
    await flush_tracker(tracker)
    await _drain(attribution_consumer)
    return click_id


async def test_referrer_install_is_attributed(
    tracker, click_consumer, attribution_consumer, owner_conn, seeded_app
):
    click_id = await _click_then_install(tracker, click_consumer, attribution_consumer, seeded_app)

    row = await owner_conn.fetchrow(
        "SELECT * FROM attributions WHERE app_id = $1 AND superseded_by IS NULL",
        seeded_app["app_id"],
    )
    assert row is not None, "the install should have been attributed"
    assert row["method"] == "referrer"
    assert str(row["click_id"]) == click_id
    assert row["campaign_id"] == seeded_app["campaign_id"]
    assert row["tracking_link_id"] == seeded_app["tracking_link_id"]
    assert row["window_days"] == 7


async def test_install_without_a_referrer_is_organic(
    tracker, click_consumer, attribution_consumer, owner_conn, seeded_app
):
    """The honest answer when nothing deterministic matched."""
    await _click_then_install(
        tracker, click_consumer, attribution_consumer, seeded_app, referrer=False
    )
    row = await owner_conn.fetchrow(
        "SELECT method, click_id FROM attributions WHERE app_id = $1", seeded_app["app_id"]
    )
    assert row is not None
    assert row["method"] == "organic"
    assert row["click_id"] is None


async def test_one_install_yields_one_attribution_under_redelivery(
    tracker, click_consumer, attribution_consumer, ingest_redis, owner_conn, seeded_app
):
    """The invariant the product's numbers rest on, under the failure that
    actually produces duplicates: a redelivered install event."""
    from mmp_ingest.stream import EVENTS_STREAM
    from mmp_worker.attribution import ATTRIBUTION_GROUP

    await _click_then_install(tracker, click_consumer, attribution_consumer, seeded_app)

    for _ in range(3):
        await ingest_redis.xgroup_setid(EVENTS_STREAM, ATTRIBUTION_GROUP, id="0")
        await attribution_consumer.run_once()

    count = await owner_conn.fetchval(
        "SELECT count(*) FROM attributions WHERE app_id = $1 AND superseded_by IS NULL",
        seeded_app["app_id"],
    )
    assert count == 1


async def test_concurrent_workers_cannot_double_attribute(owner_conn, seeded_app, ingest_redis):
    """Two workers, same install, at the same time.

    This is what the partial unique index is for. Enforcing it in application
    code would mean enforcing it across processes consuming a queue that
    redelivers on timeout — the database is the only place the rule can actually
    hold.
    """
    from mmp_attrib.store import record
    from mmp_core.ids import uuid7
    from mmp_db.pool import Database

    from mmp_attrib import Click, Install, Method, attribute

    installed_at = dt.datetime.now(dt.UTC)
    candidate = Click(
        click_id=uuid7(),
        clicked_at=installed_at - dt.timedelta(minutes=10),
        campaign_id=seeded_app["campaign_id"],
        tracking_link_id=seeded_app["tracking_link_id"],
    )
    decision = attribute(
        Install(
            app_id=seeded_app["app_id"],
            anonymous_id="race-device",
            installed_at=installed_at,
            click_id=candidate.click_id,
        ),
        [candidate],
        window_days=7,
    )
    assert decision.method is Method.CLICK_ID

    async def worker() -> None:
        database = await Database.connect(seeded_app["worker_settings"], role="mmp_worker")
        try:
            async with database.acquire_raw() as conn:
                await record(
                    conn,
                    ingest_redis,
                    organization_id=seeded_app["organization_id"],
                    app_id=seeded_app["app_id"],
                    anonymous_id="race-device",
                    user_id=None,
                    installed_at=installed_at,
                    decision=decision,
                    event_window_days=30,
                )
        finally:
            await database.close()

    await asyncio.gather(*(worker() for _ in range(6)))

    count = await owner_conn.fetchval(
        "SELECT count(*) FROM attributions "
        "WHERE app_id = $1 AND install_key LIKE '%race-device' AND superseded_by IS NULL",
        seeded_app["app_id"],
    )
    assert count == 1, "six concurrent workers must produce exactly one attribution"


async def test_better_evidence_supersedes_without_erasing_history(
    owner_conn, seeded_app, ingest_redis
):
    """Play's referrer can arrive after an install was already attributed by
    device match — its API is queried on first launch and may need a retry.

    The correction must append, not overwrite: a number already reported to an
    ad network has to stay reconstructable.
    """
    from mmp_attrib.store import record
    from mmp_core.ids import uuid7
    from mmp_db.pool import Database

    from mmp_attrib import Click, Decision, Method

    installed_at = dt.datetime.now(dt.UTC)
    device_click = Click(
        click_id=uuid7(),
        clicked_at=installed_at - dt.timedelta(minutes=30),
        campaign_id=seeded_app["campaign_id"],
        tracking_link_id=seeded_app["tracking_link_id"],
    )
    referrer_click = Click(
        click_id=uuid7(),
        clicked_at=installed_at - dt.timedelta(minutes=90),
        campaign_id=seeded_app["campaign_id"],
        tracking_link_id=seeded_app["tracking_link_id"],
    )

    database = await Database.connect(seeded_app["worker_settings"], role="mmp_worker")
    try:
        async with database.acquire_raw() as conn:
            for method, click in (
                (Method.DEVICE_MATCH, device_click),
                (Method.REFERRER, referrer_click),
            ):
                await record(
                    conn,
                    ingest_redis,
                    organization_id=seeded_app["organization_id"],
                    app_id=seeded_app["app_id"],
                    anonymous_id="upgrade-device",
                    user_id=None,
                    installed_at=installed_at,
                    decision=Decision(method=method, click=click, window_days=7, reason="test"),
                    event_window_days=30,
                )
    finally:
        await database.close()

    rows = await owner_conn.fetch(
        "SELECT method, click_id, superseded_by FROM attributions "
        "WHERE app_id = $1 AND install_key LIKE '%upgrade-device' ORDER BY created_at",
        seeded_app["app_id"],
    )
    assert len(rows) == 2, "the original attribution must still exist"
    current = [r for r in rows if r["superseded_by"] is None]
    assert len(current) == 1
    assert current[0]["method"] == "referrer"
    assert current[0]["click_id"] == referrer_click.click_id


async def test_weaker_evidence_does_not_overwrite(owner_conn, seeded_app, ingest_redis):
    """Otherwise the answer would depend on which message arrived first."""
    from mmp_attrib.store import record
    from mmp_core.ids import uuid7
    from mmp_db.pool import Database

    from mmp_attrib import Click, Decision, Method

    installed_at = dt.datetime.now(dt.UTC)

    def make(method):
        return Decision(
            method=method,
            click=Click(
                click_id=uuid7(),
                clicked_at=installed_at - dt.timedelta(minutes=10),
                campaign_id=seeded_app["campaign_id"],
                tracking_link_id=seeded_app["tracking_link_id"],
            ),
            window_days=7,
            reason="test",
        )

    database = await Database.connect(seeded_app["worker_settings"], role="mmp_worker")
    try:
        async with database.acquire_raw() as conn:
            first = await record(
                conn,
                ingest_redis,
                organization_id=seeded_app["organization_id"],
                app_id=seeded_app["app_id"],
                anonymous_id="downgrade-device",
                user_id=None,
                installed_at=installed_at,
                decision=make(Method.REFERRER),
                event_window_days=30,
            )
            second = await record(
                conn,
                ingest_redis,
                organization_id=seeded_app["organization_id"],
                app_id=seeded_app["app_id"],
                anonymous_id="downgrade-device",
                user_id=None,
                installed_at=installed_at,
                decision=make(Method.DEVICE_MATCH),
                event_window_days=30,
            )
    finally:
        await database.close()

    assert first.created
    assert not second.created, "a weaker signal must not replace a stronger one"
    assert second.attribution_id == first.attribution_id


async def test_identity_cache_resolves_a_later_conversion(
    tracker, click_consumer, attribution_consumer, seeded_app, ingest_redis, owner_conn
):
    """A purchase three weeks later must resolve to its campaign in
    milliseconds, without a scan of the attributions table."""
    from mmp_attrib.store import lookup
    from mmp_db.pool import Database

    await _click_then_install(tracker, click_consumer, attribution_consumer, seeded_app)

    database = await Database.connect(seeded_app["worker_settings"], role="mmp_worker")
    try:
        async with database.acquire_raw() as conn:
            cached = await lookup(
                ingest_redis,
                conn,
                app_id=seeded_app["app_id"],
                anonymous_id="device-attr",
            )
    finally:
        await database.close()

    assert cached is not None
    assert cached.method == "referrer"
    assert cached.campaign_id == str(seeded_app["campaign_id"])


async def test_lookup_falls_back_to_postgres_when_redis_loses_the_key(
    tracker, click_consumer, attribution_consumer, seeded_app, ingest_redis
):
    """Redis can lose keys to a failover or an eviction. An attribution that
    silently disappeared would send a purchase to the wrong campaign, or none."""
    from mmp_attrib.store import lookup
    from mmp_db.pool import Database

    await _click_then_install(tracker, click_consumer, attribution_consumer, seeded_app)

    await ingest_redis.flushdb()  # simulate the cache going away entirely

    database = await Database.connect(seeded_app["worker_settings"], role="mmp_worker")
    try:
        async with database.acquire_raw() as conn:
            recovered = await lookup(
                ingest_redis,
                conn,
                app_id=seeded_app["app_id"],
                anonymous_id="device-attr",
            )
    finally:
        await database.close()

    assert recovered is not None, "Postgres is the source of truth, not the cache"
    assert recovered.method == "referrer"


async def test_login_aliases_the_attribution_to_the_user_id(
    tracker, click_consumer, attribution_consumer, seeded_app, ingest_redis
):
    """A server-to-server purchase carries a user id and never sees the device.
    Without the alias it would resolve to nothing and be reported organic."""
    import msgspec
    from mmp_attrib.store import CachedAttribution

    await _click_then_install(tracker, click_consumer, attribution_consumer, seeded_app)

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(event_name="login", anonymous_id="device-attr", user_id="user-99")
            ]
        },
    )
    await flush_tracker(tracker)
    await _drain(attribution_consumer)

    aliased = await ingest_redis.get(f"attr:{seeded_app['app_id']}:user:user-99")
    assert aliased is not None, "a signed-in user must resolve to their install"
    decoded = msgspec.msgpack.Decoder(CachedAttribution).decode(aliased)
    assert decoded.method == "referrer"


async def test_attribution_is_scoped_to_its_organization(
    tracker, click_consumer, attribution_consumer, owner_conn, seeded_app
):
    await _click_then_install(tracker, click_consumer, attribution_consumer, seeded_app)
    org = await owner_conn.fetchval(
        "SELECT organization_id FROM attributions WHERE app_id = $1", seeded_app["app_id"]
    )
    assert org == seeded_app["organization_id"]


@pytest.mark.parametrize("event_name", ["purchase", "app_open", "session_start"])
async def test_non_install_events_do_not_create_attributions(
    tracker, attribution_consumer, owner_conn, seeded_app, event_name
):
    """A conversion reads an attribution; it must never manufacture one."""
    await tracker.post(
        "/v1/events",
        json={"events": [sample_event(event_name=event_name, anonymous_id="device-x")]},
    )
    await flush_tracker(tracker)
    await _drain(attribution_consumer)

    count = await owner_conn.fetchval(
        "SELECT count(*) FROM attributions WHERE app_id = $1", seeded_app["app_id"]
    )
    assert count == 0


async def test_device_match_attribution_end_to_end(
    tracker, click_consumer, attribution_consumer, owner_conn, seeded_app
):
    """The Android path when the network passes the advertising ID on the click
    but the referrer never arrives — a device that was offline at first launch,
    or a Play API call that failed.
    """
    gaid = "38400000-8cf0-11bd-b23e-10b96e40000d"

    await tracker.get(
        f"/c/{seeded_app['tracking_code']}?gaid={gaid}",
        headers={"user-agent": ANDROID_UA},
        follow_redirects=False,
    )
    await flush_tracker(tracker)
    await _drain(click_consumer)

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="install",
                    anonymous_id="device-match-1",
                    properties={"gaid": gaid},
                )
            ]
        },
    )
    await flush_tracker(tracker)
    await _drain(attribution_consumer)

    row = await owner_conn.fetchrow(
        "SELECT method, campaign_id FROM attributions "
        "WHERE app_id = $1 AND install_key LIKE '%device-match-1'",
        seeded_app["app_id"],
    )
    assert row is not None, "a matching advertising ID should attribute the install"
    assert row["method"] == "device_match"
    assert row["campaign_id"] == seeded_app["campaign_id"]


async def test_raw_advertising_id_never_reaches_storage(
    tracker, click_consumer, attribution_consumer, owner_conn, seeded_app
):
    """It is hashed at the edge and the raw value dropped from the payload, so
    it exists only in the tracker process for the length of one request."""
    gaid = "38400000-8cf0-11bd-b23e-10b96e40000d"

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="install",
                    anonymous_id="privacy-1",
                    properties={"gaid": gaid, "keep_me": "yes"},
                )
            ]
        },
    )
    await flush_tracker(tracker)

    from mmp_db.pool import Database
    from mmp_worker.consumers import EventConsumer

    database = await Database.connect(seeded_app["worker_settings"], role="mmp_worker")
    try:
        writer = EventConsumer(
            redis=attribution_consumer._redis,
            database=database,
            consumer_name="privacy-writer",
        )
        await writer.start()
        for _ in range(3):
            if await writer.run_once() == 0:
                break
    finally:
        await database.close()

    row = await owner_conn.fetchrow(
        "SELECT properties FROM events WHERE app_id = $1 AND anonymous_id = 'privacy-1'",
        seeded_app["app_id"],
    )
    assert row is not None
    import json

    properties = json.loads(row["properties"])
    assert "gaid" not in properties, "the raw advertising ID must not be stored"
    assert gaid not in json.dumps(properties)
    assert "device_hash" in properties, "the digest is what attribution needs"
    assert properties["keep_me"] == "yes", "other properties must survive"


async def test_opted_out_advertising_id_does_not_create_a_match_bucket(
    tracker, click_consumer, attribution_consumer, owner_conn, seeded_app
):
    """Android returns all-zeros for a user who opted out.

    If that hashed to a value, every opted-out install would match every
    opted-out click — the single worst failure this engine could have.
    """
    opted_out = "00000000-0000-0000-0000-000000000000"

    await tracker.get(
        f"/c/{seeded_app['tracking_code']}?gaid={opted_out}",
        headers={"user-agent": ANDROID_UA},
        follow_redirects=False,
    )
    await flush_tracker(tracker)
    await _drain(click_consumer)

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="install",
                    anonymous_id="opted-out-1",
                    properties={"gaid": opted_out},
                )
            ]
        },
    )
    await flush_tracker(tracker)
    await _drain(attribution_consumer)

    row = await owner_conn.fetchrow(
        "SELECT method FROM attributions WHERE app_id = $1 AND install_key LIKE '%opted-out-1'",
        seeded_app["app_id"],
    )
    assert row is not None
    assert row["method"] == "organic", "opted-out devices must not match each other"


# --- the read API -------------------------------------------------------
async def test_summary_requires_a_bounded_range(account):
    """An unbounded range is an unbounded scan; a default range would be a
    default scan, discovered by whoever opens the page on the largest account."""
    app = (
        await account.post(
            "/v1/apps",
            json={
                "name": "Summary App",
                "platform": "android",
                "android_package_name": "com.example.summary",
            },
        )
    ).json()

    missing = await account.client.get(f"/v1/attributions/summary?app_id={app['id']}")
    assert missing.status_code == 422

    too_wide = await account.client.get(
        f"/v1/attributions/summary?app_id={app['id']}&from=2020-01-01&to=2026-01-01"
    )
    assert too_wide.status_code == 422

    backwards = await account.client.get(
        f"/v1/attributions/summary?app_id={app['id']}&from=2026-09-08&to=2026-09-01"
    )
    assert backwards.status_code == 422


async def test_summary_reports_match_rate(account):
    app = (
        await account.post(
            "/v1/apps",
            json={
                "name": "Rate App",
                "platform": "android",
                "android_package_name": "com.example.rate",
            },
        )
    ).json()
    response = await account.client.get(
        f"/v1/attributions/summary?app_id={app['id']}&from=2026-09-01&to=2026-09-08"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 0
    assert body["match_rate"] is None, "no installs means no rate, not a zero"


async def test_summary_is_tenant_scoped(api_client, account, seeded_app):
    """Another tenant's app must be invisible, not merely empty."""
    response = await account.client.get(
        f"/v1/attributions/summary?app_id={seeded_app['app_id']}&from=2026-09-01&to=2026-09-08"
    )
    assert response.status_code == 404


# ------------------------------------------------------------------- fraud
async def test_an_install_seconds_after_its_click_is_flagged_on_the_row(
    tracker, click_consumer, attribution_consumer, owner_conn, seeded_app
):
    """The verdict has to reach the database, not just the log.

    This harness clicks and installs milliseconds apart, which is exactly the
    shape of click injection — a real click-to-install contains a store page
    load and a download. So the end-to-end path here doubles as the injection
    case, and asserts the part most likely to be quietly wrong: that the
    assessment is written to the attribution rather than computed and dropped.
    """
    await _click_then_install(tracker, click_consumer, attribution_consumer, seeded_app)

    row = await owner_conn.fetchrow(
        "SELECT method, fraud_score, fraud_verdict, fraud_rules FROM attributions "
        "WHERE app_id = $1 AND superseded_by IS NULL",
        seeded_app["app_id"],
    )
    assert row is not None
    assert row["method"] == "referrer", "the install is still attributed — flag, do not discard"
    assert row["fraud_verdict"] == "fraudulent"
    assert row["fraud_score"] >= 100
    assert "click_injection" in decode_list(row["fraud_rules"])


async def test_an_organic_install_is_recorded_clean_not_unknown(
    tracker, click_consumer, attribution_consumer, owner_conn, seeded_app
):
    """There is no third state. An install nothing fired on is clean, and the
    column is NOT NULL so no reader has to invent a meaning for missing."""
    await _click_then_install(
        tracker, click_consumer, attribution_consumer, seeded_app, referrer=False
    )
    row = await owner_conn.fetchrow(
        "SELECT fraud_verdict, fraud_score, fraud_rules FROM attributions WHERE app_id = $1",
        seeded_app["app_id"],
    )
    assert row["fraud_verdict"] == "clean"
    assert row["fraud_score"] == 0
    assert row["fraud_rules"] is None, "a clean row stores no rule list to read back"


# ------------------------------------------------- reserved name matching
async def test_a_capitalised_install_still_attributes(
    tracker, click_consumer, attribution_consumer, owner_conn, seeded_app
):
    """The silent failure this guards against.

    Every special-name check used to be an exact match while validation only
    checked length, so an app sending "Install" was accepted, stored, and never
    attributed — no error anywhere, events arriving normally, and an install
    count of zero. Casing is not something a customer should lose a fortnight to.
    """
    response = await tracker.get(
        f"/c/{seeded_app['tracking_code']}",
        headers={"user-agent": ANDROID_UA},
        follow_redirects=False,
    )
    play_referrer = unquote(parse_qs(urlparse(response.headers["location"]).query)["referrer"][0])
    await flush_tracker(tracker)
    await _drain(click_consumer)

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="Install",
                    anonymous_id="device-capitalised",
                    properties={"install_referrer": play_referrer},
                )
            ]
        },
    )
    await flush_tracker(tracker)
    await _drain(attribution_consumer)

    row = await owner_conn.fetchrow(
        "SELECT method FROM attributions WHERE app_id = $1 AND anonymous_id = 'device-capitalised'",
        seeded_app["app_id"],
    )
    assert row is not None, "a capitalised install must still be attributed"
    assert row["method"] == "referrer"


async def test_the_event_name_is_stored_exactly_as_sent(
    tracker, worker_consumer, owner_conn, seeded_app
):
    """Matching is folded; reporting is not. A customer who calls their event
    "Purchase" sees "Purchase" in their reports and their exports."""
    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(event_name="Install", anonymous_id="device-verbatim"),
                sample_event(event_name="Checkout Started", anonymous_id="device-verbatim"),
            ]
        },
    )
    await flush_tracker(tracker)
    await _drain(worker_consumer)

    names = [
        row["event_name"]
        for row in await owner_conn.fetch(
            "SELECT event_name FROM events WHERE app_id = $1 AND anonymous_id = 'device-verbatim'"
            " ORDER BY event_name",
            seeded_app["app_id"],
        )
    ]
    assert names == ["Checkout Started", "Install"]


async def test_a_hyphenated_signup_still_links_the_user(
    tracker, click_consumer, attribution_consumer, seeded_app, ingest_redis
):
    """A hyphenated sign-up folds to "signup", so it resolves identity like the
    canonical name rather than being stored as an ordinary event that links
    nothing.

    Asserted through the identity cache rather than by calling the folding
    function: what matters is that a later server-to-server purchase carrying
    only this user id can find the install, and only the real path proves that.
    """
    import msgspec
    from mmp_attrib.store import CachedAttribution

    await _click_then_install(tracker, click_consumer, attribution_consumer, seeded_app)

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(event_name="Sign-Up", anonymous_id="device-attr", user_id="user-77")
            ]
        },
    )
    await flush_tracker(tracker)
    await _drain(attribution_consumer)

    aliased = await ingest_redis.get(f"attr:{seeded_app['app_id']}:user:user-77")
    assert aliased is not None, "a hyphenated sign-up must link the user like 'signup' does"
    assert msgspec.msgpack.Decoder(CachedAttribution).decode(aliased).method == "referrer"


def test_folding_matches_only_whole_names() -> None:
    from mmp_ingest.schema import canonical_event_name

    assert canonical_event_name("Sign-Up") == "signup"
    assert canonical_event_name("first_open") == "firstopen"
    assert canonical_event_name("Checkout Started") == "checkoutstarted"
    # A name that merely contains a reserved word is its own event.
    assert canonical_event_name("signup_abandoned") != "signup"


def _record_logs(monkeypatch):
    """Substitute the store's logger and record (event, reason) pairs.

    Asserting on the logger directly rather than through structlog's capture:
    the module binds its logger at import, so reconfiguring structlog afterwards
    does not reach it — and what these tests care about is that the branch
    reports a reason at all, not how it is rendered.
    """
    import mmp_attrib.store as store

    recorded: list[tuple[str, str]] = []

    class _Recorder:
        def info(self, event: str, **fields: object) -> None:
            recorded.append((event, str(fields.get("reason", ""))))

    monkeypatch.setattr(store, "log", _Recorder())
    return recorded


# --- the identity alias, and its silent branches -------------------------
async def test_link_user_mirrors_the_entry_with_the_same_expiry(ingest_redis):
    """The alias must not outlive the attribution it mirrors, or a conversion
    arriving after the window closed would still credit a campaign."""
    import uuid as _uuid

    from mmp_attrib.store import _cache_key, link_user

    app_id = _uuid.uuid4()
    await ingest_redis.set(_cache_key(app_id, "dev-1"), b"payload", ex=3600)

    await link_user(ingest_redis, app_id=app_id, anonymous_id="dev-1", user_id="u-1")

    alias = _cache_key(app_id, "user:u-1")
    assert await ingest_redis.get(alias) == b"payload"
    ttl = await ingest_redis.ttl(alias)
    assert 0 < ttl <= 3600


async def test_link_user_says_so_when_there_is_nothing_to_mirror(ingest_redis, monkeypatch):
    """A device with no cached attribution links nothing — ordinary, and it used
    to happen in complete silence.

    That silence cost a day of debugging: the alias was being created correctly
    the whole time and I was reading the wrong key, with no log on either side
    to contradict me. A branch that decides a conversion will go unattributed
    should say so.
    """
    import uuid as _uuid

    from mmp_attrib.store import _cache_key, link_user

    app_id = _uuid.uuid4()
    entries = _record_logs(monkeypatch)
    await link_user(ingest_redis, app_id=app_id, anonymous_id="absent", user_id="u-2")

    assert not await ingest_redis.exists(_cache_key(app_id, "user:u-2"))
    assert entries == [("identity_link_skipped", "no_cached_attribution")], entries


async def test_link_user_refuses_to_mirror_an_entry_with_no_expiry(ingest_redis, monkeypatch):
    """`ttl()` answers -1 for a key with no expiry.

    `cache()` always writes one, so this should be unreachable — but mirroring
    such an entry would create an alias that never expires, and an immortal
    attribution is worse than a missing one. It skips, and now says why.
    """
    import uuid as _uuid

    from mmp_attrib.store import _cache_key, link_user

    app_id = _uuid.uuid4()
    await ingest_redis.set(_cache_key(app_id, "dev-3"), b"payload")  # deliberately no ex=

    entries = _record_logs(monkeypatch)
    await link_user(ingest_redis, app_id=app_id, anonymous_id="dev-3", user_id="u-3")

    assert not await ingest_redis.exists(_cache_key(app_id, "user:u-3"))
    assert entries == [("identity_link_skipped", "source_has_no_expiry")], entries
