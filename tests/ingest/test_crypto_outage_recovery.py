from pathlib import Path
import uuid
import asyncio
from rag.ingest.pipeline import IngestPipeline
from rag.ingest.types import IngestJob
from rag.ingest.worker import IngestWorker
from rag.ingest.uploads import LocalUploadStorage
from rag.security.private_data import PrivateDataUnavailable
from tests.private_data_fixture import private_data_cipher, PlainParserInputFixture
from tests.fakes import MemoryOwnerAdmission, StaticFakeEmbedder
from tests.ingest.test_pipeline import MemoryStore, TextParser, SplitChunker


class RecoveringStore(MemoryStore):
    unavailable = True

    def __init__(self):
        super().__init__()
        self.pending_codes = []

    async def replace_chunks_and_mark_ready(self, *args):
        if self.unavailable:
            raise PrivateDataUnavailable()
        return await super().replace_chunks_and_mark_ready(*args)

    async def mark_failed(self, *args):
        if self.unavailable:
            raise PrivateDataUnavailable()
        return await super().mark_failed(*args)

    async def mark_storage_unavailable(self, document_id):
        self.statuses[document_id] = "failed"
        self.errors[document_id] = "storage_encryption_unavailable"
        self.pending_codes.append(document_id)


async def test_outage_preserves_encrypted_input_and_worker_retries_after_recovery(tmp_path: Path):
    owners = MemoryOwnerAdmission()
    store = RecoveringStore()
    storage = LocalUploadStorage(
        owner_access=owners, cipher=private_data_cipher, staging_root=tmp_path / "staging"
    )
    path = tmp_path / "fixture.encrypted"
    path.write_text("synthetic parsed fixture")
    job = IngestJob(document_id=uuid.uuid4(), path=path, user_id=str(uuid.uuid4()))
    pipeline = IngestPipeline(
        parser=TextParser(),
        chunker=SplitChunker(),
        embedder=StaticFakeEmbedder(),
        store=store,
        owner_access=owners,
        storage=PlainParserInputFixture(),
    )
    worker = IngestWorker(pipeline, owner_access=owners, storage=storage)
    worker.start()
    try:
        await worker.enqueue(job)
        async with asyncio.timeout(2):
            while not store.pending_codes:
                await asyncio.sleep(0.005)
        assert path.exists()
        assert store.errors[job.document_id] == "storage_encryption_unavailable"
        store.unavailable = False
        await asyncio.wait_for(worker.join(), 3)
        assert store.statuses[job.document_id] == "ready"
        assert not path.exists()
    finally:
        await worker.stop()
        storage.close()


async def test_erasure_cancels_delayed_crypto_retry_and_removes_only_owned_input(tmp_path: Path):
    owners = MemoryOwnerAdmission()
    store = RecoveringStore()
    store.pending_codes = []
    storage = LocalUploadStorage(
        owner_access=owners, cipher=private_data_cipher, staging_root=tmp_path / "staging"
    )
    path = tmp_path / "fixture.encrypted"
    path.write_text("synthetic fixture")
    user = str(uuid.uuid4())
    job = IngestJob(document_id=uuid.uuid4(), path=path, user_id=user)
    pipeline = IngestPipeline(
        parser=TextParser(),
        chunker=SplitChunker(),
        embedder=StaticFakeEmbedder(),
        store=store,
        owner_access=owners,
        storage=PlainParserInputFixture(),
    )
    worker = IngestWorker(pipeline, owner_access=owners, storage=storage)
    worker.start()
    try:
        await worker.enqueue(job)
        async with asyncio.timeout(2):
            while not store.pending_codes:
                await asyncio.sleep(0.005)
        assert path.exists()
        owners.erased.add(user)
        await worker.erase_user(user)
        await asyncio.wait_for(worker.join(), 2)
        assert not path.exists()
    finally:
        await worker.stop()
        storage.close()
