from __future__ import annotations

import uuid
from datetime import UTC

import pytest
from sqlalchemy.dialects import postgresql

from rag.knowledge.revisions import KnowledgeRevisionNotFound
from rag.knowledge.revisions import KnowledgeRevisionRepository


class FakeMappings:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def all(self) -> list[dict[str, object]]:
        return self._rows


class FakeResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> FakeMappings:
        return FakeMappings(self._rows)


class FakeSession:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.statements = []
        self.added = []
        self.commit_calls = 0

    async def execute(self, statement):
        self.statements.append(statement)
        return FakeResult(self.rows)

    def add(self, entity) -> None:
        self.added.append(entity)

    def add_all(self, entities) -> None:
        self.added.extend(entities)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commit_calls += 1


class FakeSessionContext:
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    async def __aenter__(self) -> FakeSession:
        return self.session

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakeSessionFactory:
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    def __call__(self) -> FakeSessionContext:
        return FakeSessionContext(self.session)


async def test_create_revision_copies_only_ready_user_documents() -> None:
    user_id = uuid.UUID("0197e50a-1234-7abc-8def-0123456789ab")
    document_id = uuid.uuid4()
    revision_id = uuid.UUID("0197e50a-1234-7abc-8def-0123456789ae")
    session = FakeSession(
        [
            {
                "document_id": document_id,
                "document_name": "support.md",
                "seq": 2,
                "text": "immutable answer",
                "metadata": {"page": 1},
                "embedding": [0.1, 0.2, 0.3],
            }
        ]
    )
    repository = KnowledgeRevisionRepository(FakeSessionFactory(session))

    revision = await repository.create(
        user_id=str(user_id),
        document_ids=[document_id],
        revision_id=revision_id,
    )

    assert revision.id == revision_id
    assert revision.user_id == user_id
    assert revision.chunk_count == 1
    assert revision.created_at.tzinfo == UTC
    assert session.commit_calls == 1
    assert len(session.added) == 2
    assert session.added[0].id == revision_id
    statement = str(session.statements[0].compile(dialect=postgresql.dialect()))
    assert "documents.user_id = %(user_id_1)s" in statement
    assert "documents.status = %(status_1)s" in statement
    assert "documents.id IN" in statement


async def test_create_revision_rejects_missing_or_unready_selected_document() -> None:
    repository = KnowledgeRevisionRepository(FakeSessionFactory(FakeSession([])))

    with pytest.raises(KnowledgeRevisionNotFound, match="selected document"):
        await repository.create(
            user_id="0197e50a-1234-7abc-8def-0123456789ab",
            document_ids=[uuid.uuid4()],
        )
