"""Shared database type aliases.

``asyncpg.Connection`` is generic to a type checker but not subscriptable at
runtime, so the parameterised form has to be confined to the type-checking
branch. Defined once here rather than repeated in every module that touches a
connection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import asyncpg

if TYPE_CHECKING:
    type DbConn = asyncpg.Connection[Any]
    type DbPool = asyncpg.Pool[Any]
else:
    DbConn = asyncpg.Connection
    DbPool = asyncpg.Pool

__all__ = ["DbConn", "DbPool"]
