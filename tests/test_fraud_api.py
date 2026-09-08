"""The fraud read API.

Mostly about what it refuses: an unbounded range, another tenant's findings,
and any attempt to change a verdict. A verdict a customer could edit would not
be evidence of anything, and the dispute these endpoints exist to settle is
usually between an advertiser and the network they are paying.
"""

from __future__ import annotations

import datetime as dt


async def _app(account, name="Fraud App", package="com.example.fraud"):
    response = await account.post(
        "/v1/apps",
        json={"name": name, "platform": "android", "android_package_name": package},
    )
    return response.json()


def _range(days=7):
    now = dt.datetime.now(dt.UTC)
    return {
        "since": (now - dt.timedelta(days=days)).isoformat(),
        "until": now.isoformat(),
    }


async def test_findings_start_empty_for_a_new_app(account):
    app = await _app(account)
    response = await account.client.get(
        "/v1/fraud/findings", params={"app_id": app["id"], **_range()}
    )
    assert response.status_code == 200
    assert response.json() == []


async def test_an_unbounded_range_is_refused(account):
    """An unbounded scan of a partitioned table is a denial of service that
    looks like a report."""
    app = await _app(account)
    response = await account.client.get(
        "/v1/fraud/findings", params={"app_id": app["id"], **_range(days=365)}
    )
    assert response.status_code == 422
    assert "90 days" in response.text


async def test_a_backwards_range_is_refused(account):
    app = await _app(account)
    now = dt.datetime.now(dt.UTC)
    response = await account.client.get(
        "/v1/fraud/findings",
        params={
            "app_id": app["id"],
            "since": now.isoformat(),
            "until": (now - dt.timedelta(days=1)).isoformat(),
        },
    )
    assert response.status_code == 422


async def test_flagged_installs_are_listed_and_clean_ones_are_not(account, owner_conn):
    """The install view filters to flagged rows. This is a filter on a fraud
    endpoint, not on reporting — analytics still counts every install."""
    app = await _app(account, name="Flagged", package="com.example.flagged")
    org_id = await owner_conn.fetchval("SELECT organization_id FROM apps WHERE id = $1", app["id"])

    now = dt.datetime.now(dt.UTC)
    for anonymous_id, score, verdict, rules in (
        ("clean-device", 0, "clean", None),
        ("dirty-device", 100, "fraudulent", '["click_injection"]'),
    ):
        await owner_conn.execute(
            """INSERT INTO attributions (id, organization_id, app_id, install_key, anonymous_id,
                                         method, installed_at, attributed_at, window_days,
                                         expires_at, fraud_score, fraud_verdict, fraud_rules,
                                         created_at, updated_at)
               VALUES (gen_random_uuid(), $1, $2, $3, $4, 'organic', $5, now(), 7,
                       $5::timestamptz + interval '30 days', $6, $7, $8::jsonb, now(), now())""",
            org_id,
            app["id"],
            f"{app['id']}:{anonymous_id}",
            anonymous_id,
            now,
            score,
            verdict,
            rules,
        )

    response = await account.client.get(
        "/v1/fraud/installs", params={"app_id": app["id"], **_range()}
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1, "only the flagged install belongs in this view"
    assert body[0]["fraud_verdict"] == "fraudulent"
    assert body[0]["rules"] == ["click_injection"]


async def test_findings_do_not_cross_a_tenant(api_client, account, owner_conn):
    """The isolation that matters most here: one advertiser must never see
    another's fraud findings, which name their networks and their volumes."""
    app = await _app(account, name="Mine", package="com.example.mine")
    org_id = await owner_conn.fetchval("SELECT organization_id FROM apps WHERE id = $1", app["id"])
    now = dt.datetime.now(dt.UTC)
    await owner_conn.execute(
        """INSERT INTO fraud_findings (id, organization_id, app_id, rule, severity, detail,
                                       window_start, window_end)
           VALUES (gen_random_uuid(), $1, $2, 'click_flooding', 100, 'a finding',
                   $3::timestamptz - interval '1 day', $3::timestamptz)""",
        org_id,
        app["id"],
        now,
    )

    # The positive half first. Without it this test would pass just as happily
    # against an endpoint that returned nothing to anybody, which is the way a
    # tenant-isolation test usually fails silently.
    own = await account.client.get("/v1/fraud/findings", params={"app_id": app["id"], **_range()})
    assert own.status_code == 200
    assert [f["rule"] for f in own.json()] == ["click_flooding"], (
        "the owning tenant must actually see its own finding"
    )

    from tests.conftest_api import register_account

    other = await register_account(api_client, name="Other Tenant")
    response = await other.client.get(
        "/v1/fraud/findings", params={"app_id": app["id"], **_range()}
    )
    assert response.status_code in (200, 403, 404)
    assert response.json() in ([], {"detail": "Forbidden"}, {"detail": "Not Found"}), (
        "another tenant's findings must never be returned"
    )


async def test_a_verdict_cannot_be_changed_through_the_api(account):
    """There is deliberately no write path. If one is ever added, this fails."""
    app = await _app(account, name="ReadOnly", package="com.example.readonly")
    for method, path in (
        ("post", "/v1/fraud/findings"),
        ("patch", f"/v1/fraud/installs?app_id={app['id']}"),
        ("delete", "/v1/fraud/findings"),
    ):
        request = getattr(account.client, method)
        response = (
            await request(path)
            if method == "delete"
            else await request(path, json={"fraud_verdict": "clean"})
        )
        assert response.status_code in (404, 405), (
            f"{method.upper()} {path} should not exist, got {response.status_code}"
        )
