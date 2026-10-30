from datetime import UTC, datetime
from unittest.mock import AsyncMock
import re
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from rag.http.documents import DocumentRecord
from rag.http.responses import register_exception_handlers
from rag.http.webpages import create_webpages_router
from rag.webpages.repository import SyncConflict, WebpageDetail, WebpageRepository
from rag.webpages.types import WebpageState
from rag.webpages.fetch import CrawlerClient
from tests.fakes import MemoryOwnerAdmission
from tests.private_data_fixture import PrivateDataCipherFake

OWNER = "0197e50a-1234-7abc-8def-0123456789ab"
DOCUMENT_ID = uuid.uuid4()


def client_and_repository(repository=None):
    state = WebpageState(urls=["https://example.com/"], auto_sync=False, sync_status="queued",
                         last_synced_at=None, last_checked_at=None, next_sync_at=None,
                         sync_error=None, last_sync_changed=None)
    document = DocumentRecord(id=DOCUMENT_ID, user_id=OWNER, knowledge_key="product",
                              name="Product", mime="text/html", status="processing", error=None,
                              created_at=datetime.now(UTC), updated_at=datetime.now(UTC), webpage=state)
    if repository is None:
        repository = AsyncMock()
        repository.create.return_value = document
        repository.get.return_value = WebpageDetail(document=document, content="")
        repository.enqueue.return_value = repository.get.return_value
        repository.set_auto_sync.return_value = repository.get.return_value
    fetcher = AsyncMock()
    fetcher.discover.return_value = state.urls
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(create_webpages_router(repository=repository, fetcher=fetcher,
                                               owner_access=MemoryOwnerAdmission()))
    return TestClient(app), repository, fetcher


def registration_client():
    session = AsyncMock(spec=AsyncSession)
    session.__aenter__.return_value = session

    def refresh(document):
        document.created_at = document.updated_at = datetime.now(UTC)

    session.refresh.side_effect = refresh
    repository = WebpageRepository(lambda: session, cipher=PrivateDataCipherFake())
    client, _, _ = client_and_repository(repository)
    return client, session


def test_registration_generates_distinct_keys_when_omitted():
    client, session = registration_client()
    keys = []
    for _ in range(2):
        response = client.post(
            "/documents/webpages", params={"userId": OWNER},
            json={"urls": ["https://Docs.Example.com.:443/guide?lang=ko#section",
                           "https://other.example.com/help"]},
        )
        assert response.status_code == 201
        document = response.json()["data"]
        assert re.fullmatch(r"knowledge_[0-9a-f]{32}", document["knowledgeKey"])
        assert document["name"] == "docs.example.com"
        assert document["webpage"]["urls"] == [
            "https://docs.example.com/guide?lang=ko", "https://other.example.com/help",
        ]
        assert document["webpage"]["autoSync"] is False
        keys.append(document["knowledgeKey"])
    assert keys[0] != keys[1]
    assert session.commit.await_count == 2


@pytest.mark.parametrize(("name", "expected_name"), [
    (None, "example.com"),
    ("", "example.com"),
    (" \t ", "example.com"),
    (" Product guide ", " Product guide "),
])
def test_registration_defaults_blank_names_and_preserves_supplied_names(name, expected_name):
    client, _ = registration_client()
    response = client.post(
        "/documents/webpages", params={"userId": OWNER},
        json={"name": name, "knowledgeKey": "product_guide", "urls": ["https://example.com/docs"]},
    )
    assert response.status_code == 201
    assert response.json()["data"]["knowledgeKey"] == "product_guide"
    assert response.json()["data"]["name"] == expected_name


