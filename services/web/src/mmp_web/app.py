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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
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
from mmp_web.formatting import bar_chart, count, default_range, money, parse_date, percentage

log = get_logger(__name__)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Grouped, because ten flat items is a list to read rather than a structure to
# navigate. The groups follow what someone is doing — looking at numbers,
# checking whether to trust them, or changing configuration — rather than which
# service happens to serve each page.
NAV_ITEMS = [
    {"key": "overview", "label": "Overview", "href": "/", "group": "Measure"},
    {"key": "apps", "label": "Apps", "href": "/apps", "group": "Measure"},
    {"key": "links", "label": "Tracking links", "href": "/links", "group": "Measure"},
    {"key": "events", "label": "Events", "href": "/events", "group": "Measure"},
    {"key": "attribution", "label": "Attribution", "href": "/attribution", "group": "Measure"},
    {"key": "fraud", "label": "Fraud", "href": "/fraud", "group": "Trust"},
    {"key": "skan", "label": "SKAdNetwork", "href": "/skan", "group": "Trust"},
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


METHOD_NOTES = {
    "referrer": "Play Install Referrer carried the click id — the strongest signal.",
    "click_id": "The SDK reported a click id from a deferred deep link.",
    "device_match": "The advertising ID seen at click matched the one at install.",
    "organic": "No deterministic signal matched. Not a failure — the honest answer.",
    "probabilistic": "Fingerprint-based. This platform does not produce these.",
}

# Everything the dashboard loads is inline, so the policy can forbid every
# external source outright. 'unsafe-inline' for style is the one concession:
# the stylesheet is in the document, and hashing it would break on every edit
# for no gain when there is no external CSS to defend against.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; "
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
        """The shell every page needs: who is signed in, and where they are."""
        user = await api.get("/v1/auth/me")
        organizations = await api.get("/v1/organizations")
        current = next((org for org in organizations if org.get("role")), None)
        return {
            "request": request,
            "user": user,
            "organization": current,
            "nav_items": NAV_ITEMS,
            "active": active,
            "csrf_token": request.cookies.get(CSRF_COOKIE, ""),
            "request_id": request_id_var.get(),
        }

    async def selection(
        request: Request, api: ApiClient
    ) -> tuple[list[dict[str, Any]], str | None, dt.date, dt.date]:
        """Resolve the app and date range from the query string.

        Defaults to the first app and the last seven days. The selection lives
        in the URL rather than in a session, so a view can be bookmarked and
        pasted to a colleague — which is how people actually share a number they
        are worried about.
        """
        apps = await api.get("/v1/apps") or []
        requested = request.query_params.get("app_id")
        valid = {app["id"] for app in apps}
        selected = requested if requested in valid else (apps[0]["id"] if apps else None)

        default_from, default_to = default_range()
        from_date = parse_date(request.query_params.get("from"), default_from)
        to_date = parse_date(request.query_params.get("to"), default_to)
        if to_date < from_date:
            from_date, to_date = to_date, from_date
        return apps, selected, from_date, to_date

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

    def render(name: str, context: dict[str, Any], status_code: int = 200) -> HTMLResponse:
        request = context.pop("request")
        return TEMPLATES.TemplateResponse(
            request=request, name=name, context=context, status_code=status_code
        )

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
                "error": None,
                "stats": [],
                "series": [],
                "campaigns": [],
                "chart": "",
                "cache_state": None,
            }
            if not app_id:
                return render("overview.html", context)

            query = f"app_id={app_id}&from={from_date}&to={to_date}"
            data, headers = await api.get_with_headers(f"/v1/analytics/overview?{query}")
            campaigns = await api.get(f"/v1/analytics/campaigns?{query}&limit=25") or []

            totals = data["totals"]
            context |= {
                "stats": [
                    {
                        "label": "Clicks",
                        "value": totals["clicks"],
                        "display": count(totals["clicks"]),
                    },
                    {
                        "label": "Installs",
                        "value": totals["installs"],
                        "display": count(totals["installs"]),
                    },
                    {
                        "label": "Install rate",
                        "value": totals["install_rate"],
                        "display": percentage(totals["install_rate"]),
                    },
                    {
                        "label": "Revenue",
                        "value": totals["revenue_minor"],
                        "display": money(totals["revenue_minor"]),
                    },
                ],
                "series": data["series"],
                "chart": bar_chart(data["series"]),
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

    @app.get("/apps", response_class=HTMLResponse, include_in_schema=False)
    async def apps_page(request: Request) -> Response:
        api = client_for(request)
        try:
            context = await page_context(request, api, "apps")
            context |= {
                "apps": await api.get("/v1/apps") or [],
                "notice": request.query_params.get("notice"),
                "new_key": None,
                "error": None,
            }
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
            context = await page_context(request, api, "apps")
            context |= {
                "apps": await api.get("/v1/apps") or [],
                "new_key": created,
                "notice": None,
                "error": None,
            }
            return render("apps.html", context)
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            return _notice("/apps", exc.detail)

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
                "events": [],
                "error": None,
            }
            if app_id:
                rows = (
                    await api.get(
                        f"/v1/analytics/events?app_id={app_id}&from={from_date}&to={to_date}"
                    )
                    or []
                )
                context["events"] = [
                    row | {"revenue_display": money(row["revenue_minor"])} for row in rows
                ]
            return render("events.html", context)
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            return await _error_page(request, api, "events", "events.html", exc)

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
                "error": None,
            }
            return render("integrations.html", context)
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            return await _error_page(request, api, "integrations", "integrations.html", exc)

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
