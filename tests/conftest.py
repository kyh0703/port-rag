"""Prepare version-pinned migration fixtures for a standalone checkout."""

import os
from pathlib import Path
import runpy

from filelock import FileLock
import pytest

from tests.postgres_fixture import isolated_postgres_database


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session) -> None:
    if hasattr(session.config, "workerinput"):
        return
    root = Path(__file__).resolve().parents[1]
    with FileLock(root / "alembic" / ".pytest-migrations.lock"):
        bootstrap = runpy.run_path(str(root / "scripts/prepare_migrations.py"))
        bootstrap["prepare"](root)


@pytest.fixture(scope="session")
def isolated_database_url():
    url = os.getenv("TEST_RAG_DATABASE_URL")
    if not url:
        pytest.skip("requires a disposable pgvector TEST_RAG_DATABASE_URL")
    with isolated_postgres_database(url) as isolated_url:
        yield isolated_url


@pytest.fixture(autouse=True)
def volatile_parser_filesystem_fixture(tmp_path, monkeypatch):
    """Replace only the OS tmpfs boundary for synthetic-file tests on macOS."""
    from rag.ingest.uploads import LocalUploadStorage

    root = tmp_path / "volatile-parser-fixture"
    root.mkdir()
    monkeypatch.setattr(LocalUploadStorage, "_require_volatile_root", staticmethod(lambda: root))
