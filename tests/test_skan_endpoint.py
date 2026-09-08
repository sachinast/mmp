"""The public SKAdNetwork postback endpoint.

The only unauthenticated write path in the platform. These tests are mostly
about what an anonymous caller cannot achieve with it: forge an install, learn
which apps we have, replay a real postback, or make us do unbounded work.
"""

from __future__ import annotations

import base64
import copy
import json

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from mmp_attrib.skadnetwork import canonical_string

from tests.test_skadnetwork import V3_WINNER, V4_WEB_HIGH

PATH = "/.well-known/skadnetwork/report-attribution"
APPLE_APP_ID = 525463029  # the id inside Apple's published vectors


async def _register_apple_id(owner_conn, seeded_app, apple_app_id=APPLE_APP_ID):
    await owner_conn.execute(
        "UPDATE apps SET apple_app_id = $1 WHERE id = $2", apple_app_id, seeded_app["app_id"]
    )


async def _post(tracker, payload):
    # Deliberately no authorization header: Apple sends none, and the endpoint
    # must work without one.
    return await tracker.post(
        PATH, content=json.dumps(payload).encode(), headers={"content-type": "application/json"}
    )


async def test_a_genuine_apple_postback_is_stored(tracker, owner_conn, seeded_app):
    await _register_apple_id(owner_conn, seeded_app)

    response = await _post(tracker, V4_WEB_HIGH)
    assert response.status_code == 200

    row = await owner_conn.fetchrow(
        "SELECT * FROM skadnetwork_postbacks WHERE transaction_id = $1",
        V4_WEB_HIGH["transaction-id"],
    )
    assert row is not None
    assert row["app_id"] == seeded_app["app_id"]
    assert row["did_win"] is True
    assert row["conversion_value"] == 63
    assert row["source_identifier"] == "5239"
    # The signed document is kept whole so a dispute months later can be
    # settled by re-verifying the exact bytes received.
    assert (
        json.loads(row["payload"])["attribution-signature"]
        == (V4_WEB_HIGH["attribution-signature"])
    )


async def test_a_forged_postback_is_refused(tracker, owner_conn, seeded_app):
    """The attack the signature exists to stop: an anonymous caller inventing an
    install. Well-formed, correctly signed — with the wrong key."""
    await _register_apple_id(owner_conn, seeded_app)

    attacker = ec.generate_private_key(ec.SECP256R1())
    forged = copy.deepcopy(V4_WEB_HIGH)
    forged["transaction-id"] = "deadbeef-0000-0000-0000-000000000001"
    forged["attribution-signature"] = base64.b64encode(
        attacker.sign(canonical_string(forged).encode(), ec.ECDSA(hashes.SHA256()))
    ).decode()

    response = await _post(tracker, forged)
    assert response.status_code == 400

    assert (
        await owner_conn.fetchval(
            "SELECT count(*) FROM skadnetwork_postbacks WHERE transaction_id = $1",
            forged["transaction-id"],
        )
        == 0
    )


async def test_tampering_with_the_conversion_value_of_a_real_postback_still_verifies(
    tracker, owner_conn, seeded_app
):
    """Documenting a real limitation of Apple's design rather than pretending
    it is closed. The conversion value is not covered by the signature, so a
    genuine postback replayed with a different value verifies — the unique
    transaction id is what stops it being counted twice."""
    await _register_apple_id(owner_conn, seeded_app)
    assert (await _post(tracker, V4_WEB_HIGH)).status_code == 200

    tampered = copy.deepcopy(V4_WEB_HIGH)
    tampered["conversion-value"] = 7
    assert (await _post(tracker, tampered)).status_code == 200

    rows = await owner_conn.fetch(
        "SELECT conversion_value FROM skadnetwork_postbacks WHERE transaction_id = $1",
        V4_WEB_HIGH["transaction-id"],
    )
    assert len(rows) == 1, "the replay must not create a second row"
    assert rows[0]["conversion_value"] == 63, "the first, genuine value stands"


async def test_a_replayed_postback_is_answered_but_stored_once(tracker, owner_conn, seeded_app):
    """Apple retries up to nine times over nine days until it gets a 200, so
    duplicates are ordinary operation rather than an attack."""
    await _register_apple_id(owner_conn, seeded_app)

    for _ in range(3):
        assert (await _post(tracker, V3_WINNER)).status_code == 200

    assert (
        await owner_conn.fetchval(
            "SELECT count(*) FROM skadnetwork_postbacks WHERE transaction_id = $1",
            V3_WINNER["transaction-id"],
        )
        == 1
    )


