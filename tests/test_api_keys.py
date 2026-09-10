"""API keys: the raw credential leaves this system exactly once.

Everything here is written to fail if that stops being true — in the response
body, in the database, in the logs, or in a repr.
"""

from __future__ import annotations

import pytest
from mmp_crypto.keys import generate_key, parse_key, verify_key

from tests.conftest_api import register_account


async def _app_and_key(account):
    app = (
        await account.post(
            "/v1/apps",
            json={
                "name": "Keyed App",
                "platform": "android",
                "android_package_name": "com.example.keys",
            },
        )
    ).json()
    created = await account.post(f"/v1/apps/{app['id']}/keys", json={"environment": "prod"})
    assert created.status_code == 201, created.text
    return app, created.json()


async def test_key_is_returned_once_and_never_again(account):
    app, created = await _app_and_key(account)
    raw = created["api_key"]
    assert raw.startswith("mmp_live_")

    listing = await account.client.get(f"/v1/apps/{app['id']}/keys")
    assert listing.status_code == 200
    for key in listing.json():
        assert "api_key" not in key, "a listing must never carry the credential"
        assert raw not in str(key)
        assert key["key_prefix"] in raw


async def test_only_the_hash_reaches_the_database(account, owner_conn):
    """A database dump must not be a credential dump."""
    _app, created = await _app_and_key(account)
    raw = created["api_key"]
    parsed = parse_key(raw)
    assert parsed is not None

    row = await owner_conn.fetchrow(
        "SELECT key_prefix, key_hash FROM api_keys WHERE key_prefix = $1", parsed.prefix
    )
    assert row is not None
    stored = bytes(row["key_hash"])
    assert isinstance(stored, bytes) and len(stored) == 32
    assert parsed.secret.encode() not in stored
    assert raw.encode() not in stored

    # And nothing anywhere in the row resembles the secret.
    full = await owner_conn.fetchrow("SELECT * FROM api_keys WHERE key_prefix = $1", parsed.prefix)
    assert parsed.secret not in " ".join(str(v) for v in full.values())


async def test_raw_key_never_appears_in_a_log_line(account, capsys):
    """The audit trail identifies a key by prefix, which is not a credential.

    Asserted against real stdout rather than a structlog capture: the loggers
    are cached at import with their processor chain, so reconfiguring mid-test
    would inspect a pipeline nothing is actually writing through.
    """
    capsys.readouterr()
    _app, created = await _app_and_key(account)
    output = capsys.readouterr().out

    raw = created["api_key"]
    parsed = parse_key(raw)
    assert parsed is not None
    assert parsed.secret not in output, "the key secret was written to a log"
    assert raw not in output
    assert "api_key_created" in output, "key creation must be auditable"
    assert created["key_prefix"] in output, "the audit line identifies the key by prefix"


def test_generated_key_repr_is_redacted():
    """A traceback or a debug print must not spill the credential."""
    generated = generate_key(environment="prod", pepper="p" * 64)
    assert generated.raw not in repr(generated)
    assert generated.raw not in str(generated)
    assert generated.prefix in repr(generated)


def test_verification_requires_the_pepper():
    """A stolen database is not enough — the pepper lives in KMS."""
    generated = generate_key(environment="dev", pepper="pepper-one" * 8)
    parsed = parse_key(generated.raw)
    assert verify_key(
        presented_secret=parsed.secret, stored_hash=generated.key_hash, pepper="pepper-one" * 8
    )
    assert not verify_key(
        presented_secret=parsed.secret, stored_hash=generated.key_hash, pepper="pepper-two" * 8
    )


def test_environment_is_visible_in_the_key():
    """A test key pasted into production config should be obvious on sight."""
    assert generate_key(environment="dev", pepper="p" * 64).raw.startswith("mmp_test_")
    assert generate_key(environment="prod", pepper="p" * 64).raw.startswith("mmp_live_")


@pytest.mark.parametrize("bad", ["", "garbage", "mmp_live_only-three", "xxx_live_a_b"])
def test_malformed_keys_are_rejected_without_raising(bad):
    assert parse_key(bad) is None


def test_every_generated_key_parses_back():
    """Regression: the first implementation used base64url, whose alphabet
    contains the '_' delimiter. 55% of keys could not be split back apart —
    and nothing noticed, because generation and parsing were tested separately.
    """
    for environment in ("dev", "prod"):
        for _ in range(500):
            generated = generate_key(environment=environment, pepper="p" * 64)
            parsed = parse_key(generated.raw)
            assert parsed is not None, f"unparseable key: {generated.raw}"
            assert parsed.prefix == generated.prefix
            assert parsed.environment == environment
            assert verify_key(
                presented_secret=parsed.secret,
                stored_hash=generated.key_hash,
                pepper="p" * 64,
            )


def test_key_secret_has_enough_entropy():
    """A fast hash is only safe because there is nothing feasible to guess."""
    from mmp_crypto.keys import secret_entropy_bits

    assert secret_entropy_bits() >= 128


def test_two_keys_never_collide():
    keys = {generate_key(environment="dev", pepper="p" * 64).raw for _ in range(2000)}
    assert len(keys) == 2000


