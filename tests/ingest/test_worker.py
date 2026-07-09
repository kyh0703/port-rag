from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from reg.ingest.types import IngestJob
from reg.ingest.worker import IngestWorker


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
    worker = IngestWorker(pipeline)
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()

    worker.start()
    await worker.enqueue(IngestJob(document_id=first_id, path=tmp_path / "missing.md"))
    await worker.enqueue(IngestJob(document_id=second_id, path=tmp_path / "next.md"))
    await worker.join()
    await worker.stop()

    assert pipeline.seen == [first_id, second_id]
