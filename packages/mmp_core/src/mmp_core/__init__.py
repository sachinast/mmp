"""Shared foundations: settings, logging, IDs, ASGI middleware, health."""

from mmp_core.context import (
    CORRELATION_ID_HEADER,
    REQUEST_ID_HEADER,
    correlation_id_var,
    new_id,
    request_id_var,
)
from mmp_core.gc_tuning import tune_for_latency
from mmp_core.health import HealthRegistry, health_routes
from mmp_core.ids import timestamp_ms, uuid7
from mmp_core.lifecycle import service_lifespan
from mmp_core.logging import configure_logging, get_logger
from mmp_core.middleware import (
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    unhandled_exception_handler,
)
from mmp_core.settings import Settings, load_settings

__all__ = [
    "CORRELATION_ID_HEADER",
    "REQUEST_ID_HEADER",
    "HealthRegistry",
    "RequestContextMiddleware",
    "SecurityHeadersMiddleware",
    "Settings",
    "configure_logging",
    "correlation_id_var",
    "get_logger",
    "health_routes",
    "load_settings",
    "new_id",
    "request_id_var",
    "service_lifespan",
    "timestamp_ms",
    "tune_for_latency",
    "unhandled_exception_handler",
    "uuid7",
]
