from __future__ import annotations
from tests.private_data_fixture import PlainParserInputFixture
from tests.private_data_fixture import private_data_cipher

import asyncio
import base64
import errno
import hashlib
import hmac
import io
import json
import os
import uuid
from pathlib import Path

import httpx
import pytest
from rag.security.private_data import StorageBinding, read_text, read_json
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from starlette.datastructures import UploadFile

from rag.db.models import Document, DocumentChunk, KnowledgeRevision, KnowledgeRevisionChunk
from rag.db.session import create_engine, create_session_factory
from rag.http.account_erasure import AccountErasureService, create_account_erasure_router
from rag.http.documents import SqlAlchemyDocumentRepository, create_documents_router
from rag.http.knowledge_revisions import create_knowledge_revisions_router
from rag.http.search import create_search_router
from rag.ingest.pipeline import IngestPipeline
from rag.ingest.store import SqlAlchemyIngestStore
from rag.ingest.types import IngestChunk, IngestJob
from rag.ingest.uploads import LocalUploadStorage
from rag.ingest.worker import IngestWorker
from rag.knowledge.revisions import KnowledgeRevisionRepository
from rag.main import create_app
from rag.search.repository import SearchRepository
from rag.search.service import SearchService
from rag.security.owner_erasure import OwnerDataErased
from rag.security.owner_erasure import SqlAlchemyOwnerErasure
from rag.security.retrieval_capability import RetrievalCapabilityVerifier
from tests.fakes import StaticFakeEmbedder
from tests.ingest.test_pipeline import SplitChunker, TextParser

KEY = "test-only-internal-server-key-0123456789"
SECRET = "test-only-retrieval-capability-secret-0123456789"


