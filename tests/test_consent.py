"""Consent, applied before anything is stored.

The ordering is the whole point. Consent checked after persistence is a deletion
problem — the data is already in a partition, a rollup, a postback and a
partner's system. Checked at the edge, a denied purpose means the field never
existed.
"""

from __future__ import annotations

import pytest
from mmp_ingest.consent import (
    ALWAYS_RETAINED,
    ConsentGate,
    ConsentSet,
    Mode,
    Purpose,
    State,
    may_attribute,
    may_forward,
    minimise,
)

from tests.conftest_ingest import flush_tracker, sample_event


# --- the decision -------------------------------------------------------
def test_an_explicit_denial_always_refuses():
    """Not configurable and not negotiable. If a user has said no, no mode
    overrides it."""
    denied = {Purpose.ATTRIBUTION: State.DENIED}
    for mode in (Mode.PERMISSIVE, Mode.STRICT):
        assert not ConsentSet(denied, mode).allows(Purpose.ATTRIBUTION)


def test_an_explicit_grant_always_permits():
    granted = {Purpose.ATTRIBUTION: State.GRANTED}
    for mode in (Mode.PERMISSIVE, Mode.STRICT):
        assert ConsentSet(granted, mode).allows(Purpose.ATTRIBUTION)


def test_unknown_depends_on_the_mode():
    """The one genuinely configurable part, and the one that matters
    commercially: strict everywhere by default would silently stop attributing
    every existing advertiser's installs."""
    assert ConsentSet(mode=Mode.PERMISSIVE).allows(Purpose.ATTRIBUTION)
    assert not ConsentSet(mode=Mode.STRICT).allows(Purpose.ATTRIBUTION)


def test_purposes_are_separable():
    """A user may allow us to count that an install happened and refuse to have
    it attributed to an ad network."""
    states = {Purpose.ANALYTICS: State.GRANTED, Purpose.ATTRIBUTION: State.DENIED}
    consent = ConsentSet(states, Mode.STRICT)
    assert consent.allows(Purpose.ANALYTICS)
    assert not consent.allows(Purpose.ATTRIBUTION)


def test_forwarding_requires_both_purposes():
    """Sending a conversion to an ad network is an advertising use *of an
    attribution*. A user who allowed one but not the other has not agreed."""
    attribution_only = ConsentSet(
        {Purpose.ATTRIBUTION: State.GRANTED, Purpose.ADVERTISING: State.DENIED},
        Mode.STRICT,
    )
    assert may_attribute(attribution_only)
    assert not may_forward(attribution_only)

    both = ConsentSet(
        {Purpose.ATTRIBUTION: State.GRANTED, Purpose.ADVERTISING: State.GRANTED},
        Mode.STRICT,
    )
    assert may_forward(both)


# --- minimisation -------------------------------------------------------
def test_denied_fields_are_removed_not_blanked():
    """A key present with a null value still tells a reader that we asked."""
    consent = ConsentSet({Purpose.ATTRIBUTION: State.DENIED})
    result = minimise({"device_hash": "abc", "custom": "keep"}, consent)
    assert "device_hash" not in result
    assert result["custom"] == "keep"


def test_minimise_does_not_mutate_its_input():
    original = {"device_hash": "abc"}
    minimise(original, ConsentSet({Purpose.ATTRIBUTION: State.DENIED}))
    assert original == {"device_hash": "abc"}


def test_granted_fields_survive():
    consent = ConsentSet({Purpose.ATTRIBUTION: State.GRANTED})
    result = minimise({"device_hash": "abc", "install_referrer": "utm_content=x"}, consent)
    assert result["device_hash"] == "abc"
    assert result["install_referrer"] == "utm_content=x"


def test_the_fact_of_an_event_is_always_retained():
    """An event that cannot be counted at all cannot be billed, reconciled or
    rate-limited, and a platform that silently stops recording that *something*
    happened cannot tell that from an outage."""
    assert "event_id" in ALWAYS_RETAINED
    assert "app_id" in ALWAYS_RETAINED
    assert "event_name" in ALWAYS_RETAINED
    # Nothing that identifies a person beyond the pseudonymous SDK id.
    for identifier in ("device_hash", "ip_hash", "user_id", "click_id"):
        assert identifier not in ALWAYS_RETAINED