async def test_rotation_overlaps_rather_than_gaps(account, owner_conn):
    """An SDK in the field cannot swap credentials atomically.

    Rotation must therefore issue the replacement before revoking the original,
    so there is never a moment with zero working keys.
    """
    app, created = await _app_and_key(account)
    original_prefix = created["key_prefix"]

    rotated = await account.post(f"/v1/apps/{app['id']}/keys/{created['id']}/rotate")
    assert rotated.status_code == 200
    new = rotated.json()
    assert new["api_key"] != created["api_key"]
    assert new["key_prefix"] != original_prefix

    rows = await owner_conn.fetch(
        "SELECT key_prefix, status FROM api_keys WHERE key_prefix = ANY($1::text[])",
        [original_prefix, new["key_prefix"]],
    )
    states = {row["key_prefix"]: row["status"] for row in rows}
    assert states[original_prefix] == "revoked"
    assert states[new["key_prefix"]] == "active"


async def test_revoked_key_is_evicted_from_the_tracker_cache(account, api_client):
    """Revocation that waits for a cache TTL is not revocation."""
    from redis.asyncio import Redis

    from tests.conftest_api import TEST_REDIS_URL

    app, created = await _app_and_key(account)
    redis = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    cache_key = f"apikey:{created['key_prefix']}"
    await redis.set(cache_key, "cached-auth-record")

    response = await account.delete(f"/v1/apps/{app['id']}/keys/{created['id']}")
    assert response.status_code == 204
    assert await redis.get(cache_key) is None, "the cached credential outlived its revocation"
    await redis.aclose()


async def test_viewer_cannot_list_keys(api_client, account):
    """Key enumeration plus revocation is a way to take tracking offline."""
    app, _created = await _app_and_key(account)
    await account.post("/v1/auth/logout")

    intruder = await register_account(api_client, name="Intruder")
    response = await intruder.client.get(f"/v1/apps/{app['id']}/keys")
    # A foreign app is invisible; the key listing under it is unreachable.
    assert response.status_code in (403, 404)


async def test_cannot_create_a_key_for_another_tenants_app(api_client, account):
    app, _created = await _app_and_key(account)
    await account.post("/v1/auth/logout")

    intruder = await register_account(api_client, name="Intruder")
    response = await intruder.post(f"/v1/apps/{app['id']}/keys", json={"environment": "prod"})
    assert response.status_code == 404


async def test_active_key_count_is_capped(account):
    """An unbounded key list is an unbounded authentication lookup surface."""
    from mmp_api.routes.keys import MAX_ACTIVE_KEYS_PER_APP

    app = (
        await account.post(
            "/v1/apps",
            json={
                "name": "Many Keys",
                "platform": "android",
                "android_package_name": "com.example.many",
            },
        )
    ).json()

    last = None
    for _ in range(MAX_ACTIVE_KEYS_PER_APP + 1):
        last = await account.post(f"/v1/apps/{app['id']}/keys", json={"environment": "dev"})
    assert last.status_code == 409


async def test_disabling_an_app_stops_ingestion_immediately(account, api_client):
    """ "Disabling stops ingestion" has to mean now, not in ten minutes.

    The tracker authenticates against a cached key record carrying the app's
    status. Without clearing those entries a disabled app kept accepting events
    until the cache expired — and the docstring promising otherwise was the only
    thing anyone would have read.
    """
    created = await account.post(
        "/v1/apps",
        json={"name": "Doomed", "platform": "android", "android_package_name": "com.x.doomed"},
    )
    app_id = created.json()["id"]
    key = await account.post(f"/v1/apps/{app_id}/keys", json={"name": "k", "kind": "sdk"})
    prefix = key.json()["key_prefix"]

    context = api_client._transport.app.state.context  # type: ignore[attr-defined]
    # Prime the cache the way an authenticated ingest would.
    await context.redis.set(f"apikey:{prefix}", b"cached", ex=600)
    assert await context.redis.exists(f"apikey:{prefix}")

    disabled = await account.delete(f"/v1/apps/{app_id}")
    assert disabled.status_code == 204, disabled.text

    assert not await context.redis.exists(f"apikey:{prefix}"), (
        "a disabled app's cached keys must be cleared, or it keeps ingesting"
    )


async def test_rotating_a_key_stops_the_old_one_at_once(account, api_client):
    """Rotation revokes, and a revocation the cache has not been told about is
    not a revocation. The old key used to keep working for up to ten minutes."""
    created = await account.post(
        "/v1/apps",
        json={"name": "Rotator", "platform": "ios", "ios_bundle_id": "com.x.rot"},
    )
    app_id = created.json()["id"]
    first = await account.post(f"/v1/apps/{app_id}/keys", json={"name": "k", "kind": "sdk"})
    key_id = first.json()["id"]
    old_prefix = first.json()["key_prefix"]

    context = api_client._transport.app.state.context  # type: ignore[attr-defined]
    await context.redis.set(f"apikey:{old_prefix}", b"cached", ex=600)

    rotated = await account.post(f"/v1/apps/{app_id}/keys/{key_id}/rotate", json={})
    assert rotated.status_code in (200, 201), rotated.text
    assert rotated.json()["key_prefix"] != old_prefix

    assert not await context.redis.exists(f"apikey:{old_prefix}"), (
        "the rotated-away key must stop working at once"
    )
