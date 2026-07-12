"""Async in-process ingest worker."""

from __future__ import annotations

import asyncio
import logging

from rag.ingest.pipeline import IngestPipeline
from rag.ingest.types import IngestJob

logger = logging.getLogger(__name__)


class IngestWorker:
    def __init__(self, pipeline: IngestPipeline) -> None:
        self._pipeline = pipeline
        self._queue: asyncio.Queue[IngestJob | None] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def enqueue(self, job: IngestJob) -> None:
        await self._queue.put(job)

    async def join(self) -> None:
        await self._queue.join()

    async def stop(self) -> None:
        if self._task is None:
            return
        await self._queue.put(None)
        await self._task
        self._task = None

    async def _run(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                if job is None:
                    return
                try:
                    await self._pipeline.ingest(job)
                except Exception:
                    logger.exception("ingest job failed", extra={"document_id": str(job.document_id)})
            finally:
                self._queue.task_done()
