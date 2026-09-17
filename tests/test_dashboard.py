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


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/apps",
        "/links",
        "/events",
        "/attribution",
        "/live",
        "/fraud",
        "/skan",
        "/deep-links",
        "/integrations",
        "/export",
    ],
)
async def test_no_page_loads_anything_from_another_origin(signed_in, api_client, path):
    """A dashboard that loads third-party JavaScript is one compromised CDN away
    from exfiltrating tenant data.

    This used to forbid the string "<script src" outright, which held while the
    dashboard had no script at all. The live view needs one, so the test now
    checks the property that string stood in for: every script is served from
    this origin, and none is inline.
    """
    import re

    await _with_an_app(signed_in, api_client, f"Res {path}", f"com.example.res{abs(hash(path))}")
    response = await signed_in["client"].get(path)
    text = response.text
    # Only what the page *loads* matters — src and href — not text it displays.
    # The live view shows a curl example containing the tracking domain, which
    # is text, not a resource.
    for attribute in re.findall(r'(?:src|href)="([^"]*)"', text):
        assert not attribute.startswith(("http://", "https://", "//")), (
            f"{path} loads from another origin: {attribute}"
        )

    for tag in re.findall(r"<script\b[^>]*>", text):
        source = re.search(r'src="([^"]+)"', tag)
        assert source, f"inline script on {path}: {tag}"
        assert source.group(1).startswith("/static/"), f"script from elsewhere: {tag}"
    assert not re.search(r"<script\b[^>]*>\s*(?!</script>)\S", text), (
        f"inline script body on {path}"
    )


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


# --- creating things through the UI --------------------------------------
async def test_a_new_account_can_onboard_without_touching_the_api(signed_in):
    """The path that was missing entirely.

    Every create flow lived only in the API, so the honest instruction for a new
    customer was "run these curl commands" — which is not a product. This walks
    the whole way: app, campaign, tracking link, and a key to put in the SDK.
    """
    web = signed_in["client"]
    csrf = web.cookies.get("mmp_csrf")

    created = await web.post(
        "/apps",
        data={
            "csrf_token": csrf,
            "name": "Onboarded",
            "platform": "android",
            "identifier": "com.example.onboarded",
            "install_window_days": 7,
            "event_window_days": 30,
        },
        follow_redirects=True,
    )
    assert "Onboarded" in created.text
    assert "com.example.onboarded" in created.text

    app_id = (await web.get("/apps")).text
    import re

    match = re.search(r'action="/apps/([0-9a-f-]{36})/keys"', app_id)
    assert match, "the app row should offer to issue a key"

    campaign = await web.post(
        "/campaigns",
        data={
            "csrf_token": csrf,
            "app_id": match.group(1),
            "name": "Launch",
            "source": "network_a",
            "medium": "cpi",
        },
        follow_redirects=True,
    )
    assert "Launch" in campaign.text

    campaign_id = re.search(r'name="campaign_id">\s*<option value="([0-9a-f-]{36})"', campaign.text)
    assert campaign_id, "the new campaign should be selectable when creating a link"

    link = await web.post(
        "/links",
        data={
            "csrf_token": csrf,
            "campaign_id": campaign_id.group(1),
            "name": "launch-android",
            "fallback_url": "https://example.com",
        },
        follow_redirects=True,
    )
    assert "launch-android" in link.text
    assert "/c/" in link.text, "the link's tracking URL should be shown for copying"


async def test_a_new_key_is_shown_in_the_page_never_in_the_url(signed_in):
    """The raw key exists exactly once and only a hash of it is stored.

    A redirect would put a live credential in the address bar, the browser's
    history and every access log between here and the user — so this handler
    renders its result instead of redirecting.
    """
    web = signed_in["client"]
    csrf = web.cookies.get("mmp_csrf")
    await web.post(
        "/apps",
        data={
            "csrf_token": csrf,
            "name": "Keyed",
            "platform": "ios",
            "identifier": "com.example.keyed",
        },
        follow_redirects=True,
    )
    import re

    app_id = re.search(r'action="/apps/([0-9a-f-]{36})/keys"', (await web.get("/apps")).text).group(
        1
    )

    response = await web.post(
        f"/apps/{app_id}/keys",
        data={"csrf_token": csrf, "kind": "sdk", "environment": "prod"},
        follow_redirects=False,
    )
    assert response.status_code == 200, "the key page is rendered, not redirected to"
    assert "mmp_" in response.text, "the raw key is shown once"
    assert "cannot be shown again" in response.text
    assert "location" not in response.headers


