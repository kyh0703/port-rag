"""pgvector-backed search repository."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from reg.db.models import Document
from reg.db.models import DocumentChunk
from reg.db.models import DocumentStatus
from reg.search.types import SearchHit


class SessionFactory(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[AsyncSession]:
        pass


class SearchRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def search(
        self,
        *,
        user_id: str,
        embedding: Sequence[float],
        top_k: int,
    ) -> list[SearchHit]:
        distance = DocumentChunk.embedding.cosine_distance(list(embedding))
        statement = (
            sa.select(
                DocumentChunk.id.label("chunk_id"),
                Document.id.label("document_id"),
                Document.name.label("document_name"),
                DocumentChunk.text.label("text"),
                (sa.literal(1.0) - distance).label("score"),
                DocumentChunk.metadata_.label("metadata"),
                DocumentChunk.seq.label("seq"),
            )
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                Document.user_id == user_id,
                Document.status == DocumentStatus.READY.value,
            )
            .order_by(distance)
            .limit(top_k)
        )

        async with self._session_factory() as session:
            result = await session.execute(statement)
            rows = result.mappings().all()

        return [
            SearchHit(
                chunk_id=str(row["chunk_id"]),
                document_id=str(row["document_id"]),
                document_name=str(row["document_name"]),
                text=str(row["text"]),
                score=float(row["score"]),
                metadata=_string_metadata(row["metadata"]),
                seq=int(row["seq"]),
            )
            for row in rows
        ]


def _string_metadata(metadata: Any) -> dict[str, str]:
    if not isinstance(metadata, dict):
        return {}
    return {str(key): str(value) for key, value in metadata.items() if value is not None}
