"""Database persistence for ingest results."""

from __future__ import annotations

import uuid

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from rag.db.models import Document
from rag.db.models import DocumentChunk
from rag.db.models import DocumentStatus
from rag.ingest.types import IngestChunk


class SqlAlchemyIngestStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def replace_chunks_and_mark_ready(
        self,
        document_id: uuid.UUID,
        chunks: list[IngestChunk],
        embeddings: list[list[float]],
    ) -> None:
        async with self._session_factory() as session:
            document = await self._require_document(session, document_id)
            await session.execute(
                delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
            )
            session.add_all(
                [
                    DocumentChunk(
                        document_id=document_id,
                        seq=chunk.seq,
                        text=chunk.text,
                        metadata_=chunk.metadata,
                        embedding=embedding,
                    )
                    for chunk, embedding in zip(chunks, embeddings, strict=True)
                ]
            )
            document.status = DocumentStatus.READY.value
            document.error = None
            await session.commit()

    async def mark_failed(self, document_id: uuid.UUID, error: str) -> None:
        async with self._session_factory() as session:
            document = await self._require_document(session, document_id)
            document.status = DocumentStatus.FAILED.value
            document.error = error[:4000]
            await session.commit()

    async def _require_document(self, session: AsyncSession, document_id: uuid.UUID) -> Document:
        document = await session.get(Document, document_id)
        if document is None:
            raise ValueError(f"document not found: {document_id}")
        return document