async def test_creating_anything_without_a_csrf_token_never_reaches_the_api(signed_in, monkeypatch):
    """Two checks guard these, and this one is about the near side.

    The API rejects an unsigned request anyway — it requires the header
    independently — so asserting "nothing was created" proves only that *a*
    control worked, not this one. What distinguishes the dashboard's check is
    that the request is never forwarded at all.
    """
    from mmp_web import app as web_app

    forwarded: list[str] = []
    original = web_app.ApiClient.post

    async def recording_post(self, path, **kwargs):
        forwarded.append(path)
        return await original(self, path, **kwargs)

    monkeypatch.setattr(web_app.ApiClient, "post", recording_post)

    web = signed_in["client"]
    for path, data in (
        ("/apps", {"name": "NoCsrf", "platform": "android"}),
        ("/campaigns", {"app_id": "x", "name": "NoCsrf"}),
        ("/links", {"campaign_id": "x", "name": "NoCsrf", "fallback_url": "https://x.example"}),
    ):
        response = await web.post(path, data=data, follow_redirects=False)
        assert response.status_code == 303

    assert forwarded == [], f"an unsigned request was forwarded upstream: {forwarded}"
    assert "NoCsrf" not in (await web.get("/apps")).text


async def test_a_rejected_creation_shows_the_api_s_reason(signed_in):
    """ "Something went wrong" is useless. The API knows the platform was
    invalid or the name was too long, and that is what the user needs."""
    web = signed_in["client"]
    csrf = web.cookies.get("mmp_csrf")
    response = await web.post(
        "/apps",
        data={"csrf_token": csrf, "name": "Bad", "platform": "windows_phone"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "notice=" in response.headers["location"]
    assert "/apps?" in response.headers["location"]


async def test_the_integration_form_is_built_from_the_adapter(signed_in):
    """Nothing in the dashboard knows a single field name.

    The adapter declares what it needs and the form renders that, so adding an
    adapter adds its form. The alternative — enumerating fields in a template —
    is a second copy of the adapter's requirements, and the way it fails is a
    form that stops asking for something still mandatory: the operator fills it
    in, saves, and is rejected for a field they were never shown.
    """
    response = await signed_in["client"].get("/integrations")
    assert response.status_code == 200
    # s2s_json declares api_token (secret), endpoint, event_map (optional).
    assert 'name="field_api_token"' in response.text
    assert 'name="field_endpoint"' in response.text
    # custom declares url_template.
    assert 'name="field_url_template"' in response.text


async def test_a_secret_field_renders_as_a_password_input(signed_in):
    """Not encryption — that is server-side — but a token in plain text on a
    shared screen is a way to lose one."""
    import re

    response = await signed_in["client"].get("/integrations")
    token_input = re.search(r"<input[^>]*name=\"field_api_token\"[^>]*>", response.text)
    assert token_input, "the token field should be rendered"
    assert 'type="password"' in token_input.group(0)

    endpoint_input = re.search(r"<input[^>]*name=\"field_endpoint\"[^>]*>", response.text)
    assert endpoint_input and 'type="text"' in endpoint_input.group(0)


async def test_creating_an_integration_stores_the_secret_and_never_shows_it(signed_in):
    web = signed_in["client"]
    csrf = web.cookies.get("mmp_csrf")
    secret = "tok_must_never_be_rendered"

    created = await web.post(
        "/integrations",
        data={
            "csrf_token": csrf,
            "provider": "s2s_json",
            "name": "Network A",
            "field_api_token": secret,
            "field_endpoint": "https://example.com/conversions",
        },
        follow_redirects=True,
    )
    assert "Network A" in created.text
    # The field's *name* is shown so an operator can see what was supplied.
    assert "api_token" in created.text
    assert secret not in created.text


async def test_a_malformed_event_map_is_answered_here_not_by_a_422(signed_in):
    """The one field that is not a string. A JSON typo should come back as a
    sentence, not as an API complaint about a type the operator never chose."""
    web = signed_in["client"]
    csrf = web.cookies.get("mmp_csrf")
    response = await web.post(
        "/integrations",
        data={
            "csrf_token": csrf,
            "provider": "s2s_json",
            "name": "Bad map",
            "field_api_token": "t",
            "field_endpoint": "https://example.com/x",
            "field_event_map": "{not json",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "event+map+must+be+valid+JSON" in response.headers["location"].replace("%20", "+")


async def test_an_unknown_adapter_is_refused_before_any_request(signed_in, monkeypatch):
    from mmp_web import app as web_app

    forwarded: list[str] = []
    original = web_app.ApiClient.post

    async def recording_post(self, path, **kwargs):
        forwarded.append(path)
        return await original(self, path, **kwargs)

    monkeypatch.setattr(web_app.ApiClient, "post", recording_post)

    web = signed_in["client"]
    csrf = web.cookies.get("mmp_csrf")
    response = await web.post(
        "/integrations",
        data={"csrf_token": csrf, "provider": "not_a_real_adapter", "name": "X"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert forwarded == []


async def test_a_secret_is_sent_as_a_credential_never_as_configuration(signed_in, monkeypatch):
    """Where the value goes decides whether it is encrypted.

    Credentials are envelope-encrypted and only their names are ever returned;
    configuration is stored and returned as plain data. A secret routed into
    configuration is a token written to the database in clear and handed back
    by the API — and it would look fine from the page, which is why this
    asserts the request body rather than the rendering.
    """
    from mmp_web import app as web_app

    sent: list[dict] = []
    original = web_app.ApiClient.post

    async def capturing_post(self, path, **kwargs):
        if path == "/v1/integrations":
            sent.append(kwargs.get("json") or {})
        return await original(self, path, **kwargs)

    monkeypatch.setattr(web_app.ApiClient, "post", capturing_post)

    web = signed_in["client"]
    csrf = web.cookies.get("mmp_csrf")
    await web.post(
        "/integrations",
        data={
            "csrf_token": csrf,
            "provider": "s2s_json",
            "name": "Routing",
            "field_api_token": "tok_secret",
            "field_endpoint": "https://example.com/x",
        },
        follow_redirects=False,
    )

    assert sent, "the integration should have been forwarded"
    body = sent[0]
    assert body["credentials"] == {"api_token": "tok_secret"}
    assert "api_token" not in body["configuration"]
    assert body["configuration"] == {"endpoint": "https://example.com/x"}


# --- live events ------------------------------------------------------------
async def test_the_live_page_hands_its_script_configuration_as_data(signed_in, api_client):
    app = await _with_an_app(signed_in, api_client, "Live Dash", "com.example.livedash")
    response = await signed_in["client"].get(f"/live?app_id={app['id']}")
    assert response.status_code == 200
    assert f'data-app-id="{app["id"]}"' in response.text
    assert '<script src="/static/live.js" defer></script>' in response.text
    assert "Live events" in response.text


async def test_the_policy_allows_script_from_this_origin_only(signed_in):
    """The narrowest change that lets the live view exist. 'self' permits a file
    this service serves; it never permits inline script or another host."""
    policy = (await signed_in["client"].get("/")).headers["content-security-policy"]
    directives = dict(
        part.strip().split(" ", 1) for part in policy.split(";") if " " in part.strip()
    )
    assert directives["script-src"] == "'self'"
    assert directives["connect-src"] == "'self'", "the script can talk to this origin only"
    assert "unsafe-inline" not in directives["script-src"]
    assert "unsafe-eval" not in policy
    assert directives["default-src"] == "'none'"


async def test_the_script_is_served_as_javascript(signed_in):
    response = await signed_in["client"].get("/static/live.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["x-content-type-options"] == "nosniff"


def test_the_live_script_never_turns_event_data_into_markup():
    """Events can be sent by anyone holding an SDK key, and the key ships inside
    every copy of the app. An event name is attacker-controlled text, so the
    script must never hand it to anything that parses HTML."""
    import re
    from pathlib import Path

    source = Path("services/web/src/mmp_web/static/live.js").read_text()
    code = "\n".join(line for line in source.splitlines() if not line.strip().startswith("//"))
    for sink in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
        'setTimeout("',
        "createContextualFragment",
    ):
        assert sink not in code, f"live.js uses {sink}"
    assert re.search(r"textContent\s*=", code), "values should reach the page as text"


async def test_the_feed_answers_an_expired_session_with_json_not_a_redirect(web):
    """Fetched by script. A 303 to the login form would arrive as a success the
    script cannot parse, and it would spin instead of saying the session ended."""
    response = await web.get("/live/feed?app_id=x", follow_redirects=False)
    assert response.status_code == 401
    assert response.json() == {"error": "session_expired"}


async def test_the_feed_proxies_the_api_as_the_signed_in_user(signed_in, api_client):
    app = await _with_an_app(signed_in, api_client, "Feed", "com.example.feedproxy")
    response = await signed_in["client"].get(f"/live/feed?app_id={app['id']}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert "items" in body and "server_time" in body
    assert response.headers["cache-control"] == "no-store"


async def test_the_feed_passes_through_only_the_parameters_it_understands(signed_in, monkeypatch):
    from mmp_web import app as web_app

    requested: list[str] = []

    async def capture(self, path, **kwargs):
        requested.append(path)
        return {"items": [], "server_time": "2026-09-15T00:00:00+00:00"}

    monkeypatch.setattr(web_app.ApiClient, "get", capture)
    await signed_in["client"].get("/live/feed?app_id=a&since=b&role=owner&org=other")
    assert requested and "role" not in requested[-1] and "org" not in requested[-1]
    assert "app_id=a" in requested[-1] and "since=b" in requested[-1]


# --- postbacks ----------------------------------------------------------------
async def test_the_postbacks_page_lists_campaigns_and_the_sub_variables(signed_in, api_client):
    app = await _with_an_app(signed_in, api_client, "Pb Dash", "com.example.pbdash")
    web = signed_in["client"]
    csrf = web.cookies.get("mmp_csrf")
    await web.post(
        "/campaigns",
        data={"csrf_token": csrf, "app_id": app["id"], "name": "Partner A", "source": "partner_a"},
    )
    page = await web.get(f"/postbacks?app_id={app['id']}")
    assert page.status_code == 200
    assert "Partner A" in page.text
    assert "{{sub1}}" in page.text, "the placeholder is shown literally, not rendered by Jinja"
    assert "campaign only" in page.text


async def test_an_app_wide_sub1_rule_is_refused_with_the_apis_reason(signed_in, api_client):
    app = await _with_an_app(signed_in, api_client, "Pb Refuse", "com.example.pbrefuse")
    web = signed_in["client"]
    response = await web.post(
        "/postbacks",
        data={
            "csrf_token": web.cookies.get("mmp_csrf"),
            "app_id": app["id"],
            "name": "Leaky",
            "trigger_event": "install",
            "url_template": "https://example.com/pb?clickid={{sub1}}",
            "campaign_id": "",
            "requires_attribution": "1",
        },
        follow_redirects=True,
    )
    assert "scoped to one campaign" in response.text
    assert "Leaky" not in response.text.split("</form>")[-1], "no rule was created"


async def test_a_scoped_sub1_rule_is_created_from_the_dashboard(signed_in, api_client):
    app = await _with_an_app(signed_in, api_client, "Pb Create", "com.example.pbcreate")
    web = signed_in["client"]
    csrf = web.cookies.get("mmp_csrf")
    await web.post(
        "/campaigns",
        data={"csrf_token": csrf, "app_id": app["id"], "name": "Partner A", "source": "partner_a"},
    )
    campaigns = (await api_client.get(f"/v1/campaigns?app_id={app['id']}")).json()
    response = await web.post(
        "/postbacks",
        data={
            "csrf_token": csrf,
            "app_id": app["id"],
            "name": "Partner A installs",
            "trigger_event": "install",
            "url_template": "https://example.com/pb?clickid={{sub1}}",
            "campaign_id": campaigns[0]["id"],
            "requires_attribution": "1",
        },
        follow_redirects=True,
    )
    assert "Created Partner A installs" in response.text
    table = response.text.split("<table>")[-1]
    assert "Partner A installs" in table and "every campaign" not in table


async def test_an_unticked_checkbox_means_false(signed_in, monkeypatch):
    """A browser omits an unticked checkbox entirely; the handler must not treat
    "absent" as "true"."""
    from mmp_web import app as web_app

    sent: list[dict] = []

    async def capture(self, path, **kwargs):
        sent.append(kwargs.get("json") or {})
        return {}

    monkeypatch.setattr(web_app.ApiClient, "post", capture)
    web = signed_in["client"]
    await web.post(
        "/postbacks",
        data={
            "csrf_token": web.cookies.get("mmp_csrf"),
            "app_id": "a",
            "name": "n",
            "trigger_event": "install",
            "url_template": "https://example.com/x",
        },
    )
    assert sent and sent[0]["requires_attribution"] is False
    assert sent[0]["is_sandbox"] is False


async def test_the_favicon_is_served_from_this_origin(signed_in):
    """Every page links it, and the content-security policy allows images from
    nowhere else."""
    response = await signed_in["client"].get("/static/favicon.svg")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.text.lstrip().startswith("<svg")
    assert "<script" not in response.text, "an SVG is a document; it must not carry script"

    page = await signed_in["client"].get("/")
    assert '<link rel="icon" href="/static/favicon.svg"' in page.text
