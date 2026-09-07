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


def test_no_sql_in_fstrings():
    offenders = []
    for path in _python_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            literal = "".join(
                part.value for part in node.values if isinstance(part, ast.Constant)
            ).lower()
            if any(keyword in literal for keyword in SQL_KEYWORDS):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, "SQL built with an f-string:\n  " + "\n  ".join(offenders)


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
