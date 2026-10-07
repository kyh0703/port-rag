"""Poll durable webpage jobs; publish only a complete, still-authorized result."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import suppress
from typing import Protocol

from rag.security.private_data import PrivateDataCipher, StorageBinding
from rag.ingest.types import IngestChunk, TextEmbedder
from rag.webpages.fetch import WebpageFetchError, WebpagePage
from rag.webpages.repository import WebpageClaim, WebpageRepository

logger = logging.getLogger(__name__)
JOB_TIMEOUT = 600.0  # Strictly shorter than the repository's 15-minute claim lease.
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024


class PageFetcher(Protocol):
    async def fetch(self, url: str) -> WebpagePage: ...


class WebpageWorker:
    def __init__(self, *, repository: WebpageRepository, fetcher: PageFetcher,
                 embedder: TextEmbedder,
        cipher: PrivateDataCipher,
        poll_interval: float = 5.0,
    ) -> None:
        self._repository = repository
        self._fetcher = fetcher
        self._embedder = embedder
        self._cipher = cipher
        self._poll_interval = poll_interval
        self._task: asyncio.Task | None = None
        self._erased_owner_hashes: set[str] = set()
        self._active: dict[object, tuple[str, asyncio.Task]] = {}

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="webpage-sync")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                if await self.run_once():
                    continue
            except Exception as exc:
                # No fetched content, URLs, or provider credentials in logs.
                logger.warning("Webpage queue iteration failed (%s)", type(exc).__name__)
            await asyncio.sleep(self._poll_interval)

    async def run_once(self) -> bool:
        claim = await self._repository.claim()
        if claim is None:
            return False
        await self.process(claim)
        return True

    async def process(self, claim: WebpageClaim) -> None:
        owner_hash = hashlib.sha256(claim.user_id.encode()).hexdigest()
        if owner_hash in self._erased_owner_hashes:
            return
        task = asyncio.create_task(self._process(claim))
        self._active[claim.token] = (owner_hash, task)
        try:
            await task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
        finally:
            self._active.pop(claim.token, None)

    async def erase_user(self, user_id: str) -> None:
        owner_hash = hashlib.sha256(user_id.encode()).hexdigest()
        self._erased_owner_hashes.add(owner_hash)
        tasks = [task for owner, task in self._active.values() if owner == owner_hash]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _process(self, claim: WebpageClaim) -> None:
        try:
            async with asyncio.timeout(JOB_TIMEOUT):
                pages = []
                total_bytes = 0
                for url in claim.urls:
                    if not await self._repository.claim_is_current(claim):
                        return
                    page = await self._fetcher.fetch(url)
                    if not page.text.strip():
                        raise WebpageFetchError("webpage has no readable server-rendered text")
                    total_bytes += len(page.text.encode("utf-8"))
                    if total_bytes > MAX_DOCUMENT_BYTES:
                        raise WebpageFetchError("selected webpage content is too large")
                    pages.append(page)
                # Include source boundaries in the digest. The selected URLs never
                # change during sync; discovery is not repeated by this worker.
                digest = hashlib.sha256(json.dumps(
                    [(url, page.text) for url, page in zip(claim.urls, pages, strict=True)],
                    ensure_ascii=False, separators=(",", ":"),
                ).encode("utf-8")).hexdigest()
                fingerprint = await self._cipher.lookup(
                    digest,
                    StorageBinding(
                        claim.user_id, str(claim.document_id), "webpage:content-fingerprint"
                    ),
                )
                content = "\n\n".join(page.text for page in pages)
                chunks = embeddings = None
                if claim.content_hash not in (digest, fingerprint):
                    chunks = []
                    for url, page in zip(claim.urls, pages, strict=True):
                        for start in range(0, len(page.text), 1440):
                            text = page.text[start:start + 1600].strip()
                            if text:
                                chunks.append(IngestChunk(
                                    seq=len(chunks), text=text,
                                    metadata={"source_url": url, "url": page.url, "source": "webpage",
                                        },
                                ))
                    embeddings = []
                    # Even worst-case Unicode stays under the provider's aggregate
                    # token limit; admission is checked before every external batch.
                    for start in range(0, len(chunks), 32):
                        if not await self._repository.claim_is_current(claim):
                            return
                        batch = chunks[start:start + 32]
                        vectors = await self._embedder.embed_texts([chunk.text for chunk in batch])
                        if len(vectors) != len(batch):
                            raise ValueError("embedding count mismatch")
                        embeddings.extend(vectors)
                await self._repository.finish(
                    claim, content=content, content_hash=fingerprint,
                    chunks=chunks, embeddings=embeddings,
                )
        except WebpageFetchError as exc:
            await self._repository.fail(claim, str(exc))
        except TimeoutError:
            await self._repository.fail(claim, "webpage synchronization timed out")
        except Exception:
            await self._repository.fail(claim, "webpage synchronization failed")
