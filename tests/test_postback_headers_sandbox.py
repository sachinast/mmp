"""Custom headers, sandbox mode and retries, against a real endpoint.

Three bugs that each looked fine from one side. A rule's headers were sealed and
stored, and never sent. A sandbox rule posted to an internal endpoint that did not
exist, so every sandbox delivery failed. And a retry re-sent the stored URL with
nothing else: a partner that authenticates with a header, or reads a POST body,
received a request on the retry that it would refuse.
"""

from __future__ import annotations

import json

from tests.conftest_ingest import flush_tracker, sample_event

SECRET = "Bearer partner-secret-7731"


async def _rule(owner_conn, seeded_app, network, *, headers=None, org_for_aad=None, **overrides):
    import msgspec
    from mmp_core.ids import uuid7
    from mmp_crypto.envelope import organization_aad, seal
    from mmp_crypto.kms import provider_from_settings

    settings = {
        "method": "GET",
        "url_template": network.url + "?click={{click_id}}&event={{event_name}}",
        "body_template": None,
        "is_sandbox": False,
    }
    settings.update(overrides)

    sealed = None
    if headers is not None:
        sealed = seal(
            msgspec.json.encode(headers),
            provider=provider_from_settings(seeded_app["worker_settings"]),
            aad=organization_aad(org_for_aad or seeded_app["organization_id"]),
        )

    rule_id = uuid7()
    await owner_conn.execute(
        """INSERT INTO postback_rules (id, organization_id, app_id, name, trigger_event,
               method, url_template, body_template, success_status_codes,
               requires_attribution, is_sandbox, enabled, headers_ciphertext,
               headers_nonce, wrapped_dek, key_version, created_at, updated_at)
           VALUES ($1, $2, $3, 'Partner', 'purchase', $4, $5, $6, '[200]'::jsonb,
                   false, $7, true, $8, $9, $10, $11, now(), now())""",
        rule_id,
        seeded_app["organization_id"],
        seeded_app["app_id"],
        settings["method"],
        settings["url_template"],
        settings["body_template"],
        settings["is_sandbox"],
        sealed.ciphertext if sealed else None,
        sealed.nonce if sealed else None,
        sealed.wrapped_dek if sealed else None,
        sealed.key_version if sealed else 1,
    )
    return rule_id


async def _purchase(tracker, anonymous_id):
    await tracker.post(
        "/v1/events",
        json={
            "events": [
                sample_event(
                    event_name="purchase",
                    anonymous_id=anonymous_id,
                    revenue_minor=100,
                    currency="USD",
                )
            ]
        },
    )
    await flush_tracker(tracker)


async def _run(seeded_app, ingest_redis, *, with_keys=True):
    from mmp_crypto.kms import provider_from_settings
    from mmp_db.pool import Database
    from mmp_worker.postbacks import PostbackConsumer

    database = await Database.connect(seeded_app["worker_settings"], role="mmp_worker")
    try:
        consumer = PostbackConsumer(
            redis=ingest_redis,
            database=database,
            consumer_name="test-headers",
            idle_sleep=0.01,
            master_keys=(
                provider_from_settings(seeded_app["worker_settings"]) if with_keys else None
            ),
        )
        await consumer.start()
        for _ in range(4):
            if await consumer.run_once() == 0:
                break
        return consumer
    finally:
        await database.close()


async def _retry(seeded_app, owner_conn, rule_id, *, with_keys=True):
    from mmp_crypto.kms import provider_from_settings
    from mmp_db.pool import Database
    from mmp_worker.postbacks import retry_due

    await owner_conn.execute(
        "UPDATE postback_deliveries SET next_retry_at = now() - interval '1 minute' "
        "WHERE postback_rule_id = $1",
        rule_id,
    )
    database = await Database.connect(seeded_app["worker_settings"], role="mmp_worker")
    try:
        keys = provider_from_settings(seeded_app["worker_settings"]) if with_keys else None
        return await retry_due(database, master_keys=keys)
    finally:
        await database.close()


