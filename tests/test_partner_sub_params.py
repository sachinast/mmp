"""A partner's sub parameters, from its tracking link to its postback.

A partner running a campaign appends its own click id to the link —
``/c/<code>?sub1=<their click id>`` — and matches conversions to its clicks by
reading that id back on the postback. These tests follow it the whole way:
click, install, purchase days later, postback.

They also cover the scoping that makes returning it safe. Rules used to fire for
every install of their app, so two partners each with a rule were told about each
other's conversions; with sub1 available, they would have been handed each
other's click ids too.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, unquote, urlparse

import msgspec
from mmp_core.ids import uuid7

from tests.conftest_ingest import ANDROID_UA, flush_tracker, sample_event
from tests.test_postback_delivery import _run

PARTNER_CLICK = "partnerA-click-7f3a9c"


async def _drain(*consumers, rounds: int = 5) -> None:
    for _ in range(rounds):
        moved = 0
        for consumer in consumers:
            moved += await consumer.run_once()
        if moved == 0:
            break


async def _click_and_install(
    tracker, click_consumer, attribution_consumer, seeded_app, *, device: str, query: str
) -> None:
    response = await tracker.get(
        f"/c/{seeded_app['tracking_code']}?{query}",
        headers={"user-agent": ANDROID_UA},
        follow_redirects=False,
    )
    referrer = unquote(parse_qs(urlparse(response.headers["location"]).query)["referrer"][0])
    await flush_tracker(tracker)
    await _drain(click_consumer)

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="install",
                    anonymous_id=device,
                    properties={"install_referrer": referrer},
                )
            ]
        },
    )
    await flush_tracker(tracker)
    await _drain(attribution_consumer)


async def _scoped_rule(owner_conn, seeded_app, endpoint, *, campaign_id, template, **extra):
    rule_id = uuid7()
    await owner_conn.execute(
        """INSERT INTO postback_rules (id, organization_id, app_id, campaign_id, name,
                                       trigger_event, method, url_template, success_status_codes,
                                       requires_attribution, is_sandbox, enabled,
                                       created_at, updated_at)
           VALUES ($1, $2, $3, $4, $5, 'purchase', 'GET', $6, '[200]'::jsonb, $7, false, true,
                   now(), now())""",
        rule_id,
        seeded_app["organization_id"],
        seeded_app["app_id"],
        campaign_id,
        extra.get("name", "Partner rule"),
        endpoint.url + template,
        extra.get("requires_attribution", True),
    )
    return rule_id


async def _other_campaign(owner_conn, seeded_app):
    campaign_id = uuid7()
    await owner_conn.execute(
        """INSERT INTO campaigns (id, organization_id, app_id, name, source, medium, status)
           VALUES ($1, $2, $3, $4, 'partner_b', 'cpi', 'active')""",
        campaign_id,
        seeded_app["organization_id"],
        seeded_app["app_id"],
        f"Partner B {campaign_id}",
    )
    return campaign_id


async def _purchase(tracker, device: str) -> None:
    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="purchase", anonymous_id=device, revenue_minor=999, currency="USD"
                )
            ]
        },
    )
    await flush_tracker(tracker)


# --- the attribution keeps what the click carried ---------------------------
async def test_the_attribution_keeps_the_clicks_sub_parameters(
    tracker, click_consumer, attribution_consumer, seeded_app, owner_conn, ingest_redis
):
    from mmp_attrib.store import CachedAttribution

    await _click_and_install(
        tracker,
        click_consumer,
        attribution_consumer,
        seeded_app,
        device="sub-device-1",
        query=f"sub1={PARTNER_CLICK}&sub2=publisher-42&sub3=creative-b",
    )

    row = await owner_conn.fetchrow(
        "SELECT sub1, sub2, sub3 FROM attributions WHERE app_id = $1 AND anonymous_id = $2",
        seeded_app["app_id"],
        "sub-device-1",
    )
    assert (row["sub1"], row["sub2"], row["sub3"]) == (PARTNER_CLICK, "publisher-42", "creative-b")

    cached = await ingest_redis.get(f"attr:{seeded_app['app_id']}:sub-device-1")
    decoded = msgspec.msgpack.Decoder(CachedAttribution).decode(cached)
    assert decoded.sub1 == PARTNER_CLICK, "a later postback reads the cache, not the click"


async def test_a_cache_entry_from_before_the_upgrade_still_resolves(ingest_redis):
    """Entries written before sub parameters existed have no such fields. They must
    decode as "no sub parameters", not fail every postback for those installs."""
    from mmp_attrib.store import CachedAttribution

    class OldShape(msgspec.Struct):
        attribution_id: str
        click_id: str | None
        campaign_id: str | None
        tracking_link_id: str | None
        method: str
        attributed_at: str
        expires_at: str

    old = msgspec.msgpack.encode(
        OldShape(
            "a",
            "c",
            "cp",
            "l",
            "referrer",
            "2026-09-01T00:00:00+00:00",
            "2026-10-01T00:00:00+00:00",
        )
    )
    decoded = msgspec.msgpack.Decoder(CachedAttribution).decode(old)
    assert decoded.sub1 is None and decoded.campaign_id == "cp"


async def test_the_database_fallback_returns_them_too(
    tracker, click_consumer, attribution_consumer, seeded_app, owner_conn, ingest_redis
):
    """Redis loses keys to failovers and evictions. The Postgres fallback must not
    quietly drop the partner's click id when it rebuilds the entry."""
    from mmp_attrib.store import lookup

    await _click_and_install(
        tracker,
        click_consumer,
        attribution_consumer,
        seeded_app,
        device="sub-device-2",
        query=f"sub1={PARTNER_CLICK}",
    )
    await ingest_redis.delete(f"attr:{seeded_app['app_id']}:sub-device-2")

    resolved = await lookup(
        ingest_redis, owner_conn, app_id=seeded_app["app_id"], anonymous_id="sub-device-2"
    )
    assert resolved is not None and resolved.sub1 == PARTNER_CLICK


