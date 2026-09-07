"""The dashboard. Server-rendered; reads through the API, never the database."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from mmp_core import (
    HealthRegistry,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    configure_logging,
    load_settings,
    service_lifespan,
)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def create_app() -> FastAPI:
    settings = load_settings(service_name="web")
    configure_logging(service="web", level=settings.log_level, json_output=settings.log_json)
    registry = HealthRegistry(service="web", version=settings.version)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with service_lifespan(registry, drain_grace_seconds=settings.shutdown_grace_seconds):
            yield

    app = FastAPI(title="MMP Dashboard", version=settings.version, lifespan=lifespan, docs_url=None)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "web", "version": settings.version}

    @app.get("/ready", include_in_schema=False)
    async def ready() -> dict[str, object]:
        healthy, checks = await registry.check()
        return {"status": "ok" if healthy else "degraded", "checks": checks}

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request=request, name="index.html", context={"version": settings.version}
        )

    return app


app = create_app()
