from __future__ import annotations
from tests.private_data_fixture import private_data_cipher

import uuid
from datetime import UTC

import pytest
from rag.security.private_data import StorageBinding, read_json

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

    def all(self) -> list[dict[str, object]]:
        return self._rows


class FakeSession:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.added = []

    async def execute(self, statement, parameters=None):
        return FakeResult(self.rows)

    def add(self, entity) -> None:
        self.added.append(entity)

    def add_all(self, entities) -> None:
        self.added.extend(entities)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None


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


async def test_create_revision_preserves_source_payload_in_snapshot() -> None:
    user_id = uuid.UUID("0197e50a-1234-7abc-8def-0123456789ab")
    document_id = uuid.uuid4()
    revision_id = uuid.UUID("0197e50a-1234-7abc-8def-0123456789ae")
    session = FakeSession(
        [
            {
                "document_id": document_id,
                "document_name": "support.md",
                "webpage_document_id": None,
                "seq": 2,
                "text": "immutable answer",
                "metadata": {"page": 1},
                "embedding": [0.1, 0.2, 0.3],
            }
        ]
    )
    repository = KnowledgeRevisionRepository(FakeSessionFactory(session), cipher=private_data_cipher
    )

    revision = await repository.create(
        user_id=str(user_id),
        document_ids=[document_id],
        revision_id=revision_id,
    )

    assert revision.id == revision_id
    assert revision.user_id == user_id
    assert revision.chunk_count == 1
    assert revision.created_at.tzinfo == UTC
    copied = session.added[1]
    assert copied.revision_id == revision_id
    assert copied.source_document_id == document_id
    assert copied.document_name.startswith("vault:")
    assert (
        await private_data_cipher.decrypt(
            copied.document_name,
            StorageBinding(str(user_id), str(revision_id), f"document:{document_id}:name"),
        )
        == "support.md"
    )
    assert (
        await private_data_cipher.decrypt(
            copied.text,
            StorageBinding(
                str(user_id), str(revision_id), f"document:{document_id}:chunk:{copied.seq}:text"
            ),
        )
        == "immutable answer"
    )
    assert await read_json(
        private_data_cipher,
        copied.metadata_,
        StorageBinding(
            str(user_id), str(revision_id), f"document:{document_id}:chunk:{copied.seq}:metadata"
        ),
    ) == {"page": 1}
    assert list(copied.embedding) == [0.1, 0.2, 0.3]


async def test_create_revision_rejects_missing_or_unready_selected_document() -> None:
    repository = KnowledgeRevisionRepository(FakeSessionFactory(FakeSession([])), cipher=private_data_cipher
    )

    with pytest.raises(KnowledgeRevisionNotFound, match="selected document"):
        await repository.create(
            user_id="0197e50a-1234-7abc-8def-0123456789ab",
            document_ids=[uuid.uuid4()],
        )
