"""Postback rule management: bad rules fail at save time, headers stay secret."""

from __future__ import annotations

import pytest

from tests.conftest_api import register_account


async def _app(account, name="Postback App", package="com.example.postbacks"):
    response = await account.post(
        "/v1/apps",
        json={"name": name, "platform": "android", "android_package_name": package},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _rule(account, app, **overrides):
    body = {
        "app_id": app["id"],
        "name": "Meta conversions",
        "trigger_event": "purchase",
        "url_template": "https://example.com/c?click={{click_id}}&rev={{revenue}}",
    }
    body.update(overrides)
    return await account.post("/v1/postback-rules", json=body)


async def test_create_and_list_a_rule(account):
    app = await _app(account)
    created = await _rule(account, app)
    assert created.status_code == 201, created.text
    rule = created.json()
    assert rule["trigger_event"] == "purchase"
    assert rule["enabled"]

    listing = await account.client.get(f"/v1/postback-rules?app_id={app['id']}")
    assert listing.status_code == 200
    assert [r["id"] for r in listing.json()] == [rule["id"]]


@pytest.mark.parametrize(
    "url_template",
    [
        "https://example.com/c?x={{7*7}}",
        "https://example.com/c?x={{ config.items() }}",
        "https://example.com/c?x={{ ''.__class__.__mro__ }}",
        "https://example.com/c?x={{clickid}}",  # a typo
        "https://example.com/c?x={{ not a name }}",
    ],
)
async def test_bad_templates_are_refused_at_save_time(account, url_template):
    """The alternative is a rule that appears to work and quietly delivers
    nothing, discovered when a network asks why conversions stopped."""
    app = await _app(account)
    response = await _rule(account, app, url_template=url_template)
    assert response.status_code == 422


@pytest.mark.parametrize(
    "url_template",
    [
        "https://169.254.169.254/latest/meta-data/",
        "https://127.0.0.1:8002/v1/apps",
        "https://10.0.0.5/internal?c={{click_id}}",
        "http://example.com/c",
    ],
)
async def test_bad_destinations_are_refused_at_save_time(account, url_template):
    """A rule pointing at the metadata endpoint must never reach the delivery
    worker, where the only thing between it and our credentials is one check."""
    app = await _app(account)
    response = await _rule(account, app, url_template=url_template)
    assert response.status_code == 422
    assert "not permitted" in response.json()["detail"]


async def test_header_values_are_never_returned(account):
    """A postback header commonly carries a partner's bearer token."""
    app = await _app(account)
    created = await _rule(account, app, headers={"Authorization": "Bearer partner-secret-token"})
    assert created.status_code == 201
    rule = created.json()

    assert rule["header_names"] == ["Authorization"]
    assert "partner-secret-token" not in created.text

    listing = await account.client.get("/v1/postback-rules")
    assert "partner-secret-token" not in listing.text
    assert listing.json()[0]["header_names"] == ["Authorization"]


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "evil.example"},
        {"Content-Length": "0"},
        {"Transfer-Encoding": "chunked"},
        {"X-Thing": "value\r\nInjected: yes"},
        {"X-Thing\n": "value"},
    ],
)
async def test_dangerous_headers_are_refused(account, headers):
    """Host would defeat the destination pinning; a newline is request
    splitting — everything after it is read as a separate header."""
    app = await _app(account)
    response = await _rule(account, app, headers=headers)
    assert response.status_code == 422


async def test_preview_renders_against_sample_values(account):
    """Lets someone see the URL a partner will receive before a real conversion
    depends on it."""
    app = await _app(account)
    rule = (await _rule(account, app)).json()

    preview = await account.client.get(f"/v1/postback-rules/{rule['id']}/preview")
    assert preview.status_code == 200
    body = preview.json()
    assert body["url"].startswith("https://example.com/c?click=")
    assert "{{" not in body["url"], "every placeholder should be substituted"
    assert set(body["variables_used"]) == {"click_id", "revenue"}
    assert "campaign_name" in body["available_variables"]


async def test_patching_out_of_sandbox_revalidates_the_destination(account):
    """A rule created in sandbox never had its destination checked.

    Flipping sandbox off must check it then, or the check can be skipped
    entirely by creating in sandbox mode and patching afterwards.
    """
    app = await _app(account)
    created = await _rule(
        account, app, is_sandbox=True, url_template="https://169.254.169.254/steal"
    )
    assert created.status_code == 201, "sandbox rules skip the destination check"

    response = await account.patch(
        f"/v1/postback-rules/{created.json()['id']}", json={"is_sandbox": False}
    )
    assert response.status_code == 422
    assert "not permitted" in response.json()["detail"]


