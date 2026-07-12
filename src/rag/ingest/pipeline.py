"""Ingest orchestration."""

from __future__ import annotations

from rag.ingest.types import DocumentChunker
from rag.ingest.types import DocumentParser
from rag.ingest.types import IngestJob
from rag.ingest.types import IngestStore
from rag.ingest.types import TextEmbedder


class IngestPipeline:
    def __init__(
        self,
        *,
        parser: DocumentParser,
        chunker: DocumentChunker,
        embedder: TextEmbedder,
        store: IngestStore,
    ) -> None:
        self._parser = parser
        self._chunker = chunker
        self._embedder = embedder
        self._store = store

    async def ingest(self, job: IngestJob) -> None:
        try:
            parsed = await self._parser.parse(job.path)
            chunks = await self._chunker.chunk(parsed)
            if not chunks:
                raise ValueError("document produced no chunks")

            embeddings = await self._embedder.embed_texts([chunk.text for chunk in chunks])
            if len(embeddings) != len(chunks):
                raise ValueError(
                    f"embedder returned {len(embeddings)} embeddings for {len(chunks)} chunks"
                )

            await self._store.replace_chunks_and_mark_ready(job.document_id, chunks, embeddings)
        except Exception as exc:
            await self._store.mark_failed(job.document_id, str(exc))
        finally:
            job.path.unlink(missing_ok=True)
