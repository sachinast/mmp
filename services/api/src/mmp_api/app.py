"""The business API: auth, organisations, apps, keys, campaigns, analytics reads."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from mmp_core import (
    HealthRegistry,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    configure_logging,
    load_settings,
    service_lifespan,
)


def create_app() -> FastAPI:
    settings = load_settings(service_name="api")
    configure_logging(service="api", level=settings.log_level, json_output=settings.log_json)
    registry = HealthRegistry(service="api", version=settings.version)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Phase 1 registers the RLS-scoped connection pool and its probe here.
        async with service_lifespan(registry, drain_grace_seconds=settings.shutdown_grace_seconds):
            yield

    app = FastAPI(
        title="MMP API",
        version=settings.version,
        lifespan=lifespan,
        # Interactive docs are a dev affordance, not a production endpoint.
        docs_url=None if settings.is_prod else "/docs",
        openapi_url=None if settings.is_prod else "/openapi.json",
    )
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "api", "version": settings.version}

    @app.get("/ready", tags=["ops"])
    async def ready() -> dict[str, object]:
        healthy, checks = await registry.check()
        return {"status": "ok" if healthy else "degraded", "checks": checks}

    return app


app = create_app()
