from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from rag.http.knowledge_revisions import create_knowledge_revisions_router
from rag.http.responses import register_exception_handlers
from rag.knowledge.revisions import KnowledgeRevisionNotFound
from rag.knowledge.revisions import KnowledgeRevisionRecord

USER_ID = "0197e50a-1234-7abc-8def-0123456789ab"
AUTHORIZATION = {"Authorization": "Bearer valid-capability"}


class FakeCapabilityVerifier:
    def __init__(self, user_id: str = USER_ID) -> None:
        self.user_id = user_id
        self.calls: list[tuple[str, str]] = []

    def verify(self, token: str, *, knowledge_revision_id: str) -> str:
        self.calls.append((token, knowledge_revision_id))
        return self.user_id


class FakeRevisionRepository:
    def __init__(self) -> None:
        self.create_calls: list[
            tuple[str, list[uuid.UUID] | None, uuid.UUID | None]
        ] = []
        self.get_calls: list[tuple[uuid.UUID, str]] = []
        self.list_calls: list[str] = []
        self.revisions: list[KnowledgeRevisionRecord] = []
        self.missing = False
        self.get_missing = False

    async def create(
        self,
        *,
        user_id: str,
        document_ids: list[uuid.UUID] | None,
        revision_id: uuid.UUID | None = None,
    ) -> KnowledgeRevisionRecord:
        self.create_calls.append((user_id, document_ids, revision_id))
        if self.missing:
            raise KnowledgeRevisionNotFound("selected document is not ready")
        return _revision(user_id, revision_id=revision_id)

    async def get(
        self,
        *,
        revision_id: uuid.UUID,
        user_id: str,
    ) -> KnowledgeRevisionRecord | None:
        self.get_calls.append((revision_id, user_id))
        if self.missing or self.get_missing:
            return None
        return _revision(user_id, revision_id=revision_id)

    async def list(self, *, user_id: str) -> list[KnowledgeRevisionRecord]:
        self.list_calls.append(user_id)
        return self.revisions


def _revision(
    user_id: str,
    *,
    revision_id: uuid.UUID | None = None,
) -> KnowledgeRevisionRecord:
    return KnowledgeRevisionRecord(
        id=revision_id or uuid.UUID("0197e50a-1234-7abc-8def-0123456789ac"),
        user_id=uuid.UUID(user_id),
        chunk_count=2,
        created_at=datetime(2026, 8, 11, tzinfo=UTC),
    )


def build_client(
    repository: FakeRevisionRepository,
    verifier: FakeCapabilityVerifier | None = None,
) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(
        create_knowledge_revisions_router(
            repository=repository,
            capability_verifier=verifier or FakeCapabilityVerifier(),
        )
    )
    return TestClient(app)


def test_create_revision_returns_exact_immutable_identity() -> None:
    repository = FakeRevisionRepository()
    repository.get_missing = True
    client = build_client(repository)
    user_id = USER_ID
    document_id = "0197e50a-1234-7abc-8def-0123456789ad"
    revision_id = "0197e50a-1234-7abc-8def-0123456789ae"

    response = client.post(
        "/knowledge-revisions",
        headers=AUTHORIZATION,
        json={
            "userId": user_id,
            "documentIds": [document_id],
            "revisionId": revision_id,
        },
    )

    assert response.status_code == 201
    assert response.json()["data"] == {
        "id": revision_id,
        "userId": user_id,
        "chunkCount": 2,
        "createdAt": "2026-08-11T00:00:00Z",
    }
    assert repository.create_calls == [
        (user_id, [uuid.UUID(document_id)], uuid.UUID(revision_id))
    ]


def test_create_revision_uses_and_reuses_a_caller_supplied_identity() -> None:
    repository = FakeRevisionRepository()
    repository.get_missing = True
    client = build_client(repository)
    user_id = USER_ID
    revision_id = "0197e50a-1234-7abc-8def-0123456789ae"

    created = client.post(
        "/knowledge-revisions",
        headers=AUTHORIZATION,
        json={"userId": user_id, "revisionId": revision_id},
    )

    assert created.status_code == 201
    assert created.json()["data"]["id"] == revision_id
    assert repository.create_calls == [
        (user_id, None, uuid.UUID(revision_id)),
    ]

    repository.get_missing = False
    reused = client.post(
        "/knowledge-revisions",
        headers=AUTHORIZATION,
        json={"userId": user_id, "revisionId": revision_id},
    )

    assert reused.status_code == 201
    assert reused.json()["data"]["id"] == revision_id
    assert len(repository.create_calls) == 1


def test_get_revision_is_user_scoped() -> None:
    repository = FakeRevisionRepository()
    client = build_client(repository)
    user_id = USER_ID
    revision_id = "0197e50a-1234-7abc-8def-0123456789ac"

    response = client.get(
        f"/knowledge-revisions/{revision_id}",
        params={"userId": user_id},
    )

    assert response.status_code == 200
    assert repository.get_calls == [(uuid.UUID(revision_id), user_id)]


def test_list_revisions_is_user_scoped_and_preserves_newest_first_contract() -> None:
    repository = FakeRevisionRepository()
    repository.revisions = [
        _revision(USER_ID, revision_id=uuid.UUID("0197e50a-1234-7abc-8def-0123456789ac")),
        KnowledgeRevisionRecord(
            id=uuid.UUID("0197e50a-1234-7abc-8def-0123456789ad"),
            user_id=uuid.UUID(USER_ID),
            chunk_count=1,
            created_at=datetime(2026, 8, 10, tzinfo=UTC),
        ),
    ]
    client = build_client(repository)

    response = client.get("/knowledge-revisions", params={"userId": USER_ID})

    assert response.status_code == 200
    assert repository.list_calls == [USER_ID]
    assert response.json()["data"] == [
        {
            "id": "0197e50a-1234-7abc-8def-0123456789ac",
            "userId": USER_ID,
            "chunkCount": 2,
            "createdAt": "2026-08-11T00:00:00Z",
        },
        {
            "id": "0197e50a-1234-7abc-8def-0123456789ad",
            "userId": USER_ID,
            "chunkCount": 1,
            "createdAt": "2026-08-10T00:00:00Z",
        },
    ]


def test_create_revision_rejects_an_unready_selected_document() -> None:
    repository = FakeRevisionRepository()
    repository.missing = True
    client = build_client(repository)

    response = client.post(
        "/knowledge-revisions",
        headers=AUTHORIZATION,
        json={
            "userId": USER_ID,
            "documentIds": ["0197e50a-1234-7abc-8def-0123456789ad"],
            "revisionId": "0197e50a-1234-7abc-8def-0123456789ae",
        },
    )

    assert response.status_code == 409
    assert response.json()["message"] == "selected document is not ready"


def test_create_revision_requires_a_capability_for_the_same_user_and_revision() -> None:
    repository = FakeRevisionRepository()
    verifier = FakeCapabilityVerifier(
        user_id="0197e50a-1234-7abc-8def-0123456789ff"
    )
    client = build_client(repository, verifier)
    body = {
        "userId": USER_ID,
        "revisionId": "0197e50a-1234-7abc-8def-0123456789ae",
    }

    missing = client.post("/knowledge-revisions", json=body)
    mismatched = client.post(
        "/knowledge-revisions",
        headers=AUTHORIZATION,
        json=body,
    )

    assert missing.status_code == 401
    assert mismatched.status_code == 401
    assert repository.create_calls == []
