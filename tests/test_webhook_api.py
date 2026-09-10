"""Webhook configuration and delivery."""

from __future__ import annotations

import pytest

from tests.conftest_api import register_account


async def _webhook(account, **overrides):
    body = {"url": "https://example.com/hooks/mmp", "events": ["purchase", "install"]}
    body.update(overrides)
    return await account.post("/v1/webhooks", json=body)


async def test_create_returns_the_secret_once(account):
    created = await _webhook(account)
    assert created.status_code == 201, created.text
    body = created.json()

    assert body["signing_secret"].startswith("whsec_")
    assert body["events"] == ["install", "purchase"], "events are normalised and sorted"
    assert body["enabled"]
    # Shipped alongside the secret so verification is written correctly the
    # first time. A signature nobody verifies does nothing.
    assert "hmac.compare_digest" in body["verification_example"]

    listing = await account.client.get("/v1/webhooks")
    assert listing.status_code == 200
    assert body["signing_secret"] not in listing.text, (
        "the secret must never appear again after creation"
    )
    assert "signing_secret" not in listing.json()[0]


async def test_rotation_issues_a_new_secret(account):
    created = (await _webhook(account)).json()
    rotated = await account.post(f"/v1/webhooks/{created['id']}/rotate")

    assert rotated.status_code == 200
    assert rotated.json()["signing_secret"] != created["signing_secret"]
    assert rotated.json()["id"] == created["id"]


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/hooks",
        "https://169.254.169.254/hooks",
        "https://127.0.0.1/hooks",
        "https://10.0.0.1/hooks",
    ],
)
async def test_bad_destinations_are_refused(account, url):
    """We post conversion data here. Over http anyone on the path reads a
    customer's revenue and can forge deliveries into their systems."""
    response = await _webhook(account, url=url)
    assert response.status_code == 422


async def test_unknown_events_are_refused_with_the_list(account):
    """A typo must not silently subscribe to nothing."""
    response = await _webhook(account, events=["purchase", "purchsae"])
    assert response.status_code == 422
    detail = response.text
    assert "purchsae" in detail
    assert "install" in detail, "the error should say what is available"


async def test_reenabling_resets_the_failure_count(account, owner_conn):
    """Otherwise a fixed endpoint is disabled again by its next single failure,
    because it is still sitting at the threshold."""
    from mmp_providers.webhooks import MAX_CONSECUTIVE_FAILURES

    created = (await _webhook(account)).json()
    await owner_conn.execute(
        "UPDATE webhooks SET consecutive_failures = $2, enabled = false, "
        "disabled_at = now() WHERE id = $1",
        __import__("uuid").UUID(created["id"]),
        MAX_CONSECUTIVE_FAILURES,
    )

    response = await account.patch(f"/v1/webhooks/{created['id']}", json={"enabled": True})
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"]
    assert body["consecutive_failures"] == 0
    assert body["disabled_at"] is None


async def test_webhooks_are_deleted_not_disabled(account):
    """Unlike a postback rule, this is the customer's own integration — nothing
    downstream needs to explain it later, and a disabled row holding an
    encrypted secret serves nobody."""
    created = (await _webhook(account)).json()
    assert (await account.delete(f"/v1/webhooks/{created['id']}")).status_code == 204
    assert (await account.client.get("/v1/webhooks")).json() == []


async def test_another_tenant_cannot_see_or_touch_a_webhook(api_client, account):
    created = (await _webhook(account)).json()
    await account.post("/v1/auth/logout")

    intruder = await register_account(api_client, name="Intruder")
    assert (await intruder.client.get("/v1/webhooks")).json() == []
    assert (await intruder.delete(f"/v1/webhooks/{created['id']}")).status_code == 404
    assert (await intruder.post(f"/v1/webhooks/{created['id']}/rotate")).status_code == 404


async def test_available_events_are_published(account):
    response = await account.client.get("/v1/webhooks/events")
    assert response.status_code == 200
    assert "purchase" in response.json()["events"]


