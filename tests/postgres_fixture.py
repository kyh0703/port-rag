"""Create disposable databases for PostgreSQL integration tests."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from rag.db.database_url import normalize_asyncpg_url


ROOT = Path(__file__).resolve().parents[1]


async def _database_command(url: str, statement: str) -> None:
    engine = create_async_engine(
        normalize_asyncpg_url(url), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    try:
        async with engine.connect() as connection:
            await connection.exec_driver_sql(statement)
    finally:
        await engine.dispose()


@contextmanager
def isolated_postgres_database(source_url: str) -> Iterator[str]:
    source = make_url(source_url)
    if not source.database:
        raise ValueError("TEST_RAG_DATABASE_URL must name a disposable PostgreSQL database")
    # Names are generated here, never derived from the configured database name.
    database_name = f"rag_pytest_{uuid4().hex}"
    isolated_url = source.set(database=database_name).render_as_string(
        hide_password=False
    )
    asyncio.run(_database_command(source_url, f'CREATE DATABASE "{database_name}"'))
    try:
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env={**os.environ, "DATABASE_URL": isolated_url},
            check=True,
        )
        yield isolated_url
    finally:
        asyncio.run(_database_command(source_url, f'DROP DATABASE "{database_name}" WITH (FORCE)'))
