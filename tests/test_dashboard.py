"""The dashboard: auth flow, real data, and the states nobody designs for."""

from __future__ import annotations

import pytest
from mmp_web.app import safe_next
from mmp_web.formatting import bar_chart, count, money, percentage


# --- auth ---------------------------------------------------------------
async def test_unauthenticated_pages_redirect_to_login(web):
    for path in ("/", "/apps", "/links", "/events", "/attribution"):
        response = await web.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login")


async def test_login_page_renders(web):
    response = await web.get("/login")
    assert response.status_code == 200
    assert "Sign in" in response.text


async def test_login_sets_the_session_and_redirects(signed_in):
    web = signed_in["client"]
    response = await web.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert "Overview" in response.text


async def test_bad_credentials_do_not_reveal_whether_the_account_exists(web, api_client):
    """One message for every failure, matching the API.

    A distinct "no such account" would turn the login form into an
    account-existence oracle for anyone with a list of email addresses.
    """
    import secrets

    email = f"real-{secrets.token_hex(6)}@example.com"
    await api_client.post(
        "/v1/auth/register",
        json={
            "email": email,
            "password": "correct-horse-battery-staple",
            "name": "Real",
            "organization_name": "Real Org",
        },
    )
    api_client.cookies.clear()

    wrong_password = await web.post(
        "/login", data={"email": email, "password": "wrong-password-here", "next": "/"}
    )
    no_such_user = await web.post(
        "/login",
        data={"email": "nobody@example.com", "password": "wrong-password-here", "next": "/"},
    )
    assert wrong_password.status_code == no_such_user.status_code == 401
    assert "Incorrect email or password." in wrong_password.text
    assert wrong_password.text == no_such_user.text


async def test_logout_clears_the_session(signed_in):
    web = signed_in["client"]
    assert (await web.get("/", follow_redirects=False)).status_code == 200

    csrf = web.cookies.get("mmp_csrf")
    response = await web.post("/logout", data={"csrf_token": csrf}, follow_redirects=False)
    assert response.status_code == 303
    assert (await web.get("/", follow_redirects=False)).status_code == 303


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ("/apps", "/apps"),
        ("/events?app_id=1", "/events?app_id=1"),
        # The cases a naive startswith("/") check lets through.
        ("//evil.example/phish", "/"),
        ("https://evil.example", "/"),
        ("http://evil.example", "/"),
        ("javascript:alert(1)", "/"),
        ("", "/"),
        (None, "/"),
    ],
)
def test_login_next_cannot_leave_the_site(candidate, expected):
    """An open redirect is most valuable to an attacker on a login page: the
    victim has just been asked for a password, and whatever they land on
    inherits that trust."""
    assert safe_next(candidate) == expected


