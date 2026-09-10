"""The privacy API, and the audit trail that evidences it.

"We deleted it" is a claim that has to stand up later.
"""

from __future__ import annotations

import pytest

from tests.conftest_api import register_account


async def _app(account, package="com.example.privacy"):
    response = await account.post(
        "/v1/apps",
        json={"name": "Privacy App", "platform": "android", "android_package_name": package},
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- consent ------------------------------------------------------------
async def test_recording_consent_and_reading_it_back(account):
    app = await _app(account)
    created = await account.post(
        "/v1/privacy/consent",
        json={
            "app_id": app["id"],
            "anonymous_id": "dev-1",
            "purpose": "attribution",
            "state": "denied",
            "source": "cmp",
        },
    )
    assert created.status_code == 201, created.text

    listing = await account.client.get(f"/v1/privacy/consent?app_id={app['id']}&anonymous_id=dev-1")
    assert listing.status_code == 200
    assert listing.json()[0]["state"] == "denied"


async def test_recording_consent_twice_updates_rather_than_duplicates(account):
    """A person changing their mind must not leave two contradictory records."""
    app = await _app(account)
    body = {"app_id": app["id"], "anonymous_id": "dev-2", "purpose": "attribution"}

    await account.post("/v1/privacy/consent", json={**body, "state": "granted"})
    await account.post("/v1/privacy/consent", json={**body, "state": "denied"})

    listing = await account.client.get(f"/v1/privacy/consent?app_id={app['id']}&anonymous_id=dev-2")
    records = listing.json()
    assert len(records) == 1
    assert records[0]["state"] == "denied"


async def test_a_withdrawal_clears_the_tracker_cache(account, api_client):
    """The tracker caches consent for five minutes. A withdrawal must take
    effect now, not when that expires — which is exactly the delay a person
    exercising their rights does not expect."""
    from redis.asyncio import Redis

    from tests.conftest_api import TEST_REDIS_URL

    app = await _app(account, package="com.example.cache")
    redis = Redis.from_url(TEST_REDIS_URL, decode_responses=False)
    # Derived from the same helper the API and the tracker use, not a third
    # copy of the format. Asserting against a literal made this test agree with
    # itself: a rename could have stopped the invalidation matching the cache
    # and nothing here would have failed.
    from mmp_ingest.consent import consent_cache_key

    cache_key = consent_cache_key(app["id"], "dev-3")
    await redis.set(cache_key, b"stale")

    await account.post(
        "/v1/privacy/consent",
        json={
            "app_id": app["id"],
            "anonymous_id": "dev-3",
            "purpose": "attribution",
            "state": "denied",
        },
    )
    assert await redis.get(cache_key) is None
    await redis.aclose()


async def test_consent_listing_requires_a_device(account):
    """An unfiltered listing is a listing of every device in the app."""
    app = await _app(account)
    response = await account.client.get(f"/v1/privacy/consent?app_id={app['id']}")
    assert response.status_code == 422


async def test_another_tenants_app_is_invisible(api_client, account, seeded_app):
    response = await account.client.get(
        f"/v1/privacy/consent?app_id={seeded_app['app_id']}&anonymous_id=x"
    )
    assert response.status_code == 404


# --- erasure ------------------------------------------------------------
async def test_erasure_removes_the_data_and_reports_what_it_removed(account, owner_conn):
    import datetime as dt

    from mmp_core.ids import uuid7

    app = await _app(account, package="com.example.erase")
    org_id = (await account.client.get("/v1/organizations")).json()[0]["id"]
    now = dt.datetime.now(dt.UTC)

    for _ in range(3):
        await owner_conn.execute(
            """INSERT INTO events (event_id, received_at, occurred_at, organization_id,
                                   app_id, event_name, anonymous_id, platform)
               VALUES ($1, $2, $2, $3, $4, 'install', 'erase-dev', 1)""",
            uuid7(),
            now,
            uuid7().__class__(org_id),
            uuid7().__class__(app["id"]),
        )

    response = await account.post(
        "/v1/privacy/erasure",
        json={"app_id": app["id"], "anonymous_id": "erase-dev"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted"]["events"] == 3
    assert body["total_deleted"] == 3
    assert body["completed_at"] is not None

    remaining = await owner_conn.fetchval(
        "SELECT count(*) FROM events WHERE anonymous_id = 'erase-dev'"
    )
    assert remaining == 0


async def test_erasure_is_admin_only(api_client, account):
    """Destructive and irreversible. A viewer's job is to read reports."""
    app = await _app(account, package="com.example.viewererase")
    await account.post("/v1/auth/logout")

    intruder = await register_account(api_client, name="Outsider")
    response = await intruder.post(
        "/v1/privacy/erasure",
        json={"app_id": app["id"], "anonymous_id": "x"},
    )
    assert response.status_code in (403, 404)


async def test_a_malformed_device_hash_is_rejected(account):
    app = await _app(account, package="com.example.badhash")
    response = await account.post(
        "/v1/privacy/erasure",
        json={"app_id": app["id"], "anonymous_id": "x", "device_hash_hex": "not-hex"},
    )
    assert response.status_code == 422


# --- audit --------------------------------------------------------------
async def test_privacy_actions_are_audited(account, owner_conn):
    """'We deleted it' is a claim that has to stand up later."""
    app = await _app(account, package="com.example.audited")
    org_id = (await account.client.get("/v1/organizations")).json()[0]["id"]

    await account.post(
        "/v1/privacy/consent",
        json={
            "app_id": app["id"],
            "anonymous_id": "audited-dev",
            "purpose": "attribution",
            "state": "denied",
        },
    )
    await account.post(
        "/v1/privacy/erasure",
        json={"app_id": app["id"], "anonymous_id": "audited-dev"},
    )

    actions = [
        row["action"]
        for row in await owner_conn.fetch(
            "SELECT action FROM audit_log WHERE organization_id = $1 ORDER BY created_at",
            __import__("uuid").UUID(org_id),
        )
    ]
    assert "consent.recorded" in actions
    assert "erasure.requested" in actions
    assert "erasure.completed" in actions, (
        "both the request and the completion, so a partial failure is visible"
    )


async def test_the_audit_chain_verifies(account):
    app = await _app(account, package="com.example.chain")
    for index in range(5):
        await account.post(
            "/v1/privacy/consent",
            json={
                "app_id": app["id"],
                "anonymous_id": f"dev-{index}",
                "purpose": "analytics",
                "state": "granted",
            },
        )

    response = await account.client.get("/v1/privacy/audit/verify")
    assert response.status_code == 200
    body = response.json()
    assert body["intact"]
    assert body["entries"] >= 5
    assert "tamper-evident" in body["note"].lower(), (
        "the limit must be stated wherever the result is reported"
    )


async def test_tampering_breaks_the_chain(account, owner_conn):
    """The property the chain exists for."""
    app = await _app(account, package="com.example.tamper")
    org_id = __import__("uuid").UUID(
        (await account.client.get("/v1/organizations")).json()[0]["id"]
    )
    for index in range(3):
        await account.post(
            "/v1/privacy/consent",
            json={
                "app_id": app["id"],
                "anonymous_id": f"t-{index}",
                "purpose": "analytics",
                "state": "granted",
            },
        )

    assert (await account.client.get("/v1/privacy/audit/verify")).json()["intact"]

    # Alter one entry's detail, exactly as someone covering their tracks would.
    victim = await owner_conn.fetchval(
        "SELECT id FROM audit_log WHERE organization_id = $1 ORDER BY created_at LIMIT 1",
        org_id,
    )
    await owner_conn.execute(
        'UPDATE audit_log SET detail = \'{"state": "granted"}\'::jsonb WHERE id = $1',
        victim,
    )

    verification = (await account.client.get("/v1/privacy/audit/verify")).json()
    assert not verification["intact"]
    assert verification["broken_at"] == str(victim)


async def test_verification_is_owner_only(api_client, account):
    """A non-owner member of the *same* organisation must be refused.

    The first version of this test registered a second account and asserted it
    was refused — but a fresh account owns its own organisation, so it passed
    for the wrong reason and would have passed with no role check at all.
    """
    import httpx

    from tests.conftest_api import register_account

    member = await register_account(api_client, name="Member")
    member_email, member_password = member.email, member.password
    await member.post("/v1/auth/logout")

    await api_client.post(
        "/v1/auth/login", json={"email": account.email, "password": account.password}
    )
    account.client.cookies = api_client.cookies
    added = await account.post(
        "/v1/organizations/members", json={"email": member_email, "role": "viewer"}
    )
    assert added.status_code == 201, added.text
    org_id = account.organization["id"]
    await account.post("/v1/auth/logout")

    async with httpx.AsyncClient(transport=api_client._transport, base_url="http://api") as client:
        await client.post(
            "/v1/auth/login", json={"email": member_email, "password": member_password}
        )
        csrf = client.cookies["mmp_csrf"]
        switched = await client.post(
            f"/v1/organizations/{org_id}/switch", headers={"x-csrf-token": csrf}
        )
        assert switched.status_code == 200

        response = await client.get("/v1/privacy/audit/verify")
        assert response.status_code == 403, "a viewer must not verify the audit chain"


def test_unknown_audit_actions_are_rejected():
    """An audit log that accepts any string is one nobody can query, and the
    failure shows up when someone needs it most."""
    import asyncio

    from mmp_db.audit import ACTIONS, record

    assert "erasure.completed" in ACTIONS
    with pytest.raises(ValueError, match="unknown audit action"):
        asyncio.run(
            record(
                None,  # type: ignore[arg-type]
                organization_id=__import__("uuid").uuid4(),
                action="something.invented",
                resource_type="x",
            )
        )
