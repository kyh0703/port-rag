"""Async SQLAlchemy engine and session helpers."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

from rag.metrics import Metrics


def create_engine(database_url: str, *, metrics: Metrics | None = None) -> AsyncEngine:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    if metrics is None:
        return engine

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def record_query_start(_connection, _cursor, _statement, _parameters, context, _executemany):
        context._rag_query_started_at = perf_counter()

    @event.listens_for(engine.sync_engine, "after_cursor_execute")
    def record_query_duration(_connection, _cursor, _statement, _parameters, context, _executemany):
        started_at = getattr(context, "_rag_query_started_at", None)
        if started_at is not None:
            metrics.db_query_duration.labels(operation="query").observe(perf_counter() - started_at)

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
