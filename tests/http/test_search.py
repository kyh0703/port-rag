from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from rag.http.responses import register_exception_handlers
from rag.http.search import create_search_router
from rag.search.service import InvalidSearchRequest
from rag.search.types import SearchHit
from rag.security.retrieval_capability import InvalidRetrievalCapability


class FakeSearchService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.raise_invalid = False

    async def search(self, *, user_id: str, query: str, top_k: int) -> list[SearchHit]:
        self.calls.append((user_id, query, top_k))
        if self.raise_invalid:
            raise InvalidSearchRequest("query is required")
        return [
            SearchHit(
                chunk_id="chunk-1",
                document_id="doc-1",
                document_name="notes.md",
                text="alpha",
                score=0.82,
                metadata={"page": "1"},
                seq=0,
            )
        ]

    async def search_revision(
        self,
        *,
        user_id: str,
        knowledge_revision_id: str,
        query: str,
        top_k: int,
    ) -> list[SearchHit]:
        self.calls.append((user_id, knowledge_revision_id, query, top_k))
        return await self._hits()

    async def _hits(self) -> list[SearchHit]:
        return [
            SearchHit(
                chunk_id="chunk-1",
                document_id="doc-1",
                document_name="notes.md",
                text="alpha",
                score=0.82,
                metadata={"page": "1"},
                seq=0,
            )
        ]


class FakeCapabilityVerifier:
    def __init__(self) -> None:
        self.user_id = "0197e50a-1234-7abc-8def-0123456789ab"
        self.calls: list[tuple[str, str]] = []

    def verify(self, token: str, *, knowledge_revision_id: str) -> str:
        self.calls.append((token, knowledge_revision_id))
        if token != "signed-capability":
            raise InvalidRetrievalCapability("invalid capability")
        return self.user_id


def build_client(service, *, raise_server_exceptions: bool = True) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(
        create_search_router(service=service, capability_verifier=FakeCapabilityVerifier())
    )
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def test_search_returns_api_response_envelope() -> None:
    service = FakeSearchService()
    client = build_client(service)

    response = client.post(
        "/search",
        json={"userId": "0197e50a-1234-7abc-8def-0123456789ab", "query": "alpha", "topK": 3},
    )

    assert response.status_code == 200
    assert response.json() == {
        "statusCode": 200,
        "message": "OK",
        "data": {
            "results": [
                {
                    "chunkId": "chunk-1",
                    "documentId": "doc-1",
                    "documentName": "notes.md",
                    "text": "alpha",
                    "score": 0.82,
                    "metadata": {"page": "1"},
                    "seq": 0,
                }
            ]
        },
    }
    assert service.calls == [("0197e50a-1234-7abc-8def-0123456789ab", "alpha", 3)]


def test_search_invalid_request_returns_api_error_envelope() -> None:
    service = FakeSearchService()
    service.raise_invalid = True
    client = build_client(service)

    response = client.post(
        "/search",
        json={"userId": "0197e50a-1234-7abc-8def-0123456789ab", "query": "alpha", "topK": 3},
    )

    assert response.status_code == 422
    assert response.json() == {
        "statusCode": 422,
        "message": "query is required",
        "error": "Unprocessable Entity",
        "data": None,
    }


def test_search_validation_error_returns_api_error_envelope() -> None:
    service = FakeSearchService()
    client = build_client(service)

    response = client.post("/search", json={"userId": "0197e50a-1234-7abc-8def-0123456789ab"})

    assert response.status_code == 422
    body = response.json()
    assert body["statusCode"] == 422
    assert body["error"] == "Unprocessable Entity"
    assert body["data"] is None
    assert any("Field required" in message for message in body["message"])
    assert service.calls == []


def test_search_unhandled_error_returns_api_error_envelope() -> None:
    class BrokenSearchService:
        async def search(self, *, user_id: str, query: str, top_k: int) -> list[SearchHit]:
            raise RuntimeError("database down")

    client = build_client(BrokenSearchService(), raise_server_exceptions=False)

    response = client.post(
        "/search",
        json={"userId": "0197e50a-1234-7abc-8def-0123456789ab", "query": "alpha", "topK": 3},
    )

    assert response.status_code == 500
    assert response.json() == {
        "statusCode": 500,
        "message": "Internal server error",
        "error": "InternalServerError",
        "data": None,
    }


def test_revision_search_uses_revision_capability_path() -> None:
    service = FakeSearchService()
    client = build_client(service)
    revision_id = "0197e50a-1234-7abc-8def-0123456789ac"

    response = client.post(
        f"/knowledge-revisions/{revision_id}/search",
        headers={"Authorization": "Bearer signed-capability"},
        json={"query": "alpha", "topK": 3},
    )

    assert response.status_code == 200
    assert response.json()["data"]["results"][0]["chunkId"] == "chunk-1"
    assert service.calls == [
        ("0197e50a-1234-7abc-8def-0123456789ab", revision_id, "alpha", 3)
    ]


def test_revision_search_rejects_missing_capability() -> None:
    service = FakeSearchService()
    client = build_client(service)
    revision_id = "0197e50a-1234-7abc-8def-0123456789ac"

    response = client.post(
        f"/knowledge-revisions/{revision_id}/search",
        json={"query": "alpha", "topK": 3},
    )

    assert response.status_code == 401
    assert service.calls == []
