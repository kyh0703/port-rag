from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from rag.db.models import DocumentStatus
from rag.http.documents import DocumentRecord
from rag.http.documents import create_documents_router
from rag.http.responses import register_exception_handlers


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
    *,
    raise_server_exceptions: bool = True,
) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(
        create_documents_router(repository=repository, worker=worker, storage=storage)
    )
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def test_post_requires_user_id(tmp_path: Path) -> None:
    client = build_client(FakeDocumentRepository(), FakeWorker(), FakeUploadStorage(tmp_path))

    response = client.post(
        "/documents",
        files={"file": ("notes.md", b"hello", "text/markdown")},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["statusCode"] == 422
    assert body["error"] == "Unprocessable Entity"
    assert body["data"] is None


def test_post_returns_processing_document_and_enqueues_ingest_job(tmp_path: Path) -> None:
    repository = FakeDocumentRepository()
    worker = FakeWorker()
    storage = FakeUploadStorage(tmp_path)
    client = build_client(repository, worker, storage)

    response = client.post(
        "/documents",
        data={"userId": "0197e50a-1234-7abc-8def-0123456789ab"},
        files={"file": ("notes.md", b"alpha", "text/markdown")},
    )

    assert response.status_code == 201
    body = response.json()
    document_id = uuid.UUID(body["data"]["id"])
    assert body == {
        "statusCode": 201,
        "message": "Created",
        "data": {
            "id": str(document_id),
            "userId": "0197e50a-1234-7abc-8def-0123456789ab",
            "name": "notes.md",
            "mime": "text/markdown",
            "status": "processing",
            "error": None,
            "createdAt": "2026-07-09T12:00:00Z",
            "updatedAt": "2026-07-09T12:00:00Z",
        },
    }
    assert repository.created == [("0197e50a-1234-7abc-8def-0123456789ab", "notes.md", "text/markdown")]
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
        data={"userId": "0197e50a-1234-7abc-8def-0123456789ab"},
        files={"file": ("a.md", b"a", "text/markdown")},
    ).json()["data"]
    client.post(
        "/documents",
        data={"userId": "0197e50a-1234-7abc-8def-0123456789ac"},
        files={"file": ("b.md", b"b", "text/markdown")},
    )

    response = client.get("/documents", params={"userId": "0197e50a-1234-7abc-8def-0123456789ab"})

    assert response.status_code == 200
    assert response.json() == {
        "statusCode": 200,
        "message": "OK",
        "data": [user_a],
    }


def test_get_document_is_scoped_by_user_id(tmp_path: Path) -> None:
    repository = FakeDocumentRepository()
    worker = FakeWorker()
    storage = FakeUploadStorage(tmp_path)
    client = build_client(repository, worker, storage)
    document = client.post(
        "/documents",
        data={"userId": "0197e50a-1234-7abc-8def-0123456789ab"},
        files={"file": ("notes.md", b"a", "text/markdown")},
    ).json()["data"]

    not_found = client.get(f"/documents/{document['id']}", params={"userId": "0197e50a-1234-7abc-8def-0123456789ac"})

    assert not_found.status_code == 404
    assert not_found.json() == {
        "statusCode": 404,
        "message": "document not found",
        "error": "Not Found",
        "data": None,
    }

    response = client.get(f"/documents/{document['id']}", params={"userId": "0197e50a-1234-7abc-8def-0123456789ab"})

    assert response.status_code == 200
    assert response.json() == {
        "statusCode": 200,
        "message": "OK",
        "data": document,
    }


def test_delete_document_is_scoped_by_user_id(tmp_path: Path) -> None:
    repository = FakeDocumentRepository()
    worker = FakeWorker()
    storage = FakeUploadStorage(tmp_path)
    client = build_client(repository, worker, storage)
    document = client.post(
        "/documents",
        data={"userId": "0197e50a-1234-7abc-8def-0123456789ab"},
        files={"file": ("notes.md", b"a", "text/markdown")},
    ).json()["data"]

    assert (
        client.delete(f"/documents/{document['id']}", params={"userId": "0197e50a-1234-7abc-8def-0123456789ac"}).status_code
        == 404
    )
    assert uuid.UUID(document["id"]) in repository.documents

    response = client.delete(f"/documents/{document['id']}", params={"userId": "0197e50a-1234-7abc-8def-0123456789ab"})

    assert response.status_code == 200
    assert response.json() == {
        "statusCode": 200,
        "message": "OK",
        "data": None,
    }
    assert uuid.UUID(document["id"]) not in repository.documents


def test_read_endpoints_require_user_id(tmp_path: Path) -> None:
    client = build_client(FakeDocumentRepository(), FakeWorker(), FakeUploadStorage(tmp_path))
    document_id = uuid.uuid4()

    for response in [
        client.get("/documents"),
        client.get(f"/documents/{document_id}"),
        client.delete(f"/documents/{document_id}"),
    ]:
        assert response.status_code == 422
        body = response.json()
        assert body["statusCode"] == 422
        assert body["error"] == "Unprocessable Entity"
        assert body["data"] is None


def test_post_deletes_saved_file_when_document_create_fails(tmp_path: Path) -> None:
    repository = FakeDocumentRepository()
    repository.fail_create = True
    storage = FakeUploadStorage(tmp_path)
    client = build_client(
        repository,
        FakeWorker(),
        storage,
        raise_server_exceptions=False,
    )

    response = client.post(
        "/documents",
        data={"userId": "0197e50a-1234-7abc-8def-0123456789ab"},
        files={"file": ("notes.md", b"alpha", "text/markdown")},
    )

    assert response.status_code == 500
    assert response.json() == {
        "statusCode": 500,
        "message": "Internal server error",
        "error": "InternalServerError",
        "data": None,
    }
    assert storage.saved_paths
    assert not storage.saved_paths[0].exists()


def test_post_deletes_saved_file_when_worker_enqueue_fails(tmp_path: Path) -> None:
    repository = FakeDocumentRepository()
    worker = FakeWorker()
    worker.fail_enqueue = True
    storage = FakeUploadStorage(tmp_path)
    client = build_client(
        repository,
        worker,
        storage,
        raise_server_exceptions=False,
    )

    response = client.post(
        "/documents",
        data={"userId": "0197e50a-1234-7abc-8def-0123456789ab"},
        files={"file": ("notes.md", b"alpha", "text/markdown")},
    )

    assert response.status_code == 500
    assert response.json() == {
        "statusCode": 500,
        "message": "Internal server error",
        "error": "InternalServerError",
        "data": None,
    }
    assert storage.saved_paths
    assert not storage.saved_paths[0].exists()
