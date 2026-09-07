"""Repo-wide ban on interpolated SQL.

asyncpg is parameterised-only. An f-string containing SQL keywords is either a
mistake or the start of an injection, and either way it does not belong in a
route, a worker, or a service.

Postgres cannot bind an identifier, though — there is no way to parameterise a
column name in a SELECT list or an ORDER BY — so a small amount of SQL text has
to be assembled in Python. Rather than judging each site by proximity to a
comment, the rule is structural: **only three modules may do it**, each of them
in `mmp_db`, each reviewed, and each taking its identifiers from an allowlist
rather than from a request. Anywhere else, an f-string with SQL in it fails the
build.
"""

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SQL_KEYWORDS = ("select ", "insert ", "update ", "delete ", "where ", "from ", "copy ")
SEARCH_DIRS = ("packages", "services")

# The complete list of modules permitted to turn an identifier into SQL text.
# Adding to this list is a security review, not a refactor.
SQL_BUILDING_MODULES = {
    "packages/mmp_db/src/mmp_db/sql.py",  # validated SELECT/UPDATE builders
    "packages/mmp_db/src/mmp_db/rls.py",  # RLS policies, table names from ORM metadata
    "packages/mmp_db/src/mmp_db/partitions.py",  # partition DDL, names from constants
}
MARKER = "sql-identifier-ok"


def _python_files():
    for directory in SEARCH_DIRS:
        yield from (ROOT / directory).rglob("*.py")


def _relative(path: pathlib.Path) -> str:
    return path.relative_to(ROOT).as_posix()


def test_no_sql_in_fstrings_outside_the_builders():
    offenders = []
    for path in _python_files():
        if _relative(path) in SQL_BUILDING_MODULES:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            literal = "".join(
                part.value for part in node.values if isinstance(part, ast.Constant)
            ).lower()
            if any(keyword in literal for keyword in SQL_KEYWORDS):
                offenders.append(f"{_relative(path)}:{node.lineno}")
    assert not offenders, (
        "SQL built with an f-string outside mmp_db's reviewed builders. Use bind "
        "parameters; if you need an identifier, add it to an allowlist and go "
        "through mmp_db.sql:\n  " + "\n  ".join(offenders)
    )


def test_builder_modules_still_exist_and_are_marked():
    """Guards against the allowlist quietly outliving the files it names."""
    for relative in SQL_BUILDING_MODULES:
        path = ROOT / relative
        assert path.exists(), f"{relative} is allowlisted but does not exist"
        assert MARKER in path.read_text(), (
            f"{relative} builds SQL but carries no '{MARKER}' justification"
        )


def test_allowlist_has_not_grown():
    """Three modules. If this needs to be four, that is a deliberate decision."""
    assert len(SQL_BUILDING_MODULES) == 3


def test_identifier_validation_rejects_injection():
    """The allowlist is only safe because the builders validate what they take."""
    import pytest
    from mmp_db.sql import UnsafeIdentifierError, identifier

    for attempt in ("name; DROP TABLE apps", "name)", '"name"', "name--", "1abc", "NAME", ""):
        with pytest.raises(UnsafeIdentifierError):
            identifier(attempt)


def test_columns_rejects_a_prejoined_string():
    """A string is iterable, so this would otherwise validate one character at
    a time and emit nonsense. It happened once during development."""
    import pytest
    from mmp_db.sql import UnsafeIdentifierError, columns

    with pytest.raises(UnsafeIdentifierError):
        columns("id, name, status")


def test_pickle_is_banned():
    """Queue payloads are JSON or msgpack. pickle is a deserialisation RCE, and
    it is the single largest reason Celery's defaults are not in this stack."""
    offenders = []
    for path in _python_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(name.split(".")[0] in {"pickle", "cPickle", "dill", "shelve"} for name in names):
                offenders.append(f"{_relative(path)}:{node.lineno}")
    assert not offenders, "pickle-family import found:\n  " + "\n  ".join(offenders)
