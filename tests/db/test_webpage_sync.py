from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from rag.db.models import Document, DocumentChunk, DocumentWebpage, KnowledgeRevisionWebpage
from rag.db.session import create_engine, create_session_factory
from rag.http.account_erasure import AccountErasureService
from rag.http.documents import SqlAlchemyDocumentRepository
from rag.knowledge.revisions import KnowledgeRevisionRepository
from rag.search.repository import SearchRepository
from rag.security.owner_erasure import OwnerDataErased, SqlAlchemyOwnerErasure
from rag.webpages.fetch import WebpageFetchError, WebpagePage
from rag.webpages.repository import SyncConflict, WebpageRepository
from rag.webpages.worker import WebpageWorker


@pytest.fixture
async def webpage_database():
    url = os.getenv("TEST_RAG_DATABASE_URL")
    if not url:
        pytest.skip("requires a disposable migrated pgvector TEST_RAG_DATABASE_URL")
    engine = create_engine(url)
    sessions = create_session_factory(engine)
    owners = SqlAlchemyOwnerErasure(sessions)
    user_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    try:
        yield sessions, owners, user_ids
    finally:
        try:
            for user_id in user_ids:
                await owners.erase(user_id)
            async with sessions() as session:
                await session.execute(sa.text(
                    "DELETE FROM public.rag_erased_owners WHERE owner_hash IN (:first, :second)"
                ), dict(zip(("first", "second"), [hashlib.sha256(u.encode()).hexdigest() for u in user_ids])))
                await session.commit()
        finally:
            await engine.dispose()


class PageSource:
    text = "Original useful webpage content"
    error = None

    async def fetch(self, url):
        if self.error:
            raise self.error
        return WebpagePage(url=url, text=self.text, links=[])


class Embedder:
    calls = 0

    async def embed_texts(self, texts):
        self.calls += 1
        return [[1.0] + [0.0] * 1535 for _ in texts]


async def register(repo, owner, key="product", auto_sync=True):
    return await repo.create(
        user_id=owner, knowledge_key=key, name="Product", urls=["https://example.com/"],
        auto_sync=auto_sync,
    )


async def chunks(sessions, document_id):
    async with sessions() as session:
        return (await session.scalars(sa.select(DocumentChunk).where(
            DocumentChunk.document_id == document_id,
        ).order_by(DocumentChunk.seq))).all()


async def test_changed_unchanged_and_failed_sync_preserve_last_ready_content(webpage_database):
    sessions, _, (owner, _) = webpage_database
    repo, source, embedder = WebpageRepository(sessions), PageSource(), Embedder()
    worker = WebpageWorker(repository=repo, fetcher=source, embedder=embedder)
    document = await register(repo, owner)
    assert document.status == "processing"
    assert document.webpage.sync_status == "queued"
    assert await worker.run_once()
    first = await repo.get(document_id=document.id, user_id=owner)
    before = await chunks(sessions, document.id)
    assert first.content == source.text
    assert first.document.webpage.last_sync_changed is True
    assert first.document.webpage.next_sync_at - first.document.webpage.last_checked_at == timedelta(hours=24)

    await repo.enqueue(document_id=document.id, user_id=owner)
    assert await worker.run_once()
    unchanged = await repo.get(document_id=document.id, user_id=owner)
    assert unchanged.document.webpage.last_sync_changed is False
    assert [c.id for c in await chunks(sessions, document.id)] == [c.id for c in before]
    assert embedder.calls == 1

    source.text = "New updated useful webpage content"
    await repo.enqueue(document_id=document.id, user_id=owner)
    assert await worker.run_once()
    changed = await repo.get(document_id=document.id, user_id=owner)
    after = await chunks(sessions, document.id)
    assert changed.content == source.text
    assert changed.document.webpage.last_sync_changed is True
    assert [c.id for c in after] != [c.id for c in before]

    source.error = WebpageFetchError("source unavailable")
    await repo.enqueue(document_id=document.id, user_id=owner)
    assert await worker.run_once()
    failed = await repo.get(document_id=document.id, user_id=owner)
    assert failed.document.status == "ready"
    assert failed.document.webpage.sync_status == "failed"
    assert failed.document.webpage.last_sync_changed is None
    assert failed.content == changed.content
    assert failed.document.webpage.last_synced_at == changed.document.webpage.last_synced_at
    assert [c.id for c in await chunks(sessions, document.id)] == [c.id for c in after]


