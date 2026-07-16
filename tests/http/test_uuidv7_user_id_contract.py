from __future__ import annotations

from pathlib import Path

from tests.http.test_documents import FakeDocumentRepository
from tests.http.test_documents import FakeUploadStorage
from tests.http.test_documents import FakeWorker
from tests.http.test_documents import build_client as build_documents_client
from tests.http.test_search import FakeSearchService
from tests.http.test_search import build_client as build_search_client


UUID_V7 = "0197e50a-1234-7abc-8def-0123456789ab"


def test_documents_rejects_non_uuidv7_user_id(tmp_path: Path) -> None:
    client = build_documents_client(
        FakeDocumentRepository(),
        FakeWorker(),
        FakeUploadStorage(tmp_path),
    )

    response = client.post(
        "/documents",
        data={"userId": "42"},
        files={"file": ("notes.md", b"hello", "text/markdown")},
    )

    assert response.status_code == 422


def test_search_rejects_non_uuidv7_user_id() -> None:
    client = build_search_client(FakeSearchService())

    response = client.post("/search", json={"userId": "user-a", "query": "alpha", "topK": 3})

    assert response.status_code == 422


def test_uuidv7_user_id_is_accepted_at_http_boundaries(tmp_path: Path) -> None:
    documents_client = build_documents_client(
        FakeDocumentRepository(),
        FakeWorker(),
        FakeUploadStorage(tmp_path),
    )
    search_service = FakeSearchService()
    search_client = build_search_client(search_service)

    document_response = documents_client.post(
        "/documents",
        data={"userId": UUID_V7},
        files={"file": ("notes.md", b"hello", "text/markdown")},
    )
    search_response = search_client.post(
        "/search",
        json={"userId": UUID_V7, "query": "alpha", "topK": 3},
    )

    assert document_response.status_code == 201
    assert search_response.status_code == 200
    assert search_service.calls == [(UUID_V7, "alpha", 3)]
