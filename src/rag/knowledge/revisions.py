"""Freeze file chunks and snapshot selected membership of live webpage sources."""

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
from rag.db.models import DocumentWebpage
from rag.db.models import KnowledgeRevision
from rag.db.models import KnowledgeRevisionChunk
from rag.db.models import KnowledgeRevisionWebpage
from rag.security.owner_erasure import lock_active_owner


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
    live_webpage_ids: tuple[uuid.UUID, ...] = ()


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
                DocumentWebpage.document_id.label("webpage_document_id"),
                DocumentChunk.seq.label("seq"),
                DocumentChunk.text.label("text"),
                DocumentChunk.metadata_.label("metadata"),
                DocumentChunk.embedding.label("embedding"),
            )
            .join(DocumentChunk, DocumentChunk.document_id == Document.id)
            .outerjoin(DocumentWebpage, DocumentWebpage.document_id == Document.id)
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
            await lock_active_owner(session, user_id)
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
            await session.flush()
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
                    if row["webpage_document_id"] is None
                ]
            )
            live_webpage_ids = tuple(dict.fromkeys(
                row["document_id"] for row in rows if row["webpage_document_id"] is not None
            ))
            session.add_all([
                KnowledgeRevisionWebpage(revision_id=resolved_revision_id, document_id=document_id)
                for document_id in live_webpage_ids
            ])
            await session.commit()

        return KnowledgeRevisionRecord(
            id=resolved_revision_id,
            user_id=normalized_user_id,
            chunk_count=len(rows),
            created_at=created_at,
            live_webpage_ids=live_webpage_ids,
        )

    @staticmethod
    def _summary():
        frozen_count = (
            sa.select(sa.func.count(KnowledgeRevisionChunk.id))
            .where(KnowledgeRevisionChunk.revision_id == KnowledgeRevision.id)
            .correlate(KnowledgeRevision).scalar_subquery()
        )
        live_count = (
            sa.select(sa.func.count(DocumentChunk.id))
            .select_from(KnowledgeRevisionWebpage)
            .join(Document, Document.id == KnowledgeRevisionWebpage.document_id)
            .join(DocumentWebpage, DocumentWebpage.document_id == Document.id)
            .join(DocumentChunk, DocumentChunk.document_id == Document.id)
            .where(KnowledgeRevisionWebpage.revision_id == KnowledgeRevision.id,
                   Document.user_id == KnowledgeRevision.user_id,
                   Document.status == DocumentStatus.READY.value)
            .correlate(KnowledgeRevision).scalar_subquery()
        )
        live_ids = (
            sa.select(sa.func.array_agg(KnowledgeRevisionWebpage.document_id))
            .where(KnowledgeRevisionWebpage.revision_id == KnowledgeRevision.id)
            .correlate(KnowledgeRevision).scalar_subquery()
        )
        return sa.select(
            KnowledgeRevision.id.label("id"),
            KnowledgeRevision.user_id.label("user_id"),
            (frozen_count + live_count).label("chunk_count"),
            KnowledgeRevision.created_at.label("created_at"),
            live_ids.label("live_webpage_ids"),
        )

    @staticmethod
    def _record(row) -> KnowledgeRevisionRecord:
        return KnowledgeRevisionRecord(
            id=row["id"], user_id=row["user_id"], chunk_count=int(row["chunk_count"]),
            created_at=row["created_at"],
            live_webpage_ids=tuple(sorted(row["live_webpage_ids"] or [])),
        )

    async def get(
        self, *, revision_id: uuid.UUID, user_id: str,
    ) -> KnowledgeRevisionRecord | None:
        statement = self._summary().where(
            KnowledgeRevision.id == revision_id,
            KnowledgeRevision.user_id == uuid.UUID(user_id),
        )
        async with self._session_factory() as session:
            await lock_active_owner(session, user_id)
            row = (await session.execute(statement)).mappings().one_or_none()
        return None if row is None else self._record(row)

    async def list(self, *, user_id: str) -> list[KnowledgeRevisionRecord]:
        statement = self._summary().where(
            KnowledgeRevision.user_id == uuid.UUID(user_id),
        ).order_by(KnowledgeRevision.created_at.desc(), KnowledgeRevision.id.desc())
        async with self._session_factory() as session:
            await lock_active_owner(session, user_id)
            rows = (await session.execute(statement)).mappings().all()
        return [self._record(row) for row in rows]
