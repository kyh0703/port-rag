from __future__ import annotations

import hashlib
import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from rag.db.models import Document, DocumentChunk
from rag.db.session import create_engine, create_session_factory
from rag.security.owner_erasure import SqlAlchemyOwnerErasure


@pytest.fixture
async def owner_database():
    url = os.getenv("TEST_RAG_DATABASE_URL")
    if not url:
        pytest.skip("requires a disposable migrated pgvector TEST_RAG_DATABASE_URL")
    engine = create_engine(url)
    sessions = create_session_factory(engine)
    owners = SqlAlchemyOwnerErasure(sessions)
    user_ids = (str(uuid.uuid4()), str(uuid.uuid4()))
    try:
        yield engine, sessions, owners, user_ids
    finally:
        try:
            for user_id in user_ids:
                await owners.fence(user_id)
                await owners.erase(user_id)
            async with sessions() as session:
                await session.execute(sa.text(
                    "DELETE FROM public.rag_erased_owners WHERE owner_hash IN (:first, :second)"
                ), {
                    "first": hashlib.sha256(user_ids[0].encode()).hexdigest(),
                    "second": hashlib.sha256(user_ids[1].encode()).hexdigest(),
                })
                await session.commit()
        finally:
            await engine.dispose()


async def insert_document_and_chunk(
    connection: AsyncConnection | AsyncSession, *, user_id: str,
    document_id: uuid.UUID, chunk_id: uuid.UUID, knowledge_key: str,
) -> None:
    await connection.execute(Document.__table__.insert().values(
        id=document_id, user_id=uuid.UUID(user_id), knowledge_key=knowledge_key,
        name="synthetic-private.txt", mime="text/plain",
    ))
    await connection.execute(DocumentChunk.__table__.insert().values(
        id=chunk_id, document_id=document_id, seq=0, text="synthetic private content",
        metadata={"private": "synthetic private metadata"},
        embedding=[1.0] + [0.0] * 1535,
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize(("isolation", "expected_sqlstate"), [
    ("READ COMMITTED", "55000"),
    ("REPEATABLE READ", "0A000"),
    ("SERIALIZABLE", "0A000"),
])
async def test_old_snapshot_cannot_resurrect_owned_document_or_chunks_after_erasure(
    owner_database, isolation: str, expected_sqlstate: str,
) -> None:
    engine, sessions, owners, (owner, other) = owner_database
    target_id, target_chunk = uuid.uuid4(), uuid.uuid4()
    other_id, other_chunk = uuid.uuid4(), uuid.uuid4()
    late_id, late_chunk = uuid.uuid4(), uuid.uuid4()
    async with sessions() as session:
        await insert_document_and_chunk(
            session, user_id=owner, document_id=target_id, chunk_id=target_chunk,
            knowledge_key="before_erasure",
        )
        await insert_document_and_chunk(
            session, user_id=other, document_id=other_id, chunk_id=other_chunk,
            knowledge_key="before_erasure",
        )
        await session.commit()
    marker_query = sa.text(
        "SELECT EXISTS (SELECT 1 FROM public.rag_erased_owners WHERE owner_hash = :owner_hash)"
    )
    parameters = {"owner_hash": hashlib.sha256(owner.encode()).hexdigest()}
    async with engine.connect() as connection:
        writer = await connection.execution_options(isolation_level=isolation)
        # Fix A's snapshot before its first admission/owner advisory lock.
        assert not await writer.scalar(marker_query, parameters)
        await owners.fence(owner)
        await owners.erase(owner)
        assert await writer.scalar(marker_query, parameters) == (isolation == "READ COMMITTED")
        with pytest.raises(DBAPIError) as rejection:
            await insert_document_and_chunk(
                writer, user_id=owner, document_id=late_id, chunk_id=late_chunk,
                knowledge_key="late_after_erasure",
            )
            await writer.commit()
        assert getattr(rejection.value.orig, "sqlstate", None) == expected_sqlstate
        await writer.rollback()
    async with sessions() as session:
        assert await session.scalar(marker_query, parameters)
        assert await session.get(Document, target_id) is None
        assert await session.get(Document, late_id) is None
        for chunk_id in (target_chunk, late_chunk):
            assert await session.get(DocumentChunk, chunk_id) is None
        preserved = await session.get(Document, other_id)
        assert preserved.user_id == uuid.UUID(other)
        preserved_chunk = await session.get(DocumentChunk, other_chunk)
        assert preserved_chunk.text == "synthetic private content"
        assert preserved_chunk.metadata_ == {"private": "synthetic private metadata"}


@pytest.mark.asyncio
@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
@pytest.mark.parametrize("routine", [
    "rag_assert_owner_active", "rag_fence_owner_erasure", "rag_erase_user_data",
])
async def test_snapshot_isolation_authority_rejects_without_fencing_or_deleting_active_owner(
    owner_database, isolation: str, routine: str,
) -> None:
    engine, sessions, _, (owner, _) = owner_database
    document_id, chunk_id = uuid.uuid4(), uuid.uuid4()
    async with sessions() as session:
        await insert_document_and_chunk(
            session, user_id=owner, document_id=document_id, chunk_id=chunk_id,
            knowledge_key="active_owner",
        )
        await session.commit()
    marker_query = sa.text(
        "SELECT EXISTS (SELECT 1 FROM public.rag_erased_owners WHERE owner_hash = :owner_hash)"
    )
    parameters = {"owner_hash": hashlib.sha256(owner.encode()).hexdigest()}
    async with engine.connect() as connection:
        writer = await connection.execution_options(isolation_level=isolation)
        assert not await writer.scalar(marker_query, parameters)
        with pytest.raises(DBAPIError) as rejection:
            await writer.execute(
                sa.text(f"SELECT public.{routine}(CAST(:user_id AS uuid))"), {"user_id": owner},
            )
            await writer.commit()
        assert getattr(rejection.value.orig, "sqlstate", None) == "0A000"
        await writer.rollback()
    async with sessions() as session:
        assert not await session.scalar(marker_query, parameters)
        retained = await session.get(Document, document_id)
        assert retained.user_id == uuid.UUID(owner)
        retained_chunk = await session.get(DocumentChunk, chunk_id)
        assert retained_chunk.text == "synthetic private content"