def test_registration_uses_query_owner_and_persists_queued_status():
    client, repository, _ = client_and_repository()
    body = {"knowledgeKey": "product", "urls": ["https://example.com/"]}
    assert client.post("/documents/webpages", json=body).status_code == 422
    response = client.post("/documents/webpages", params={"userId": OWNER}, json=body)
    assert response.status_code == 201
    assert response.json()["data"]["webpage"]["syncStatus"] == "queued"
    assert response.json()["data"]["webpage"]["autoSync"] is False
    assert repository.create.await_args.kwargs["user_id"] == OWNER
    assert repository.create.await_args.kwargs["knowledge_key"] == "product"
    assert repository.create.await_args.kwargs["auto_sync"] is False


def test_detail_toggle_sync_discovery_and_conflict_envelopes():
    client, repository, fetcher = client_and_repository()
    params = {"userId": OWNER}
    prefix = f"/documents/{DOCUMENT_ID}"
    response = client.get(prefix + "/webpage", params=params)
    assert response.status_code == 200
    assert response.json()["data"]["content"] == ""
    assert client.patch(prefix + "/webpage", params=params, json={"autoSync": True}).status_code == 200
    assert repository.set_auto_sync.await_args.kwargs["auto_sync"] is True
    assert client.post(prefix + "/sync", params=params).status_code == 202
    repository.enqueue.side_effect = SyncConflict("already queued")
    assert client.post(prefix + "/sync", params=params).status_code == 409
    response = client.post("/documents/webpages/discover", params=params,
                           json={"url": "https://example.com/"})
    assert response.json()["data"] == {"urls": ["https://example.com/"]}
    fetcher.discover.assert_awaited_once_with("https://example.com/")
    repository.get.return_value = None
    assert client.get(prefix + "/webpage", params=params).status_code == 404


def test_registration_rejects_empty_urls_invalid_key_and_nonboolean_toggle():
    client, repository, _ = client_and_repository()
    for body in ({"knowledgeKey": "product", "urls": []},
                 {"knowledgeKey": "BAD", "urls": ["https://example.com/"]},
                 {"knowledgeKey": "", "urls": ["https://example.com/"]},
                 {"knowledgeKey": " ", "urls": ["https://example.com/"]},
                 {"knowledgeKey": None, "urls": ["https://example.com/"]},
                 {"knowledgeKey": "product", "urls": ["https://example.com/"], "autoSync": "yes"}):
        assert client.post("/documents/webpages", params={"userId": OWNER}, json=body).status_code == 422
    repository.create.assert_not_awaited()


@pytest.mark.parametrize("query", [
    "offset=-1", "offset=1.5", "limit=0", "limit=101", "limit=not-a-number",
])
def test_chunk_preview_rejects_unbounded_or_invalid_pages(query):
    client, repository, _ = client_and_repository()
    response = client.get(
        f"/documents/{DOCUMENT_ID}/webpage/chunks?userId={OWNER}&{query}",
    )
    assert response.status_code == 422
    repository.get_chunks.assert_not_awaited()


@pytest.mark.parametrize(("status", "code", "expected_status"), [
    (422, "unsafe_address", 422),
    (503, "browser_unavailable", 503),
])
async def test_discovery_preserves_public_envelope_and_distinguishes_source_from_service_failure(
    status, code, expected_status,
):
    secret = "https://source.example/private?token=secret"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(
            status, json={"error": {"code": code, "message": secret}},
        )),
        trust_env=False,
    ) as upstream:
        fetcher = CrawlerClient(
            base_url="http://crawler:8000",
            internal_server_key="test-only-internal-server-key-0123456789",
            client=upstream,
        )
        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(create_webpages_router(
            repository=AsyncMock(), fetcher=fetcher, owner_access=MemoryOwnerAdmission(),
        ))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://rag",
        ) as client:
            response = await client.post(
                "/documents/webpages/discover", params={"userId": OWNER},
                json={"url": "example.com"},
            )
    assert response.status_code == expected_status
    payload = response.json()
    assert set(payload) == {"statusCode", "message", "error", "data"}
    assert payload["statusCode"] == expected_status
    assert payload["data"] is None
    assert secret not in response.text
