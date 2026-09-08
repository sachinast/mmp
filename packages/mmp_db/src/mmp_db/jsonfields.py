"""Decoding json/jsonb columns read through asyncpg.

asyncpg returns ``json`` and ``jsonb`` as **strings** unless a custom codec is
registered. The obvious fix — registering one on the pool — cannot be used here,
and the reason is worth recording so nobody re-adds it:

``copy_records_to_table`` writes in binary format, and a custom text codec
replaces asyncpg's built-in binary encoder for that type. Registering one turns
every ingest batch into ``InternalClientError: no binary format encoder for type
jsonb`` — the entire write path, broken by a convenience on the read path.

So JSON is decoded where it is read. ``decode`` is deliberately tolerant of
already-decoded values so it is safe to apply anywhere, including to a column
whose representation changes later.

The failure this prevents is quiet rather than loud: ``list("[200, 201]")``
yields ``['[', '2', '0', ...]``, so a status-code check against it silently
never matches and every delivery is recorded as a failure.
"""

from __future__ import annotations

import json
from typing import Any


def decode(value: Any) -> Any:
    """Turn a jsonb column into a Python object. Idempotent."""
    if isinstance(value, str | bytes):
        return json.loads(value)
    return value


def decode_list(value: Any) -> list[Any]:
    decoded = decode(value)
    if decoded is None:
        return []
    if not isinstance(decoded, list):
        raise TypeError(f"expected a JSON list, got {type(decoded).__name__}")
    return decoded