# --- the postback hands it back, to the right partner only -------------------
async def test_the_partner_gets_its_own_click_id_on_the_purchase_postback(
    tracker,
    click_consumer,
    attribution_consumer,
    seeded_app,
    owner_conn,
    ingest_redis,
    network,
    allow_loopback,
):
    rule_id = await _scoped_rule(
        owner_conn,
        seeded_app,
        network,
        campaign_id=seeded_app["campaign_id"],
        template="?clickid={{sub1}}&pub={{sub2}}&rev={{revenue}}",
    )
    await _click_and_install(
        tracker,
        click_consumer,
        attribution_consumer,
        seeded_app,
        device="sub-device-3",
        query=f"sub1={PARTNER_CLICK}&sub2=publisher-42",
    )
    await _purchase(tracker, "sub-device-3")
    await _run(seeded_app, ingest_redis)

    assert network.received, "the partner's endpoint should have been called"
    query = network.received[-1]["query"]
    assert query["clickid"] == [PARTNER_CLICK]
    assert query["pub"] == ["publisher-42"]
    delivered = await owner_conn.fetchval(
        "SELECT status FROM postback_deliveries WHERE postback_rule_id = $1", rule_id
    )
    assert delivered == "delivered"


async def test_a_rule_for_another_campaign_hears_nothing(
    tracker,
    click_consumer,
    attribution_consumer,
    seeded_app,
    owner_conn,
    ingest_redis,
    network,
    allow_loopback,
):
    """Partner B's rule must not be told about partner A's installs — and so can
    never see partner A's click ids."""
    partner_b = await _other_campaign(owner_conn, seeded_app)
    b_rule = await _scoped_rule(
        owner_conn,
        seeded_app,
        network,
        campaign_id=partner_b,
        template="?b_clickid={{sub1}}",
        name="Partner B",
    )
    await _click_and_install(
        tracker,
        click_consumer,
        attribution_consumer,
        seeded_app,
        device="sub-device-4",
        query=f"sub1={PARTNER_CLICK}",
    )
    await _purchase(tracker, "sub-device-4")
    consumer = await _run(seeded_app, ingest_redis)

    assert not any("b_clickid" in r["query"] for r in network.received)
    assert (
        await owner_conn.fetchval(
            "SELECT count(*) FROM postback_deliveries WHERE postback_rule_id = $1", b_rule
        )
        == 0
    )
    assert consumer.metrics.skipped_other_campaign >= 1
    assert PARTNER_CLICK not in json.dumps(network.received)


async def test_a_scoped_rule_never_fires_for_an_organic_install(
    tracker, seeded_app, owner_conn, ingest_redis, network, allow_loopback
):
    """An organic install belongs to no campaign. A scoped rule stays silent even
    when it does not require attribution."""
    rule_id = await _scoped_rule(
        owner_conn,
        seeded_app,
        network,
        campaign_id=seeded_app["campaign_id"],
        template="?c={{campaign_id}}",
        requires_attribution=False,
    )
    await _purchase(tracker, "organic-device")
    await _run(seeded_app, ingest_redis)
    assert (
        await owner_conn.fetchval(
            "SELECT count(*) FROM postback_deliveries WHERE postback_rule_id = $1", rule_id
        )
        == 0
    )


async def test_an_app_wide_rule_still_fires_for_every_campaign(
    tracker,
    click_consumer,
    attribution_consumer,
    seeded_app,
    owner_conn,
    ingest_redis,
    network,
    allow_loopback,
):
    """Unscoped rules keep their old behaviour: an advertiser's own analytics
    endpoint that wants every conversion is a legitimate use."""
    rule_id = await _scoped_rule(
        owner_conn, seeded_app, network, campaign_id=None, template="?all={{campaign_name}}"
    )
    await _click_and_install(
        tracker,
        click_consumer,
        attribution_consumer,
        seeded_app,
        device="sub-device-5",
        query="sub1=whatever",
    )
    await _purchase(tracker, "sub-device-5")
    await _run(seeded_app, ingest_redis)
    assert (
        await owner_conn.fetchval(
            "SELECT count(*) FROM postback_deliveries WHERE postback_rule_id = $1", rule_id
        )
        == 1
    )


