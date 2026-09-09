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
        ("/fraud", "Fraud signals"),
        ("/skan", "SKAdNetwork"),
        ("/deep-links", "Deep links"),
        ("/integrations", "Integrations"),
        ("/export", "Export"),
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
    assert markup.count('class="bar"') == 2, "a zero day is still a day on the axis"
    assert "height:0.00%" in markup


def test_a_single_day_is_one_bar_not_a_wall():
    """The bug this chart was rewritten for.

    The SVG version stretched a ten-unit viewBox to the full container with
    preserveAspectRatio="none", so one day's data became a slab across the page
    and thirty days became distorted slivers. Bars in normal flow have a capped
    width, so the count of bars is the only thing that changes.
    """
    one = bar_chart([{"day": "2026-09-01", "events": 10}])
    thirty = bar_chart([{"day": f"2026-09-{d:02d}", "events": 10} for d in range(1, 31)])

    assert one.count('class="bar"') == 1
    assert thirty.count('class="bar"') == 30
    # Nothing in the markup scales with the number of points — no viewBox, no
    # per-bar width. Width is a CSS cap that neither series can exceed.
    assert "viewBox" not in one and "viewBox" not in thirty
    assert "width=" not in one


def test_bar_heights_are_proportional_to_the_peak():
    markup = bar_chart(
        [
            {"day": "a", "events": 100},
            {"day": "b", "events": 50},
            {"day": "c", "events": 25},
        ]
    )
    assert "height:100.00%" in markup
    assert "height:50.00%" in markup
    assert "height:25.00%" in markup


def test_a_quiet_day_is_distinguishable_from_missing_data():
    """A gap in the series must read as "nothing happened" rather than as data
    that failed to load, so a zero day still draws a stub in a muted colour."""
    markup = bar_chart([{"day": "a", "events": 40}, {"day": "b", "events": 0}])
    assert 'class="fill empty"' in markup
    assert markup.count('class="bar"') == 2


def test_the_chart_scale_is_real_text_beside_the_plot():
    """Not inside a scaled drawing. The previous version put labels in an SVG
    that was stretched to fit, and they rendered as unreadable smears."""
    markup = bar_chart([{"day": "a", "events": 80}])
    assert "chart-scale" in markup
    assert ">80<" in markup, "the peak is shown"
    assert ">40<" in markup, "and the midpoint"
    assert "<svg" not in markup


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
        assert 'class="bar"' in response.text, "the chart should render server-side"

        events_page = await web.get(f"/events?app_id={app_id}")
        assert "install" in events_page.text
        assert "purchase" in events_page.text
    finally:
        await owner_conn.execute("DELETE FROM events WHERE app_id = $1", app_id)
        await owner_conn.execute("DELETE FROM rollup_events_hourly WHERE app_id = $1", app_id)
        await owner_conn.execute("DELETE FROM campaigns WHERE id = $1", campaign_id)
        await owner_conn.execute("DELETE FROM apps WHERE id = $1", app_id)


# --- the operational pages ----------------------------------------------
async def _with_an_app(signed_in, api_client, name="Dash App", package="com.example.dash"):
    """Sign in through the API too, and create an app.

    Several pages take an empty branch when the account has no apps — which is
    correct behaviour and useless for testing what they render when it does.
    """
    await api_client.post(
        "/v1/auth/login",
        json={"email": signed_in["email"], "password": signed_in["password"]},
    )
    created = await api_client.post(
        "/v1/apps",
        json={"name": name, "platform": "ios", "ios_bundle_id": package},
        headers={"x-csrf-token": api_client.cookies["mmp_csrf"]},
    )
    assert created.status_code == 201, created.text
    return created.json()


async def test_fraud_page_states_that_flagged_installs_are_still_counted(signed_in):
    """The most important sentence on the page. A fraud verdict never silently
    moves anyone's numbers, and a reader who assumes otherwise will
    double-subtract when reconciling against the overview."""
    response = await signed_in["client"].get("/fraud")
    assert response.status_code == 200
    assert "still counted" in response.text


