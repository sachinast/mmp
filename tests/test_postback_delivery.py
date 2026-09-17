"""Postback delivery end to end, against a real ad-network endpoint.

A real HTTP server rather than a mocked send, because the bugs this path has
produced were all in the seam between components: a status-code list read as a
string, a URL built from a template, a claim that did not prevent a second send.
None of those are visible from either side alone.
"""

from __future__ import annotations

import json

from tests.conftest_ingest import flush_tracker, sample_event


async def _rule(owner_conn, seeded_app, network, **overrides):
    from mmp_core.ids import uuid7

    settings = {
        "url_template": network.url + "?click={{click_id}}&rev={{revenue}}&cur={{currency}}",
        "success_status_codes": [200],
        "requires_attribution": False,
        "trigger_event": "purchase",
    }
    settings.update(overrides)

    rule_id = uuid7()
    await owner_conn.execute(
        """INSERT INTO postback_rules (id, organization_id, app_id, name, trigger_event,
                                       method, url_template, success_status_codes,
                                       requires_attribution, is_sandbox, enabled,
                                       created_at, updated_at)
           VALUES ($1, $2, $3, 'Network', $4, 'GET', $5, $6::jsonb, $7, false, true,
                   now(), now())""",
        rule_id,
        seeded_app["organization_id"],
        seeded_app["app_id"],
        settings["trigger_event"],
        settings["url_template"],
        json.dumps(settings["success_status_codes"]),
        settings["requires_attribution"],
    )
    return rule_id


async def _run(seeded_app, ingest_redis, rounds: int = 4):
    from mmp_db.pool import Database
    from mmp_worker.postbacks import PostbackConsumer

    database = await Database.connect(seeded_app["worker_settings"], role="mmp_worker")
    try:
        consumer = PostbackConsumer(
            redis=ingest_redis,
            database=database,
            consumer_name="test-postbacks",
            idle_sleep=0.01,
        )
        await consumer.start()
        for _ in range(rounds):
            if await consumer.run_once() == 0:
                break
        return consumer
    finally:
        await database.close()


async def test_a_conversion_reaches_the_network(
    tracker, ingest_redis, owner_conn, seeded_app, network, allow_loopback
):
    rule_id = await _rule(owner_conn, seeded_app, network)

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="purchase", anonymous_id="pb-dev", revenue_minor=2599, currency="USD"
                )
            ]
        },
    )
    await flush_tracker(tracker)
    await _run(seeded_app, ingest_redis)

    assert network.received, "the network endpoint should have been called"
    query = network.received[0]["query"]
    # Major units, because that is what every network's macro expects.
    assert query["rev"] == ["25.99"]
    assert query["cur"] == ["USD"]

    delivery = await owner_conn.fetchrow(
        "SELECT status, response_status, request_url FROM postback_deliveries "
        "WHERE postback_rule_id = $1",
        rule_id,
    )
    assert delivery["status"] == "delivered", (
        "a 200 against a success list of [200] must record as delivered — this is "
        "the check that read the status list as a string and never matched"
    )
    assert delivery["response_status"] == 200
    assert delivery["request_url"], "the URL is stored so a retry can re-send it"


async def test_a_redelivered_conversion_is_not_sent_twice(
    tracker, ingest_redis, owner_conn, seeded_app, network, allow_loopback
):
    """On cost-per-action, a duplicate means paying twice for one action."""
    from mmp_ingest.stream import EVENTS_STREAM
    from mmp_worker.postbacks import POSTBACK_GROUP

    await _rule(owner_conn, seeded_app, network)
    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="purchase", anonymous_id="pb-dup", revenue_minor=100, currency="USD"
                )
            ]
        },
    )
    await flush_tracker(tracker)

    await _run(seeded_app, ingest_redis, rounds=1)
    await ingest_redis.xgroup_setid(EVENTS_STREAM, POSTBACK_GROUP, id="0")
    await _run(seeded_app, ingest_redis, rounds=1)

    assert len(network.received) == 1


async def test_unattributed_conversions_are_skipped_when_required(
    tracker, ingest_redis, owner_conn, seeded_app, network, allow_loopback
):
    """Reporting an unattributed conversion would credit a network for
    something it did not cause."""
    await _rule(owner_conn, seeded_app, network, requires_attribution=True)

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="purchase",
                    anonymous_id="pb-unattributed",
                    revenue_minor=500,
                    currency="USD",
                )
            ]
        },
    )
    await flush_tracker(tracker)
    consumer = await _run(seeded_app, ingest_redis)

    assert not network.received
    assert consumer.metrics.skipped_unattributed >= 1


