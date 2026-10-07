"""Prepare version-pinned migration fixtures for a standalone checkout."""

import pytest
from pathlib import Path
import runpy


def pytest_sessionstart(session) -> None:
    root = Path(__file__).resolve().parents[1]
    bootstrap = runpy.run_path(str(root / "scripts/prepare_migrations.py"))
    bootstrap["prepare"](root)


@pytest.fixture(autouse=True)
def volatile_parser_filesystem_fixture(tmp_path, monkeypatch):
    """Replace only the OS tmpfs boundary for synthetic-file tests on macOS."""
    from rag.ingest.uploads import LocalUploadStorage

    root = tmp_path / "volatile-parser-fixture"
    root.mkdir()
    monkeypatch.setattr(LocalUploadStorage, "_require_volatile_root", staticmethod(lambda: root))
