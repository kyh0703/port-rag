"""Exercise the same independent database lifecycle used by pytest workers."""

import asyncio
import os

import sqlalchemy as sa
from sqlalchemy.engine import make_url

from rag.db.session import create_engine
from tests.postgres_fixture import isolated_postgres_database


async def _assert_databases_are_isolated(worker_url: str, other_url: str) -> None:
    worker_engine = create_engine(worker_url)
    other_engine = create_engine(other_url)
    try:
        async with other_engine.begin() as connection:
            await connection.execute(
                sa.text("CREATE TABLE public.pytest_isolation_probe (id integer)")
            )
            await connection.execute(sa.text("INSERT INTO public.pytest_isolation_probe VALUES (1)"))
            await connection.execute(sa.text(
                "ALTER TABLE public.knowledge_revision_chunks "
                "DISABLE TRIGGER knowledge_revision_chunks_are_immutable"
            ))
            assert await connection.scalar(sa.text(
                "SELECT tgenabled::text FROM pg_trigger "
                "WHERE tgrelid='public.knowledge_revision_chunks'::regclass "
                "AND tgname='knowledge_revision_chunks_are_immutable'"
            )) == "D"
        async with worker_engine.connect() as connection:
            assert await connection.scalar(
                sa.text("SELECT to_regclass('public.pytest_isolation_probe')")
            ) is None
            assert await connection.scalar(sa.text(
                "SELECT tgenabled::text FROM pg_trigger "
                "WHERE tgrelid='public.knowledge_revision_chunks'::regclass "
                "AND tgname='knowledge_revision_chunks_are_immutable'"
            )) == "O"
    finally:
        await other_engine.dispose()
        await worker_engine.dispose()


async def _database_exists(source_url: str, database_name: str) -> bool:
    engine = create_engine(source_url)
    try:
        async with engine.connect() as connection:
            return bool(await connection.scalar(
                sa.text("SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = :name)"),
                {"name": database_name},
            ))
    finally:
        await engine.dispose()


def test_independent_migrated_databases_do_not_share_rows_or_triggers(isolated_database_url):
    source_url = os.environ["TEST_RAG_DATABASE_URL"]
    source_name = make_url(source_url).database
    worker_name = make_url(isolated_database_url).database
    assert worker_name != source_name
    assert worker_name.startswith("rag_pytest_")

    with isolated_postgres_database(source_url) as other_url:
        other_name = make_url(other_url).database
        assert other_name.startswith("rag_pytest_")
        assert other_name not in {source_name, worker_name}
        asyncio.run(_assert_databases_are_isolated(isolated_database_url, other_url))

    assert not asyncio.run(_database_exists(source_url, other_name))
    assert asyncio.run(_database_exists(source_url, source_name))