async def test_a_failing_network_is_recorded_for_retry(
    tracker, ingest_redis, owner_conn, seeded_app, network, allow_loopback
):
    network.respond_with = 503
    rule_id = await _rule(owner_conn, seeded_app, network)

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="purchase", anonymous_id="pb-fail", revenue_minor=100, currency="USD"
                )
            ]
        },
    )
    await flush_tracker(tracker)
    await _run(seeded_app, ingest_redis)

    delivery = await owner_conn.fetchrow(
        "SELECT status, response_status, next_retry_at FROM postback_deliveries "
        "WHERE postback_rule_id = $1",
        rule_id,
    )
    assert delivery["status"] == "failed"
    assert delivery["response_status"] == 503
    assert delivery["next_retry_at"] is not None, "a 5xx must be scheduled for retry"


async def test_a_refusal_is_not_scheduled_for_retry(
    tracker, ingest_redis, owner_conn, seeded_app, network, allow_loopback
):
    """A 4xx means the network understood us and refused; an identical request
    will be refused identically."""
    network.respond_with = 400
    rule_id = await _rule(owner_conn, seeded_app, network)

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="purchase",
                    anonymous_id="pb-refused",
                    revenue_minor=100,
                    currency="USD",
                )
            ]
        },
    )
    await flush_tracker(tracker)
    await _run(seeded_app, ingest_redis)

    delivery = await owner_conn.fetchrow(
        "SELECT status, next_retry_at FROM postback_deliveries WHERE postback_rule_id = $1",
        rule_id,
    )
    assert delivery["status"] == "abandoned"
    assert delivery["next_retry_at"] is None


async def test_a_due_retry_is_re_sent(
    tracker, ingest_redis, owner_conn, seeded_app, network, allow_loopback
):
    """The queue message was acknowledged when the first attempt was recorded,
    so the retry has only the stored URL to work from."""
    from mmp_db.pool import Database
    from mmp_worker.postbacks import retry_due

    network.respond_with = 503
    rule_id = await _rule(owner_conn, seeded_app, network)

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="purchase",
                    anonymous_id="pb-retry",
                    revenue_minor=100,
                    currency="USD",
                )
            ]
        },
    )
    await flush_tracker(tracker)
    await _run(seeded_app, ingest_redis)
    assert len(network.received) == 1

    # The network recovers, and the backoff elapses.
    network.respond_with = 200
    await owner_conn.execute(
        "UPDATE postback_deliveries SET next_retry_at = now() - interval '1 minute' "
        "WHERE postback_rule_id = $1",
        rule_id,
    )

    database = await Database.connect(seeded_app["worker_settings"], role="mmp_worker")
    try:
        assert await retry_due(database) == 1
    finally:
        await database.close()

    assert len(network.received) == 2, "the retry must actually re-send"
    delivery = await owner_conn.fetchrow(
        "SELECT status, attempt_count FROM postback_deliveries WHERE postback_rule_id = $1",
        rule_id,
    )
    assert delivery["status"] == "delivered"
    assert delivery["attempt_count"] == 2


# --- delivery through a provider adapter --------------------------------
async def _integration_rule(owner_conn, seeded_app, network, master_keys, **settings):
    """A postback rule bound to an s2s_json integration."""
    import msgspec
    from mmp_core.ids import uuid7
    from mmp_crypto.envelope import organization_aad, seal

    config = {"endpoint": network.url, **settings}
    sealed = seal(
        msgspec.json.encode({"api_token": "tok-123"}),
        provider=master_keys,
        aad=organization_aad(seeded_app["organization_id"]),
    )
    integration_id, rule_id = uuid7(), uuid7()

    await owner_conn.execute(
        """INSERT INTO provider_integrations (id, organization_id, provider, name,
               credentials_ciphertext, credentials_nonce, wrapped_dek, key_version,
               configuration, status, created_at, updated_at)
           VALUES ($1, $2, 's2s_json', 'Network', $3, $4, $5, 1, $6::jsonb,
                   'active', now(), now())""",
        integration_id,
        seeded_app["organization_id"],
        sealed.ciphertext,
        sealed.nonce,
        sealed.wrapped_dek,
        json.dumps(config),
    )
    await owner_conn.execute(
        """INSERT INTO postback_rules (id, organization_id, app_id,
               provider_integration_id, name, trigger_event, method, url_template,
               success_status_codes, requires_attribution, is_sandbox, enabled,
               created_at, updated_at)
           VALUES ($1, $2, $3, $4, 'Via adapter', 'purchase', 'POST', $5,
                   '[200]'::jsonb, false, false, true, now(), now())""",
        rule_id,
        seeded_app["organization_id"],
        seeded_app["app_id"],
        integration_id,
        network.url,
    )
    return rule_id


