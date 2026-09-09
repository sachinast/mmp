"""Guards on the documentation.

A stale specification is worse than a missing one, because it is trusted. These
tests cover the parts of the docs that can be checked mechanically — the rest
still has to be maintained by hand.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

pytestmark = pytest.mark.skipif(not DOCS.exists(), reason="docs not present")


def test_every_internal_documentation_link_resolves() -> None:
    """Broken cross-references are how a set of documents stops being read."""
    broken: list[str] = []
    for markdown in sorted(DOCS.rglob("*.md")):
        for _text, target in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", markdown.read_text()):
            if target.startswith(("http://", "https://", "#")):
                continue
            path = target.split("#")[0]
            if not path:
                continue
            if not (markdown.parent / path).resolve().exists():
                broken.append(f"{markdown.relative_to(ROOT)} -> {target}")
    assert not broken, "broken documentation links:\n  " + "\n  ".join(broken)


def test_the_documented_tracker_endpoints_exist() -> None:
    """The tracker is Starlette and publishes no OpenAPI schema, so its endpoint
    list in the docs generator is hand-written — the one part of the generated
    reference that can drift. This is what stops it."""
    import sys

    from mmp_tracker.app import create_app

    sys.path.insert(0, str(ROOT / "infra" / "docs"))
    from generate_api_docs import TRACKER

    from tests.conftest_ingest import build_settings_for

    actual = {route.path for route in create_app(build_settings_for("mmp_tracker")).routes}  # type: ignore[attr-defined]
    documented = {path for _method, path, _purpose, _auth in TRACKER}

    missing = documented - actual
    assert not missing, f"the docs list tracker endpoints that do not exist: {sorted(missing)}"


def test_the_specification_index_lists_every_specification_document() -> None:
    """A document nobody links to is a document nobody reads."""
    index = (DOCS / "spec" / "00-index.md").read_text()
    for document in sorted((DOCS / "spec").glob("[0-9][0-9]-*.md")):
        if document.name.startswith("00-"):
            continue
        assert document.name in index, f"{document.name} is missing from the index"


def test_documents_describing_unbuilt_systems_say_so_prominently() -> None:
    """Two of these specify systems that do not exist. If the warning is ever
    edited away, they read as descriptions of built features — which is exactly
    the misunderstanding that gets something scheduled that was never written."""
    for name in ("08-billing.md", "13-ai.md"):
        header = (DOCS / "spec" / name).read_text()[:900]
        assert "NOT BUILT" in header, f"{name} must state up front that it is not built"
