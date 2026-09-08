"""Identity tables are the one deliberate exception to row-level security.

``users`` and ``organization_members`` are read *before* a tenant is
established — resolving which organisation a session belongs to is what the
membership lookup is for, so a policy keyed on the active organisation would
make it impossible to determine the active organisation.

That exception is only safe while every query against them filters explicitly,
and "every query does today" is precisely what was true of the event tables
before the audit found them readable across tenants. So the property is enforced
here rather than assumed: every statement in the API that touches
``organization_members`` must constrain it by ``user_id`` or by
``organization_id``.

This is weaker than RLS and it is stated as such in SECURITY.md. It catches the
realistic mistake — a new endpoint that forgets a filter — and would not catch a
deliberately crafted one.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
API = ROOT / "services" / "api" / "src"

IDENTITY_TABLES = ("organization_members", "users")
# Patterns rather than literals: the first version listed "id = $1" and flagged
# a correct `WHERE id = $2`, which is the kind of false positive that gets a
# guard test deleted rather than fixed.
# A filter must bind to a value the caller supplies — `= $1` — not to another
# column.
#
# The first version accepted any `organization_id =`, which a JOIN condition
# satisfies: `JOIN organization_members m ON m.organization_id = o.id` looked
# like a tenancy filter to the check while constraining nothing. Deleting the
# real `WHERE m.user_id = $1` left the test green, which is how this was found —
# by mutating the source and watching the guard not react.
REQUIRED_FILTERS = (
    r"\buser_id\s*=\s*\$\d+",
    r"\borganization_id\s*=\s*\$\d+",
    r"\bemail\s*=\s*\$\d+",
    r"\bid\s*=\s*\$\d+",
)


def _sql_literals(path: pathlib.Path) -> list[tuple[int, str]]:
    """Every string constant in a file that looks like a SQL statement."""
    tree = ast.parse(path.read_text(), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            lowered = text.lower()
            if any(verb in lowered for verb in ("select ", "update ", "delete from")):
                found.append((node.lineno, text))
    return found


def _python_files() -> list[pathlib.Path]:
    return sorted(API.rglob("*.py"))


@pytest.mark.parametrize("table", IDENTITY_TABLES)
def test_every_identity_query_is_constrained(table: str):
    offenders: list[str] = []
    for path in _python_files():
        for lineno, statement in _sql_literals(path):
            lowered = " ".join(statement.lower().split())
            if table not in lowered:
                continue
            # An aggregate over a table the query already constrained elsewhere
            # is fine; what matters is that the statement names a filter.
            if not any(re.search(pattern, lowered) for pattern in REQUIRED_FILTERS):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")

    assert not offenders, (
        f"queries against {table} with no user_id or organization_id filter. "
        "These tables have no row-level security by design — they are read "
        "before a tenant exists — so the filter is the only thing between one "
        "organisation's membership and another's:\n  " + "\n  ".join(offenders)
    )


def test_the_exception_is_documented():
    """A known gap that is not written down is an unknown gap."""
    security = ROOT / "docs" / "SECURITY.md"
    assert security.exists(), "SECURITY.md must exist"
    text = security.read_text().lower()
    assert "organization_members" in text
    assert "row-level security" in text or "rls" in text


def test_no_other_table_relies_on_this_exception():
    """Every table carrying organization_id must have RLS, except the identity
    tables named above. A new one that skips it is a regression."""
    import mmp_db.models  # noqa: F401
    from mmp_db.base import Base, OrgScopedMixin

    exempt = {"organization_members", "users"}
    unscoped = []
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        table = getattr(cls, "__tablename__", None)
        if table in exempt or table is None:
            continue
        if "organization_id" in mapper.columns and not issubclass(cls, OrgScopedMixin):
            unscoped.append(table)

    assert not unscoped, (
        "tables carrying organization_id without OrgScopedMixin, and therefore "
        f"without an RLS policy: {sorted(unscoped)}"
    )


def test_session_lookups_do_not_widen_the_exception():
    """The membership query that resolves a session must name both columns.

    It is the one query that legitimately runs without a tenant, and it is the
    one where a missing filter would hand a session someone else's role.
    """
    deps = (API / "mmp_api" / "deps.py").read_text()
    membership = re.search(r"SELECT role FROM organization_members[^\"']*", deps, re.IGNORECASE)
    assert membership, "the session's membership lookup was not found"
    statement = " ".join(membership.group(0).lower().split())
    assert "user_id = $1" in statement
    assert "organization_id = $2" in statement
