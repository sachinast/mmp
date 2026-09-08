"""Migrations must apply to an empty database, not just to yours.

A migration chain that only works incrementally is a chain that works on every
developer's machine and fails on every fresh one — CI, a new environment, a
restore-and-replay. It is also the failure that is easiest to not notice, because
the databases you look at every day are already migrated.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import uuid

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "packages" / "mmp_db" / "src" / "mmp_db" / "migrations" / "versions"

# Names that read the *live* codebase. A migration that calls one of these does
# whatever the code means today, not what it meant when it was written.
LIVE_METADATA = {
    "org_scoped_tables",  # walks the ORM registry
    "Base",  # the declarative metadata itself
    "TABLES",  # mmp_db.rollups.TABLES — grows over time
    "PARTITION_INDEXES",  # mmp_db.partitions — same hazard
}


def _migration_files() -> list[pathlib.Path]:
    return sorted(p for p in VERSIONS.glob("*.py") if not p.name.startswith("__"))


def test_there_are_migrations_to_check():
    assert _migration_files(), "no migrations found — has the path changed?"


@pytest.mark.parametrize("path", _migration_files(), ids=lambda p: p.stem)
def test_migrations_do_not_read_live_metadata(path: pathlib.Path):
    """A migration is a snapshot of an intent at a point in time.

    Regression test for a real failure: the RLS migration derived its table list
    from the ORM registry, so adding a model four migrations later made it try to
    enable row-level security on a table that would not exist for another four
    steps. Every incrementally-migrated database was fine; every fresh one failed.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[-1] for alias in node.names)

    offenders = imported & LIVE_METADATA
    assert not offenders, (
        f"{path.name} imports {sorted(offenders)}, which reflects the codebase as "
        "it is now rather than as it was. Write the values out literally."
    )


@pytest.mark.slow
def test_the_whole_chain_applies_to_an_empty_database(db_available):
    """The only test that would have caught the failure above.

    Creates a real database, runs every migration, and drops it. Slower than the
    rest of the suite and worth it: this is the one thing no other test can
    check, because every other test runs against a database that is already
    migrated.
    """
    if not db_available:
        pytest.skip("postgres not reachable")

    import os

    name = f"mmp_migtest_{uuid.uuid4().hex[:10]}"
    user = os.environ.get("MMP_TEST_OWNER", os.environ.get("USER", "postgres"))
    env = {
        **os.environ,
        "MMP_DATABASE_URL": f"postgresql+asyncpg://{user}@127.0.0.1:5432/{name}",
    }

    subprocess.run(["createdb", name], check=True, capture_output=True)
    try:
        result = subprocess.run(
            ["uv", "run", "alembic", "upgrade", "head"],
            cwd=ROOT / "packages" / "mmp_db",
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, (
            "migrations failed on an empty database:\n"
            + result.stdout[-3000:]
            + result.stderr[-3000:]
        )

        tables = subprocess.run(
            [
                "psql",
                "-d",
                name,
                "-tAc",
                "select count(*) from pg_tables where schemaname='public'",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert int(tables.stdout.strip()) > 20, "the schema looks incomplete"
    finally:
        subprocess.run(["dropdb", "--if-exists", name], check=False, capture_output=True)
