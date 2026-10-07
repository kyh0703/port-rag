"""Ingest orchestration."""

from __future__ import annotations

import uuid

from rag.ingest.types import DocumentChunker
from rag.ingest.types import DocumentParser
from rag.ingest.types import IngestJob
from rag.ingest.types import IngestStore
from rag.ingest.types import ReindexFailedError, RetryableIngestError
from rag.security.private_data import PrivateDataUnavailable
from rag.ingest.types import TextEmbedder
from rag.ingest.uploads import LocalUploadStorage
from rag.security.owner_erasure import OwnerAdmission
from rag.security.owner_erasure import OwnerDataErased


class IngestPipeline:
    def __init__(
        self,
        *,
        parser: DocumentParser,
        chunker: DocumentChunker,
        embedder: TextEmbedder,
        store: IngestStore,
        owner_access: OwnerAdmission,
        storage: LocalUploadStorage,
    ) -> None:
        self._parser = parser
        self._chunker = chunker
        self._embedder = embedder
        self._store = store
        self._owner_access = owner_access
        self._storage = storage

    async def ingest(self, job: IngestJob) -> None:
        preserve_original = False
        try:
            await self._owner_access.assert_active(job.user_id)
            async with self._storage.decrypted_path(job.path, user_id=job.user_id) as path:
                parsed = await self._parser.parse(path)
            await self._owner_access.assert_active(job.user_id)
            chunks = await self._chunker.chunk(parsed)
            if not chunks:
                raise ValueError("document produced no chunks")

            await self._owner_access.assert_active(job.user_id)
            embeddings = await self._embedder.embed_texts([chunk.text for chunk in chunks])
            if len(embeddings) != len(chunks):
                raise ValueError(
                    f"embedder returned {len(embeddings)} embeddings for {len(chunks)} chunks"
                )

            await self._owner_access.assert_active(job.user_id)
            await self._store.replace_chunks_and_mark_ready(job.document_id, chunks, embeddings)
        except OwnerDataErased:
            return
        except PrivateDataUnavailable:
            preserve_original = True
            try:
                await self._owner_access.assert_active(job.user_id)
                await self._store.mark_storage_unavailable(job.document_id)
            except OwnerDataErased:
                preserve_original = False
                return
            except Exception:
                pass  # The original remains retryable even if the DB is also down.
            raise RetryableIngestError("storage_encryption_unavailable") from None
        except Exception as exc:
            try:
                await self._owner_access.assert_active(job.user_id)
                await self._store.mark_failed(job.document_id, str(exc))
            except OwnerDataErased:
                return
            except Exception:
                preserve_original = True
                raise RetryableIngestError("failure_status_unavailable") from None
        finally:
            if not preserve_original:
                job.path.unlink(missing_ok=True)

    async def reindex(self, *, document_id: uuid.UUID, user_id: str) -> bool:
        try:
            await self._owner_access.assert_active(user_id)
            chunks = await self._store.get_chunks_for_reindex(document_id, user_id)
            if chunks is None:
                return False
            if not chunks:
                raise ValueError("document has no chunks to reindex")

            await self._owner_access.assert_active(user_id)
            embeddings = await self._embedder.embed_texts([chunk.text for chunk in chunks])
            if len(embeddings) != len(chunks):
                raise ValueError(
                    f"embedder returned {len(embeddings)} embeddings for {len(chunks)} chunks"
                )

            await self._owner_access.assert_active(user_id)
            return await self._store.replace_embeddings_and_mark_ready(
                document_id,
                user_id,
                embeddings,
            )
        except OwnerDataErased:
            raise
        except Exception as exc:
            marked_failed = await self._store.mark_reindex_failed(document_id, user_id, str(exc))
            if not marked_failed:
                return False
            raise ReindexFailedError("document reindex failed") from exc
