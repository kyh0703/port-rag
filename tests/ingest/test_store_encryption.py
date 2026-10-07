from tests.private_data_fixture import private_data_cipher
import uuid
import pytest
from unittest.mock import AsyncMock
from rag.db.models import Document
from rag.ingest.store import SqlAlchemyIngestStore
from rag.ingest.types import IngestChunk
from tests.private_data_fixture import PrivateDataCipherFake


@pytest.mark.asyncio
async def test_store_encrypts_chunk_text_metadata_and_failures_before_commit():
    stored = []

    class Session:
        execute = AsyncMock()
        commit = AsyncMock()

        def add_all(self, values):
            stored.extend(values)

    class Context:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *args):
            pass

    session = Session()
    document = Document(id=uuid.uuid4(), user_id=uuid.uuid4(), name="private.txt")
    store = SqlAlchemyIngestStore(lambda: Context(), cipher=private_data_cipher)
    store._cipher = PrivateDataCipherFake()
    store._require_document = AsyncMock(return_value=document)
    await store.replace_chunks_and_mark_ready(
        document.id,
        [
            IngestChunk(
                seq=0, text="김민수 010-1234-5678", metadata={"email": "private@example.test"}
            )
        ],
        [[0.1] * 1536],
    )
    assert "김민수" not in stored[0].text
    assert "private@example" not in str(stored[0].metadata_)
    assert stored[0].text.startswith("vault:")
    await store.mark_failed(document.id, "Parse failure for private@example.test")
    assert "private@example" not in document.error
