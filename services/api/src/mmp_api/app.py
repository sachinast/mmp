"""The business API: auth, organisations, apps, keys, and analytics reads."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from mmp_core.settings import Settings

from mmp_api.context import AppContext
from mmp_api.routes import (
    analytics,
    apps,
    attributions,
    auth,
    campaigns,
    integrations,
    keys,
    organizations,
    postbacks,
    privacy,
    webhooks,
)
from mmp_core import (
    HealthRegistry,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    configure_logging,
    get_logger,
    load_settings,
    service_lifespan,
)

log = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings(service_name="api")
    configure_logging(service="api", level=settings.log_level, json_output=settings.log_json)
    registry = HealthRegistry(service="api", version=settings.version)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        context = await AppContext.create(settings)
        app.state.context = context
        registry.register("postgres", context.ping_database)
        registry.register("redis", context.ping_redis)
        async with service_lifespan(
            registry,
            closers=[context.close],
            drain_grace_seconds=settings.shutdown_grace_seconds,
        ):
            yield

    app = FastAPI(
        title="MMP API",
        version=settings.version,
        lifespan=lifespan,
        docs_url=None if settings.is_prod else "/docs",
        openapi_url=None if settings.is_prod else "/openapi.json",
    )
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        """Never let an internal error describe itself to a client.

        A database error message can carry a table name, a constraint name, or a
        fragment of a query. The client gets a request ID; we get the detail.
        """
        log.exception("unhandled_exception", path=request.url.path)
        return JSONResponse({"error": "internal_error"}, status_code=500)

    for router in (
        auth.router,
        organizations.router,
        apps.router,
        keys.router,
        campaigns.router,
        attributions.router,
        analytics.router,
        postbacks.router,
        webhooks.router,
        privacy.router,
        integrations.router,
    ):
        app.include_router(router, prefix="/v1")

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "api", "version": settings.version}

    @app.get("/ready", tags=["ops"])
    async def ready() -> JSONResponse:
        healthy, checks = await registry.check()
        return JSONResponse(
            {"status": "ok" if healthy else "degraded", "checks": checks},
            status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return app


app = create_app()