async def test_initial_failure_and_explicit_off_never_schedule(webpage_database):
    sessions, _, (owner, other) = webpage_database
    repo, source = WebpageRepository(sessions), PageSource()
    source.error = WebpageFetchError("source unavailable")
    doc = await register(repo, owner, auto_sync=False)
    worker = WebpageWorker(repository=repo, fetcher=source, embedder=Embedder())
    assert await worker.run_once()
    detail = await repo.get(document_id=doc.id, user_id=owner)
    assert detail.document.status == "failed"
    assert detail.content == ""
    assert detail.document.webpage.next_sync_at is None
    assert await repo.get(document_id=doc.id, user_id=other) is None
    assert await repo.enqueue(document_id=doc.id, user_id=other) is None
    assert await repo.set_auto_sync(document_id=doc.id, user_id=other, auto_sync=True) is None
    assert not await worker.run_once()


async def test_concurrent_claim_stale_recovery_off_and_delete_fence(webpage_database):
    sessions, _, (owner, _) = webpage_database
    repo, source = WebpageRepository(sessions), PageSource()
    worker = WebpageWorker(repository=repo, fetcher=source, embedder=Embedder())
    doc = await register(repo, owner)
    with pytest.raises(SyncConflict):
        await repo.enqueue(document_id=doc.id, user_id=owner)
    claims = await asyncio.gather(repo.claim(), repo.claim())
    active = [claim for claim in claims if claim is not None]
    assert len(active) == 1
    old_claim = active[0]
    async with sessions() as session:
        await session.execute(sa.update(DocumentWebpage).where(DocumentWebpage.document_id == doc.id)
                              .values(claimed_at=datetime.now(UTC) - timedelta(hours=1)))
        await session.commit()
    recovered = await repo.claim()
    assert recovered.token != old_claim.token
    assert not await repo.finish(old_claim, content="stale", content_hash="stale", chunks=[], embeddings=[])
    await worker.process(recovered)

    async with sessions() as session:
        await session.execute(sa.update(DocumentWebpage).where(DocumentWebpage.document_id == doc.id)
                              .values(next_sync_at=datetime.now(UTC) - timedelta(seconds=1)))
        await session.commit()
    scheduled = await asyncio.gather(repo.claim(), repo.claim())
    assert len([claim for claim in scheduled if claim is not None]) == 1
    automatic = next(claim for claim in scheduled if claim is not None)
    assert automatic.reason == "automatic"
    await repo.set_auto_sync(document_id=doc.id, user_id=owner, auto_sync=False)
    assert not await repo.finish(automatic, content="off stale", content_hash="stale", chunks=[], embeddings=[])
    assert not await worker.run_once()
    await repo.enqueue(document_id=doc.id, user_id=owner)
    deleting = await repo.claim()
    await SqlAlchemyDocumentRepository(sessions).delete_document(document_id=doc.id, user_id=owner)
    assert not await repo.finish(deleting, content="deleted stale", content_hash="stale", chunks=[], embeddings=[])
    assert await chunks(sessions, doc.id) == []


async def test_revision_resolves_only_selected_live_webpages_and_frozen_files(webpage_database):
    sessions, owners, (owner, other) = webpage_database
    repo, source, embedder = WebpageRepository(sessions), PageSource(), Embedder()
    worker = WebpageWorker(repository=repo, fetcher=source, embedder=embedder)
    selected = await register(repo, owner, "selected")
    await worker.run_once()
    await register(repo, owner, "not_selected")
    await worker.run_once()
    await register(repo, other, "other_tenant")
    await worker.run_once()
    file_id = uuid.uuid4()
    async with sessions() as session:
        session.add(Document(id=file_id, user_id=uuid.UUID(owner), knowledge_key="frozen_file",
                             name="File", mime="text/plain", status="ready"))
        await session.flush()
        session.add(DocumentChunk(document_id=file_id, seq=0, text="Frozen file text",
                                  metadata_={}, embedding=[1.0] + [0.0] * 1535))
        await session.commit()
    revisions = KnowledgeRevisionRepository(sessions)
    revision = await revisions.create(user_id=owner, document_ids=[selected.id, file_id])
    assert revision.live_webpage_ids == (selected.id,)
    async with sessions() as session:
        refs = (await session.scalars(sa.select(KnowledgeRevisionWebpage).where(
            KnowledgeRevisionWebpage.revision_id == revision.id,
        ))).all()
        assert [ref.document_id for ref in refs] == [selected.id]
        await session.execute(sa.update(DocumentChunk).where(DocumentChunk.document_id == file_id)
                              .values(text="File changed after snapshot"))
        await session.commit()
    source.text = "Fresh selected live webpage text"
    await repo.enqueue(document_id=selected.id, user_id=owner)
    await worker.run_once()
    search = SearchRepository(sessions)
    arguments = dict(user_id=owner, knowledge_revision_id=str(revision.id),
                     embedding=[1.0] + [0.0] * 1535, top_k=50)
    hits = await search.search_revision(**arguments)
    assert {hit.document_id for hit in hits} == {str(selected.id), str(file_id)}
    assert {hit.text for hit in hits} == {source.text, "Frozen file text"}
    assert await search.search_revision(**{**arguments, "user_id": other}) == []
    await SqlAlchemyDocumentRepository(sessions).delete_document(document_id=selected.id, user_id=owner)
    hits = await search.search_revision(**arguments)
    assert [hit.text for hit in hits] == ["Frozen file text"]
    async with sessions() as session:
        with pytest.raises(DBAPIError, match="immutable"):
            await session.execute(sa.delete(KnowledgeRevisionWebpage).where(
                KnowledgeRevisionWebpage.revision_id == revision.id,
            ))
            await session.commit()
    await owners.erase(owner)
    with pytest.raises(OwnerDataErased):
        await search.search_revision(**arguments)