async def test_a_non_winning_postback_is_recorded_as_such(tracker, owner_conn, seeded_app):
    from tests.test_skadnetwork import V3_LOSER

    await _register_apple_id(owner_conn, seeded_app)
    assert (await _post(tracker, V3_LOSER)).status_code == 200

    row = await owner_conn.fetchrow(
        "SELECT did_win FROM skadnetwork_postbacks WHERE transaction_id = $1",
        V3_LOSER["transaction-id"],
    )
    assert row["did_win"] is False


async def test_an_unknown_app_gets_the_same_answer_as_a_known_one(tracker, owner_conn, seeded_app):
    """Otherwise this endpoint enumerates our customers' App Store ids to
    anyone who can send it a valid postback — and it needs no credential."""
    known = await _post(tracker, V4_WEB_HIGH)  # no apple_app_id registered yet
    await _register_apple_id(owner_conn, seeded_app)
    other = copy.deepcopy(V3_WINNER)
    registered = await _post(tracker, other)

    assert known.status_code == registered.status_code == 200
    assert known.json() == registered.json()


async def test_junk_is_refused_without_touching_the_database(tracker):
    for body in (b"", b"not json", b"[]", b"null", b'{"version": "9.9"}'):
        response = await tracker.post(
            PATH, content=body, headers={"content-type": "application/json"}
        )
        assert response.status_code == 400


async def test_an_oversized_body_is_refused(tracker):
    """An anonymous caller must not be able to choose how much we read."""
    response = await tracker.post(
        PATH,
        content=b'{"padding": "' + b"x" * 20_000 + b'"}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413


async def test_the_endpoint_needs_no_credential(tracker, owner_conn, seeded_app):
    """Apple sends none. If this ever starts requiring one, every postback is
    silently lost — so it is pinned."""
    await _register_apple_id(owner_conn, seeded_app)
    saved = tracker.headers.pop("authorization", None)
    try:
        assert (await _post(tracker, V4_WEB_HIGH)).status_code == 200
    finally:
        if saved:
            tracker.headers["authorization"] = saved


async def test_a_postback_for_a_disabled_app_is_not_stored(tracker, owner_conn, seeded_app):
    """A disabled app has stopped measuring. Accepting postbacks for it would
    keep its numbers moving after its owner switched it off — the same rule the
    link cache applies to redirects."""
    await _register_apple_id(owner_conn, seeded_app)
    await owner_conn.execute(
        "UPDATE apps SET status = 'disabled' WHERE id = $1", seeded_app["app_id"]
    )
    try:
        assert (await _post(tracker, V4_WEB_HIGH)).status_code == 200, (
            "still answered, so Apple stops retrying"
        )
        assert (
            await owner_conn.fetchval(
                "SELECT count(*) FROM skadnetwork_postbacks WHERE transaction_id = $1",
                V4_WEB_HIGH["transaction-id"],
            )
            == 0
        )
    finally:
        await owner_conn.execute(
            "UPDATE apps SET status = 'active' WHERE id = $1", seeded_app["app_id"]
        )


async def test_an_unexpected_unique_violation_is_not_swallowed(monkeypatch):
    """Only the transaction-id duplicate is tolerated. A different constraint
    failing is something we have not anticipated, and discarding it quietly
    would turn a schema problem into missing data nobody notices."""
    from asyncpg.exceptions import UniqueViolationError
    from mmp_tracker import skan

    class FakeConn:
        async def fetchrow(self, *_args):
            return {"organization_id": "org", "id": "app"}

        async def execute(self, *_args):
            error = UniqueViolationError("some other constraint")
            error.constraint_name = "uq_something_else"
            raise error

    class FakeDatabase:
        def acquire_raw(self):
            class Ctx:
                async def __aenter__(self_inner):
                    return FakeConn()

                async def __aexit__(self_inner, *_exc):
                    return False

            return Ctx()

    class FakeState:
        database = FakeDatabase()

    verified = skan.verify(V4_WEB_HIGH)
    with __import__("pytest").raises(UniqueViolationError):
        await skan._store(FakeState(), verified, b"{}")
