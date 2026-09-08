"""Raw data export.

The tests that matter most here are the ones about what does *not* come out.
An export is the point where tenant data leaves the system permanently — once a
file is downloaded no later deletion request can reach it — so the column
allowlist and the tenant scoping are the load-bearing parts, not the CSV.
"""

from __future__ import annotations

import csv
import datetime as dt
import io

import pytest
from mmp_api.routes.exports import DATASETS, MAX_RANGE, _query
from mmp_core.ids import uuid7

# Ours, not the advertiser's: derived from an advertising ID or an IP address
# under a system-wide pepper, which makes them stable pseudonyms for a person.
# Exporting one turns a hash we keep for attribution into a join key someone
# else can re-identify against.
NEVER_EXPORTED = ("device_hash", "ip_hash", "user_agent")


def _range(days=1):
    now = dt.datetime.now(dt.UTC)
    return {
        "since": (now - dt.timedelta(days=days)).isoformat(),
        "until": (now + dt.timedelta(minutes=5)).isoformat(),
    }


async def _app(account, name="Export App", package="com.example.export"):
    response = await account.post(
        "/v1/apps",
        json={"name": name, "platform": "android", "android_package_name": package},
    )
    return response.json()


# ------------------------------------------------------------- the allowlist
@pytest.mark.parametrize("dataset_name", sorted(DATASETS))
@pytest.mark.parametrize("forbidden", NEVER_EXPORTED)
def test_internal_identifiers_are_never_in_a_query(dataset_name, forbidden):
    assert forbidden not in _query(DATASETS[dataset_name])


@pytest.mark.parametrize("dataset_name", sorted(DATASETS))
def test_a_dataset_never_selects_a_star(dataset_name):
    """SELECT * would export whatever a future migration adds, which is exactly
    how an internal identifier escapes without anyone deciding it should."""
    assert "*" not in _query(DATASETS[dataset_name])


@pytest.mark.parametrize("dataset_name", sorted(DATASETS))
async def test_every_exported_column_exists_in_the_real_table(dataset_name, owner_conn):
    """Guards the other direction. A column renamed by a migration would turn
    every export into a 500 discovered by a customer; this turns it into a
    failing test discovered by us."""
    dataset = DATASETS[dataset_name]
    actual = {
        row["column_name"]
        for row in await owner_conn.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = $1",
            dataset.table,
        )
    }
    missing = set(dataset.columns) - actual
    assert not missing, f"{dataset_name} exports columns that do not exist: {sorted(missing)}"


async def test_no_dataset_leaks_an_identifier_the_table_happens_to_hold(owner_conn):
    """The allowlist is only meaningful if the columns it excludes are really
    there to be excluded. If a table stopped carrying ip_hash, this test would
    stop proving anything, so it asserts the risk still exists."""
    columns = {
        row["column_name"]
        for row in await owner_conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name IN ('events', 'clicks')"
        )
    }
    assert "ip_hash" in columns, "the exclusion test is only meaningful while this exists"
    assert "device_hash" in columns


# ------------------------------------------------------------------ refusals
async def test_an_unknown_dataset_is_refused(account):
    app = await _app(account)
    response = await account.client.get(
        "/v1/exports/passwords", params={"app_id": app["id"], **_range()}
    )
    assert response.status_code == 404
    assert "available" in response.text


async def test_an_over_long_range_is_refused(account):
    app = await _app(account, name="Long", package="com.example.long")
    response = await account.client.get(
        "/v1/exports/events",
        params={"app_id": app["id"], **_range(days=MAX_RANGE.days + 1)},
    )
    assert response.status_code == 422


async def test_another_tenants_app_is_not_found(api_client, account):
    app = await _app(account, name="Mine", package="com.example.exportmine")

    from tests.conftest_api import register_account

    other = await register_account(api_client, name="Other")
    response = await other.client.get(
        "/v1/exports/events", params={"app_id": app["id"], **_range()}
    )
    assert response.status_code == 404


