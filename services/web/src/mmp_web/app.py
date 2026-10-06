"""The dashboard.

Server-rendered, and deliberately thin: it holds no database connection, no
business logic and no service credentials. Every page is the result of calling
the API **as the signed-in user**, forwarding their own session cookie, so the
dashboard cannot surface anything that user could not fetch themselves. A
template bug cannot become a tenancy bug, because the tenancy decision was never
made here.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Form, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from mmp_core.context import request_id_var
from mmp_core.settings import Settings

from mmp_core import (
    HealthRegistry,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    configure_logging,
    get_logger,
    load_settings,
    service_lifespan,
)
from mmp_web.client import CSRF_COOKIE, SESSION_COOKIE, ApiClient, ApiError, Unauthorized
from mmp_web.formatting import (
    CHART_METRICS,
    count,
    default_range,
    delta,
    money,
    parse_date,
    percentage,
    sparkline,
    volume_chart,
)

log = get_logger(__name__)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
# The selected app ("project"), remembered per browser. A convenience, not a
# security boundary: every API call is still scoped by the session and by RLS,
# and an id in this cookie that the user cannot see is simply ignored.
APP_COOKIE = "mmp_app"
APP_COOKIE_MAX_AGE = 365 * 24 * 3600

LIVE_SCRIPT = Path(__file__).parent / "static" / "live.js"
APP_SCRIPT = Path(__file__).parent / "static" / "app.js"
FAVICON = Path(__file__).parent / "static" / "favicon.svg"

# Grouped, because twelve flat items is a list to read rather than a structure
# to navigate. Tracking comes first: the live view, the event catalogue and the
# SDK setup are what someone integrating an app reaches for every day. The rest
# follow what someone is doing — measuring, checking whether to trust the
# numbers, or changing configuration — rather than which service serves them.
NAV_ITEMS = [
    {"key": "overview", "label": "Overview", "href": "/", "group": "Track"},
    {"key": "live", "label": "Live events", "href": "/live", "group": "Track"},
    {"key": "events", "label": "Events", "href": "/events", "group": "Track"},
    {"key": "logs", "label": "Logs", "href": "/logs", "group": "Track"},
    {"key": "apps", "label": "Apps & SDK", "href": "/apps", "group": "Track"},
    {"key": "links", "label": "Tracking links", "href": "/links", "group": "Measure"},
    {"key": "attribution", "label": "Attribution", "href": "/attribution", "group": "Measure"},
    {"key": "fraud", "label": "Fraud", "href": "/fraud", "group": "Trust"},
    {"key": "skan", "label": "SKAdNetwork", "href": "/skan", "group": "Trust"},
    {"key": "postbacks", "label": "Postbacks", "href": "/postbacks", "group": "Configure"},
    {"key": "deeplinks", "label": "Deep links", "href": "/deep-links", "group": "Configure"},
    {"key": "integrations", "label": "Integrations", "href": "/integrations", "group": "Configure"},
    {"key": "export", "label": "Export", "href": "/export", "group": "Configure"},
]

# The datasets the export page offers, with a sentence each on what is in them.
# Held here rather than fetched: the API has no endpoint that lists them, and
# inventing one to populate a static list would be the wrong direction.
EXPORT_DATASETS = [
    {
        "name": "events",
        "title": "Events",
        "description": "Every event as received, including your own properties.",
    },
    {
        "name": "clicks",
        "title": "Clicks",
        "description": "Clicks through your tracking links, with campaign and sub-parameters.",
    },
    {
        "name": "attributions",
        "title": "Attributions",
        "description": (
            "One row per install, with the method, the winning click and the fraud verdict."
        ),
    },
]

# Severity is a number in the API — the same weights the fraud rules use — so
# the mapping to a colour lives here rather than being reinvented per template.
SEVERITY_CLASSES = {100: "sev-critical", 50: "sev-high", 25: "sev-medium", 10: "sev-low"}
VERDICT_CLASSES = {
    "fraudulent": "sev-critical",
    "suspicious": "sev-high",
    "clean": "active",
}


def severity_class(severity: int | None) -> str:
    """A CSS class for a severity weight.

    An unrecognised weight renders neutrally rather than falling through to
    whatever class happened to be last — a new severity should look
    unremarkable, not accidentally critical.
    """
    return SEVERITY_CLASSES.get(severity or 0, "sev-low")


def verdict_class(verdict: str | None) -> str:
    return VERDICT_CLASSES.get(verdict or "", "sev-low")


EVENT_CATEGORIES = (
    "lifecycle",
    "account",
    "commerce",
    "engagement",
    "content",
    "gaming",
    "custom",
)


def event_constants(catalogue: list[dict[str, Any]]) -> str:
    """A TypeScript constants object for the SDK, from the app's catalogue.

    Only names in use or defined — the full standard vocabulary would be forty
    lines of events the app does not send. SDK-owned names are left out too;
    the SDK sends those itself and refuses them from track().
    """
    chosen = [
        entry
        for entry in catalogue
        if (entry.get("defined") or entry.get("count_30d")) and not entry.get("sdk_owned")
    ]
    if not chosen:
        chosen = [
            entry
            for entry in catalogue
            if entry.get("name") in ("purchase", "add_to_cart", "view_item", "tutorial_complete")
        ]
    lines = ["export const Events = {"]
    for entry in chosen:
        name = str(entry["name"])
        constant = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper() or "EVENT"
        escaped = name.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'  {constant}: "{escaped}",')
    lines.append("} as const;")
    return "\n".join(lines)


def _share(part: int, whole: int) -> int:
    """A percentage of installs for the funnel bar, capped at 100: sessions can
    exceed installs, and a bar that overflows its track says less than one
    that is full."""
    if not whole:
        return 0
    return min(100, round(part / whole * 100))


def _share_display(part: int, whole: int) -> str:
    return percentage(part / whole, places=0) if whole else "—"


def track_snippet(entry: dict[str, Any]) -> str:
    """A copy-ready SDK call for one catalogue entry.

    Built from the event's recommended properties so what someone pastes into
    their app matches what the dashboard says the event expects. Revenue events
    carry money as integer minor units with a currency — the one shape the
    tracker accepts.
    """
    name = entry["name"]
    props = [p["name"] for p in entry.get("properties", [])]
    fields = ", ".join(f"{p}: ..." for p in props[:3])
    if entry.get("revenue"):
        inner = 'revenueMinor: 499, currency: "USD"'
        if fields:
            inner += f", properties: {{ {fields} }}"
        return f'await MMP.track("{name}", {{ {inner} }});'
    if fields:
        return f'await MMP.track("{name}", {{ properties: {{ {fields} }} }});'
    return f'await MMP.track("{name}");'


METHOD_NOTES = {
    "referrer": "Play Install Referrer carried the click id — the strongest signal.",
    "click_id": "The SDK reported a click id from a deferred deep link.",
    "device_match": "The advertising ID seen at click matched the one at install.",
    "organic": "No deterministic signal matched. Not a failure — the honest answer.",
    "probabilistic": "Fingerprint-based. This platform does not produce these.",
}

# The dashboard loads nothing from anywhere else, so the policy forbids every
# external source outright.
#
# Script is allowed from this origin only, for the live view — which cannot be
# live without it. That is a narrower change than it looks: 'self' permits a
# file this service serves, never inline script and never another host, so the
# property the policy exists for still holds. A dashboard that loads third-party
# JavaScript is one compromised CDN away from exfiltrating tenant data, and this
# one still cannot. connect-src 'self' lets that script poll this origin and
# nothing else, so even a script that went wrong could not send data elsewhere.
#
# 'unsafe-inline' for style is the one concession: the stylesheet is in the
# document, and hashing it would break on every edit for no gain when there is
# no external CSS to defend against.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; "
    "script-src 'self'; "
    "connect-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'"
)


def safe_next(candidate: str | None) -> str:
    """Only ever redirect within this site.

    A ``next`` parameter used at face value is an open redirect, and a login page
    is where one is most valuable to an attacker: the victim has just been asked
    to type a password, and the page they land on afterwards inherits that trust.
    A protocol-relative URL (``//evil.example``) is the case a naive
    startswith("/") check misses.
    """
    if not candidate or not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    return candidate


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings(service_name="web")
    configure_logging(service="web", level=settings.log_level, json_output=settings.log_json)
    registry = HealthRegistry(service="web", version=settings.version)
    api_base = str(getattr(settings, "api_base_url", None) or "http://127.0.0.1:8002")
    tracking_domain = str(getattr(settings, "tracking_domain", None) or "https://track.example.com")

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with service_lifespan(registry, drain_grace_seconds=settings.shutdown_grace_seconds):
            yield

    app = FastAPI(
        title="MMP Dashboard",
        version=settings.version,
        lifespan=lifespan,
        docs_url=None,
        openapi_url=None,
    )
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        SecurityHeadersMiddleware, extra={"content-security-policy": CONTENT_SECURITY_POLICY}
    )

    def client_for(request: Request) -> ApiClient:
        return ApiClient(
            base_url=api_base,
            session_token=request.cookies.get(SESSION_COOKIE),
            csrf_token=request.cookies.get(CSRF_COOKIE),
        )

    def login_redirect(request: Request) -> RedirectResponse:
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(f"/login?next={target}", status_code=status.HTTP_303_SEE_OTHER)

    async def page_context(request: Request, api: ApiClient, active: str) -> dict[str, Any]:
        """The shell every page needs: who is signed in, where they are, and
        which app they are looking at.

        The app is resolved once here, for every page, in this order: an
        ``app_id`` in the URL (so a pasted link opens what it was looking at),
        then the remembered cookie, then the only app if there is just one.
        With several apps and no choice made, the page shows the chooser
        instead of guessing — a dashboard that silently picks the first app is
        how someone reads the wrong product's numbers.
        """
        user = await api.get("/v1/auth/me")
        organizations = await api.get("/v1/organizations")
        current = next((org for org in organizations if org.get("role")), None)
        apps = await api.get("/v1/apps") or []
        valid = {app["id"] for app in apps}
        requested = request.query_params.get("app_id")
        remembered = request.cookies.get(APP_COOKIE)
        if requested in valid:
            current_app_id: str | None = requested
        elif remembered in valid:
            current_app_id = remembered
        elif len(apps) == 1:
            current_app_id = apps[0]["id"]
        else:
            current_app_id = None
        request.state.apps = apps
        request.state.current_app_id = current_app_id
        # Remember a choice that arrived by URL, so the next page follows it.
        request.state.set_app_cookie = (
            current_app_id if current_app_id and current_app_id != remembered else None
        )
        return {
            "request": request,
            "user": user,
            "organization": current,
            "nav_items": NAV_ITEMS,
            "active": active,
            "csrf_token": request.cookies.get(CSRF_COOKIE, ""),
            "request_id": request_id_var.get(),
            "apps": apps,
            "current_app": next((a for a in apps if a["id"] == current_app_id), None),
            "app_chooser": current_app_id is None and len(apps) > 1,
            "next_path": safe_next(
                f"{request.url.path}?{request.url.query}" if request.url.query else request.url.path
            ),
        }

    async def selection(
        request: Request, api: ApiClient
    ) -> tuple[list[dict[str, Any]], str | None, dt.date, dt.date]:
        """The current app (see page_context) and the date range from the URL.

        Dates default to the last seven days and live in the URL rather than in
        a session, so a view can be bookmarked and pasted to a colleague —
        which is how people actually share a number they are worried about.
        """
        apps = getattr(request.state, "apps", None)
        if apps is None:
            apps = await api.get("/v1/apps") or []
        selected: str | None = getattr(request.state, "current_app_id", None)
        if selected is None and len(apps) == 1:
            selected = apps[0]["id"]

        default_from, default_to = default_range()
        from_date = parse_date(request.query_params.get("from"), default_from)
        to_date = parse_date(request.query_params.get("to"), default_to)
        if to_date < from_date:
            from_date, to_date = to_date, from_date
        return apps, selected, from_date, to_date

    def presets_for(
        request: Request, app_id: str | None, from_date: dt.date, to_date: dt.date
    ) -> list[dict[str, Any]]:
        """Quick-range links for the controls: this page, a different range.

        Links rather than buttons, so they work without script and can be
        bookmarked like every other dashboard view.
        """
        today = dt.datetime.now(dt.UTC).date()
        presets = []
        for label, days in (("7d", 7), ("14d", 14), ("30d", 30), ("90d", 90)):
            start = today - dt.timedelta(days=days)
            query = urlencode({"app_id": app_id or "", "from": start, "to": today})
            presets.append(
                {
                    "label": label,
                    "href": f"{request.url.path}?{query}",
                    "on": from_date == start and to_date == today,
                }
            )
        return presets

    def _range_params(app_id: str, from_date: dt.date, to_date: dt.date) -> str:
        """The reporting endpoints take timestamps and refuse an unbounded range.

        `until` is the end of the chosen day: a from==to selection must mean
        "that day", not an empty window.
        """
        return urlencode(
            {
                "app_id": app_id,
                "since": f"{from_date.isoformat()}T00:00:00Z",
                "until": f"{to_date.isoformat()}T23:59:59Z",
            }
        )

    # Pages that make sense without a chosen app: the sign-in, and the app list
    # itself, which is where a new app is added.
    CHOOSER_EXEMPT = {"login.html", "apps.html", "choose_app.html"}

    def render(name: str, context: dict[str, Any], status_code: int = 200) -> HTMLResponse:
        request = context.pop("request")
        if context.get("app_chooser") and name not in CHOOSER_EXEMPT:
            name = "choose_app.html"
        response = TEMPLATES.TemplateResponse(
            request=request, name=name, context=context, status_code=status_code
        )
        remember = getattr(request.state, "set_app_cookie", None)
        if remember:
            response.set_cookie(
                APP_COOKIE,
                remember,
                max_age=APP_COOKIE_MAX_AGE,
                path="/",
                httponly=True,
                samesite="lax",
            )
        return response

    @app.post("/app/select", include_in_schema=False)
    async def select_app(
        request: Request,
        app_id: str = Form(...),
        next: str = Form("/"),
        csrf_token: str = Form(""),
    ) -> Response:
        """Choose the app every page follows. The choice is checked against the
        user's own apps before it is remembered."""
        api = client_for(request)
        target = safe_next(next)
        if not csrf_token or csrf_token != request.cookies.get(CSRF_COOKIE):
            return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
        try:
            apps = await api.get("/v1/apps") or []
        except Unauthorized:
            return login_redirect(request)
        except ApiError:
            return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
        if app_id not in {a["id"] for a in apps}:
            return RedirectResponse("/apps", status_code=status.HTTP_303_SEE_OTHER)
        # A stale app_id in the destination would override the choice just made.
        if "app_id=" in target:
            target = target.split("?", 1)[0]
        response = RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(
            APP_COOKIE, app_id, max_age=APP_COOKIE_MAX_AGE, path="/", httponly=True, samesite="lax"
        )
        return response

    # ---------------------------------------------------------------- auth
    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_form(request: Request) -> HTMLResponse:
        return render(
            "login.html",
            {
                "request": request,
                "next_path": safe_next(request.query_params.get("next")),
                "error": None,
            },
        )

    @app.post("/login", include_in_schema=False)
    async def login_submit(
        request: Request,
        email: str = Form(...),
        password: str = Form(...),
        next: str = Form("/"),
    ) -> Response:
        try:
            # Called directly rather than through ApiClient: this is the one
            # request with no session to forward, and the response's Set-Cookie
            # headers are the thing we actually need from it.
            async with httpx.AsyncClient(base_url=api_base, timeout=10.0) as http:
                upstream = await http.post(
                    "/v1/auth/login", json={"email": email, "password": password}
                )
        except Exception:
            log.exception("login_upstream_failed")
            return render(
                "login.html",
                {
                    "request": request,
                    "next_path": safe_next(next),
                    "error": "The service is unavailable. Try again in a moment.",
                },
                status_code=503,
            )

        if upstream.status_code != 200:
            # One message for every failure, matching the API: a distinct
            # "no such account" would turn this form into an account-existence
            # oracle for anyone with a list of email addresses.
            return render(
                "login.html",
                {
                    "request": request,
                    "next_path": safe_next(next),
                    "error": "Incorrect email or password.",
                },
                status_code=401,
            )

        response = RedirectResponse(safe_next(next), status_code=status.HTTP_303_SEE_OTHER)
        # The API's own Set-Cookie headers are passed through unchanged, so the
        # dashboard never mints or stores a session of its own.
        for cookie in upstream.headers.get_list("set-cookie"):
            response.raw_headers.append((b"set-cookie", cookie.encode("latin-1")))
        return response

    @app.post("/logout", include_in_schema=False)
    async def logout(request: Request) -> Response:
        api = client_for(request)
        # Already-gone sessions are the common case here (an expired cookie, a
        # second tab that logged out first). Whatever the API says, the cookies
        # are cleared below: a failed logout must never leave someone signed in.
        with contextlib.suppress(ApiError):
            await api.post("/v1/auth/logout")
        response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie(CSRF_COOKIE, path="/")
        return response

    # ---------------------------------------------------------------- pages
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def overview(request: Request) -> Response:
        api = client_for(request)
        try:
            context = await page_context(request, api, "overview")
            apps, app_id, from_date, to_date = await selection(request, api)
            context |= {
                "apps": apps,
                "selected_app_id": app_id,
                "from_date": from_date.isoformat(),
                "to_date": to_date.isoformat(),
                "presets": presets_for(request, app_id, from_date, to_date),
                "error": None,
                "stats": [],
                "series": [],
                "campaigns": [],
                "chart": "",
                "cache_state": None,
                "install_rate_display": percentage(None),
                "sessions_display": count(None),
                "conversions_display": count(None),
                "previous_range": "",
                "metric": "events",
                "metric_links": [],
                "funnel": [],
            }
            if not app_id:
                return render("overview.html", context)

            query = f"app_id={app_id}&from={from_date}&to={to_date}"
            data, headers = await api.get_with_headers(f"/v1/analytics/overview?{query}")
            campaigns = await api.get(f"/v1/analytics/campaigns?{query}&limit=25") or []

            # The same span immediately before this one, so each tile can say
            # whether the number moved. Read through the same cached endpoint;
            # a comparison that needed its own query would be the first thing
            # dropped under load.
            metric = request.query_params.get("metric", "events")
            if metric not in CHART_METRICS:
                metric = "events"
            span = (to_date - from_date).days + 1
            previous_to = from_date - dt.timedelta(days=1)
            previous_from = previous_to - dt.timedelta(days=span - 1)
            previous = await api.get(
                f"/v1/analytics/overview?app_id={app_id}&from={previous_from}&to={previous_to}"
            )
            before = (previous or {}).get("totals", {})

            totals = data["totals"]
            series = data["series"]

            def tile(label: str, key: str, display: str) -> dict[str, Any]:
                return {
                    "label": label,
                    "value": totals[key],
                    "display": display,
                    "delta": delta(totals[key], before.get(key)),
                    # Clicks come from a different rollup and are not in the
                    # series, so that tile has no sparkline rather than a flat one.
                    "spark": sparkline([int(point.get(key) or 0) for point in series])
                    if key != "clicks"
                    else "",
                }

            context |= {
                "stats": [
                    tile("Installs", "installs", count(totals["installs"])),
                    tile("Clicks", "clicks", count(totals["clicks"])),
                    tile("Events", "events", count(totals["events"])),
                    tile("Revenue", "revenue_minor", money(totals["revenue_minor"])),
                ],
                "install_rate_display": percentage(totals["install_rate"]),
                "sessions_display": count(totals["sessions"]),
                "conversions_display": count(totals["conversions"]),
                "previous_range": f"{previous_from} to {previous_to}",
                "series": data["series"],
                "chart": volume_chart(data["series"], metric=metric),
                "metric": metric,
                "metric_links": [
                    {
                        "key": key,
                        "label": label,
                        "on": key == metric,
                        "href": "/?"
                        + urlencode(
                            {"app_id": app_id, "from": from_date, "to": to_date, "metric": key}
                        ),
                    }
                    for key, label in CHART_METRICS.items()
                ],
                # The funnel, as shares of installs. A share with no installs is
                # undefined rather than zero, like every other rate here.
                "funnel": [
                    {
                        "label": "Installs",
                        "value": count(totals["installs"]),
                        "share": 100 if totals["installs"] else 0,
                        "share_display": "100%" if totals["installs"] else "—",
                    },
                    {
                        "label": "Sessions",
                        "value": count(totals["sessions"]),
                        "share": _share(totals["sessions"], totals["installs"]),
                        "share_display": _share_display(totals["sessions"], totals["installs"]),
                    },
                    {
                        "label": "Conversions",
                        "value": count(totals["conversions"]),
                        "share": _share(totals["conversions"], totals["installs"]),
                        "share_display": _share_display(totals["conversions"], totals["installs"]),
                    },
                ],
                "campaigns": [
                    row
                    | {
                        "install_rate_display": percentage(row["install_rate"]),
                        "revenue_display": money(row["revenue_minor"]),
                    }
                    for row in campaigns
                ],
                "cache_state": headers.get("x-cache"),
            }
            return render("overview.html", context)
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            return await _error_page(request, api, "overview", "overview.html", exc)

    async def apps_context(request: Request, api: ApiClient, app_id: str | None) -> dict[str, Any]:
        """The apps page: the list, and the setup panel for one of them.

        The panel needs the app's keys and its event catalogue — the constants
        it generates come from the catalogue, so names in code match names in
        reports. Both are read as the signed-in user; a viewer who cannot list
        keys sees the page without them.
        """
        context = await page_context(request, api, "apps")
        apps = await api.get("/v1/apps") or []
        selected = next((a for a in apps if a["id"] == app_id), None)
        keys: list[dict[str, Any]] = []
        constants = ""
        if selected:
            with contextlib.suppress(ApiError):
                keys = await api.get(f"/v1/apps/{selected['id']}/keys") or []
            catalogue = await api.get(f"/v1/apps/{selected['id']}/events") or []
            constants = event_constants(catalogue)
        context |= {
            "apps": apps,
            "selected_app": selected,
            "keys": keys,
            "event_constants": constants,
            "tracking_domain": tracking_domain,
            "notice": request.query_params.get("notice"),
            "new_key": None,
            "error": None,
        }
        return context

    @app.get("/apps", response_class=HTMLResponse, include_in_schema=False)
    async def apps_page(request: Request) -> Response:
        api = client_for(request)
        try:
            context = await apps_context(request, api, request.query_params.get("app_id"))
            if context["selected_app"] is None:
                current = getattr(request.state, "current_app_id", None) or (
                    context["apps"][0]["id"] if context["apps"] else None
                )
                if current:
                    context = await apps_context(request, api, current)
            return render("apps.html", context)
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            return await _error_page(request, api, "apps", "apps.html", exc)

    @app.post("/apps", include_in_schema=False)
    async def create_app_action(
        request: Request,
        name: str = Form(...),
        platform: str = Form(...),
        identifier: str = Form(""),
        install_window_days: int = Form(7),
        event_window_days: int = Form(30),
        csrf_token: str = Form(""),
    ) -> Response:
        api = client_for(request)
        if not csrf_token or csrf_token != request.cookies.get(CSRF_COOKIE):
            return RedirectResponse("/apps", status_code=status.HTTP_303_SEE_OTHER)

        body: dict[str, Any] = {
            "name": name.strip(),
            "platform": platform,
            "install_window_days": install_window_days,
            "event_window_days": event_window_days,
        }
        # One field in the form, two in the API. Which one it is follows from
        # the platform, and asking someone to know that is asking them to know
        # our schema.
        identifier = identifier.strip()
        if identifier:
            if platform == "ios":
                body["ios_bundle_id"] = identifier
            else:
                body["android_package_name"] = identifier

        try:
            await api.post("/v1/apps", json=body)
            return _notice("/apps", f"Created {name.strip()}")
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            return _notice("/apps", exc.detail)

    @app.post("/apps/{app_id}/keys", include_in_schema=False)
    async def create_key_action(
        request: Request,
        app_id: str,
        name: str = Form("default"),
        environment: str = Form("prod"),
        kind: str = Form("sdk"),
        csrf_token: str = Form(""),
    ) -> Response:
        """Renders the result instead of redirecting.

        The raw key is returned once and never again, so it must reach the page
        in a response body rather than a URL — a redirect would put a live
        credential into the address bar, the browser's history, and every
        access log between here and the user.
        """
        api = client_for(request)
        if not csrf_token or csrf_token != request.cookies.get(CSRF_COOKIE):
            return RedirectResponse("/apps", status_code=status.HTTP_303_SEE_OTHER)
        try:
            created = await api.post(
                f"/v1/apps/{app_id}/keys",
                json={"name": name.strip() or "default", "environment": environment, "kind": kind},
            )
            context = await apps_context(request, api, app_id)
            context["new_key"] = created
            return render("apps.html", context)
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            return _notice(f"/apps?app_id={app_id}", exc.detail)

    @app.post("/apps/{app_id}/keys/{key_id}/rotate", include_in_schema=False)
    async def rotate_key_action(
        request: Request, app_id: str, key_id: str, csrf_token: str = Form("")
    ) -> Response:
        """Rotation overlaps: the old key keeps working for a grace period so an
        app in the field is never left without a credential. The new key is
        shown once, in the body, like a created one."""
        api = client_for(request)
        if not csrf_token or csrf_token != request.cookies.get(CSRF_COOKIE):
            return RedirectResponse("/apps", status_code=status.HTTP_303_SEE_OTHER)
        try:
            rotated = await api.post(f"/v1/apps/{app_id}/keys/{key_id}/rotate")
            context = await apps_context(request, api, app_id)
            context["new_key"] = rotated
            return render("apps.html", context)
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            return _notice(f"/apps?app_id={app_id}", exc.detail)

    @app.post("/apps/{app_id}/keys/{key_id}/revoke", include_in_schema=False)
    async def revoke_key_action(
        request: Request, app_id: str, key_id: str, csrf_token: str = Form("")
    ) -> Response:
        api = client_for(request)
        if not csrf_token or csrf_token != request.cookies.get(CSRF_COOKIE):
            return RedirectResponse("/apps", status_code=status.HTTP_303_SEE_OTHER)
        try:
            await api.delete(f"/v1/apps/{app_id}/keys/{key_id}")
            return _notice(f"/apps?app_id={app_id}", "Revoked the key. It stops working now.")
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            return _notice(f"/apps?app_id={app_id}", exc.detail)

    @app.post("/campaigns", include_in_schema=False)
    async def create_campaign_action(
        request: Request,
        app_id: str = Form(...),
        name: str = Form(...),
        source: str = Form(""),
        medium: str = Form(""),
        csrf_token: str = Form(""),
    ) -> Response:
        api = client_for(request)
        if not csrf_token or csrf_token != request.cookies.get(CSRF_COOKIE):
            return RedirectResponse("/links", status_code=status.HTTP_303_SEE_OTHER)
        body: dict[str, Any] = {"app_id": app_id, "name": name.strip()}
        if source.strip():
            body["source"] = source.strip()
        if medium.strip():
            body["medium"] = medium.strip()
        try:
            await api.post("/v1/campaigns", json=body)
            return _notice("/links", f"Created campaign {name.strip()}")
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            return _notice("/links", exc.detail)

    @app.post("/links", include_in_schema=False)
    async def create_link_action(
        request: Request,
        campaign_id: str = Form(...),
        name: str = Form(...),
        fallback_url: str = Form(...),
        android_url: str = Form(""),
        ios_url: str = Form(""),
        deep_link_path: str = Form(""),
        csrf_token: str = Form(""),
    ) -> Response:
        api = client_for(request)
        if not csrf_token or csrf_token != request.cookies.get(CSRF_COOKIE):
            return RedirectResponse("/links", status_code=status.HTTP_303_SEE_OTHER)
        body: dict[str, Any] = {
            "campaign_id": campaign_id,
            "name": name.strip(),
            "fallback_url": fallback_url.strip(),
        }
        for field, value in (
            ("android_url", android_url),
            ("ios_url", ios_url),
            ("deep_link_path", deep_link_path),
        ):
            if value.strip():
                body[field] = value.strip()
        try:
            await api.post("/v1/tracking-links", json=body)
            return _notice("/links", f"Created {name.strip()}")
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            return _notice("/links", exc.detail)

    def _notice(path: str, message: str) -> RedirectResponse:
        """Post-redirect-get, so a refresh does not offer to create it again.

        Only ever used for messages. A secret goes in a response body, never in
        a URL — see the key handler above.
        """
        return RedirectResponse(
            f"{path}?{urlencode({'notice': message})}", status_code=status.HTTP_303_SEE_OTHER
        )

    @app.get("/links", response_class=HTMLResponse, include_in_schema=False)
    async def links_page(request: Request) -> Response:
        api = client_for(request)
        try:
            context = await page_context(request, api, "links")
            context |= {
                "links": await api.get("/v1/tracking-links") or [],
                # Both needed to create a link: a link belongs to a campaign,
                # and a campaign belongs to an app.
                "apps": await api.get("/v1/apps") or [],
                "campaigns": await api.get("/v1/campaigns") or [],
                "tracking_domain": tracking_domain,
                "notice": request.query_params.get("notice"),
                "error": None,
            }
            return render("links.html", context)
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            return await _error_page(request, api, "links", "links.html", exc)

    @app.get("/events", response_class=HTMLResponse, include_in_schema=False)
    async def events_page(request: Request) -> Response:
        api = client_for(request)
        try:
            context = await page_context(request, api, "events")
            apps, app_id, from_date, to_date = await selection(request, api)
            context |= {
                "apps": apps,
                "selected_app_id": app_id,
                "from_date": from_date.isoformat(),
                "to_date": to_date.isoformat(),
                "presets": presets_for(request, app_id, from_date, to_date),
                "notice": request.query_params.get("notice"),
                "events": [],
                "catalogue": [],
                "categories": EVENT_CATEGORIES,
                "blocked_count": 0,
                "distinct_display": count(None),
                "period_total_display": count(None),
                "period_revenue_display": money(None),
                "error": None,
            }
            if app_id:
                rows = (
                    await api.get(
                        f"/v1/analytics/events?app_id={app_id}&from={from_date}&to={to_date}"
                    )
                    or []
                )
                peak = max((row["event_count"] for row in rows), default=0) or 1
                catalogue = await api.get(f"/v1/apps/{app_id}/events") or []
                context |= {
                    "events": [
                        row
                        | {
                            "revenue_display": money(row["revenue_minor"]),
                            "share": round(row["event_count"] / peak * 100, 1),
                        }
                        for row in rows
                    ],
                    "catalogue": [
                        entry
                        | {
                            "spark": sparkline(entry["trend"]) if entry["count_30d"] else "",
                            "count_display": count(entry["count_30d"]),
                            "revenue_display": (
                                money(entry["revenue_minor_30d"]) if entry["revenue"] else "—"
                            ),
                            "snippet": track_snippet(entry),
                        }
                        for entry in catalogue
                    ],
                    "blocked_count": sum(1 for e in catalogue if e["status"] == "blocked"),
                    "distinct_display": count(sum(1 for e in catalogue if e["count_30d"])),
                    "period_total_display": count(sum(row["event_count"] for row in rows)),
                    "period_revenue_display": money(sum(row["revenue_minor"] for row in rows)),
                }
            return render("events.html", context)
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            return await _error_page(request, api, "events", "events.html", exc)

    def _events_redirect(app_id: str, notice: str) -> RedirectResponse:
        query = urlencode({"app_id": app_id, "notice": notice})
        return RedirectResponse(f"/events?{query}", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/events/define", include_in_schema=False)
    async def define_event_action(
        request: Request,
        app_id: str = Form(...),
        name: str = Form(...),
        display_name: str = Form(""),
        category: str = Form("custom"),
        description: str = Form(""),
        revenue: str = Form(""),
        csrf_token: str = Form(""),
    ) -> Response:
        api = client_for(request)
        if not csrf_token or csrf_token != request.cookies.get(CSRF_COOKIE):
            return RedirectResponse("/events", status_code=status.HTTP_303_SEE_OTHER)
        body: dict[str, Any] = {
            "name": name.strip(),
            "category": category if category in EVENT_CATEGORIES else "custom",
            "revenue": bool(revenue),
        }
        if display_name.strip():
            body["display_name"] = display_name.strip()
        if description.strip():
            body["description"] = description.strip()
        try:
            await api.post(f"/v1/apps/{app_id}/events", json=body)
            return _events_redirect(app_id, f"Defined {name.strip()}")
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            return _events_redirect(app_id, exc.detail)

    @app.post("/events/{definition_id}/status", include_in_schema=False)
    async def event_status_action(
        request: Request,
        definition_id: str,
        app_id: str = Form(...),
        status_value: str = Form(..., alias="status"),
        csrf_token: str = Form(""),
    ) -> Response:
        api = client_for(request)
        if not csrf_token or csrf_token != request.cookies.get(CSRF_COOKIE):
            return RedirectResponse("/events", status_code=status.HTTP_303_SEE_OTHER)
        if status_value not in ("active", "blocked"):
            return _events_redirect(app_id, "unknown status")
        try:
            updated = await api.patch(
                f"/v1/apps/{app_id}/events/{definition_id}", json={"status": status_value}
            )
            verb = "Blocked" if status_value == "blocked" else "Unblocked"
            return _events_redirect(app_id, f"{verb} {updated['name']}")
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            return _events_redirect(app_id, exc.detail)

    @app.post("/events/{definition_id}/delete", include_in_schema=False)
    async def event_delete_action(
        request: Request,
        definition_id: str,
        app_id: str = Form(...),
        csrf_token: str = Form(""),
    ) -> Response:
        api = client_for(request)
        if not csrf_token or csrf_token != request.cookies.get(CSRF_COOKIE):
            return RedirectResponse("/events", status_code=status.HTTP_303_SEE_OTHER)
        try:
            await api.delete(f"/v1/apps/{app_id}/events/{definition_id}")
            return _events_redirect(app_id, "Removed the definition")
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            return _events_redirect(app_id, exc.detail)

    LOG_KINDS = ("events", "clicks", "installs")
    LOG_DATASETS = {"events": "events", "clicks": "clicks", "installs": "attributions"}
    LOG_FILTERS = {
        "events": ("event_name", "platform", "anonymous_id", "user_id"),
        "clicks": ("platform", "country"),
        "installs": ("method", "verdict", "anonymous_id"),
    }

    @app.get("/logs", response_class=HTMLResponse, include_in_schema=False)
    async def logs_page(request: Request) -> Response:
        """Raw rows with filters and keyset paging, all carried in the URL.

        The cursor is opaque to this layer: it is whatever the API returned,
        passed back as given, so a bookmarked page three still means page three.
        """
        api = client_for(request)
        kind = request.query_params.get("kind", "events")
        if kind not in LOG_KINDS:
            kind = "events"
        try:
            context = await page_context(request, api, "logs")
            apps, app_id, from_date, to_date = await selection(request, api)
            filters = {
                name: request.query_params.get(name, "").strip() for name in LOG_FILTERS[kind]
            }
            cursor = request.query_params.get("cursor", "")
            base = {"kind": kind, "app_id": app_id or "", "from": from_date, "to": to_date}
            base |= {k: v for k, v in filters.items() if v}
            context |= {
                "apps": apps,
                "selected_app_id": app_id,
                "from_date": from_date.isoformat(),
                "to_date": to_date.isoformat(),
                "kind": kind,
                "filters": filters,
                "cursor": cursor,
                "items": [],
                "next_href": None,
                "first_href": f"/logs?{urlencode(base)}",
                "export_dataset": LOG_DATASETS[kind],
                "tabs": [
                    {
                        "label": label,
                        "href": "/logs?"
                        + urlencode(
                            {"kind": k, "app_id": app_id or "", "from": from_date, "to": to_date}
                        ),
                        "on": k == kind,
                    }
                    for k, label in (
                        ("events", "Events"),
                        ("clicks", "Clicks"),
                        ("installs", "Installs"),
                    )
                ],
                "verdict_class": verdict_class,
                "error": None,
            }
            if app_id:
                query = {"app_id": app_id, "from": from_date, "to": to_date, "limit": 50}
                query |= {k: v for k, v in filters.items() if v}
                if cursor:
                    query["cursor"] = cursor
                page = await api.get(f"/v1/logs/{kind}?{urlencode(query)}") or {}
                items = page.get("items", [])
                if kind == "events":
                    items = [
                        row
                        | {
                            "revenue_display": (
                                money(row["revenue_minor"], row["currency"] or "USD")
                                if row.get("revenue_minor") is not None
                                else "—"
                            ),
                            "properties_json": json.dumps(row.get("properties") or {}, indent=2),
                        }
                        for row in items
                    ]
                context["items"] = items
                if page.get("next_cursor"):
                    context["next_href"] = (
                        f"/logs?{urlencode(base | {'cursor': page['next_cursor']})}"
                    )
            return render("logs.html", context)
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            return await _error_page(request, api, "logs", "logs.html", exc)

    @app.get("/attribution", response_class=HTMLResponse, include_in_schema=False)
    async def attribution_page(request: Request) -> Response:
        api = client_for(request)
        try:
            context = await page_context(request, api, "attribution")
            apps, app_id, from_date, to_date = await selection(request, api)
            anonymous_id = (request.query_params.get("anonymous_id") or "").strip()
            empty: dict[str, Any] = {
                "total": 0,
                "attributed": 0,
                "organic": 0,
                "match_rate": None,
                "by_method": [],
            }
            context |= {
                "apps": apps,
                "selected_app_id": app_id,
                "from_date": from_date.isoformat(),
                "to_date": to_date.isoformat(),
                "summary": empty,
                "presets": presets_for(request, app_id, from_date, to_date),
                "match_rate_display": percentage(None),
                "method_notes": METHOD_NOTES,
                "anonymous_id": anonymous_id,
                "device_history": [],
                "error": None,
            }
            if app_id:
                summary = await api.get(
                    f"/v1/attributions/summary?app_id={app_id}&from={from_date}&to={to_date}"
                )
                context["summary"] = summary
                context["match_rate_display"] = percentage(summary["match_rate"])
                if anonymous_id:
                    context["device_history"] = (
                        await api.get(
                            f"/v1/attributions/lookup?app_id={app_id}&anonymous_id={anonymous_id}"
                        )
                        or []
                    )
            return render("attribution.html", context)
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            return await _error_page(request, api, "attribution", "attribution.html", exc)

    @app.get("/export/{dataset}", include_in_schema=False)
    async def download_export(request: Request, dataset: str) -> Response:
        """Proxy the download rather than linking straight at the API.

        A direct link is one hop shorter and works perfectly in development,
        where both services answer on 127.0.0.1 and cookies ignore the port. It
        breaks the moment they are deployed on different hostnames: the session
        cookie is scoped to the dashboard's host, so the browser would not send
        it to the API and every download would 401 — with nothing in the
        dashboard's logs to explain why.

        Streaming through here costs a hop and works in every deployment.
        """
        if dataset not in {item["name"] for item in EXPORT_DATASETS}:
            return RedirectResponse("/export", status_code=status.HTTP_303_SEE_OTHER)

        api = client_for(request)
        query = urlencode(
            {
                key: value
                for key, value in request.query_params.items()
                if key in {"app_id", "since", "until"}
            }
        )
        try:
            async with api.stream("GET", f"/v1/exports/{dataset}?{query}") as upstream:
                # Read fully before responding: a StreamingResponse would
                # outlive this context manager and read from a closed client.
                # The API caps an export at a million rows, so this is bounded —
                # and if that cap ever rises, this is the line that has to
                # change with it.
                body = await upstream.aread()
                return Response(
                    content=body,
                    media_type="text/csv",
                    headers={
                        "content-disposition": upstream.headers.get(
                            "content-disposition", f'attachment; filename="{dataset}.csv"'
                        ),
                        "cache-control": "no-store",
                    },
                )
        except Unauthorized:
            return login_redirect(request)
        except ApiError:
            return RedirectResponse("/export", status_code=status.HTTP_303_SEE_OTHER)

    # ------------------------------------------------------------- live
    @app.get("/live", response_class=HTMLResponse, include_in_schema=False)
    async def live_page(request: Request) -> Response:
        api = client_for(request)
        try:
            context = await page_context(request, api, "live")
            apps, app_id, _from, _to = await selection(request, api)
            context |= {
                "apps": apps,
                "selected_app_id": app_id,
                "tracking_domain": tracking_domain,
                "error": None,
            }
            return render("live.html", context)
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            return await _error_page(request, api, "live", "live.html", exc)

    @app.get("/live/feed", include_in_schema=False)
    async def live_feed_proxy(request: Request) -> Response:
        """The live view's poll target, answered as JSON in every case.

        Never a redirect to the login page: this is fetched by script, and a 303
        to an HTML form would arrive as a successful response the script cannot
        parse. A status the script understands lets it say "your session ended"
        instead of spinning.
        """
        api = client_for(request)
        params = {
            key: value for key, value in request.query_params.items() if key in {"app_id", "since"}
        }
        try:
            data = await api.get(f"/v1/live?{urlencode(params)}")
        except Unauthorized:
            return JSONResponse({"error": "session_expired"}, status_code=401)
        except ApiError as exc:
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
        except httpx.HTTPError:
            return JSONResponse({"error": "api_unreachable"}, status_code=502)
        return JSONResponse(data, headers={"cache-control": "no-store"})

    @app.get("/static/live.js", include_in_schema=False)
    async def live_script() -> Response:
        """Served from this origin, which is the only place script-src allows.

        Read per request rather than cached in memory so an edit shows up on
        reload in development; the file is a few kilobytes and this is not the
        hot path.
        """
        return Response(
            content=LIVE_SCRIPT.read_bytes(),
            media_type="text/javascript",
            headers={"cache-control": "no-cache", "x-content-type-options": "nosniff"},
        )

    @app.get("/static/app.js", include_in_schema=False)
    async def app_script() -> Response:
        """The shell's own script: theme, copy buttons, the drawer. Same origin,
        same rules as live.js — it never turns data into markup."""
        return Response(
            content=APP_SCRIPT.read_bytes(),
            media_type="text/javascript",
            headers={"cache-control": "no-cache", "x-content-type-options": "nosniff"},
        )

    @app.get("/static/favicon.svg", include_in_schema=False)
    async def favicon() -> Response:
        """Same origin, like everything else this page loads.

        Cached for a day: it is requested on every page load and changes about
        as often as the product's name does.
        """
        return Response(
            content=FAVICON.read_bytes(),
            media_type="image/svg+xml",
            headers={"cache-control": "public, max-age=86400", "x-content-type-options": "nosniff"},
        )

    # ------------------------------------------------------------ fraud
    @app.get("/fraud", response_class=HTMLResponse, include_in_schema=False)
    async def fraud_page(request: Request) -> Response:
        api = client_for(request)
        try:
            context = await page_context(request, api, "fraud")
            apps, app_id, from_date, to_date = await selection(request, api)
            context |= {
                "apps": apps,
                "selected_app_id": app_id,
                "from_date": from_date.isoformat(),
                "to_date": to_date.isoformat(),
                "findings": [],
                "flagged": [],
                "fraudulent": 0,
                "suspicious": 0,
                "presets": presets_for(request, app_id, from_date, to_date),
                "severity_class": severity_class,
                "verdict_class": verdict_class,
                "error": None,
            }
            if app_id:
                window = _range_params(app_id, from_date, to_date)
                context["findings"] = await api.get(f"/v1/fraud/findings?{window}") or []
                flagged = await api.get(f"/v1/fraud/installs?{window}") or []
                context["flagged"] = flagged
                # Counted here rather than asking the API for a summary it does
                # not have. The list is capped at 500, so these are counts of
                # what is shown, which is what the table beside them displays.
                context["fraudulent"] = sum(
                    1 for row in flagged if row.get("fraud_verdict") == "fraudulent"
                )
                context["suspicious"] = sum(
                    1 for row in flagged if row.get("fraud_verdict") == "suspicious"
                )
            return render("fraud.html", context)
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            return await _error_page(request, api, "fraud", "fraud.html", exc)

    # ------------------------------------------------------- skadnetwork
    @app.get("/skan", response_class=HTMLResponse, include_in_schema=False)
    async def skan_page(request: Request) -> Response:
        api = client_for(request)
        try:
            context = await page_context(request, api, "skan")
            apps, app_id, from_date, to_date = await selection(request, api)
            context |= {
                "apps": apps,
                "selected_app_id": app_id,
                "from_date": from_date.isoformat(),
                "to_date": to_date.isoformat(),
                "rows": [],
                "caveat": "",
                "presets": presets_for(request, app_id, from_date, to_date),
                "total_winning": 0,
                "total_non_winning": 0,
                "total_suppressed": 0,
                "total_redownloads": 0,
                "conversion_values": [],
                "error": None,
            }
            if app_id:
                window = _range_params(app_id, from_date, to_date)
                summary = await api.get(f"/v1/skan/summary?{window}") or {}
                rows = summary.get("rows", [])
                context |= {
                    "rows": rows,
                    # Carried from the API rather than written into the
                    # template, so the warning cannot be lost in a redesign.
                    "caveat": summary.get("caveat", ""),
                    "total_winning": sum(r["winning_postbacks"] for r in rows),
                    "total_non_winning": sum(r["non_winning_postbacks"] for r in rows),
                    "total_suppressed": sum(r["suppressed"] for r in rows),
                    "total_redownloads": sum(r["redownloads"] for r in rows),
                    "conversion_values": (
                        await api.get(f"/v1/skan/conversion-values?app_id={app_id}") or []
                    ),
                }
            return render("skan.html", context)
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            return await _error_page(request, api, "skan", "skan.html", exc)

    # --------------------------------------------------------- deep links
    # ---------------------------------------------------------- postbacks
    @app.get("/postbacks", response_class=HTMLResponse, include_in_schema=False)
    async def postbacks_page(request: Request) -> Response:
        api = client_for(request)
        try:
            context = await page_context(request, api, "postbacks")
            apps, app_id, _from, _to = await selection(request, api)
            context |= {
                "apps": apps,
                "selected_app_id": app_id,
                "rules": [],
                "campaigns": [],
                "variables": [],
                "scoped_only": [],
                "notice": request.query_params.get("notice"),
                "error": None,
            }
            if app_id:
                variables = await api.get("/v1/postback-rules/variables") or {}
                campaigns = await api.get(f"/v1/campaigns?app_id={app_id}") or []
                context |= {
                    "rules": await api.get(f"/v1/postback-rules?app_id={app_id}") or [],
                    "campaigns": campaigns,
                    "campaign_names": {c["id"]: c["name"] for c in campaigns},
                    "variables": variables.get("variables", []),
                    "scoped_only": variables.get("campaign_scoped_only", []),
                }
            return render("postbacks.html", context)
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            return await _error_page(request, api, "postbacks", "postbacks.html", exc)

    @app.post("/postbacks", include_in_schema=False)
    async def create_postback_action(
        request: Request,
        app_id: str = Form(...),
        name: str = Form(...),
        trigger_event: str = Form(...),
        url_template: str = Form(...),
        campaign_id: str = Form(""),
        method: str = Form("GET"),
        requires_attribution: str = Form(""),
        is_sandbox: str = Form(""),
        csrf_token: str = Form(""),
    ) -> Response:
        api = client_for(request)
        back = f"/postbacks?{urlencode({'app_id': app_id})}"
        if not csrf_token or csrf_token != request.cookies.get(CSRF_COOKIE):
            return RedirectResponse(back, status_code=status.HTTP_303_SEE_OTHER)
        body: dict[str, Any] = {
            "app_id": app_id,
            "name": name.strip(),
            "trigger_event": trigger_event.strip(),
            "url_template": url_template.strip(),
            "method": "POST" if method == "POST" else "GET",
            # An unticked checkbox is absent from a form post, not "false".
            "requires_attribution": bool(requires_attribution),
            # Sandbox renders the request and records it without sending it, so a
            # rule can be checked on Live events before the partner hears anything.
            "is_sandbox": bool(is_sandbox),
        }
        if campaign_id:
            body["campaign_id"] = campaign_id
        try:
            await api.post("/v1/postback-rules", json=body)
            return _app_notice("/postbacks", app_id, f"Created {name.strip()}")
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            # The API explains a refused {{sub1}} or an unreachable destination
            # better than anything this layer could say.
            return _app_notice("/postbacks", app_id, exc.detail)

    @app.post("/postbacks/{rule_id}/toggle", include_in_schema=False)
    async def toggle_postback_action(
        request: Request,
        rule_id: str,
        app_id: str = Form(""),
        enable: str = Form(""),
        csrf_token: str = Form(""),
    ) -> Response:
        api = client_for(request)
        if not csrf_token or csrf_token != request.cookies.get(CSRF_COOKIE):
            return RedirectResponse("/postbacks", status_code=status.HTTP_303_SEE_OTHER)
        try:
            if enable:
                await api.patch(f"/v1/postback-rules/{rule_id}", json={"enabled": True})
                return _app_notice("/postbacks", app_id, "Rule enabled")
            await api.delete(f"/v1/postback-rules/{rule_id}")
            return _app_notice("/postbacks", app_id, "Rule disabled")
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            return _app_notice("/postbacks", app_id, exc.detail)

    def _app_notice(path: str, app_id: str, message: str) -> RedirectResponse:
        query = urlencode({"app_id": app_id, "notice": message})
        return RedirectResponse(f"{path}?{query}", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/deep-links", response_class=HTMLResponse, include_in_schema=False)
    async def deep_links_page(request: Request) -> Response:
        api = client_for(request)
        try:
            context = await page_context(request, api, "deeplinks")
            apps, app_id, _from, _to = await selection(request, api)
            context |= {
                "apps": apps,
                "selected_app_id": app_id,
                "deep_links": [],
                "notice": request.query_params.get("notice"),
                "error": None,
            }
            if app_id:
                context["deep_links"] = await api.get(f"/v1/deep-links?app_id={app_id}") or []
            return render("deeplinks.html", context)
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            return await _error_page(request, api, "deeplinks", "deeplinks.html", exc)

    @app.post("/deep-links", include_in_schema=False)
    async def create_deep_link(
        request: Request,
        app_id: str = Form(...),
        code: str = Form(...),
        destination: str = Form(...),
        fallback_url: str = Form(...),
        csrf_token: str = Form(""),
    ) -> Response:
        """Create, then redirect.

        Post-redirect-get, so a refresh after creating does not offer to create
        it again — which for a code that must be unique means a 409 the user did
        not ask for.
        """
        api = client_for(request)
        if not csrf_token or csrf_token != request.cookies.get(CSRF_COOKIE):
            return RedirectResponse("/deep-links", status_code=status.HTTP_303_SEE_OTHER)
        try:
            await api.post(
                "/v1/deep-links",
                json={
                    "app_id": app_id,
                    "code": code.strip(),
                    "destination": destination.strip(),
                    "fallback_url": fallback_url.strip(),
                },
            )
            return _deep_link_redirect(app_id, f"Registered {code.strip()}")
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            # The API's own message — "code already exists for this app", or the
            # reason a destination was refused — is more useful than anything
            # this layer could invent.
            return _deep_link_redirect(app_id, exc.detail)

    @app.post("/deep-links/{deep_link_id}/delete", include_in_schema=False)
    async def delete_deep_link(
        request: Request,
        deep_link_id: str,
        app_id: str = Form(""),
        csrf_token: str = Form(""),
    ) -> Response:
        api = client_for(request)
        if not csrf_token or csrf_token != request.cookies.get(CSRF_COOKIE):
            return RedirectResponse("/deep-links", status_code=status.HTTP_303_SEE_OTHER)
        try:
            await api.delete(f"/v1/deep-links/{deep_link_id}")
            return _deep_link_redirect(app_id, "Removed")
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            return _deep_link_redirect(app_id, exc.detail)

    def _deep_link_redirect(app_id: str, notice: str) -> RedirectResponse:
        query = urlencode({"app_id": app_id, "notice": notice})
        return RedirectResponse(f"/deep-links?{query}", status_code=status.HTTP_303_SEE_OTHER)

    # ------------------------------------------------------ integrations
    @app.get("/integrations", response_class=HTMLResponse, include_in_schema=False)
    async def integrations_page(request: Request) -> Response:
        api = client_for(request)
        try:
            context = await page_context(request, api, "integrations")
            context |= {
                "integrations": await api.get("/v1/integrations") or [],
                "providers": await api.get("/v1/providers") or [],
                "notice": request.query_params.get("notice"),
                "error": None,
            }
            return render("integrations.html", context)
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            return await _error_page(request, api, "integrations", "integrations.html", exc)

    @app.post("/integrations", include_in_schema=False)
    async def create_integration_action(request: Request) -> Response:
        """Fields are read from the form by name, using the adapter's own
        declaration to decide which are secret.

        Deliberately not a fixed signature: the fields belong to the adapter, and
        enumerating them here would put a second copy of its requirements in the
        dashboard — the thing that made this form worth waiting for rather than
        hardcoding.
        """
        api = client_for(request)
        form = await request.form()
        csrf_token = str(form.get("csrf_token") or "")
        if not csrf_token or csrf_token != request.cookies.get(CSRF_COOKIE):
            return RedirectResponse("/integrations", status_code=status.HTTP_303_SEE_OTHER)

        provider_name = str(form.get("provider") or "")
        try:
            providers = await api.get("/v1/providers") or []
            provider = next((p for p in providers if p["name"] == provider_name), None)
            if provider is None:
                return _notice("/integrations", f"unknown adapter: {provider_name}")

            credentials: dict[str, str] = {}
            configuration: dict[str, Any] = {}
            for field in provider["fields"]:
                value = str(form.get(f"field_{field['name']}") or "").strip()
                if not value:
                    continue
                if field["secret"]:
                    credentials[field["name"]] = value
                elif field["name"] == "event_map":
                    # The one field that is not a string. Parsed here so a typo
                    # is a message on this page rather than a 422 from the API
                    # about a type the operator never chose.
                    try:
                        configuration[field["name"]] = json.loads(value)
                    except ValueError:
                        return _notice("/integrations", "event map must be valid JSON")
                else:
                    configuration[field["name"]] = value

            await api.post(
                "/v1/integrations",
                json={
                    "provider": provider_name,
                    "name": str(form.get("name") or "").strip(),
                    "credentials": credentials,
                    "configuration": configuration,
                },
            )
            return _notice("/integrations", f"Connected {form.get('name')}")
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            # The API validates against the adapter and returns every problem at
            # once. Passing that through beats inventing a summary of it.
            return _notice("/integrations", exc.detail)

    # ----------------------------------------------------------- export
    @app.get("/export", response_class=HTMLResponse, include_in_schema=False)
    async def export_page(request: Request) -> Response:
        api = client_for(request)
        try:
            context = await page_context(request, api, "export")
            apps, app_id, from_date, to_date = await selection(request, api)
            context |= {
                "apps": apps,
                "selected_app_id": app_id,
                "from_date": from_date.isoformat(),
                "to_date": to_date.isoformat(),
                "datasets": EXPORT_DATASETS,
                "api_base": api_base,
                "presets": presets_for(request, app_id, from_date, to_date),
                # The export API takes timestamps, not dates. `until` is the end
                # of the chosen day rather than its start, or a one-day
                # selection would export nothing.
                "since": f"{from_date.isoformat()}T00:00:00Z",
                "until": f"{to_date.isoformat()}T23:59:59Z",
                "error": None,
            }
            return render("export.html", context)
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            return await _error_page(request, api, "export", "export.html", exc)

    async def _error_page(
        request: Request, api: ApiClient, active: str, template: str, exc: ApiError
    ) -> HTMLResponse:
        """Render the page shell with the error inside it.

        A failed panel should not lose the navigation: a user who cannot load
        Campaigns can still reach Apps, and a full-page error would strand them.
        """
        log.warning("dashboard_api_error", status=exc.status_code, detail=exc.detail)
        try:
            context = await page_context(request, api, active)
        except ApiError:
            context = {
                "request": request,
                "user": None,
                "organization": None,
                "nav_items": NAV_ITEMS,
                "active": active,
                # Empty rather than absent: the template renders a sign-out
                # form, and a missing token would make that form silently fail
                # instead of being visibly unavailable.
                "csrf_token": "",  # nosec B105 - an absent token, not a secret
                "request_id": request_id_var.get(),
                "apps": [],
                "current_app": None,
                "app_chooser": False,
                "next_path": "/",
            }
        context |= {
            "error": True,
            "title": "Could not load this view",
            "detail": exc.detail,
            "apps": [],
            "links": [],
            "events": [],
            "campaigns": [],
            "stats": [],
            "series": [],
            "chart": "",
            "device_history": [],
            "anonymous_id": None,
            "selected_app_id": None,
            "from_date": "",
            "to_date": "",
            "summary": {
                "total": 0,
                "attributed": 0,
                "organic": 0,
                "match_rate": None,
                "by_method": [],
            },
            "match_rate_display": percentage(None),
            "sessions_display": count(None),
            "conversions_display": count(None),
            "previous_range": "",
            "presets": [],
            "metric": "events",
            "metric_links": [],
            "funnel": [],
            "catalogue": [],
            "kind": "events",
            "filters": {},
            "cursor": "",
            "items": [],
            "next_href": None,
            "first_href": "/logs",
            "export_dataset": "events",
            "tabs": [],
            "selected_app": None,
            "keys": [],
            "event_constants": "",
            "categories": EVENT_CATEGORIES,
            "blocked_count": 0,
            "distinct_display": count(None),
            "period_total_display": count(None),
            "period_revenue_display": money(None),
            "method_notes": METHOD_NOTES,
            "tracking_domain": tracking_domain,
            "cache_state": None,
            # The new pages' collections. Present and empty rather than absent:
            # an undefined name in Jinja renders as nothing and hides the fact
            # that the panel failed, which is the opposite of what an error page
            # is for.
            "findings": [],
            "flagged": [],
            "fraudulent": 0,
            "suspicious": 0,
            "rows": [],
            "caveat": "",
            "total_winning": 0,
            "total_non_winning": 0,
            "total_suppressed": 0,
            "total_redownloads": 0,
            "conversion_values": [],
            "deep_links": [],
            "notice": None,
            "new_key": None,
            "campaigns_for_links": [],
            "rules": [],
            "campaign_names": {},
            "variables": [],
            "scoped_only": [],
            "integrations": [],
            "providers": [],
            "datasets": EXPORT_DATASETS,
            "api_base": api_base,
            "since": "",
            "until": "",
            "severity_class": severity_class,
            "verdict_class": verdict_class,
        }
        return render(template, context, status_code=200)

    # ---------------------------------------------------------------- ops
    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "web", "version": settings.version}

    @app.get("/ready", include_in_schema=False)
    async def ready() -> dict[str, object]:
        healthy, checks = await registry.check()
        return {"status": "ok" if healthy else "degraded", "checks": checks}

    return app


app = create_app()
