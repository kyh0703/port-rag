"""pgvector-backed search repository."""

from __future__ import annotations
from rag.security.private_data import PrivateDataCipher, StorageBinding, read_json, read_text

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any
from typing import Protocol
import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from rag.db.models import Document
from rag.db.models import DocumentChunk
from rag.db.models import DocumentStatus
from rag.db.models import DocumentWebpage
from rag.db.models import KnowledgeRevisionChunk
from rag.db.models import KnowledgeRevision
from rag.db.models import KnowledgeRevisionWebpage
from rag.search.types import SearchHit
from rag.security.owner_erasure import lock_active_owner


class SessionFactory(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[AsyncSession]:
        pass


class SearchRepository:
    def __init__(self, session_factory: SessionFactory, cipher: PrivateDataCipher) -> None:
        self._session_factory = session_factory
        self._cipher = cipher

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
            await lock_active_owner(session, user_id)
            result = await session.execute(statement)
            rows = result.mappings().all()

        return [await self._hit(row, user_id)
            for row in rows
        ]

    async def search_revision(
        self,
        *,
        user_id: str,
        knowledge_revision_id: str,
        embedding: Sequence[float],
        top_k: int,
    ) -> list[SearchHit]:
        revision_id = uuid.UUID(knowledge_revision_id)
        owner_id = uuid.UUID(user_id)
        query_vector = list(embedding)
        distance = KnowledgeRevisionChunk.embedding.cosine_distance(query_vector)
        selected_live_source = sa.exists().where(
            KnowledgeRevisionWebpage.revision_id == KnowledgeRevisionChunk.revision_id,
            KnowledgeRevisionWebpage.document_id == KnowledgeRevisionChunk.source_document_id,
        )
        frozen = (
            sa.select(
                KnowledgeRevisionChunk.id.label("chunk_id"),
                KnowledgeRevisionChunk.source_document_id.label("document_id"),
                KnowledgeRevisionChunk.document_name.label("document_name"),
                KnowledgeRevisionChunk.text.label("text"),
                (sa.literal(1.0) - distance).label("score"),
                KnowledgeRevisionChunk.metadata_.label("metadata"),
                KnowledgeRevisionChunk.seq.label("seq"),
                KnowledgeRevisionChunk.revision_id.label("storage_revision_id"),
            )
            .join(
                KnowledgeRevision,
                KnowledgeRevision.id == KnowledgeRevisionChunk.revision_id,
            )
            .where(
                KnowledgeRevisionChunk.revision_id == revision_id,
                KnowledgeRevision.user_id == owner_id,
                ~selected_live_source,
            )
            .order_by(distance)
            .limit(top_k)
        )
        live_distance = DocumentChunk.embedding.cosine_distance(query_vector)
        live = (
            sa.select(
                DocumentChunk.id.label("chunk_id"),
                Document.id.label("document_id"),
                Document.name.label("document_name"),
                DocumentChunk.text.label("text"),
                (sa.literal(1.0) - live_distance).label("score"),
                DocumentChunk.metadata_.label("metadata"),
                DocumentChunk.seq.label("seq"),
                sa.cast(sa.null(), sa.UUID).label("storage_revision_id"),
            )
            .select_from(KnowledgeRevisionWebpage)
            .join(KnowledgeRevision, KnowledgeRevision.id == KnowledgeRevisionWebpage.revision_id)
            .join(Document, Document.id == KnowledgeRevisionWebpage.document_id)
            .join(DocumentWebpage, DocumentWebpage.document_id == Document.id)
            .join(DocumentChunk, DocumentChunk.document_id == Document.id)
            .where(
                KnowledgeRevisionWebpage.revision_id == revision_id,
                KnowledgeRevision.user_id == owner_id,
                Document.user_id == owner_id,
                Document.status == DocumentStatus.READY.value,
            )
            .order_by(live_distance)
            .limit(top_k)
        )
        candidates = sa.union_all(frozen, live).subquery()
        statement = sa.select(candidates).order_by(candidates.c.score.desc()).limit(top_k)

        async with self._session_factory() as session:
            await lock_active_owner(session, user_id)
            result = await session.execute(statement)
            rows = result.mappings().all()

        return [await self._hit(row, user_id) for row in rows]

    async def _hit(self, row: Any, user_id: str) -> SearchHit:
        document_id = str(row["document_id"])
        revision_id = row.get("storage_revision_id")
        resource = str(revision_id) if revision_id is not None else document_id
        prefix = f"document:{document_id}:" if revision_id is not None else ""
        seq = int(row["seq"])
        name_field = f"document:{document_id}:name" if revision_id is not None else "document:name"
        return SearchHit(
                chunk_id=str(row["chunk_id"]),
                document_id=document_id,
                document_name=await read_text(
                self._cipher,
                str(row["document_name"]),
                StorageBinding(user_id, resource, name_field),
            ),
            text=await read_text(
                self._cipher,
                str(row["text"]),
                StorageBinding(user_id, resource, f"{prefix}chunk:{seq}:text"),
            ),
                score=float(row["score"]),
            seq=seq,
            metadata=_string_metadata(
                await read_json(
                    self._cipher,
                    row["metadata"],
                    StorageBinding(user_id, resource, f"{prefix}chunk:{seq}:metadata"),
            )
            ),
        )


def _string_metadata(metadata: Any) -> dict[str, str]:
    if not isinstance(metadata, dict):
        return {}
    return {str(key): str(value) for key, value in metadata.items() if value is not None}
