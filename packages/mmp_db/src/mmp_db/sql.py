"""Safe construction of the few SQL fragments that cannot be bind parameters.

Postgres accepts a parameter for a *value* but never for an *identifier*: there
is no way to bind a column name in a SELECT list or an ORDER BY. So a small
amount of SQL text has to be assembled in Python.

Rather than scattering that across route modules — where each site is one
careless edit away from interpolating a request field — every such fragment is
built here, from an explicit allowlist, and each identifier is validated against
a strict pattern before it can reach a query. There is exactly one place in the
codebase where an identifier becomes SQL text, and this is it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

# Deliberately narrower than Postgres allows. Nothing in this schema needs a
# quoted, mixed-case, or unicode identifier, and permitting them would mean
# accepting characters that change how the surrounding statement parses.
IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class UnsafeIdentifierError(ValueError):
    """An identifier failed validation. Always a programming error, never input."""


def identifier(name: str) -> str:
    if not IDENTIFIER.match(name):
        raise UnsafeIdentifierError(f"not a permitted SQL identifier: {name!r}")
    return name


def columns(names: Iterable[str]) -> str:
    """A validated comma-separated column list.

    A bare ``str`` is rejected explicitly: it is iterable, so passing one would
    silently validate it one character at a time and produce nonsense. That is
    exactly the mistake this rejected during development.
    """
    if isinstance(names, str):
        raise UnsafeIdentifierError("pass a sequence of column names, not a pre-joined string")
    return ", ".join(identifier(name) for name in names)


def select(table: str, names: Sequence[str], *, where: str = "", suffix: str = "") -> str:
    """Build a SELECT from validated identifiers.

    ``where`` and ``suffix`` are literal fragments written by us and must
    contain only bind placeholders — never an interpolated value. That is a
    convention this function cannot enforce, which is why it lives beside the
    allowlists rather than being handed arbitrary strings from a request.
    """
    # sql-identifier-ok: every identifier passes IDENTIFIER above; values are
    # always bound by the caller. This is the codebase's only identifier-to-SQL
    # site, and it takes no request-derived input.
    statement = f"SELECT {columns(names)} FROM {identifier(table)}"  # noqa: S608 # nosec B608
    if where:
        statement += f" WHERE {where}"
    if suffix:
        statement += f" {suffix}"
    return statement


def with_returning(statement: str, names: Sequence[str]) -> str:
    """Append a validated RETURNING clause to a literal statement.

    Callers pass their INSERT as a plain string literal and let this add the
    column list, so no route module ever concatenates SQL itself.
    """
    # sql-identifier-ok: see module docstring.
    return f"{statement} RETURNING {columns(names)}"


def update(
    table: str,
    changed: Sequence[str],
    *,
    where: str,
    returning: Sequence[str] | None = None,
    start: int = 2,
) -> str:
    """Build a partial UPDATE.

    ``changed`` comes from a caller-side allowlist and is validated again here;
    the *values* are bound positionally. A request can therefore influence what
    is written but never which column is written to — the distinction that makes
    a partial-update endpoint safe.
    """
    assignments = ", ".join(
        f"{identifier(name)} = ${index}" for index, name in enumerate(changed, start)
    )
    # sql-identifier-ok: see module docstring.
    statement = (
        f"UPDATE {identifier(table)} SET {assignments}, "  # noqa: S608 # nosec B608
        f"updated_at = now() WHERE {where}"
    )
    return with_returning(statement, returning) if returning else statement
