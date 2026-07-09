from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from reg.db.models import DocumentStatus
from reg.http.documents import DocumentRecord
from reg.http.documents import create_documents_router


NOW = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)


class FakeDocumentRepository:
    def __init__(self) -> None:
        self.documents: dict[uuid.UUID, DocumentRecord] = {}
        self.created: list[tuple[str, str, str]] = []
        self.fail_create = False

    async def create_processing_document(
        self,
        *,
        user_id: str,
        name: str,
        mime: str,
    ) -> DocumentRecord:
        if self.fail_create:
            raise RuntimeError("create failed")
        self.created.append((user_id, name, mime))
        document = DocumentRecord(
            id=uuid.uuid4(),
            user_id=user_id,
            name=name,
            mime=mime,
            status=DocumentStatus.PROCESSING.value,
            error=None,
            created_at=NOW,
            updated_at=NOW,
        )
        self.documents[document.id] = document
        return document

    async def list_documents(self, *, user_id: str) -> list[DocumentRecord]:
        return [document for document in self.documents.values() if document.user_id == user_id]

    async def get_document(
        self,
        *,
        document_id: uuid.UUID,
        user_id: str,
    ) -> DocumentRecord | None:
        document = self.documents.get(document_id)
        if document is None or document.user_id != user_id:
            return None
        return document

    async def delete_document(self, *, document_id: uuid.UUID, user_id: str) -> bool:
        document = self.documents.get(document_id)
        if document is None or document.user_id != user_id:
            return False
        del self.documents[document_id]
        return True


class FakeWorker:
    def __init__(self) -> None:
        self.jobs: list[object] = []
        self.fail_enqueue = False

    async def enqueue(self, job: object) -> None:
        if self.fail_enqueue:
            raise RuntimeError("enqueue failed")
        self.jobs.append(job)


class FakeUploadStorage:
    def __init__(self, base_path: Path) -> None:
        self.base_path = base_path
        self.saved_paths: list[Path] = []

    async def save(self, upload) -> Path:
        path = self.base_path / f"{uuid.uuid4()}-{upload.filename}"
        path.write_bytes(await upload.read())
        self.saved_paths.append(path)
        return path


def build_client(
    repository: FakeDocumentRepository,
    worker: FakeWorker,
    storage: FakeUploadStorage,
) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_documents_router(repository=repository, worker=worker, storage=storage)
    )
    return TestClient(app)


def test_post_requires_user_id(tmp_path: Path) -> None:
    client = build_client(FakeDocumentRepository(), FakeWorker(), FakeUploadStorage(tmp_path))

    response = client.post(
        "/documents",
        files={"file": ("notes.md", b"hello", "text/markdown")},
    )

    assert response.status_code == 422


def test_post_returns_processing_document_and_enqueues_ingest_job(tmp_path: Path) -> None:
    repository = FakeDocumentRepository()
    worker = FakeWorker()
    storage = FakeUploadStorage(tmp_path)
    client = build_client(repository, worker, storage)

    response = client.post(
        "/documents",
        data={"userId": "user-a"},
        files={"file": ("notes.md", b"alpha", "text/markdown")},
    )

    assert response.status_code == 201
    body = response.json()
    document_id = uuid.UUID(body["id"])
    assert body == {
        "id": str(document_id),
        "userId": "user-a",
        "name": "notes.md",
        "mime": "text/markdown",
        "status": "processing",
        "error": None,
        "createdAt": "2026-07-09T12:00:00Z",
        "updatedAt": "2026-07-09T12:00:00Z",
    }
    assert repository.created == [("user-a", "notes.md", "text/markdown")]
    assert len(worker.jobs) == 1
    assert worker.jobs[0].document_id == document_id
    assert worker.jobs[0].path == storage.saved_paths[0]
    assert storage.saved_paths[0].read_bytes() == b"alpha"


def test_list_documents_is_scoped_by_user_id(tmp_path: Path) -> None:
    repository = FakeDocumentRepository()
    worker = FakeWorker()
    storage = FakeUploadStorage(tmp_path)
    client = build_client(repository, worker, storage)

    user_a = client.post(
        "/documents",
        data={"userId": "user-a"},
        files={"file": ("a.md", b"a", "text/markdown")},
    ).json()
    client.post(
        "/documents",
        data={"userId": "user-b"},
        files={"file": ("b.md", b"b", "text/markdown")},
    )

    response = client.get("/documents", params={"userId": "user-a"})

    assert response.status_code == 200
    assert response.json() == [user_a]


def test_get_document_is_scoped_by_user_id(tmp_path: Path) -> None:
    repository = FakeDocumentRepository()
    worker = FakeWorker()
    storage = FakeUploadStorage(tmp_path)
    client = build_client(repository, worker, storage)
    document = client.post(
        "/documents",
        data={"userId": "user-a"},
        files={"file": ("notes.md", b"a", "text/markdown")},
    ).json()

    assert client.get(f"/documents/{document['id']}", params={"userId": "user-b"}).status_code == 404

    response = client.get(f"/documents/{document['id']}", params={"userId": "user-a"})

    assert response.status_code == 200
    assert response.json() == document


def test_delete_document_is_scoped_by_user_id(tmp_path: Path) -> None:
    repository = FakeDocumentRepository()
    worker = FakeWorker()
    storage = FakeUploadStorage(tmp_path)
    client = build_client(repository, worker, storage)
    document = client.post(
        "/documents",
        data={"userId": "user-a"},
        files={"file": ("notes.md", b"a", "text/markdown")},
    ).json()

    assert (
        client.delete(f"/documents/{document['id']}", params={"userId": "user-b"}).status_code
        == 404
    )
    assert uuid.UUID(document["id"]) in repository.documents

    response = client.delete(f"/documents/{document['id']}", params={"userId": "user-a"})

    assert response.status_code == 204
    assert uuid.UUID(document["id"]) not in repository.documents


def test_read_endpoints_require_user_id(tmp_path: Path) -> None:
    client = build_client(FakeDocumentRepository(), FakeWorker(), FakeUploadStorage(tmp_path))
    document_id = uuid.uuid4()

    assert client.get("/documents").status_code == 422
    assert client.get(f"/documents/{document_id}").status_code == 422
    assert client.delete(f"/documents/{document_id}").status_code == 422


def test_post_deletes_saved_file_when_document_create_fails(tmp_path: Path) -> None:
    repository = FakeDocumentRepository()
    repository.fail_create = True
    storage = FakeUploadStorage(tmp_path)
    client = build_client(repository, FakeWorker(), storage)

    with pytest.raises(RuntimeError, match="create failed"):
        client.post(
            "/documents",
            data={"userId": "user-a"},
            files={"file": ("notes.md", b"alpha", "text/markdown")},
        )

    assert storage.saved_paths
    assert not storage.saved_paths[0].exists()


def test_post_deletes_saved_file_when_worker_enqueue_fails(tmp_path: Path) -> None:
    repository = FakeDocumentRepository()
    worker = FakeWorker()
    worker.fail_enqueue = True
    storage = FakeUploadStorage(tmp_path)
    client = build_client(repository, worker, storage)

    with pytest.raises(RuntimeError, match="enqueue failed"):
        client.post(
            "/documents",
            data={"userId": "user-a"},
            files={"file": ("notes.md", b"alpha", "text/markdown")},
        )

    assert storage.saved_paths
    assert not storage.saved_paths[0].exists()