# --- install postbacks wait for the attribution they report ------------------
async def _install_rule(owner_conn, seeded_app, endpoint, *, campaign_id, template, **extra):
    rule_id = await _scoped_rule(
        owner_conn, seeded_app, endpoint, campaign_id=campaign_id, template=template, **extra
    )
    await owner_conn.execute(
        "UPDATE postback_rules SET trigger_event = 'install' WHERE id = $1", rule_id
    )
    return rule_id


async def test_an_install_postback_is_not_lost_to_the_attribution_race(
    tracker,
    click_consumer,
    attribution_consumer,
    seeded_app,
    owner_conn,
    ingest_redis,
    network,
    allow_loopback,
):
    """The bug found by testing this on a running stack.

    The postback sender and the attribution writer are separate consumer groups
    reading the same install. The sender does less work, so it usually got there
    first: it looked up an attribution that did not exist yet, skipped the rule as
    unattributed, acknowledged the event, and never looked again. Nearly every
    install postback was lost — including every sub1 a partner needed back.

    This runs them in exactly that order.
    """
    rule_id = await _install_rule(
        owner_conn,
        seeded_app,
        network,
        campaign_id=seeded_app["campaign_id"],
        template="?clickid={{sub1}}&event={{event_name}}",
    )
    response = await tracker.get(
        f"/c/{seeded_app['tracking_code']}?sub1={PARTNER_CLICK}",
        headers={"user-agent": ANDROID_UA},
        follow_redirects=False,
    )
    referrer = unquote(parse_qs(urlparse(response.headers["location"]).query)["referrer"][0])
    await flush_tracker(tracker)
    await _drain(click_consumer)
    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="install",
                    anonymous_id="race-device",
                    properties={"install_referrer": referrer},
                )
            ]
        },
    )
    await flush_tracker(tracker)

    first = await _run(seeded_app, ingest_redis)  # the postback sender wins the race
    assert first.metrics.skipped_unattributed == 0, "the raw install must not be judged yet"
    await _drain(attribution_consumer)  # attribution commits, then publishes the install
    await _run(seeded_app, ingest_redis)

    assert (
        await owner_conn.fetchval(
            "SELECT count(*) FROM postback_deliveries WHERE postback_rule_id = $1", rule_id
        )
        == 1
    )
    assert network.received[-1]["query"]["clickid"] == [PARTNER_CLICK]


async def test_an_organic_install_still_reaches_an_app_wide_rule(
    tracker, attribution_consumer, seeded_app, owner_conn, ingest_redis, network, allow_loopback
):
    """Organic installs are re-published too. A rule that does not require
    attribution — an advertiser's own endpoint — still wants them."""
    rule_id = await _install_rule(
        owner_conn,
        seeded_app,
        network,
        campaign_id=None,
        template="?method={{attribution_method}}",
        requires_attribution=False,
    )
    await tracker.post(
        "/v1/events",
        json={"events": [sample_event(event_name="install", anonymous_id="organic-install")]},
    )
    await flush_tracker(tracker)
    await _run(seeded_app, ingest_redis)
    await _drain(attribution_consumer)
    await _run(seeded_app, ingest_redis)

    assert (
        await owner_conn.fetchval(
            "SELECT count(*) FROM postback_deliveries WHERE postback_rule_id = $1", rule_id
        )
        == 1
    )
    assert network.received[-1]["query"]["method"] == ["organic"]


async def test_a_redelivered_install_is_published_again_but_sent_once(
    tracker, attribution_consumer, seeded_app, owner_conn, ingest_redis, network, allow_loopback
):
    """The attribution worker publishes on every pass, including a redelivery.
    Delivery claims are unique per rule and event, so the partner hears once."""
    from mmp_ingest.schema import QueuedEvent
    from mmp_ingest.stream import ATTRIBUTED_INSTALLS_STREAM, StreamProducer

    rule_id = await _install_rule(
        owner_conn,
        seeded_app,
        network,
        campaign_id=None,
        template="?e={{event_id}}",
        requires_attribution=False,
    )
    await tracker.post(
        "/v1/events",
        json={"events": [sample_event(event_name="install", anonymous_id="twice-device")]},
    )
    await flush_tracker(tracker)
    await _drain(attribution_consumer)

    # Replay what the attribution worker published, as a redelivery would.
    entries = await ingest_redis.xrevrange(ATTRIBUTED_INSTALLS_STREAM, count=1)
    event = msgspec.msgpack.decode(entries[0][1][b"d"], type=QueuedEvent)
    await StreamProducer(ingest_redis, stream=ATTRIBUTED_INSTALLS_STREAM).publish([event])

    await _run(seeded_app, ingest_redis)
    assert (
        await owner_conn.fetchval(
            "SELECT count(*) FROM postback_deliveries WHERE postback_rule_id = $1", rule_id
        )
        == 1
    )
