"""Repo-wide ban on interpolated SQL.

asyncpg is parameterised-only. An f-string containing SQL keywords is either a
mistake or the start of an injection, and either way it does not belong in this
codebase. Dynamic identifiers (ORDER BY columns) go through an allowlist helper
instead.
"""

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SQL_KEYWORDS = ("select ", "insert ", "update ", "delete ", "where ", "from ", "copy ")
SEARCH_DIRS = ("packages", "services")


def _python_files():
    for directory in SEARCH_DIRS:
        yield from (ROOT / directory).rglob("*.py")


# Postgres cannot bind a parameter in identifier position, so DDL that names a
# table has no parameterised form. Those few sites carry an explicit marker and
# are reviewed individually; everything else is banned outright.
EXEMPTION_MARKER = "sql-identifier-ok"


def test_no_sql_in_fstrings():
    offenders = []
    for path in _python_files():
        source = path.read_text()
        lines = source.splitlines()
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            literal = "".join(
                part.value for part in node.values if isinstance(part, ast.Constant)
            ).lower()
            if not any(keyword in literal for keyword in SQL_KEYWORDS):
                continue
            # Look at the statement and the few lines above it for the marker.
            start = max(0, node.lineno - 4)
            context = "\n".join(lines[start : (node.end_lineno or node.lineno)])
            if EXEMPTION_MARKER in context:
                continue
            offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, (
        "SQL built with an f-string. Use bind parameters; if the value is an "
        "identifier that cannot be bound, mark the site with "
        f"'{EXEMPTION_MARKER}' and a justification:\n  " + "\n  ".join(offenders)
    )


def test_exemptions_stay_rare():
    """A ban with unlimited exemptions is not a ban. If this number grows,
    something is being worked around rather than fixed."""
    count = sum(f.read_text().count(EXEMPTION_MARKER) for f in _python_files())
    assert count <= 3, f"{count} f-string SQL exemptions — review before raising this limit"


def test_pickle_is_banned():
    """Queue payloads are JSON or msgpack. pickle is a deserialisation RCE."""
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
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, "pickle-family import found:\n  " + "\n  ".join(offenders)
