"""Campaigns and tracking links through the API."""

from __future__ import annotations

import pytest

from tests.conftest_api import register_account


async def _app_and_campaign(account):
    app = (
        await account.post(
            "/v1/apps",
            json={
                "name": "Campaign App",
                "platform": "android",
                "android_package_name": "com.example.campaigns",
            },
        )
    ).json()
    campaign = await account.post(
        "/v1/campaigns",
        json={"app_id": app["id"], "name": "Meta US", "source": "meta", "medium": "cpi"},
    )
    assert campaign.status_code == 201, campaign.text
    return app, campaign.json()


async def test_create_campaign_and_link(account):
    _app, campaign = await _app_and_campaign(account)
    response = await account.post(
        "/v1/tracking-links",
        json={
            "campaign_id": campaign["id"],
            "name": "US Android",
            "fallback_url": "https://example.com/landing",
            "android_url": "https://play.google.com/store/apps/details?id=com.example",
        },
    )
    assert response.status_code == 201, response.text
    link = response.json()
    assert len(link["tracking_code"]) == 22
    assert link["status"] == "active"


async def test_tracking_codes_are_unguessable(account):
    """A code appears in ad creative and browser history — it is effectively
    public. Short or sequential codes let a competitor enumerate an advertiser's
    campaign structure, and let anyone fabricate clicks against a known link."""
    _app, campaign = await _app_and_campaign(account)
    codes = set()
    for index in range(15):
        response = await account.post(
            "/v1/tracking-links",
            json={
                "campaign_id": campaign["id"],
                "name": f"Link {index}",
                "fallback_url": "https://example.com/landing",
            },
        )
        codes.add(response.json()["tracking_code"])

    assert len(codes) == 15
    # ~131 bits of entropy: no shared prefixes, no ordering to exploit.
    assert len({code[:6] for code in codes}) == 15
    assert sorted(codes) != list(codes) or len(codes) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/insecure",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "not-a-url",
    ],
)
async def test_destination_urls_are_validated(account, url):
    """These become 302 Location headers.

    An unvalidated destination is an open redirect wearing our domain's
    reputation — and an http:// one would downgrade a user from a secure page
    and let anyone on the path rewrite the store link we just sent them to.
    """
    _app, campaign = await _app_and_campaign(account)
    response = await account.post(
        "/v1/tracking-links",
        json={"campaign_id": campaign["id"], "name": "Bad", "fallback_url": url},
    )
    assert response.status_code == 422


async def test_cannot_create_a_link_on_another_tenants_campaign(api_client, account):
    _app, campaign = await _app_and_campaign(account)
    await account.post("/v1/auth/logout")

    intruder = await register_account(api_client, name="Intruder")
    response = await intruder.post(
        "/v1/tracking-links",
        json={
            "campaign_id": campaign["id"],
            "name": "Stolen",
            "fallback_url": "https://example.com/x",
        },
    )
    assert response.status_code == 404


async def test_campaign_names_are_unique_per_app(account):
    app, campaign = await _app_and_campaign(account)
    duplicate = await account.post(
        "/v1/campaigns", json={"app_id": app["id"], "name": campaign["name"]}
    )
    assert duplicate.status_code == 409


async def test_links_can_be_disabled_not_deleted(account):
    """Clicks already recorded reference this link. Historical reporting must
    not develop holes because someone tidied up."""
    _app, campaign = await _app_and_campaign(account)
    link = (
        await account.post(
            "/v1/tracking-links",
            json={
                "campaign_id": campaign["id"],
                "name": "Temporary",
                "fallback_url": "https://example.com/landing",
            },
        )
    ).json()

    assert (await account.delete(f"/v1/tracking-links/{link['id']}")).status_code == 204

    listing = await account.client.get("/v1/tracking-links")
    stored = next(item for item in listing.json() if item["id"] == link["id"])
    assert stored["status"] == "disabled", "the row must survive, disabled"


async def test_viewer_cannot_create_links(api_client, account):
    _app, campaign = await _app_and_campaign(account)
    await account.post("/v1/auth/logout")

    intruder = await register_account(api_client, name="Outsider")
    response = await intruder.post(
        "/v1/tracking-links",
        json={
            "campaign_id": campaign["id"],
            "name": "No",
            "fallback_url": "https://example.com/x",
        },
    )
    assert response.status_code in (403, 404)