async def test_rules_disable_rather_than_delete(account):
    """Delivery rows reference the rule; a deleted one would leave an advertiser
    unable to explain conversions their network already received."""
    app = await _app(account)
    rule = (await _rule(account, app)).json()

    assert (await account.delete(f"/v1/postback-rules/{rule['id']}")).status_code == 204
    listing = await account.client.get("/v1/postback-rules")
    stored = next(r for r in listing.json() if r["id"] == rule["id"])
    assert stored["enabled"] is False


async def test_viewer_cannot_create_rules(api_client, account):
    app = await _app(account)
    await account.post("/v1/auth/logout")

    intruder = await register_account(api_client, name="Intruder")
    response = await intruder.post(
        "/v1/postback-rules",
        json={
            "app_id": app["id"],
            "name": "Stolen",
            "trigger_event": "purchase",
            "url_template": "https://evil.example/c",
        },
    )
    assert response.status_code == 404


async def test_delivery_listing_is_bounded(account):
    """This is the busiest write target in the system; an unpaginated listing
    gets slower every day until someone notices."""
    response = await account.client.get("/v1/postback-deliveries?limit=500")
    assert response.status_code == 422

    ok = await account.client.get("/v1/postback-deliveries?limit=50")
    assert ok.status_code == 200


async def test_only_a_failed_delivery_can_be_retried(account, owner_conn, seeded_app):
    """Re-queueing something in flight would let an impatient click produce the
    duplicate conversion the delivery design exists to prevent."""
    from mmp_core.ids import uuid7

    app = await _app(account)
    rule = (await _rule(account, app)).json()

    # An in-flight delivery, as a worker would leave it mid-send.
    delivery_id = uuid7()
    org_id = (await account.client.get("/v1/organizations")).json()[0]["id"]
    await owner_conn.execute(
        """INSERT INTO postback_deliveries (id, organization_id, postback_rule_id,
                                            event_id, status, attempt_count, created_at)
           VALUES ($1, $2, $3, $4, 'in_flight', 1, now())""",
        delivery_id,
        uuid7().__class__(org_id),
        uuid7().__class__(rule["id"]),
        uuid7(),
    )

    response = await account.post(f"/v1/postback-deliveries/{delivery_id}/retry")
    assert response.status_code == 409

    await owner_conn.execute(
        "UPDATE postback_deliveries SET status = 'failed' WHERE id = $1", delivery_id
    )
    retried = await account.post(f"/v1/postback-deliveries/{delivery_id}/retry")
    assert retried.status_code == 200
    assert retried.json()["status"] == "failed"


async def test_success_codes_survive_the_round_trip(account, owner_conn):
    """asyncpg returns jsonb as a string, and the failure mode is quiet.

    `list("[200, 201]")` yields `['[', '2', '0', ...]`, so a status check
    against it silently never matches and every delivery is recorded as failed.
    Asserted both through the API and at the point the worker reads it, because
    those are two different call sites and only one of them had the bug.
    """
    from mmp_db.jsonfields import decode_list

    app = await _app(account)
    rule = (await _rule(account, app, success_status_codes=[200, 204, 302])).json()
    assert rule["success_status_codes"] == [200, 204, 302]

    listing = await account.client.get("/v1/postback-rules")
    assert listing.json()[0]["success_status_codes"] == [200, 204, 302]

    # And as the delivery worker reads it, straight from asyncpg.
    raw = await owner_conn.fetchval(
        "SELECT success_status_codes FROM postback_rules WHERE id = $1",
        __import__("uuid").UUID(rule["id"]),
    )
    codes = decode_list(raw)
    assert codes == [200, 204, 302]
    assert 200 in codes, "the check the worker actually performs"


def test_json_decoding_is_idempotent_and_typed():
    """Safe to apply anywhere, including to a column whose representation
    changes later."""
    import pytest as _pytest
    from mmp_db.jsonfields import decode, decode_list

    assert decode("[1, 2]") == [1, 2]
    assert decode([1, 2]) == [1, 2]
    assert decode(None) is None
    assert decode_list(None) == []
    with _pytest.raises(TypeError):
        decode_list('{"not": "a list"}')


