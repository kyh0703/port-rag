"""Prepare version-pinned migration fixtures for a standalone checkout."""
from pathlib import Path
import runpy


def pytest_sessionstart(session) -> None:
    root = Path(__file__).resolve().parents[1]
    bootstrap = runpy.run_path(str(root / "scripts/prepare_migrations.py"))
    bootstrap["prepare"](root)
