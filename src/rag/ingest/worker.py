"""Async in-process ingest worker."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid

from rag.ingest.pipeline import IngestPipeline
from rag.ingest.types import IngestJob, RetryableIngestError
from rag.ingest.uploads import LocalUploadStorage
from rag.metrics import Metrics
from rag.security.owner_erasure import OwnerAdmission
from rag.security.owner_erasure import OwnerDataErased

logger = logging.getLogger(__name__)


class IngestWorker:
    def __init__(
        self,
        pipeline: IngestPipeline,
        *,
        owner_access: OwnerAdmission,
        storage: LocalUploadStorage,
        metrics: Metrics | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._queue: asyncio.Queue[IngestJob | None] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._metrics = metrics
        self._owner_access = owner_access
        self._storage = storage
        self._erased_owner_hashes: set[str] = set()
        self._active_job: IngestJob | None = None
        self._active_task: asyncio.Task[None] | None = None
        self._retry_tasks: dict[uuid.UUID, tuple[IngestJob, asyncio.Task[None]]] = {}
        self._retry_attempts: dict[uuid.UUID, int] = {}
        self._stopping = False

    def start(self) -> None:
        self._stopping = False
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def enqueue(self, job: IngestJob) -> None:
        try:
            await self._owner_access.assert_active(job.user_id)
            owner_hash = hashlib.sha256(str(uuid.UUID(job.user_id)).encode()).hexdigest()
            if owner_hash in self._erased_owner_hashes:
                raise OwnerDataErased()
            self._queue.put_nowait(job)
        except OwnerDataErased:
            job.path.unlink(missing_ok=True)
            raise
        self._observe_queue_depth()

    async def erase_user(self, user_id: str) -> None:
        owner_hash = hashlib.sha256(str(uuid.UUID(user_id)).encode()).hexdigest()
        self._erased_owner_hashes.add(owner_hash)
        # No await between marking the owner and rebuilding the queue: a late
        # enqueue cannot slip into the owned work being drained.
        jobs = [self._queue.get_nowait() for _ in range(self._queue.qsize())]
        try:
            for job in jobs:
                if job is not None and job.user_id == user_id:
                    job.path.unlink(missing_ok=True)
        except BaseException:
            for job in jobs:
                self._queue.put_nowait(job)
                self._queue.task_done()
            self._observe_queue_depth()
            raise
        for job in jobs:
            if job is None or job.user_id != user_id:
                self._queue.put_nowait(job)
            self._queue.task_done()
        self._observe_queue_depth()
        cancelled = []
        for identifier, (job, task) in list(self._retry_tasks.items()):
            if job.user_id == user_id:
                self._retry_tasks.pop(identifier, None)
                self._retry_attempts.pop(identifier, None)
                task.cancel()
                cancelled.append(task)
                job.path.unlink(missing_ok=True)
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)
        active = self._active_task
        if self._active_job is not None and self._active_job.user_id == user_id and active is not None:
            # Drain instead of canceling asyncio.to_thread parser/chunker work:
            # canceling its coroutine would leave the underlying thread alive.
            await asyncio.shield(active)

    async def join(self) -> None:
        while True:
            await self._queue.join()
            delayed = [task for _, task in self._retry_tasks.values()]
            if not delayed:
                return
            await asyncio.gather(*delayed, return_exceptions=True)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopping = True
        delayed = [task for _, task in self._retry_tasks.values()]
        for task in delayed:
            task.cancel()
        if delayed:
            await asyncio.gather(*delayed, return_exceptions=True)
        await self._queue.put(None)
        await self._task
        self._task = None

    async def _run(self) -> None:
        while True:
            job = await self._queue.get()
            self._observe_queue_depth()
            try:
                if job is None:
                    return
                try:
                    self._active_job = job
                    self._active_task = asyncio.create_task(self._ingest_job(job))
                    await self._active_task
                except OwnerDataErased:
                    self._retry_attempts.pop(job.document_id, None)
                except RetryableIngestError:
                    self._schedule_retry(job)
                    logger.warning("ingest storage unavailable; encrypted original retained", extra={"document_id": str(job.document_id)})
                except Exception:
                    if self._metrics is not None:
                        self._metrics.ingest_jobs.labels(result="failed").inc()
                    logger.exception("ingest job failed", extra={"document_id": str(job.document_id)})
                else:
                    self._retry_attempts.pop(job.document_id, None)
                    if self._metrics is not None:
                        self._metrics.ingest_jobs.labels(result="succeeded").inc()
            finally:
                self._queue.task_done()
                self._active_job = None
                self._active_task = None

    def _schedule_retry(self, job: IngestJob) -> None:
        owner_hash = hashlib.sha256(str(uuid.UUID(job.user_id)).encode()).hexdigest()
        if owner_hash in self._erased_owner_hashes:
            job.path.unlink(missing_ok=True)
            return
        if self._stopping:
            return
        attempt = self._retry_attempts.get(job.document_id, 0)
        self._retry_attempts[job.document_id] = attempt + 1
        delay = min(0.1 * 2 ** min(attempt, 9), 30.0)
        async def retry() -> None:
            try:
                await asyncio.sleep(delay)
                if not self._stopping:
                    await self.enqueue(job)
            except OwnerDataErased:
                pass
            finally:
                self._retry_tasks.pop(job.document_id, None)
        task = asyncio.create_task(retry(), name=f"ingest-storage-retry-{job.document_id}")
        self._retry_tasks[job.document_id] = (job, task)

    async def _ingest_job(self, job: IngestJob) -> None:
        preserve_original = False
        try:
            async with self._storage.hold_owner(job.user_id):
                await self._owner_access.assert_active(job.user_id)
                await self._pipeline.ingest(job)
        except RetryableIngestError:
            preserve_original = True
            raise
        finally:
            if not preserve_original:
                job.path.unlink(missing_ok=True)

    def _observe_queue_depth(self) -> None:
        if self._metrics is not None:
            self._metrics.ingest_queue_depth.set(self._queue.qsize())