async def _delivery(owner_conn, rule_id):
    return await owner_conn.fetchrow(
        "SELECT * FROM postback_deliveries WHERE postback_rule_id = $1", rule_id
    )


# --- custom headers ------------------------------------------------------
async def test_a_rules_custom_headers_are_sent(
    tracker, ingest_redis, owner_conn, seeded_app, network, allow_loopback
):
    await _rule(owner_conn, seeded_app, network, headers={"Authorization": SECRET, "X-Src": "mmp"})
    await _purchase(tracker, "hdr-sent")
    await _run(seeded_app, ingest_redis)

    assert network.received, "the partner should have been called"
    received = network.received[0]["headers"]
    assert received["authorization"] == SECRET
    assert received["x-src"] == "mmp"


async def test_headers_that_cannot_be_opened_block_the_delivery(
    tracker, ingest_redis, owner_conn, seeded_app, network, allow_loopback
):
    """Sealed for another organisation, so the AAD does not match. Sending without
    the partner's authentication would only earn a refusal, and raising used to
    leave the message unacknowledged forever."""
    from mmp_core.ids import uuid7

    rule_id = await _rule(
        owner_conn, seeded_app, network, headers={"Authorization": SECRET}, org_for_aad=uuid7()
    )
    await _purchase(tracker, "hdr-bad")
    consumer = await _run(seeded_app, ingest_redis)

    assert not network.received
    delivery = await _delivery(owner_conn, rule_id)
    assert delivery["status"] == "abandoned"
    assert delivery["error"].startswith("blocked:")
    assert "could not be decrypted" in delivery["error"]
    assert consumer.metrics.blocked == 1


async def test_a_worker_without_master_keys_blocks_rather_than_crashes(
    tracker, ingest_redis, owner_conn, seeded_app, network, allow_loopback
):
    """How production ran: the postback consumer was built with no key provider."""
    rule_id = await _rule(owner_conn, seeded_app, network, headers={"Authorization": SECRET})
    await _purchase(tracker, "hdr-nokeys")
    await _run(seeded_app, ingest_redis, with_keys=False)

    assert not network.received
    delivery = await _delivery(owner_conn, rule_id)
    assert delivery["status"] == "abandoned"
    assert "no master key provider" in delivery["error"]
    assert SECRET not in (delivery["error"] or "")


# --- sandbox ---------------------------------------------------------------
async def test_a_sandbox_rule_records_the_request_and_sends_nothing(
    tracker, ingest_redis, owner_conn, seeded_app, network, allow_loopback
):
    rule_id = await _rule(
        owner_conn,
        seeded_app,
        network,
        is_sandbox=True,
        method="POST",
        body_template='{"event": "{{event_name}}"}',
        headers={"Authorization": SECRET},
    )
    await _purchase(tracker, "sandbox-dev")
    consumer = await _run(seeded_app, ingest_redis)

    assert not network.received, "sandbox must never reach the partner"
    delivery = await _delivery(owner_conn, rule_id)
    assert delivery["status"] == "sandbox"
    assert delivery["request_url"].startswith(network.url + "?click=")
    assert "event=purchase" in delivery["request_url"]
    assert delivery["request_method"] == "POST"
    assert json.loads(bytes(delivery["request_body"])) == {"event": "purchase"}
    assert delivery["error"] is None
    assert delivery["next_retry_at"] is None, "a sandbox delivery is never retried"
    assert delivery["headers_ciphertext"] is None, "nothing to replay, so nothing kept"
    assert consumer.metrics.sandboxed == 1


