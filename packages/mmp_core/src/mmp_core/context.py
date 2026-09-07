"""Request-scoped identifiers.

``request_id`` identifies one HTTP request. ``correlation_id`` follows the work
it spawns across the queue into the workers, so a postback delivery six seconds
later can be traced back to the event that caused it. Both are propagated in
stream message headers, not just HTTP headers.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)

REQUEST_ID_HEADER = "x-request-id"
CORRELATION_ID_HEADER = "x-correlation-id"


def new_id() -> str:
    return uuid.uuid4().hex
