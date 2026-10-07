from __future__ import annotations
from tests.private_data_fixture import private_data_cipher

import uuid
from pathlib import Path

import pytest

from rag.ingest.types import IngestJob
from rag.ingest.worker import IngestWorker
from rag.ingest.uploads import LocalUploadStorage
from tests.fakes import MemoryOwnerAdmission


class RecordingPipeline:
    def __init__(self) -> None:
        self.seen: list[uuid.UUID] = []

    async def ingest(self, job: IngestJob) -> None:
        self.seen.append(job.document_id)
        if len(self.seen) == 1:
            raise ValueError("document not found")


@pytest.mark.asyncio
async def test_worker_continues_after_job_failure(tmp_path: Path) -> None:
    pipeline = RecordingPipeline()
    owners = MemoryOwnerAdmission()
    storage = LocalUploadStorage(
        staging_root=tmp_path / "staging", legacy_root=tmp_path / "legacy", owner_access=owners,
        cipher=private_data_cipher,
    )
    worker = IngestWorker(pipeline, owner_access=owners, storage=storage)
    user_id = "0197e50a-1234-7abc-8def-0123456789ab"
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()

    worker.start()
    await worker.enqueue(IngestJob(document_id=first_id, path=tmp_path / "missing.md", user_id=user_id))
    await worker.enqueue(IngestJob(document_id=second_id, path=tmp_path / "next.md", user_id=user_id))
    await worker.join()
    await worker.stop()
    storage.close()

    assert pipeline.seen == [first_id, second_id]