# --- retries ---------------------------------------------------------------
async def test_a_retry_replays_method_body_and_headers(
    tracker, ingest_redis, owner_conn, seeded_app, network, allow_loopback
):
    network.respond_with = 503
    rule_id = await _rule(
        owner_conn,
        seeded_app,
        network,
        method="POST",
        body_template='{"event": "{{event_name}}"}',
        headers={"Authorization": SECRET},
    )
    await _purchase(tracker, "retry-full")
    await _run(seeded_app, ingest_redis)
    assert len(network.received) == 1

    stored = await _delivery(owner_conn, rule_id)
    assert stored["status"] == "failed"
    assert stored["headers_ciphertext"] is not None
    assert SECRET.encode() not in bytes(stored["headers_ciphertext"]), "stored sealed"

    network.respond_with = 200
    assert await _retry(seeded_app, owner_conn, rule_id) == 1

    first, second = network.received
    assert second["method"] == first["method"] == "POST"
    assert second["body"] == first["body"]
    assert json.loads(second["body"]) == {"event": "purchase"}
    assert second["headers"]["authorization"] == SECRET
    assert second["headers"]["content-type"] == "application/json"
    assert second["query"] == first["query"]

    delivered = await _delivery(owner_conn, rule_id)
    assert delivered["status"] == "delivered"
    assert delivered["headers_ciphertext"] is None, "sealed headers go once delivered"


async def test_a_retry_that_cannot_open_its_headers_is_abandoned(
    tracker, ingest_redis, owner_conn, seeded_app, network, allow_loopback
):
    network.respond_with = 503
    rule_id = await _rule(owner_conn, seeded_app, network, headers={"Authorization": SECRET})
    await _purchase(tracker, "retry-nokeys")
    await _run(seeded_app, ingest_redis)

    network.respond_with = 200
    assert await _retry(seeded_app, owner_conn, rule_id, with_keys=False) == 0
    assert len(network.received) == 1, "no retry without its authentication"
    delivery = await _delivery(owner_conn, rule_id)
    assert delivery["status"] == "abandoned"
    assert delivery["error"].startswith("blocked:")


async def test_a_rule_switched_to_sandbox_stops_its_retries(
    tracker, ingest_redis, owner_conn, seeded_app, network, allow_loopback
):
    network.respond_with = 503
    rule_id = await _rule(owner_conn, seeded_app, network)
    await _purchase(tracker, "retry-sandboxed")
    await _run(seeded_app, ingest_redis)

    await owner_conn.execute("UPDATE postback_rules SET is_sandbox = true WHERE id = $1", rule_id)
    network.respond_with = 200
    assert await _retry(seeded_app, owner_conn, rule_id) == 0
    assert len(network.received) == 1
    delivery = await _delivery(owner_conn, rule_id)
    assert delivery["status"] == "abandoned"
    assert "sandbox" in delivery["error"]


async def test_an_old_failure_is_replayed_only_when_the_url_is_the_whole_request(
    tracker, ingest_redis, owner_conn, seeded_app, network, allow_loopback
):
    """Rows stored before requests were kept in full have only a URL."""
    network.respond_with = 503
    plain = await _rule(owner_conn, seeded_app, network)
    with_headers = await _rule(owner_conn, seeded_app, network, headers={"Authorization": SECRET})
    await _purchase(tracker, "retry-legacy")
    await _run(seeded_app, ingest_redis)
    assert len(network.received) == 2

    for rule_id in (plain, with_headers):
        await owner_conn.execute(
            "UPDATE postback_deliveries SET request_method = NULL, request_body = NULL, "
            "headers_ciphertext = NULL, headers_nonce = NULL, wrapped_dek = NULL "
            "WHERE postback_rule_id = $1",
            rule_id,
        )
        await owner_conn.execute(
            "UPDATE postback_deliveries SET next_retry_at = now() - interval '1 minute' "
            "WHERE postback_rule_id = $1",
            rule_id,
        )

    network.respond_with = 200
    assert await _retry(seeded_app, owner_conn, plain) == 1
    assert len(network.received) == 3
    assert "authorization" not in network.received[2]["headers"]
    assert (await _delivery(owner_conn, plain))["status"] == "delivered"

    legacy = await _delivery(owner_conn, with_headers)
    assert legacy["status"] == "abandoned"
    assert "cannot retry faithfully" in legacy["error"]