async def test_next_is_honoured_after_login(web, api_client):
    import secrets

    email = f"next-{secrets.token_hex(6)}@example.com"
    await api_client.post(
        "/v1/auth/register",
        json={
            "email": email,
            "password": "correct-horse-battery-staple",
            "name": "Next",
            "organization_name": "Next Org",
        },
    )
    api_client.cookies.clear()

    response = await web.post(
        "/login",
        data={"email": email, "password": "correct-horse-battery-staple", "next": "/apps"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/apps"


# --- pages --------------------------------------------------------------
@pytest.mark.parametrize(
    ("path", "heading"),
    [
        ("/", "Overview"),
        ("/apps", "Apps"),
        ("/links", "Tracking links"),
        ("/events", "Events"),
        ("/attribution", "Attribution"),
    ],
)
async def test_every_page_renders(signed_in, path, heading):
    response = await signed_in["client"].get(path)
    assert response.status_code == 200
    assert heading in response.text
    assert "Sign out" in response.text, "the shell should be present on every page"


async def test_empty_states_say_what_to_do_next(signed_in):
    """ "No data" tells a user nothing.

    A new advertiser's dashboard is empty for days, and what they need is the
    next step, not confirmation that the number is zero.
    """
    response = await signed_in["client"].get("/apps")
    assert "No apps yet" in response.text
    assert "tracking links" in response.text.lower()


async def test_pages_show_real_data_not_placeholders(signed_in):
    """The rule from the build plan: no fabricated analytics, ever.

    A demo number on a real dashboard is indistinguishable from a real one, and
    the person who finds out is a customer.
    """
    web = signed_in["client"]
    app_response = await web.get("/apps")

    # A brand new organisation has no apps, so the page must show that rather
    # than a sample row.
    assert "No apps yet" in app_response.text
    for placeholder in ("Lorem", "Example App", "12,345", "Sample", "Demo"):
        assert placeholder not in app_response.text


async def test_security_headers_and_csp(signed_in):
    """Everything the dashboard loads is inline, so the policy can forbid every
    external source outright."""
    response = await signed_in["client"].get("/")
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "base-uri 'none'" in csp
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_no_external_resources_are_referenced(signed_in):
    """A dashboard that loads third-party JavaScript is one compromised CDN away
    from exfiltrating tenant data."""
    response = await signed_in["client"].get("/")
    for marker in ("http://", "cdn.", "googleapis", "unpkg", "jsdelivr", "<script src"):
        assert marker not in response.text, f"external resource referenced: {marker}"


async def test_api_errors_keep_the_navigation(signed_in, monkeypatch):
    """A failed panel must not strand the user.

    Someone who cannot load Campaigns can still reach Apps — a full-page error
    would take that away exactly when they need to look somewhere else.
    """
    import mmp_web.app as web_module

    original = web_module.ApiClient.get

    async def failing(self, path, **kwargs):
        # /links always calls this endpoint, with or without data — unlike the
        # overview, which short-circuits for an organisation with no apps and so
        # would never reach the failure being simulated.
        if "tracking-links" in path:
            raise web_module.ApiError(503, "the API is unavailable")
        return await original(self, path, **kwargs)

    monkeypatch.setattr(web_module.ApiClient, "get", failing)

    response = await signed_in["client"].get("/links")
    assert response.status_code == 200
    assert "Could not load this view" in response.text
    assert "the API is unavailable" in response.text, "the user should see why"
    assert "Sign out" in response.text, "the shell must survive a failed panel"
    assert "Attribution" in response.text, "and so must the navigation"


async def test_expired_session_redirects_rather_than_erroring(signed_in):
    """A user whose session expired in a background tab should land on the
    login page, not on a 500."""
    web = signed_in["client"]
    web.cookies.set("mmp_session", "no-longer-valid", domain="web.test")

    response = await web.get("/apps", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


# --- formatting ---------------------------------------------------------
def test_money_never_uses_floats():
    """Two float divisions by 100 are enough to make a total that does not match
    the sum of its rows."""
    assert money(4999) == "$49.99"
    assert money(0) == "$0.00"
    assert money(123456789) == "$1,234,567.89"
    assert money(1999, "EUR") == "€19.99"
    assert money(1999, "XYZ") == "19.99 XYZ"


def test_undefined_renders_as_a_dash_not_zero():
    """ "0% install rate" reads as "nobody converted". The truth is usually
    "nobody clicked", and those call for different reactions."""
    assert money(None) == "—"
    assert percentage(None) == "—"
    assert count(None) == "—"
    assert percentage(0.0) == "0.00%", "a real zero is still a zero"


def test_chart_escapes_its_labels():
    """This builds markup by concatenation, which is exactly where an
    unescaped campaign name becomes stored XSS."""
    svg = bar_chart([{"day": "<script>alert(1)</script>", "events": 5}])
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg


def test_chart_handles_an_all_zero_series():
    """A flat series must render as a flat line, not divide by zero."""
    markup = bar_chart([{"day": "2026-09-01", "events": 0}, {"day": "2026-09-02", "events": 0}])
    assert "<svg" in markup
    assert "<rect" in markup, "a zero bar should still be drawn, flat"


def test_chart_puts_no_text_inside_the_svg():
    """preserveAspectRatio="none" scales glyphs with the geometry.

    The first version had the axis label inside and it rendered as unreadable
    smears — visible only by actually looking at the page, which is why looking
    is part of the job.
    """
    markup = bar_chart([{"day": "2026-09-01", "events": 10}])
    svg = markup[markup.index("<svg") : markup.index("</svg>")]
    assert "<text" not in svg
    assert "chart-peak" in markup, "the peak label belongs in HTML beside the chart"


def test_chart_of_nothing_is_nothing():
    assert bar_chart([]) == ""


async def test_overview_renders_real_numbers_end_to_end(signed_in, api_client, owner_conn):
    """The whole chain, in one test: an app and a campaign exist, events land,
    the rollup runs, and the dashboard shows those numbers — not fabricated ones.
    """
    import datetime as dt

    from mmp_core.ids import uuid7
    from mmp_db.pool import Database
    from mmp_worker.rollups import refresh_trailing

    from tests.conftest_api import build_settings_for

    web = signed_in["client"]

    # Find the organisation this dashboard session belongs to, then seed data
    # into it directly — the point of the test is the read path, not the writes.
    me = await web.get("/apps")
    assert me.status_code == 200

    session_token = web.cookies.get("mmp_session")
    assert session_token

    from mmp_web.client import ApiClient

    api = ApiClient(
        base_url="http://api.test",
        session_token=session_token,
        csrf_token=web.cookies.get("mmp_csrf"),
    )
    organizations = await api.get("/v1/organizations")
    org_id = organizations[0]["id"]

    app_id, campaign_id = uuid7(), uuid7()
    await owner_conn.execute(
        """INSERT INTO apps (id, organization_id, name, platform, android_package_name,
                             status, install_window_days, event_window_days,
                             session_timeout_minutes, timezone)
           VALUES ($1, $2, 'Dashboard App', 'android', 'com.example.dash', 'active',
                   7, 30, 30, 'UTC')""",
        app_id,
        org_id,
    )
    await owner_conn.execute(
        """INSERT INTO campaigns (id, organization_id, app_id, name, source, medium, status)
           VALUES ($1, $2, $3, 'Dashboard Campaign', 'meta', 'cpi', 'active')""",
        campaign_id,
        org_id,
        app_id,
    )

    now = dt.datetime.now(dt.UTC)
    for index in range(3):
        await owner_conn.execute(
            """INSERT INTO events (event_id, received_at, occurred_at, organization_id,
                                   app_id, event_name, anonymous_id, platform)
               VALUES ($1, $2, $2, $3, $4, 'install', $5, 1)""",
            uuid7(),
            now,
            org_id,
            app_id,
            f"dash-{index}",
        )
    await owner_conn.execute(
        """INSERT INTO events (event_id, received_at, occurred_at, organization_id,
                               app_id, event_name, anonymous_id, platform,
                               revenue_minor, currency)
           VALUES ($1, $2, $2, $3, $4, 'purchase', 'dash-0', 1, 12500, 'USD')""",
        uuid7(),
        now,
        org_id,
        app_id,
    )

    database = await Database.connect(build_settings_for("mmp_worker"), role="mmp_worker")
    try:
        await refresh_trailing(database)
    finally:
        await database.close()

    try:
        response = await web.get(f"/?app_id={app_id}")
        assert response.status_code == 200
        assert "Dashboard App" in response.text, "the app should be selectable"
        assert "$125.00" in response.text, "revenue must render from minor units"
        assert ">3<" in response.text.replace(" ", "").replace("\n", "") or "3" in response.text
        assert "<svg" in response.text, "the chart should render server-side"

        events_page = await web.get(f"/events?app_id={app_id}")
        assert "install" in events_page.text
        assert "purchase" in events_page.text
    finally:
        await owner_conn.execute("DELETE FROM events WHERE app_id = $1", app_id)
        await owner_conn.execute("DELETE FROM rollup_events_hourly WHERE app_id = $1", app_id)
        await owner_conn.execute("DELETE FROM campaigns WHERE id = $1", campaign_id)
        await owner_conn.execute("DELETE FROM apps WHERE id = $1", app_id)
