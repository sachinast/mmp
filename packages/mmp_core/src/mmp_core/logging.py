"""Structured logging with a redaction filter.

Two rules hold everywhere in this codebase:

1. Logs are JSON in every environment except a developer's terminal.
2. No PII reaches a log line. ``REDACTED_KEYS`` is enforced by a processor and
   asserted by a test, because "remember not to log the API key" is not a
   control.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

from mmp_core.context import correlation_id_var, request_id_var

# Anything whose key matches is replaced before it reaches a formatter.
REDACTED_KEYS: frozenset[str] = frozenset(
    {
        "api_key",
        "raw_key",
        "authorization",
        "password",
        "password_hash",
        "secret",
        "session_secret",
        "pepper",
        "token",
        "refresh_token",
        "credentials",
        "ip",
        "ip_address",
        "gaid",
        "idfa",
        "advertising_id",
        "email",
    }
)
_MASK = "[redacted]"


def _redact(
    _logger: object, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if key.lower() in REDACTED_KEYS:
            event_dict[key] = _MASK
    return event_dict


def _bind_request_context(
    _logger: object, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    if (rid := request_id_var.get()) is not None:
        event_dict.setdefault("request_id", rid)
    if (cid := correlation_id_var.get()) is not None:
        event_dict.setdefault("correlation_id", cid)
    return event_dict


def configure_logging(*, service: str, level: str = "info", json_output: bool = True) -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper()),
    )
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=True)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _bind_request_context,
            _redact,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper())),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.bind_contextvars(service=service)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