async def test_owner_erasure_fences_inflight_sync_and_removes_state(webpage_database):
    sessions, owners, (owner, _) = webpage_database
    repo = WebpageRepository(sessions)
    doc = await register(repo, owner)
    claim = await repo.claim()
    await owners.erase(owner)
    assert not await repo.finish(claim, content="late", content_hash="late", chunks=[], embeddings=[])
    async with sessions() as session:
        assert await session.get(DocumentWebpage, doc.id) is None
    with pytest.raises(OwnerDataErased):
        await register(repo, owner)


async def test_disabling_auto_sync_does_not_discard_manual_completion(webpage_database):
    sessions, _, (owner, _) = webpage_database
    repo, source = WebpageRepository(sessions), PageSource()
    worker = WebpageWorker(repository=repo, fetcher=source, embedder=Embedder())
    doc = await register(repo, owner)
    await worker.run_once()
    await repo.enqueue(document_id=doc.id, user_id=owner)
    manual = await repo.claim()
    assert manual.reason == "manual"
    await repo.set_auto_sync(document_id=doc.id, user_id=owner, auto_sync=False)
    source.text = "A manually requested fresh update"
    await worker.process(manual)
    detail = await repo.get(document_id=doc.id, user_id=owner)
    assert detail.content == source.text
    assert detail.document.webpage.last_sync_changed is True
    assert detail.document.webpage.next_sync_at is None


async def test_erasure_service_drains_webpage_fetch_before_acknowledging(webpage_database):
    sessions, owners, (owner, _) = webpage_database
    repo = WebpageRepository(sessions)
    doc = await register(repo, owner)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked_fetch(url):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    fetcher, embedder = AsyncMock(), AsyncMock()
    fetcher.fetch.side_effect = blocked_fetch
    worker = WebpageWorker(repository=repo, fetcher=fetcher, embedder=embedder)
    task = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(started.wait(), timeout=2)
    storage = Mock()
    storage.erase_owner = AsyncMock()
    service = AccountErasureService(repository=owners, worker=AsyncMock(), storage=storage,
                                   webpage_worker=worker)
    await service.erase(owner)
    assert cancelled.is_set()
    assert await task
    embedder.embed_texts.assert_not_awaited()
    async with sessions() as session:
        assert await session.get(Document, doc.id) is None
        assert await session.get(DocumentWebpage, doc.id) is None


async def test_embedding_failure_cannot_replace_previously_ready_content(webpage_database):
    sessions, _, (owner, _) = webpage_database
    repo, source = WebpageRepository(sessions), PageSource()
    doc = await register(repo, owner)
    await WebpageWorker(repository=repo, fetcher=source, embedder=Embedder()).run_once()
    before = await chunks(sessions, doc.id)
    source.text = "Changed page that cannot currently be embedded"
    embedder = AsyncMock()
    embedder.embed_texts.side_effect = RuntimeError("synthetic provider failure")
    await repo.enqueue(document_id=doc.id, user_id=owner)
    await WebpageWorker(repository=repo, fetcher=source, embedder=embedder).run_once()
    detail = await repo.get(document_id=doc.id, user_id=owner)
    assert detail.document.status == "ready"
    assert detail.content == PageSource.text
    assert detail.document.webpage.sync_status == "failed"
    assert detail.document.webpage.sync_error == "webpage synchronization failed"
    assert [chunk.id for chunk in await chunks(sessions, doc.id)] == [chunk.id for chunk in before]