# -------------------------------------------------------------- the happy path
async def test_events_export_streams_the_rows(account, owner_conn):
    app = await _app(account, name="Streamed", package="com.example.streamed")
    org_id = await owner_conn.fetchval("SELECT organization_id FROM apps WHERE id = $1", app["id"])

    now = dt.datetime.now(dt.UTC)
    for i in range(3):
        await owner_conn.execute(
            """INSERT INTO events (event_id, received_at, occurred_at, organization_id, app_id,
                                   event_name, anonymous_id, platform, ip_hash)
               VALUES ($1, $2, $2, $3, $4, 'purchase', $5, 1, $6)""",
            uuid7(),
            now,
            org_id,
            app["id"],
            f"device-{i}",
            b"\x01" * 32,
        )

    response = await account.client.get(
        "/v1/exports/events", params={"app_id": app["id"], **_range()}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"

    rows = list(csv.reader(io.StringIO(response.text)))
    header, body = rows[0], rows[1:]
    assert "event_name" in header
    assert len(body) == 3
    assert {r[header.index("anonymous_id")] for r in body} == {"device-0", "device-1", "device-2"}


async def test_a_stored_ip_hash_does_not_reach_the_file(account, owner_conn):
    """The end-to-end version of the allowlist test: a row that *has* an ip_hash
    in the database must not carry it out of the system."""
    app = await _app(account, name="Hashed", package="com.example.hashed")
    org_id = await owner_conn.fetchval("SELECT organization_id FROM apps WHERE id = $1", app["id"])
    marker = bytes.fromhex("de") * 32

    await owner_conn.execute(
        """INSERT INTO events (event_id, received_at, occurred_at, organization_id, app_id,
                               event_name, anonymous_id, platform, ip_hash)
           VALUES ($1, now(), now(), $2, $3, 'install', 'traceable', 1, $4)""",
        uuid7(),
        org_id,
        app["id"],
        marker,
    )
    response = await account.client.get(
        "/v1/exports/events", params={"app_id": app["id"], **_range()}
    )
    assert "traceable" in response.text, "the row must actually be in the export"
    assert marker.hex() not in response.text.lower()
    assert "\\xde" not in response.text.lower()


async def test_an_export_is_recorded_in_the_audit_log(account, owner_conn):
    """An export leaves the erasure boundary — once downloaded, a later deletion
    request cannot reach it. The audit entry is the record that it happened."""
    app = await _app(account, name="Audited", package="com.example.audited")
    await account.client.get("/v1/exports/attributions", params={"app_id": app["id"], **_range()})

    entry = await owner_conn.fetchrow(
        "SELECT action, resource_id, detail FROM audit_log "
        "WHERE action = 'export.requested' ORDER BY created_at DESC LIMIT 1"
    )
    assert entry is not None, "an export must leave an audit trail"
    assert entry["resource_id"] == "attributions"


async def test_a_member_can_read_reports_but_cannot_export(account, api_client):
    """An export is the whole dataset leaving the system, which is a different
    act from reading a report and needs a different level of authority."""
    import httpx

    from tests.conftest_api import register_account

    member = await register_account(api_client, name="Member")
    member_email, member_password = member.email, member.password
    await member.post("/v1/auth/logout")

    await api_client.post(
        "/v1/auth/login", json={"email": account.email, "password": account.password}
    )
    account.client.cookies = api_client.cookies
    app = await _app(account, name="Role", package="com.example.role")
    await account.post("/v1/organizations/members", json={"email": member_email, "role": "member"})
    org_id = account.organization["id"]

    async with httpx.AsyncClient(transport=api_client._transport, base_url="http://api") as client:
        await client.post(
            "/v1/auth/login", json={"email": member_email, "password": member_password}
        )
        csrf = client.cookies["mmp_csrf"]
        await client.post(f"/v1/organizations/{org_id}/switch", headers={"x-csrf-token": csrf})

        # They can see the app — this is a real member, not a broken session.
        assert (await client.get("/v1/apps")).status_code == 200

        refused = await client.get("/v1/exports/clicks", params={"app_id": app["id"], **_range()})
        assert refused.status_code == 403, "a member must not be able to export"

    # And the admin still can, so the check above is about role, not breakage.
    allowed = await account.client.get(
        "/v1/exports/clicks", params={"app_id": app["id"], **_range()}
    )
    assert allowed.status_code == 200
