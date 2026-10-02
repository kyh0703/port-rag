"""Durable, content-free admission fence for withdrawn RAG owners."""

from __future__ import annotations

import uuid
from contextlib import AbstractAsyncContextManager
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession


class OwnerDataErased(Exception):
    def __init__(self) -> None:
        super().__init__("RAG_OWNER_DATA_ERASED")


class OwnerAdmission(Protocol):
    async def assert_active(self, user_id: str) -> None: ...


class SessionFactory(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[AsyncSession]: ...


async def lock_active_owner(session: AsyncSession, user_id: str) -> None:
    """Hold the SQL owner lock through this transaction's reads or writes."""
    try:
        await session.execute(
            sa.text("SELECT public.rag_assert_owner_active(CAST(:user_id AS uuid))"),
            {"user_id": str(uuid.UUID(user_id))},
        )
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == "55000":
            raise OwnerDataErased() from exc
        raise


class SqlAlchemyOwnerErasure:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def assert_active(self, user_id: str) -> None:
        async with self._session_factory() as session:
            await lock_active_owner(session, user_id)

    async def fence(self, user_id: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                sa.text("SELECT public.rag_fence_owner_erasure(CAST(:user_id AS uuid))"),
                {"user_id": str(uuid.UUID(user_id))},
            )
            await session.commit()

    async def erase(self, user_id: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                sa.text("SELECT public.rag_erase_user_data(CAST(:user_id AS uuid))"),
                {"user_id": str(uuid.UUID(user_id))},
            )
            await session.commit()