# --- the cache ----------------------------------------------------------
async def test_recorded_consent_is_read_back(ingest_redis):
    gate = ConsentGate(ingest_redis)
    await gate.record("app-1", "dev-1", {Purpose.ATTRIBUTION: State.GRANTED})

    consent = await gate.lookup("app-1", "dev-1", mode=Mode.STRICT)
    assert consent.allows(Purpose.ATTRIBUTION)


async def test_purposes_merge_rather_than_replace(ingest_redis):
    """An SDK reporting one purpose must not silently withdraw another."""
    gate = ConsentGate(ingest_redis)
    await gate.record("app-1", "dev-1", {Purpose.ATTRIBUTION: State.GRANTED})
    await gate.record("app-1", "dev-1", {Purpose.ANALYTICS: State.GRANTED})

    consent = await gate.lookup("app-1", "dev-1", mode=Mode.STRICT)
    assert consent.allows(Purpose.ATTRIBUTION)
    assert consent.allows(Purpose.ANALYTICS)


async def test_withdrawal_takes_effect(ingest_redis):
    gate = ConsentGate(ingest_redis)
    await gate.record("app-1", "dev-1", {Purpose.ATTRIBUTION: State.GRANTED})
    await gate.record("app-1", "dev-1", {Purpose.ATTRIBUTION: State.DENIED})

    consent = await gate.lookup("app-1", "dev-1", mode=Mode.PERMISSIVE)
    assert not consent.allows(Purpose.ATTRIBUTION), (
        "a withdrawal must override a prior grant, in either mode"
    )


async def test_devices_do_not_share_consent(ingest_redis):
    gate = ConsentGate(ingest_redis)
    await gate.record("app-1", "dev-1", {Purpose.ATTRIBUTION: State.DENIED})
    other = await gate.lookup("app-1", "dev-2", mode=Mode.PERMISSIVE)
    assert other.is_empty


# --- end to end ---------------------------------------------------------
async def test_an_sdk_can_report_consent_and_it_applies_to_the_same_batch(
    tracker, worker_consumer, owner_conn, seeded_app
):
    """An SDK flushing after the user accepted a dialogue sends exactly this
    batch: the decision, then data collected under it."""
    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="consent_update",
                    anonymous_id="consent-dev",
                    properties={"attribution": "denied", "analytics": "granted"},
                ),
                sample_event(
                    event_name="install",
                    anonymous_id="consent-dev",
                    properties={"device_hash": "aabbcc", "custom": "keep"},
                ),
            ]
        },
    )
    await flush_tracker(tracker)
    for _ in range(3):
        if await worker_consumer.run_once() == 0:
            break

    row = await owner_conn.fetchrow(
        "SELECT properties, ip_hash FROM events "
        "WHERE app_id = $1 AND anonymous_id = 'consent-dev' AND event_name = 'install'",
        seeded_app["app_id"],
    )
    assert row is not None, "the event itself is still recorded"

    import json

    properties = json.loads(row["properties"])
    assert "device_hash" not in properties, "a denied purpose means the field never existed"
    assert properties["custom"] == "keep", "unrelated properties survive"
    assert row["ip_hash"] is None, "columns are cleared too, not just properties"


async def test_a_denial_stops_attribution_end_to_end(
    tracker, click_consumer, attribution_consumer, owner_conn, seeded_app
):
    """The install is still counted; it is simply not linked to a click."""
    from urllib.parse import parse_qs, unquote, urlparse

    from tests.conftest_ingest import ANDROID_UA

    response = await tracker.get(
        f"/c/{seeded_app['tracking_code']}",
        headers={"user-agent": ANDROID_UA},
        follow_redirects=False,
    )
    referrer = unquote(parse_qs(urlparse(response.headers["location"]).query)["referrer"][0])
    await flush_tracker(tracker)
    for _ in range(3):
        if await click_consumer.run_once() == 0:
            break

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="consent_update",
                    anonymous_id="denied-dev",
                    properties={"attribution": "denied"},
                ),
                sample_event(
                    event_name="install",
                    anonymous_id="denied-dev",
                    properties={"install_referrer": referrer},
                ),
            ]
        },
    )
    await flush_tracker(tracker)
    for _ in range(3):
        if await attribution_consumer.run_once() == 0:
            break

    row = await owner_conn.fetchrow(
        "SELECT method, click_id FROM attributions "
        "WHERE app_id = $1 AND install_key LIKE '%denied-dev'",
        seeded_app["app_id"],
    )
    assert row is not None, "the install is still recorded"
    assert row["method"] == "organic", "and it is not linked to a click"
    assert row["click_id"] is None