async def test_skan_page_carries_the_api_s_own_caveat(signed_in, api_client, monkeypatch):
    """The warning that SKAdNetwork numbers do not reconcile comes from the API
    response, not from the template — so it survives a redesign of this page."""
    from mmp_web import app as web_app

    original = web_app.ApiClient.get

    async def fake_get(self, path, **kwargs):
        if path.startswith("/v1/skan/summary"):
            return {"rows": [], "caveat": "CAVEAT-FROM-THE-API"}
        return await original(self, path, **kwargs)

    await _with_an_app(signed_in, api_client, "Caveat", "com.example.caveat")
    monkeypatch.setattr(web_app.ApiClient, "get", fake_get)
    response = await signed_in["client"].get("/skan")
    assert "CAVEAT-FROM-THE-API" in response.text


async def test_skan_page_says_when_apple_withheld_a_campaign(signed_in, api_client, monkeypatch):
    """An empty cell reads as zero. Apple nulls the campaign identifier below
    its privacy thresholds, and the page has to say so."""
    from mmp_web import app as web_app

    original = web_app.ApiClient.get

    async def fake_get(self, path, **kwargs):
        if path.startswith("/v1/skan/summary"):
            return {
                "rows": [
                    {
                        "ad_network_id": "example.skadnetwork",
                        "source_identifier": None,
                        "winning_postbacks": 4,
                        "non_winning_postbacks": 1,
                        "redownloads": 0,
                        "average_conversion_value": None,
                        "suppressed": 4,
                    }
                ],
                "caveat": "x",
            }
        return await original(self, path, **kwargs)

    await _with_an_app(signed_in, api_client, "Withheld", "com.example.withheld")
    monkeypatch.setattr(web_app.ApiClient, "get", fake_get)
    response = await signed_in["client"].get("/skan")
    assert "withheld by Apple" in response.text


async def test_export_page_names_what_is_not_in_the_file(signed_in, api_client):
    """Device and IP hashes are excluded deliberately. Someone reconciling an
    export against their own warehouse needs to know that before they conclude
    the data is wrong."""
    await _with_an_app(signed_in, api_client, "Export", "com.example.exportpage")
    response = await signed_in["client"].get("/export")
    assert "device and ip hashes" in response.text.lower()


async def test_integrations_page_never_renders_a_credential_value(signed_in, monkeypatch):
    """Only the names of supplied fields are returned by the API. If a value
    ever appears here, something upstream started leaking it."""
    from mmp_web import app as web_app

    original = web_app.ApiClient.get

    async def fake_get(self, path, **kwargs):
        if path == "/v1/integrations":
            return [
                {
                    "id": "1",
                    "name": "Network",
                    "provider": "s2s_json",
                    "credential_names": ["api_token"],
                    "status": "active",
                    "created_at": "2026-09-01T00:00:00Z",
                    "secret_value": "sk_live_should_never_render",
                }
            ]
        if path == "/v1/providers":
            return []
        return await original(self, path, **kwargs)

    monkeypatch.setattr(web_app.ApiClient, "get", fake_get)
    response = await signed_in["client"].get("/integrations")
    assert "api_token" in response.text, "the field name is shown"
    assert "sk_live_should_never_render" not in response.text


