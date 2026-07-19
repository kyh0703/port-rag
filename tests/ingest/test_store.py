from __future__ import annotations

import uuid

import pytest
from sqlalchemy.dialects import postgresql

from rag.db.models import Document
from rag.db.models import DocumentChunk
from rag.db.models import DocumentStatus
from rag.ingest.store import SqlAlchemyIngestStore


class FakeScalarResult:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def all(self) -> list[object]:
        return self._values


class FakeSession:
    def __init__(
        self,
        *,
        scalar_values: list[object | None],
        scalar_rows: list[list[object]] | None = None,
    ) -> None:
        self._scalar_values = scalar_values
        self._scalar_rows = scalar_rows or []
        self.scalar_statements = []
        self.scalars_statements = []
        self.commit_calls = 0

    async def scalar(self, statement):
        self.scalar_statements.append(statement)
        return self._scalar_values.pop(0)

    async def scalars(self, statement) -> FakeScalarResult:
        self.scalars_statements.append(statement)
        return FakeScalarResult(self._scalar_rows.pop(0))

    async def commit(self) -> None:
        self.commit_calls += 1


class FakeSessionContext:
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> FakeSession:
        return self._session

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakeSessionFactory:
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    def __call__(self) -> FakeSessionContext:
        return FakeSessionContext(self.session)


@pytest.mark.asyncio
async def test_reindex_lookup_is_owner_scoped_and_excludes_processing_documents() -> None:
    session = FakeSession(scalar_values=[None])
    store = SqlAlchemyIngestStore(FakeSessionFactory(session))

    chunks = await store.get_chunks_for_reindex(
        document_id=uuid.uuid4(),
        user_id="0197e50a-1234-7abc-8def-0123456789ab",
    )

    assert chunks is None
    assert session.scalars_statements == []
    assert session.commit_calls == 0
    compiled = str(session.scalar_statements[0].compile(dialect=postgresql.dialect()))
    assert "documents.user_id = %(user_id_1)s" in compiled
    assert "documents.status != %(status_1)s" in compiled


@pytest.mark.asyncio
async def test_reindex_updates_only_embeddings_and_commits() -> None:
    document_id = uuid.uuid4()
    user_id = uuid.UUID("0197e50a-1234-7abc-8def-0123456789ab")
    document = Document(
        id=document_id,
        user_id=user_id,
        name="support.md",
        mime="text/markdown",
        status=DocumentStatus.FAILED.value,
        error="previous embedding failure",
    )
    chunks = [
        DocumentChunk(
            document_id=document_id,
            seq=0,
            text="first answer",
            metadata_={"source": "support.md", "section": "one"},
            embedding=[9.0, 9.0, 9.0],
        ),
        DocumentChunk(
            document_id=document_id,
            seq=1,
            text="second answer",
            metadata_={"source": "support.md", "section": "two"},
            embedding=[8.0, 8.0, 8.0],
        ),
    ]
    session = FakeSession(scalar_values=[document], scalar_rows=[chunks])
    store = SqlAlchemyIngestStore(FakeSessionFactory(session))

    updated = await store.replace_embeddings_and_mark_ready(
        document_id=document_id,
        user_id=str(user_id),
        embeddings=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
    )

    assert updated is True
    assert document.status == DocumentStatus.READY.value
    assert document.error is None
    assert [(chunk.text, chunk.metadata_) for chunk in chunks] == [
        ("first answer", {"source": "support.md", "section": "one"}),
        ("second answer", {"source": "support.md", "section": "two"}),
    ]
    assert [chunk.embedding for chunk in chunks] == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    assert session.commit_calls == 1
    document_query = str(session.scalar_statements[0].compile(dialect=postgresql.dialect()))
    chunks_query = str(session.scalars_statements[0].compile(dialect=postgresql.dialect()))
    assert "documents.user_id = %(user_id_1)s" in document_query
    assert "documents.status != %(status_1)s" in document_query
    assert "FROM chunks" in chunks_query
    assert "ORDER BY chunks.seq" in chunks_query