# --- signing ------------------------------------------------------------
def test_a_customer_can_verify_our_signature():
    """The verification snippet we hand customers must actually work.

    It lives next to the signing code so the two cannot drift — a documented
    scheme that no longer matches the implementation is worse than none, because
    the customer's verification silently rejects everything.
    """
    import hmac
    import time
    from hashlib import sha256

    from mmp_providers.webhooks import build_payload, signed_headers

    secret = "whsec_example"
    payload = build_payload(event_type="purchase", data={"revenue_minor": 1999})
    body = payload.encode()
    headers = signed_headers(
        payload=body, secret=secret, url_path="/hooks/mmp", delivery_id=payload.id
    )

    # Exactly the algorithm from verification_snippet, transcribed by hand — if
    # this drifts from the snippet, one of them is wrong.
    timestamp = headers["x-mmp-timestamp"]
    assert abs(time.time() - int(timestamp)) < 300
    canonical = "\n".join(
        ["v1", "POST", "/hooks/mmp", timestamp, sha256(body).hexdigest()]
    ).encode()
    expected = hmac.new(secret.encode(), canonical, sha256).hexdigest()
    presented = headers["x-mmp-signature"].removeprefix("v1=")
    assert hmac.compare_digest(expected, presented)


def test_a_tampered_payload_fails_verification():
    import hmac
    from hashlib import sha256

    from mmp_providers.webhooks import build_payload, signed_headers

    secret = "whsec_example"
    payload = build_payload(event_type="purchase", data={"revenue_minor": 1999})
    headers = signed_headers(
        payload=payload.encode(),
        secret=secret,
        url_path="/hooks/mmp",
        delivery_id=payload.id,
    )
    tampered = payload.encode().replace(b"1999", b"9999")

    canonical = "\n".join(
        ["v1", "POST", "/hooks/mmp", headers["x-mmp-timestamp"], sha256(tampered).hexdigest()]
    ).encode()
    expected = hmac.new(secret.encode(), canonical, sha256).hexdigest()
    assert not hmac.compare_digest(expected, headers["x-mmp-signature"].removeprefix("v1="))


def test_delivery_id_is_echoed_for_receiver_side_deduplication():
    """At-least-once is the only thing an outbound delivery system can honestly
    promise, so the receiver needs a key to deduplicate on."""
    from mmp_providers.webhooks import build_payload, signed_headers

    payload = build_payload(event_type="install", data={})
    headers = signed_headers(
        payload=payload.encode(),
        secret="whsec_x",
        url_path="/h",
        delivery_id=payload.id,
    )
    assert headers["x-mmp-delivery-id"] == payload.id


def test_auto_disable_threshold():
    from mmp_providers.webhooks import MAX_CONSECUTIVE_FAILURES, should_disable

    assert not should_disable(MAX_CONSECUTIVE_FAILURES - 1)
    assert should_disable(MAX_CONSECUTIVE_FAILURES)


async def test_a_webhook_created_after_a_rotation_records_the_new_key_version(
    account, api_client, owner_conn
):
    """The seal side of the same bug.

    `seal()` reports which master key wrapped the data key. The create path used
    to discard it, so a webhook sealed under a rotated key was recorded as
    version 1 and could never be opened again — the sender abandons the
    delivery rather than send it unsigned, and the endpoint silently stops
    receiving anything.

    Nothing else in the suite can reach version 2: the local provider is built
    with a single key, which is exactly why the assumption went unnoticed.
    """
    import os
    import uuid as _uuid

    from mmp_crypto.envelope import (
        LocalMasterKeyProvider,
        SealedSecret,
        open_sealed,
        organization_aad,
    )

    app = api_client._transport.app  # type: ignore[attr-defined]
    rotated = LocalMasterKeyProvider({1: os.urandom(32), 2: os.urandom(32)}, current_version=2)
    app.state.context.master_keys = rotated

    created = await _webhook(account)
    assert created.status_code == 201, created.text
    secret = created.json()["signing_secret"]

    row = await owner_conn.fetchrow(
        "SELECT secret_ciphertext, secret_nonce, wrapped_dek, key_version"
        " FROM webhooks WHERE id = $1",
        _uuid.UUID(created.json()["id"]),
    )
    assert row["key_version"] == 2, (
        "the stored version must be the one that sealed it, not a default"
    )

    # And it opens, which is the thing the sender needs to be able to do.
    opened = open_sealed(
        SealedSecret(
            ciphertext=bytes(row["secret_ciphertext"]),
            nonce=bytes(row["secret_nonce"]),
            wrapped_dek=bytes(row["wrapped_dek"]),
            key_version=int(row["key_version"]),
        ),
        provider=rotated,
        aad=organization_aad(_uuid.UUID(account.organization["id"])),
    ).decode()
    assert opened == secret
