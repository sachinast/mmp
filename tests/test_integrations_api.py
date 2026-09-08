"""Configuring a provider integration.

The value over a bare postback rule: a missing credential or an unmapped event
is rejected when the integration is saved, rather than discovered as a delivery
that never arrives.
"""

from __future__ import annotations

import pytest

from tests.conftest_api import register_account


async def _integration(account, **overrides):
    body = {
        "provider": "s2s_json",
        "name": "Network A",
        "credentials": {"api_token": "secret-token-value"},
        "configuration": {"endpoint": "https://network.example/conversions"},
    }
    body.update(overrides)
    return await account.post("/v1/integrations", json=body)


async def test_the_provider_catalogue_is_read_from_the_registry(account):
    """So a deployment cannot advertise an integration it cannot perform."""
    response = await account.client.get("/v1/providers")
    assert response.status_code == 200
    names = {p["name"] for p in response.json()}
    assert {"custom", "s2s_json"} <= names

    json_provider = next(p for p in response.json() if p["name"] == "s2s_json")
    assert json_provider["auth_style"] == "bearer"
    assert "revenue" in json_provider["capabilities"]
    assert json_provider["event_map"]["signup"] == "complete_registration"


async def test_creating_an_integration(account):
    response = await _integration(account)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["provider"] == "s2s_json"
    assert body["status"] == "active"
    assert body["configuration"]["endpoint"] == "https://network.example/conversions"


async def test_credentials_are_never_returned(account):
    """Write-only, like every other stored secret here."""
    created = await _integration(account)
    assert "secret-token-value" not in created.text
    assert created.json()["credential_names"] == ["api_token"]

    listing = await account.client.get("/v1/integrations")
    assert "secret-token-value" not in listing.text
    assert "credentials" not in listing.json()[0]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"credentials": {}}, "api_token"),
        ({"configuration": {"endpoint": "http://network.example/c"}}, "https"),
        ({"configuration": {}}, "endpoint"),
        (
            {"configuration": {"endpoint": "https://n.example/c", "event_map": {"purchsae": "x"}}},
            "purchsae",
        ),
    ],
)
async def test_the_adapter_validates_at_save_time(account, payload, expected):
    """A delivery that never arrives is a much worse way to learn this."""
    response = await _integration(account, **payload)
    assert response.status_code == 422
    assert expected in response.text


async def test_every_problem_is_reported_at_once(account):
    """One per submission is a form nobody finishes."""
    response = await _integration(
        account, credentials={}, configuration={"endpoint": "http://n.example/c"}
    )
    assert response.status_code == 422
    problems = response.json()["detail"]["problems"]
    assert len(problems) >= 2


async def test_an_unknown_provider_is_refused_with_the_list(account):
    response = await _integration(account, provider="a-network-we-do-not-have")
    assert response.status_code == 422
    assert "custom" in response.text, "the error should say what is available"


async def test_capabilities_are_published_per_integration(account):
    """Lets a dashboard refuse to attach a refund rule to a provider that does
    not take refunds."""
    created = (await _integration(account)).json()
    response = await account.client.get(f"/v1/integrations/{created['id']}/capabilities")
    assert response.status_code == 200
    body = response.json()
    assert "refunds" in body["capabilities"]
    assert body["requires_attribution"] is True
    assert "purchase" in body["accepts_events"]


async def test_updating_revalidates(account):
    """A configuration change that breaks the integration must fail here rather
    than at the next conversion."""
    created = (await _integration(account)).json()
    response = await account.patch(
        f"/v1/integrations/{created['id']}",
        json={"configuration": {"endpoint": "http://insecure.example/c"}},
    )
    assert response.status_code == 422
    assert "https" in response.text


async def test_integrations_are_admin_only(api_client, account):
    """An integration list is a list of which networks an advertiser works
    with, which is commercially sensitive even without the credentials."""
    await _integration(account)
    await account.post("/v1/auth/logout")

    intruder = await register_account(api_client, name="Outsider")
    listing = await intruder.client.get("/v1/integrations")
    assert listing.status_code == 200
    assert listing.json() == [], "another tenant sees none of them"


async def test_deleting_an_integration_leaves_its_rules_alone(account, owner_conn):
    """Removing an integration should not silently delete the postback rules an
    advertiser configured — a rule with no provider still works."""
    app = (
        await account.post(
            "/v1/apps",
            json={
                "name": "Integ App",
                "platform": "android",
                "android_package_name": "com.example.integ",
            },
        )
    ).json()
    integration = (await _integration(account)).json()

    rule = await account.post(
        "/v1/postback-rules",
        json={
            "app_id": app["id"],
            "name": "Via provider",
            "trigger_event": "purchase",
            # A resolvable host: destinations are validated when a rule is
            # saved, so example.invalid would be refused here — correctly.
            "url_template": "https://example.com/c?click={{click_id}}",
        },
    )
    assert rule.status_code == 201
    import uuid as _uuid

    await owner_conn.execute(
        "UPDATE postback_rules SET provider_integration_id = $2 WHERE id = $1",
        _uuid.UUID(rule.json()["id"]),
        _uuid.UUID(integration["id"]),
    )

    assert (await account.delete(f"/v1/integrations/{integration['id']}")).status_code == 204

    surviving = await owner_conn.fetchrow(
        "SELECT enabled, provider_integration_id FROM postback_rules WHERE id = $1",
        _uuid.UUID(rule.json()["id"]),
    )
    assert surviving is not None, "the rule must survive"
    assert surviving["enabled"]
    assert surviving["provider_integration_id"] is None


async def test_integration_changes_are_audited(account, owner_conn):
    import uuid as _uuid

    org_id = (await account.client.get("/v1/organizations")).json()[0]["id"]
    created = (await _integration(account)).json()
    await account.delete(f"/v1/integrations/{created['id']}")

    actions = [
        row["action"]
        for row in await owner_conn.fetch(
            "SELECT action FROM audit_log WHERE organization_id = $1",
            _uuid.UUID(org_id),
        )
    ]
    assert "integration.created" in actions
    assert "integration.deleted" in actions