async def _run_with_keys(seeded_app, ingest_redis, rounds: int = 4):
    from mmp_crypto.kms import provider_from_settings
    from mmp_db.pool import Database
    from mmp_worker.postbacks import PostbackConsumer

    database = await Database.connect(seeded_app["worker_settings"], role="mmp_worker")
    try:
        consumer = PostbackConsumer(
            redis=ingest_redis,
            database=database,
            consumer_name="test-adapter",
            idle_sleep=0.01,
            master_keys=provider_from_settings(seeded_app["worker_settings"]),
        )
        await consumer.start()
        for _ in range(rounds):
            if await consumer.run_once() == 0:
                break
        return consumer
    finally:
        await database.close()


async def test_an_adapter_builds_and_sends_the_conversion(
    tracker, ingest_redis, owner_conn, seeded_app, network, allow_loopback
):
    """The whole framework, end to end: credentials unsealed, event name
    translated, JSON body posted, response read."""
    from mmp_crypto.kms import provider_from_settings

    await _integration_rule(
        owner_conn,
        seeded_app,
        network,
        provider_from_settings(seeded_app["worker_settings"]),
    )

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="purchase",
                    anonymous_id="adapter-dev",
                    revenue_minor=3499,
                    currency="USD",
                )
            ]
        },
    )
    await flush_tracker(tracker)
    await _run_with_keys(seeded_app, ingest_redis)

    assert network.received, "the adapter's request should have been sent"
    sent = network.received[0]
    assert sent["method"] == "POST"
    assert sent["headers"]["authorization"] == "Bearer tok-123", (
        "credentials must be unsealed and applied"
    )
    payload = json.loads(sent["body"])
    assert payload["event_name"] == "purchase"
    assert payload["value"] == "34.99", "major units, as every network's macro expects"
    assert payload["currency"] == "USD"


async def test_an_unmapped_event_is_skipped_not_failed(
    tracker, ingest_redis, owner_conn, seeded_app, network, allow_loopback
):
    """An event this integration was not configured to send is skipped quietly
    rather than attempted and recorded as a failure."""
    from mmp_crypto.kms import provider_from_settings

    rule_id = await _integration_rule(
        owner_conn,
        seeded_app,
        network,
        provider_from_settings(seeded_app["worker_settings"]),
        event_map={"install": "app_install"},  # purchase deliberately absent
    )
    await owner_conn.execute(
        "UPDATE postback_rules SET trigger_event = 'purchase' WHERE id = $1", rule_id
    )

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="purchase",
                    anonymous_id="unmapped-dev",
                    revenue_minor=100,
                    currency="USD",
                )
            ]
        },
    )
    await flush_tracker(tracker)
    consumer = await _run_with_keys(seeded_app, ingest_redis)

    assert not network.received, "an unmapped event must not be sent"
    assert consumer.metrics.skipped_unmapped >= 1


async def test_a_success_code_carrying_an_error_is_recorded_as_a_failure(
    tracker, ingest_redis, owner_conn, seeded_app, network, allow_loopback
):
    """The reason the adapter reads the body.

    A provider returning 200 with {"error": ...} is a rejection wearing a
    success code, and recording it as delivered is how an advertiser spends a
    month believing conversions are arriving.
    """
    from mmp_crypto.kms import provider_from_settings

    network.respond_with = 200
    network.body = json.dumps({"error": "unknown campaign"})

    rule_id = await _integration_rule(
        owner_conn,
        seeded_app,
        network,
        provider_from_settings(seeded_app["worker_settings"]),
    )

    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="purchase",
                    anonymous_id="rejected-dev",
                    revenue_minor=100,
                    currency="USD",
                )
            ]
        },
    )
    await flush_tracker(tracker)
    await _run_with_keys(seeded_app, ingest_redis)

    assert network.received, "the request was made"
    delivery = await owner_conn.fetchrow(
        "SELECT status, response_status, error FROM postback_deliveries "
        "WHERE postback_rule_id = $1",
        rule_id,
    )
    assert delivery["response_status"] == 200
    assert delivery["status"] != "delivered", (
        "a 200 carrying an error must not be recorded as a delivery"
    )
    assert "unknown campaign" in (delivery["error"] or "")