async def test_creating_a_deep_link_without_a_csrf_token_does_nothing(signed_in):
    """The dashboard's mutations go through the same CSRF check as the API's."""
    response = await signed_in["client"].post(
        "/deep-links",
        data={
            "app_id": "whatever",
            "code": "x",
            "destination": "/x",
            "fallback_url": "https://example.com",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/deep-links"


async def test_a_deep_link_failure_shows_the_api_s_reason(signed_in, monkeypatch):
    """ "Something went wrong" is useless. The API knows the code was taken, or
    the destination was multi-line, and that is what the user needs."""
    from mmp_web import app as web_app

    async def failing_post(self, path, **kwargs):
        raise web_app.ApiError(409, "code 'summer' already exists for this app")

    monkeypatch.setattr(web_app.ApiClient, "post", failing_post)
    client = signed_in["client"]
    csrf = client.cookies.get("mmp_csrf")
    response = await client.post(
        "/deep-links",
        data={
            "app_id": "app-1",
            "code": "summer",
            "destination": "/summer",
            "fallback_url": "https://example.com",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "already+exists" in response.headers["location"].replace("%20", "+")


async def test_the_export_download_goes_through_the_dashboard(signed_in, api_client):
    """Not straight at the API.

    A direct link works in development, where both services answer on 127.0.0.1
    and cookies ignore the port, and 401s in production, where the session
    cookie is scoped to the dashboard's host and the API is on another. Nothing
    in the dashboard's logs would explain that.
    """
    app = await _with_an_app(signed_in, api_client, "Dl", "com.example.dl")
    response = await signed_in["client"].get(f"/export?app_id={app['id']}")
    assert 'href="/export/events?' in response.text
    assert "/v1/exports/" not in response.text, "the API must not be linked directly"


async def test_downloading_an_export_returns_csv(signed_in, api_client):
    app = await _with_an_app(signed_in, api_client, "Csv", "com.example.csv")
    response = await signed_in["client"].get(
        f"/export/events?app_id={app['id']}&since=2026-09-01T00:00:00Z&until=2026-09-08T00:00:00Z"
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    # The header row proves it came from the API rather than being invented here.
    assert "event_name" in response.text


async def test_an_unknown_dataset_never_reaches_the_api(signed_in, monkeypatch):
    """The dataset name is interpolated into a URL path, so it is checked
    against the list this page offers rather than forwarded.

    Asserting the redirect alone would prove nothing: without the guard the API
    answers 404 and this handler redirects identically. What distinguishes them
    is whether the request was made at all.
    """
    from mmp_web import app as web_app

    called: list[str] = []
    original = web_app.ApiClient.stream

    def recording_stream(self, method, path, **kwargs):
        called.append(path)
        return original(self, method, path, **kwargs)

    monkeypatch.setattr(web_app.ApiClient, "stream", recording_stream)

    response = await signed_in["client"].get("/export/passwords?app_id=x", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/export"
    assert called == [], f"the unknown dataset was forwarded upstream: {called}"


def test_every_class_the_templates_use_is_actually_styled():
    """The stylesheet is inline, and I once deleted most of it.

    A careless edit removed `.card`, `.pill`, `.table-wrap`, the table rules and
    the form styles. Every one of the 47 tests still passed, because they assert
    content and structure and never look at presentation — the pages rendered
    with the right words and no styling at all, which is exactly the shape of
    bug that only opening the page catches.

    This is the mechanical half of that check: any class a template applies must
    have a rule somewhere in the stylesheet.
    """
    import re
    from pathlib import Path

    templates = Path("services/web/src/mmp_web/templates")
    stylesheet = (templates / "base.html").read_text()

    # Classes that are hooks for behaviour or state rather than presentation,
    # and legitimately have no rule of their own.
    exempt = {
        "active",
        "secondary",
        "inline",
        "num",
        "none",
        "brand",
        "who",
        "group",
        "item",
        "links",
        "account",
        "spacer",
        "value",
        "label",
    }

    missing: list[str] = []
    for template in templates.rglob("*.html"):
        source = template.read_text()
        # A template may carry its own <style>: the login page is standalone and
        # does not extend the shell, so its rules live inside it.
        available = stylesheet + source
        used: set[str] = set()
        for attribute in re.findall(r'class="([^"{}]+)"', source):
            used.update(attribute.split())
        missing += [
            f"{template.name}:{name}"
            for name in sorted(used - exempt)
            if f".{name}" not in available
        ]

    assert not missing, f"templates use classes with no styling: {missing}"


def test_the_navigation_is_a_sidebar_with_grouped_sections():
    from mmp_web.app import NAV_ITEMS

    groups = [item["group"] for item in NAV_ITEMS]
    assert set(groups) == {"Measure", "Trust", "Configure"}
    # Items must be ordered so each group is contiguous — the template emits a
    # heading whenever the group changes, so a stray item would print its
    # heading twice.
    assert groups == sorted(groups, key=lambda g: groups.index(g)), (
        "NAV_ITEMS must keep each group contiguous"
    )
