"""Create immutable copies of ready document chunks."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from rag.db.models import Document
from rag.db.models import DocumentChunk
from rag.db.models import DocumentStatus
from rag.db.models import KnowledgeRevision
from rag.db.models import KnowledgeRevisionChunk


class SessionFactory(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[AsyncSession]:
        pass


class KnowledgeRevisionNotFound(ValueError):
    """Raised when a selected source document is not ready for revisioning."""


@dataclass(frozen=True)
class KnowledgeRevisionRecord:
    id: uuid.UUID
    user_id: uuid.UUID
    chunk_count: int
    created_at: datetime


class KnowledgeRevisionRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        *,
        user_id: str,
        document_ids: Sequence[uuid.UUID] | None = None,
        revision_id: uuid.UUID | None = None,
    ) -> KnowledgeRevisionRecord:
        normalized_user_id = uuid.UUID(user_id)
        statement = (
            sa.select(
                Document.id.label("document_id"),
                Document.name.label("document_name"),
                DocumentChunk.seq.label("seq"),
                DocumentChunk.text.label("text"),
                DocumentChunk.metadata_.label("metadata"),
                DocumentChunk.embedding.label("embedding"),
            )
            .join(DocumentChunk, DocumentChunk.document_id == Document.id)
            .where(
                Document.user_id == normalized_user_id,
                Document.status == DocumentStatus.READY.value,
            )
            .order_by(Document.id, DocumentChunk.seq)
        )
        selected_ids = set(document_ids) if document_ids is not None else None
        if selected_ids is not None:
            statement = statement.where(Document.id.in_(selected_ids))

        async with self._session_factory() as session:
            rows = (await session.execute(statement)).mappings().all()
            found_ids = {row["document_id"] for row in rows}
            if selected_ids is not None and found_ids != selected_ids:
                raise KnowledgeRevisionNotFound(
                    "selected document is missing, assigned to another user, or not ready"
                )

            resolved_revision_id = revision_id or uuid.uuid4()
            created_at = datetime.now(UTC)
            session.add(
                KnowledgeRevision(
                    id=resolved_revision_id,
                    user_id=normalized_user_id,
                    created_at=created_at,
                )
            )
            session.add_all(
                [
                    KnowledgeRevisionChunk(
                        revision_id=resolved_revision_id,
                        source_document_id=row["document_id"],
                        document_name=row["document_name"],
                        seq=row["seq"],
                        text=row["text"],
                        metadata_=row["metadata"],
                        embedding=row["embedding"],
                    )
                    for row in rows
                ]
            )
            await session.flush()
            await session.commit()

        return KnowledgeRevisionRecord(
            id=resolved_revision_id,
            user_id=normalized_user_id,
            chunk_count=len(rows),
            created_at=created_at,
        )

    async def get(
        self,
        *,
        revision_id: uuid.UUID,
        user_id: str,
    ) -> KnowledgeRevisionRecord | None:
        statement = (
            sa.select(
                KnowledgeRevision,
                sa.func.count(KnowledgeRevisionChunk.id).label("chunk_count"),
            )
            .outerjoin(
                KnowledgeRevisionChunk,
                KnowledgeRevisionChunk.revision_id == KnowledgeRevision.id,
            )
            .where(
                KnowledgeRevision.id == revision_id,
                KnowledgeRevision.user_id == uuid.UUID(user_id),
            )
            .group_by(KnowledgeRevision.id)
        )
        async with self._session_factory() as session:
            row = (await session.execute(statement)).one_or_none()
        if row is None:
            return None
        revision, chunk_count = row
        return KnowledgeRevisionRecord(
            id=revision.id,
            user_id=revision.user_id,
            chunk_count=int(chunk_count),
            created_at=revision.created_at,
        )

    async def list(self, *, user_id: str) -> list[KnowledgeRevisionRecord]:
        statement = (
            sa.select(
                KnowledgeRevision.id.label("id"),
                KnowledgeRevision.user_id.label("user_id"),
                sa.func.count(KnowledgeRevisionChunk.id).label("chunk_count"),
                KnowledgeRevision.created_at.label("created_at"),
            )
            .outerjoin(
                KnowledgeRevisionChunk,
                KnowledgeRevisionChunk.revision_id == KnowledgeRevision.id,
            )
            .where(KnowledgeRevision.user_id == uuid.UUID(user_id))
            .group_by(KnowledgeRevision.id)
            .order_by(
                KnowledgeRevision.created_at.desc(),
                KnowledgeRevision.id.desc(),
            )
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).mappings().all()
        return [
            KnowledgeRevisionRecord(
                id=row["id"],
                user_id=row["user_id"],
                chunk_count=int(row["chunk_count"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]