def capability(user_id: str, revision_id: str) -> str:
    payload = (
        base64.urlsafe_b64encode(json.dumps({
        "userId": user_id, "knowledgeRevisionId": revision_id,
        "sessionId": "synthetic-session", "exp": 2000,
    }, separators=(",", ":"),
            ).encode()).decode().rstrip("=")
    )
    signature = (
        base64.urlsafe_b64encode(
        hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")
    )
    return f"{payload}.{signature}"


@pytest.fixture
async def database():
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
                await owners.fence(user_id)
                await owners.erase(user_id)
            async with sessions() as session:
                await session.execute(sa.text(
                    "DELETE FROM public.rag_erased_owners WHERE owner_hash IN (:first, :second)"
                ), {"first": hashlib.sha256(user_ids[0].encode()).hexdigest(),
                    "second": hashlib.sha256(user_ids[1].encode()).hexdigest(),
                    },
                )
                await session.commit()
        finally:
            await engine.dispose()


async def seed(sessions, user_id: str, *, name: str, text: str):
    documents = SqlAlchemyDocumentRepository(sessions, cipher=private_data_cipher)
    document = await documents.create_processing_document(
        user_id=user_id, knowledge_key="synthetic_notes", name=name, mime="text/plain"
    )
    await SqlAlchemyIngestStore(sessions, cipher=private_data_cipher).replace_chunks_and_mark_ready(
        document.id, [IngestChunk(seq=0, text=text, metadata={"private": text})],
        [[1.0] + [0.0] * 1535],
    )
    revision = await KnowledgeRevisionRepository(sessions, cipher=private_data_cipher).create(
        user_id=user_id, document_ids=[document.id]
    )
    return document, revision


@pytest.mark.asyncio
async def test_authenticated_repeat_purge_removes_current_immutable_and_staging_only_for_owner(
    database, tmp_path: Path,
) -> None:
    sessions, owners, (owner, other) = database
    target, target_revision = await seed(sessions, owner, name="private.txt", text="private answer")
    preserved, other_revision = await seed(sessions, other, name="other.txt", text="other answer")
    storage = LocalUploadStorage(
        staging_root=tmp_path / "staging", legacy_root=tmp_path / "legacy", owner_access=owners,
        cipher=private_data_cipher,
    )
    embedder = StaticFakeEmbedder()
    pipeline = IngestPipeline(
        parser=TextParser(), chunker=SplitChunker(), embedder=embedder,
        store=SqlAlchemyIngestStore(sessions, cipher=private_data_cipher), owner_access=owners,
        storage=storage,
    )
    worker = IngestWorker(pipeline, owner_access=owners, storage=storage)
    target_path = await storage.save(UploadFile(io.BytesIO(b"private raw"), filename="a.txt"), user_id=owner)
    other_path = await storage.save(UploadFile(io.BytesIO(b"other raw"), filename="b.txt"), user_id=other)
    await worker.enqueue(IngestJob(document_id=target.id, path=target_path, user_id=owner))
    await worker.enqueue(IngestJob(document_id=preserved.id, path=other_path, user_id=other))
    app = create_app(internal_server_key=KEY, metrics_enabled=False)
    app.include_router(create_account_erasure_router(service=AccountErasureService(
        repository=owners, worker=worker, storage=storage,
    )))
    app.include_router(create_documents_router(
        repository=SqlAlchemyDocumentRepository(sessions, cipher=private_data_cipher), worker=worker, storage=storage,
        reindexer=pipeline, owner_access=owners,
    ))
    verifier = RetrievalCapabilityVerifier(SECRET, now=lambda: 1000)
    app.include_router(create_knowledge_revisions_router(
        repository=KnowledgeRevisionRepository(sessions, cipher=private_data_cipher), capability_verifier=verifier,
        owner_access=owners,
    ))
    app.include_router(create_search_router(
        service=SearchService(embedder=embedder, repository=SearchRepository(sessions, cipher=private_data_cipher)),
        capability_verifier=verifier, owner_access=owners,
    ))
    headers = {"x-internal-server": KEY}
    signed_headers = {**headers, "Authorization": f"Bearer {capability(owner, str(target_revision.id))}",
    }
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://rag.test") as client:
            url = f"/users/{owner}/data"
            for rejected in ({}, {"x-internal-server": "wrong-internal-server-key-0123456789"}):
                response = await client.delete(url, headers=rejected)
                assert response.status_code == 401
                assert target_path.read_bytes().startswith(b"port-openbao-upload-v1\n")
            before = await client.post(
                f"/knowledge-revisions/{target_revision.id}/search", json={"query": "private"},
                headers=signed_headers,
            )
            assert [hit["text"] for hit in before.json()["data"]["results"]] == ["private answer"]
            for _ in range(2):
                response = await client.delete(url, headers=headers)
                assert response.status_code == 200
                assert response.json()["data"] == {"userId": owner, "erased": True}
            assert not target_path.exists()
            assert other_path.read_bytes().startswith(b"port-openbao-upload-v1\n")
            rejected_search = await client.post(
                f"/knowledge-revisions/{target_revision.id}/search", json={"query": "private"},
                headers=signed_headers,
            )
            assert rejected_search.status_code == 410
            replay = await client.post("/knowledge-revisions", headers=signed_headers, json={
                "userId": owner, "revisionId": str(target_revision.id), "documentIds": [],
            },
            )
            assert replay.status_code == 410
            late_upload = await client.post("/documents", headers=headers, data={"userId": owner},
                files={"file": ("late.txt", b"must not survive", "text/plain")},
            )
            assert late_upload.status_code == 410
            other_search = await client.post(
                f"/knowledge-revisions/{other_revision.id}/search", json={"query": "other"},
                headers={**headers, "Authorization": f"Bearer {capability(other, str(other_revision.id))}",
                },
            )
            assert [hit["text"] for hit in other_search.json()["data"]["results"]] == ["other answer"]
        async with sessions() as session:
            assert await session.get(Document, target.id) is None
            assert await session.get(KnowledgeRevision, target_revision.id) is None
            assert (
                await session.scalar(sa.select(sa.func.count()).select_from(DocumentChunk).where(
                DocumentChunk.document_id == target.id)) == 0
            )
            assert (
                await session.scalar(sa.select(sa.func.count()).select_from(KnowledgeRevisionChunk).where(
                KnowledgeRevisionChunk.revision_id == target_revision.id)) == 0
            )
            other_chunk = await session.scalar(sa.select(DocumentChunk).where(
                DocumentChunk.document_id == preserved.id))
            assert (
                await read_text(
                    private_data_cipher,
                    other_chunk.text,
                    StorageBinding(other, str(preserved.id), "chunk:0:text"),
                )
                == "other answer"
            )
            assert await read_json(
                private_data_cipher,
                other_chunk.metadata_,
                StorageBinding(other, str(preserved.id), "chunk:0:metadata"),
            ) == {"private": "other answer"}
            assert list(other_chunk.embedding) == [1.0] + [0.0] * 1535
    finally:
        storage.close()


@pytest.mark.asyncio
async def test_fence_rejects_direct_late_roots_and_keeps_other_history_immutable(database) -> None:
    sessions, owners, (owner, other) = database
    target, revision = await seed(sessions, owner, name="target.txt", text="target")
    _, preserved_revision = await seed(sessions, other, name="other.txt", text="other")
    await owners.fence(owner)
    await owners.erase(owner)
    for entity in (
        Document(user_id=uuid.UUID(owner), knowledge_key="late", name="late", mime="text/plain"),
        KnowledgeRevision(user_id=uuid.UUID(owner)),
    ):
        async with sessions() as session:
            session.add(entity)
            with pytest.raises(DBAPIError, match="rag owner data erased"):
                await session.commit()
    async with sessions() as session:
        with pytest.raises(DBAPIError, match="immutable"):
            await session.execute(sa.delete(KnowledgeRevisionChunk).where(
                KnowledgeRevisionChunk.revision_id == preserved_revision.id))
    async with sessions() as session:
        assert await session.get(Document, target.id) is None
        assert await session.get(KnowledgeRevision, revision.id) is None
        assert await session.get(KnowledgeRevision, preserved_revision.id) is not None


@pytest.mark.asyncio
async def test_inflight_embedding_cannot_repopulate_erased_document(database, tmp_path: Path) -> None:
    sessions, owners, (owner, other) = database
    target, _ = await seed(sessions, owner, name="target.txt", text="old private data")
    preserved, _ = await seed(sessions, other, name="other.txt", text="other data")
    started, release = asyncio.Event(), asyncio.Event()

    class DelayedEmbedder(StaticFakeEmbedder):
        async def embed_texts(self, texts):
            started.set()
            await release.wait()
            return await super().embed_texts(texts)

    path = tmp_path / "inflight.txt"
    path.write_text("late private data")
    pipeline = IngestPipeline(parser=TextParser(), chunker=SplitChunker(), embedder=DelayedEmbedder(),
        store=SqlAlchemyIngestStore(sessions, cipher=private_data_cipher), owner_access=owners,
        storage=PlainParserInputFixture(),
    )
    ingestion = asyncio.create_task(pipeline.ingest(IngestJob(
        document_id=target.id, path=path, user_id=owner,
    )))
    try:
        await started.wait()
        await owners.fence(owner)
        await owners.erase(owner)
    finally:
        release.set()
        await ingestion
    async with sessions() as session:
        assert await session.get(Document, target.id) is None
        assert (
            await session.scalar(sa.select(sa.func.count()).select_from(DocumentChunk).where(
            DocumentChunk.document_id == target.id)) == 0
        )
        other_chunk = await session.scalar(sa.select(DocumentChunk).where(
            DocumentChunk.document_id == preserved.id))
        assert (
            await read_text(
                private_data_cipher,
                other_chunk.text,
                StorageBinding(other, str(preserved.id), "chunk:0:text"),
            )
            == "other data"
        )
    assert not path.exists()


@pytest.mark.asyncio
async def test_current_search_reindex_and_revision_snapshot_preserve_owner_boundaries(database,
) -> None:
    sessions, _, (owner, other) = database
    target, revision = await seed(sessions, owner, name="target.txt", text="owner answer")
    preserved, _ = await seed(sessions, other, name="other.txt", text="other answer")
    repository = SearchRepository(sessions, cipher=private_data_cipher)
    store = SqlAlchemyIngestStore(sessions, cipher=private_data_cipher)
    embedding = [1.0] + [0.0] * 1535
    assert [hit.text for hit in await repository.search(
        user_id=owner, embedding=embedding, top_k=5,
    )] == ["owner answer"]
    assert (
        await repository.search_revision(
        user_id=other, knowledge_revision_id=str(revision.id), embedding=embedding, top_k=5,
    ) == []
    )
    assert await store.get_chunks_for_reindex(target.id, other) is None
    assert not await store.replace_embeddings_and_mark_ready(target.id, other, [embedding])
    replacement = [0.0, 1.0] + [0.0] * 1534
    assert await store.replace_embeddings_and_mark_ready(target.id, owner, [replacement])
    async with sessions() as session:
        current = await session.scalar(sa.select(DocumentChunk).where(
            DocumentChunk.document_id == target.id))
        snapshot = await session.scalar(sa.select(KnowledgeRevisionChunk).where(
            KnowledgeRevisionChunk.revision_id == revision.id))
        assert (
            await read_text(
                private_data_cipher,
                current.text,
                StorageBinding(owner, str(target.id), "chunk:0:text"),
            )
            == "owner answer"
        )
        assert await read_json(
            private_data_cipher,
            current.metadata_,
            StorageBinding(owner, str(target.id), "chunk:0:metadata"),
        ) == {"private": "owner answer"}
        assert list(current.embedding) == replacement
        assert (
            await read_text(
                private_data_cipher,
                snapshot.document_name,
                StorageBinding(owner, str(revision.id), f"document:{target.id}:name"),
            )
            == "target.txt"
        )
        assert (
            await read_text(
                private_data_cipher,
                snapshot.text,
                StorageBinding(owner, str(revision.id), f"document:{target.id}:chunk:0:text"),
            )
            == "owner answer"
        )
        assert await read_json(
            private_data_cipher,
            snapshot.metadata_,
            StorageBinding(owner, str(revision.id), f"document:{target.id}:chunk:0:metadata"),
        ) == {"private": "owner answer"}
        assert list(snapshot.embedding) == embedding
        other_chunk = await session.scalar(sa.select(DocumentChunk).where(
            DocumentChunk.document_id == preserved.id))
        assert list(other_chunk.embedding) == embedding
    # Ordinary document deletion must still retain immutable published data;
    # only the account-erasure routine may remove it.
    assert await SqlAlchemyDocumentRepository(sessions, cipher=private_data_cipher).delete_document(
        document_id=target.id, user_id=owner,
    )
    assert [hit.text for hit in await repository.search_revision(
        user_id=owner, knowledge_revision_id=str(revision.id), embedding=embedding, top_k=5,
    )] == ["owner answer"]


@pytest.mark.asyncio
async def test_legacy_cleanup_failure_keeps_sql_retry_handles_and_blocks_new_work(
    database, tmp_path: Path,
) -> None:
    sessions, owners, (owner, _) = database
    target, revision = await seed(sessions, owner, name="target.txt", text="private answer")
    legacy = tmp_path / "legacy" / "rag-uploads-crashed"
    legacy.mkdir(parents=True)
    original = legacy / "unknown-owner.pdf"
    original.write_bytes(b"unclassified original")
    storage = LocalUploadStorage(
        staging_root=tmp_path / "staging", legacy_root=legacy.parent, owner_access=owners,
        cipher=private_data_cipher,
    )
    pipeline = IngestPipeline(
        parser=TextParser(), chunker=SplitChunker(), embedder=StaticFakeEmbedder(),
        store=SqlAlchemyIngestStore(sessions, cipher=private_data_cipher), owner_access=owners,
        storage=storage,
    )
    worker = IngestWorker(pipeline, owner_access=owners, storage=storage)
    app = create_app(internal_server_key=KEY, metrics_enabled=False)
    app.include_router(create_account_erasure_router(service=AccountErasureService(
        repository=owners, worker=worker, storage=storage,
    )))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://rag.test",
        ) as client:
            response = await client.delete(f"/users/{owner}/data", headers={"x-internal-server": KEY})
            assert response.status_code == 409
            assert response.json() == {
                "statusCode": 409, "message": "RAG_LEGACY_UPLOAD_CLEANUP_REQUIRED",
                "error": "Conflict", "data": None,
            }
        assert original.read_bytes() == b"unclassified original"
        async with sessions() as session:
            assert await session.get(Document, target.id) is not None
            assert await session.get(KnowledgeRevision, revision.id) is not None
        with pytest.raises(OwnerDataErased):
            await owners.assert_active(owner)
    finally:
        storage.close()
    cutover = LocalUploadStorage(
        staging_root=tmp_path / "staging", legacy_root=legacy.parent, owner_access=owners,
        clean_legacy_uploads_on_start=True,
        cipher=private_data_cipher,
    )
    try:
        retry_worker = IngestWorker(pipeline, owner_access=owners, storage=cutover)
        await AccountErasureService(repository=owners, worker=retry_worker, storage=cutover).erase(owner)
        assert not original.exists()
        async with sessions() as session:
            assert await session.get(Document, target.id) is None
            assert await session.get(KnowledgeRevision, revision.id) is None
    finally:
        cutover.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("boundary", "error_number"), [
    ("upload", errno.EIO),
    ("owner", errno.EIO),
    ("owners", errno.EIO),
    ("owners", errno.EINVAL),
],
)
async def test_directory_sync_failure_retains_sql_retry_handles_until_durable_retry(
    database, tmp_path: Path, monkeypatch, boundary: str, error_number: int,
) -> None:
    sessions, owners, (owner, other) = database
    target, revision = await seed(sessions, owner, name="target.txt", text="private answer")
    preserved, other_revision = await seed(sessions, other, name="other.txt", text="other answer")
    storage = LocalUploadStorage(
        staging_root=tmp_path / "staging", legacy_root=tmp_path / "legacy", owner_access=owners,
        cipher=private_data_cipher,
    )
    target_path = await storage.save(
        UploadFile(io.BytesIO(b"private original"), filename="private.txt"), user_id=owner,
    )
    other_path = await storage.save(
        UploadFile(io.BytesIO(b"other original"), filename="other.txt"), user_id=other,
    )
    directory = {
        "upload": target_path.parent,
        "owner": target_path.parent.parent,
        "owners": target_path.parent.parent.parent,
    }[boundary]
    directory_stat = directory.stat()
    original_fsync = os.fsync

    def fail_directory_sync(descriptor: int) -> None:
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) == (
            directory_stat.st_dev, directory_stat.st_ino,
        ):
            raise OSError(error_number, "synthetic directory durability failure")
        original_fsync(descriptor)

    pipeline = IngestPipeline(
        parser=TextParser(), chunker=SplitChunker(), embedder=StaticFakeEmbedder(),
        store=SqlAlchemyIngestStore(sessions, cipher=private_data_cipher), owner_access=owners,
        storage=storage,
    )
    worker = IngestWorker(pipeline, owner_access=owners, storage=storage)
    app = create_app(internal_server_key=KEY, metrics_enabled=False)
    app.include_router(create_account_erasure_router(service=AccountErasureService(
        repository=owners, worker=worker, storage=storage,
    )))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://rag.test",
        ) as client:
            with monkeypatch.context() as fault:
                fault.setattr(os, "fsync", fail_directory_sync)
                for _ in range(2):
                    response = await client.delete(
                        f"/users/{owner}/data", headers={"x-internal-server": KEY},
                    )
                    assert response.status_code == 500
                    async with sessions() as session:
                        assert await session.get(Document, target.id) is not None
                        assert await session.get(KnowledgeRevision, revision.id) is not None
                    assert other_path.read_bytes().startswith(b"port-openbao-upload-v1\n")
                with pytest.raises(OwnerDataErased):
                    await owners.assert_active(owner)
            response = await client.delete(
                f"/users/{owner}/data", headers={"x-internal-server": KEY},
            )
            assert response.status_code == 200
            assert response.json()["data"] == {"userId": owner, "erased": True}
        assert not target_path.exists()
        assert other_path.read_bytes().startswith(b"port-openbao-upload-v1\n")
        async with sessions() as session:
            assert await session.get(Document, target.id) is None
            assert await session.get(KnowledgeRevision, revision.id) is None
            assert await session.get(Document, preserved.id) is not None
            assert await session.get(KnowledgeRevision, other_revision.id) is not None
    finally:
        storage.close()
