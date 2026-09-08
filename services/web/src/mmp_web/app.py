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

NAV_ITEMS = [
    {"key": "overview", "label": "Overview", "href": "/"},
    {"key": "apps", "label": "Apps", "href": "/apps"},
    {"key": "links", "label": "Tracking links", "href": "/links"},
    {"key": "events", "label": "Events", "href": "/events"},
    {"key": "attribution", "label": "Attribution", "href": "/attribution"},
]

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
            context |= {"apps": await api.get("/v1/apps") or [], "error": None}
            return render("apps.html", context)
        except Unauthorized:
            return login_redirect(request)
        except ApiError as exc:
            return await _error_page(request, api, "apps", "apps.html", exc)

    @app.get("/links", response_class=HTMLResponse, include_in_schema=False)
    async def links_page(request: Request) -> Response:
        api = client_for(request)
        try:
            context = await page_context(request, api, "links")
            context |= {
                "links": await api.get("/v1/tracking-links") or [],
                "tracking_domain": tracking_domain,
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
