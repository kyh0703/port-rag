"""Durable queue, owner-scoped reads, and fenced atomic webpage publication."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from rag.db.models import Document, DocumentChunk, DocumentStatus, DocumentWebpage
from rag.http.documents import DocumentRecord, DuplicateKnowledgeKey, SessionFactory, _to_record
from rag.ingest.types import IngestChunk
from rag.security.owner_erasure import OwnerDataErased, lock_active_owner
from rag.webpages.fetch import MAX_URLS, WebpageFetchError, normalize_url

SYNC_INTERVAL = timedelta(hours=24)
CLAIM_LEASE = timedelta(minutes=15)


class SyncConflict(ValueError):
    """This document already has durable queued or running work."""


@dataclass(frozen=True)
class WebpageDetail:
    document: DocumentRecord
    content: str


@dataclass(frozen=True)
class WebpageClaim:
    document_id: uuid.UUID
    user_id: str
    token: uuid.UUID
    urls: tuple[str, ...]
    content_hash: str | None
    reason: str


class WebpageRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(self, *, user_id: str, knowledge_key: str, name: str | None,
                     urls: list[str], auto_sync: bool = False) -> DocumentRecord:
        if not 1 <= len(urls) <= MAX_URLS:
            raise WebpageFetchError("select between 1 and 100 webpage URLs")
        normalized_urls = list(dict.fromkeys(normalize_url(url) for url in urls))
        async with self._session_factory() as session:
            await lock_active_owner(session, user_id)
            document = Document(
                user_id=uuid.UUID(user_id), knowledge_key=knowledge_key,
                name=name or normalized_urls[0], mime="text/html", status=DocumentStatus.PROCESSING,
            )
            session.add(document)
            try:
                await session.flush()
            except IntegrityError as exc:
                await session.rollback()
                original = exc.orig
                cause = getattr(original, "__cause__", None)
                if (getattr(cause, "constraint_name", None) == "uq_documents_user_knowledge_key"
                        or getattr(getattr(original, "diag", None), "constraint_name", None)
                        == "uq_documents_user_knowledge_key"):
                    raise DuplicateKnowledgeKey from exc
                raise
            page = DocumentWebpage(
                document_id=document.id, urls=normalized_urls, auto_sync=auto_sync,
                sync_status="queued", queue_reason="initial", content="",
            )
            session.add(page)
            await session.flush()
            await session.refresh(document)
            record = _to_record(document, page)
            await session.commit()
            return record

    @staticmethod
    def _select(document_id: uuid.UUID, user_id: str):
        return (sa.select(Document, DocumentWebpage)
                .join(DocumentWebpage, DocumentWebpage.document_id == Document.id)
                .where(Document.id == document_id, Document.user_id == uuid.UUID(user_id)))

    async def get(self, *, document_id: uuid.UUID, user_id: str) -> WebpageDetail | None:
        async with self._session_factory() as session:
            await lock_active_owner(session, user_id)
            row = (await session.execute(self._select(document_id, user_id))).one_or_none()
            if row is None:
                return None
            document, page = row
            return WebpageDetail(document=_to_record(document, page), content=page.content)

    async def enqueue(self, *, document_id: uuid.UUID, user_id: str) -> WebpageDetail | None:
        async with self._session_factory() as session:
            await lock_active_owner(session, user_id)
            row = (await session.execute(self._select(document_id, user_id).with_for_update()))
            row = row.one_or_none()
            if row is None:
                return None
            document, page = row
            if page.sync_status in {"queued", "running"}:
                raise SyncConflict("webpage sync is already queued or running")
            page.sync_status = "queued"
            page.queue_reason = "manual"
            page.sync_error = None
            page.last_sync_changed = None
            page.claim_token = None
            page.claimed_at = None
            if page.content_hash is None:
                document.status = DocumentStatus.PROCESSING
                document.error = None
            document.updated_at = datetime.now(UTC)
            await session.commit()
            return WebpageDetail(document=_to_record(document, page), content=page.content)

    async def set_auto_sync(self, *, document_id: uuid.UUID, user_id: str,
                            auto_sync: bool) -> WebpageDetail | None:
        async with self._session_factory() as session:
            await lock_active_owner(session, user_id)
            row = (await session.execute(self._select(document_id, user_id).with_for_update()))
            row = row.one_or_none()
            if row is None:
                return None
            document, page = row
            now = datetime.now(UTC)
            if page.auto_sync != auto_sync:
                page.auto_sync = auto_sync
                page.next_sync_at = ((page.last_checked_at or now) + SYNC_INTERVAL
                                     if auto_sync else None)
                # Turning off scheduled work must not discard an explicit manual
                # request (or initial registration). Fence only automatic claims.
                if (not auto_sync and page.queue_reason == "automatic"
                        and page.sync_status in {"queued", "running"}):
                    page.sync_status = "idle"
                    page.claim_token = None
                    page.claimed_at = None
                document.updated_at = now
            await session.commit()
            return WebpageDetail(document=_to_record(document, page), content=page.content)

    @staticmethod
    def _due(now: datetime):
        return sa.or_(
            DocumentWebpage.sync_status == "queued",
            sa.and_(DocumentWebpage.sync_status == "running",
                    DocumentWebpage.claimed_at < now - CLAIM_LEASE),
            sa.and_(DocumentWebpage.auto_sync.is_(True),
                    DocumentWebpage.sync_status.in_(["idle", "failed"]),
                    DocumentWebpage.next_sync_at <= now),
        )

    async def claim(self) -> WebpageClaim | None:
        # Discover IDs without row locks, then acquire owner admission before row
        # locks, in the same order as deletion/erasure. Recheck eligibility under
        # lock: multiple workers cannot claim the same row from stale candidates.
        async with self._session_factory() as session:
            candidates = (await session.execute(
                sa.select(Document.id, Document.user_id)
                .join(DocumentWebpage, DocumentWebpage.document_id == Document.id)
                .where(self._due(datetime.now(UTC)))
                .order_by(Document.updated_at, Document.id).limit(100)
            )).all()
        for document_id, owner in candidates:
            try:
                async with self._session_factory() as session:
                    await lock_active_owner(session, str(owner))
                    now = datetime.now(UTC)
                    row = (await session.execute(
                        self._select(document_id, str(owner)).where(self._due(now))
                        .with_for_update(skip_locked=True)
                    )).one_or_none()
                    if row is None:
                        continue
                    document, page = row
                    if page.sync_status in {"idle", "failed"}:
                        page.queue_reason = "automatic"
                    page.sync_status = "running"
                    page.claim_token = uuid.uuid4()
                    page.claimed_at = now
                    page.sync_error = None
                    page.last_sync_changed = None
                    document.updated_at = now
                    claim = WebpageClaim(
                        document_id=document.id, user_id=str(owner), token=page.claim_token,
                        urls=tuple(page.urls), content_hash=page.content_hash, reason=page.queue_reason,
                    )
                    await session.commit()
                    return claim
            except OwnerDataErased:
                continue
        return None

    async def claim_is_current(self, claim: WebpageClaim) -> bool:
        try:
            async with self._session_factory() as session:
                await lock_active_owner(session, claim.user_id)
                return bool(await session.scalar(
                    sa.select(DocumentWebpage.document_id)
                    .join(Document, Document.id == DocumentWebpage.document_id)
                    .where(Document.id == claim.document_id,
                           Document.user_id == uuid.UUID(claim.user_id),
                           DocumentWebpage.sync_status == "running",
                           DocumentWebpage.claim_token == claim.token)
                ))
        except OwnerDataErased:
            return False

    async def finish(self, claim: WebpageClaim, *, content: str, content_hash: str,
                     chunks: list[IngestChunk] | None = None,
                     embeddings: list[list[float]] | None = None) -> bool:
        return await self._complete(claim, content=content, content_hash=content_hash,
                                    chunks=chunks, embeddings=embeddings, error=None)

    async def fail(self, claim: WebpageClaim, error: str) -> bool:
        return await self._complete(claim, content=None, content_hash=None,
                                    chunks=None, embeddings=None, error=error[:1000])

    async def _complete(self, claim: WebpageClaim, *, content: str | None,
                        content_hash: str | None, chunks: list[IngestChunk] | None,
                        embeddings: list[list[float]] | None, error: str | None) -> bool:
        try:
            async with self._session_factory() as session:
                await lock_active_owner(session, claim.user_id)
                row = (await session.execute(
                    self._select(claim.document_id, claim.user_id).with_for_update()
                )).one_or_none()
                if row is None:
                    return False
                document, page = row
                if page.sync_status != "running" or page.claim_token != claim.token:
                    return False
                changed = page.content_hash != content_hash
                if error is None:
                    if not content or not content_hash:
                        raise ValueError("webpage publication requires nonempty content")
                    if changed:
                        if not chunks or embeddings is None or len(chunks) != len(embeddings):
                            raise ValueError("webpage publication requires all embedded chunks")
                        await session.execute(sa.delete(DocumentChunk).where(
                            DocumentChunk.document_id == document.id,
                        ))
                        session.add_all([
                            DocumentChunk(document_id=document.id, seq=chunk.seq, text=chunk.text,
                                          metadata_=chunk.metadata, embedding=embedding)
                            for chunk, embedding in zip(chunks, embeddings, strict=True)
                        ])
                        page.content = content
                        page.content_hash = content_hash
                    document.status = DocumentStatus.READY
                    document.error = None
                    page.sync_status = "idle"
                else:
                    page.sync_status = "failed"
                    if page.content_hash is None:
                        document.status = DocumentStatus.FAILED
                        document.error = error
                now = datetime.now(UTC)
                page.sync_error = error
                page.last_sync_changed = changed if error is None else None
                page.last_checked_at = now
                if error is None:
                    page.last_synced_at = now
                # Read current auto_sync under lock, never the captured claim value.
                page.next_sync_at = now + SYNC_INTERVAL if page.auto_sync else None
                page.claim_token = None
                page.claimed_at = None
                document.updated_at = now
                await session.commit()
                return True
        except OwnerDataErased:
            return False