async def test_an_unknown_purpose_does_not_fail_the_batch(tracker):
    """An older SDK reporting a purpose we have since renamed must not lose its
    whole batch."""
    response = await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="consent_update",
                    anonymous_id="old-sdk",
                    properties={"telepathy": "granted", "attribution": "granted"},
                ),
                sample_event(event_name="install", anonymous_id="old-sdk"),
            ]
        },
    )
    assert response.status_code == 202
    assert response.json()["accepted"] == 2


@pytest.mark.parametrize("mode", ["permissive", "strict"])
async def test_the_app_mode_decides_what_silence_means(
    tracker, worker_consumer, owner_conn, seeded_app, mode
):
    """No consent reported at all — the state every existing integration is in."""
    await owner_conn.execute(
        "UPDATE apps SET consent_mode = $2 WHERE id = $1", seeded_app["app_id"], mode
    )
    # The key cache holds the old mode; clear it as a revocation would.
    from mmp_crypto.keys import api_key_cache_key, parse_key

    prefix = parse_key(seeded_app["api_key"]).prefix
    await tracker.tracker_app.state.tracker.redis.delete(api_key_cache_key(prefix))

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="install",
                    anonymous_id=f"silent-{mode}",
                    properties={"device_hash": "ddeeff"},
                )
            ]
        },
    )
    await flush_tracker(tracker)
    for _ in range(3):
        if await worker_consumer.run_once() == 0:
            break

    import json

    row = await owner_conn.fetchrow(
        "SELECT properties FROM events WHERE app_id = $1 AND anonymous_id = $2",
        seeded_app["app_id"],
        f"silent-{mode}",
    )
    properties = json.loads(row["properties"])
    if mode == "permissive":
        assert "device_hash" in properties, "silence proceeds"
    else:
        assert "device_hash" not in properties, "silence denies"


async def test_batched_lookup_matches_the_single_path(ingest_redis):
    """The fast path must give the same answers as the simple one.

    lookup_many exists purely for latency — a Redis GET per device took ingest
    p50 from 2.4 ms to 7.3 ms on a twenty-device batch, the same mistake as the
    first version of session assignment and caught by the same gate. If the two
    ever disagreed, whether a field was stored would depend on how events
    happened to be batched.
    """
    gate = ConsentGate(ingest_redis)
    await gate.record("app-b", "dev-1", {Purpose.ATTRIBUTION: State.GRANTED})
    await gate.record("app-b", "dev-2", {Purpose.ATTRIBUTION: State.DENIED})

    for mode in (Mode.PERMISSIVE, Mode.STRICT):
        batched = await gate.lookup_many("app-b", ["dev-1", "dev-2", "dev-never-seen"], mode=mode)
        for device in ("dev-1", "dev-2", "dev-never-seen"):
            single = await gate.lookup("app-b", device, mode=mode)
            assert batched[device].states == single.states, device
            assert batched[device].allows(Purpose.ATTRIBUTION) == single.allows(
                Purpose.ATTRIBUTION
            ), device


async def test_batched_lookup_deduplicates_devices(ingest_redis):
    """A batch is usually one device sending several events."""
    gate = ConsentGate(ingest_redis)
    await gate.record("app-c", "dev-1", {Purpose.ATTRIBUTION: State.DENIED})

    resolved = await gate.lookup_many("app-c", ["dev-1"] * 20, mode=Mode.PERMISSIVE)
    assert len(resolved) == 1
    assert not resolved["dev-1"].allows(Purpose.ATTRIBUTION)


async def test_batched_lookup_of_nothing_is_empty(ingest_redis):
    assert await ConsentGate(ingest_redis).lookup_many("app-d", []) == {}