async def test_rule_headers_sealed_after_a_rotation_can_still_be_listed(account, api_client):
    """The same key-version bug as webhooks, in the other table that seals.

    A rule's headers commonly carry a partner's bearer token. Sealed under a
    rotated master key and read back as version 1, they cannot be opened —
    and `_header_names` answers an unopenable header set with an empty list, so
    the failure shows up as headers quietly vanishing from the UI rather than as
    an error.
    """
    import os

    from mmp_crypto.envelope import LocalMasterKeyProvider

    app_context = api_client._transport.app  # type: ignore[attr-defined]
    rotated = LocalMasterKeyProvider({1: os.urandom(32), 2: os.urandom(32)}, current_version=2)
    app_context.state.context.master_keys = rotated

    app = await _app(account)
    created = await _rule(account, app, headers={"Authorization": "Bearer partner-token"})
    assert created.status_code == 201, created.text

    assert created.json()["header_names"] == ["Authorization"], (
        "headers sealed under a rotated key must still be listable"
    )
    assert "partner-token" not in created.text

    listing = await account.client.get("/v1/postback-rules")
    assert listing.json()[0]["header_names"] == ["Authorization"]


# --- partner sub parameters and campaign scope -------------------------------
async def _campaign(account, app, name="Partner A"):
    response = await account.post(
        "/v1/campaigns",
        json={"app_id": app["id"], "name": name, "source": name.lower().replace(" ", "_")},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_sub_parameters_are_refused_in_an_app_wide_rule(account):
    """sub1 is typically a partner's own click id. An app-wide rule fires for
    every partner's installs, so it would send one partner's ids to another."""
    app = await _app(account, package="com.example.subunscoped")
    response = await _rule(account, app, url_template="https://example.com/pb?clickid={{sub1}}")
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "{{sub1}}" in detail and "scoped to one campaign" in detail


async def test_sub_parameters_are_refused_in_the_body_too(account):
    app = await _app(account, package="com.example.subbody")
    response = await _rule(
        account,
        app,
        method="POST",
        url_template="https://example.com/pb",
        body_template='{"click": "{{sub2}}"}',
    )
    assert response.status_code == 422
    assert "{{sub2}}" in response.json()["detail"]


async def test_a_campaign_scoped_rule_may_use_them(account):
    app = await _app(account, package="com.example.subscoped")
    campaign = await _campaign(account, app)
    response = await _rule(
        account,
        app,
        campaign_id=campaign["id"],
        url_template="https://example.com/pb?clickid={{sub1}}&pub={{sub2}}",
    )
    assert response.status_code == 201, response.text
    assert response.json()["campaign_id"] == campaign["id"]


async def test_a_campaign_from_another_app_is_refused(account):
    app = await _app(account, package="com.example.subapp1")
    other = await _app(account, name="Other", package="com.example.subapp2")
    foreign = await _campaign(account, other, name="Elsewhere")
    response = await _rule(
        account, app, campaign_id=foreign["id"], url_template="https://example.com/pb"
    )
    assert response.status_code == 422
    assert "campaign not found" in response.json()["detail"]


async def test_widening_a_sub_rule_to_every_campaign_is_refused(account):
    """The unscoped case arrived at in two steps: create scoped, then clear the
    campaign. The update validates the rule as it will be, not as it was."""
    app = await _app(account, package="com.example.subwiden")
    campaign = await _campaign(account, app)
    created = await _rule(
        account,
        app,
        campaign_id=campaign["id"],
        url_template="https://example.com/pb?clickid={{sub1}}",
    )
    assert created.status_code == 201, created.text

    widened = await account.patch(
        f"/v1/postback-rules/{created.json()['id']}", json={"campaign_id": None}
    )
    assert widened.status_code == 422
    assert "scoped to one campaign" in widened.json()["detail"]


async def test_the_variables_endpoint_names_the_scoped_ones(account):
    response = await account.client.get("/v1/postback-rules/variables")
    assert response.status_code == 200
    body = response.json()
    assert {"sub1", "sub2", "sub3", "click_id"} <= set(body["variables"])
    assert body["campaign_scoped_only"] == ["sub1", "sub2", "sub3"]


async def test_the_preview_shows_what_sub1_will_look_like(account):
    app = await _app(account, package="com.example.subpreview")
    campaign = await _campaign(account, app)
    created = await _rule(
        account,
        app,
        campaign_id=campaign["id"],
        url_template="https://example.com/pb?clickid={{sub1}}",
    )
    preview = await account.client.get(f"/v1/postback-rules/{created.json()['id']}/preview")
    assert preview.status_code == 200
    assert "clickid=partner-click-" in preview.json()["url"]
