from datetime import UTC, datetime
from unittest.mock import AsyncMock
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from rag.http.documents import DocumentRecord
from rag.http.responses import register_exception_handlers
from rag.http.webpages import create_webpages_router
from rag.webpages.repository import SyncConflict, WebpageDetail
from rag.webpages.types import WebpageState
from tests.fakes import MemoryOwnerAdmission

OWNER = "0197e50a-1234-7abc-8def-0123456789ab"
DOCUMENT_ID = uuid.uuid4()


def client_and_repository():
    state = WebpageState(urls=["https://example.com/"], auto_sync=False, sync_status="queued",
                         last_synced_at=None, last_checked_at=None, next_sync_at=None,
                         sync_error=None, last_sync_changed=None)
    document = DocumentRecord(id=DOCUMENT_ID, user_id=OWNER, knowledge_key="product",
                              name="Product", mime="text/html", status="processing", error=None,
                              created_at=datetime.now(UTC), updated_at=datetime.now(UTC), webpage=state)
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


def test_registration_uses_query_owner_and_persists_queued_status():
    client, repository, _ = client_and_repository()
    body = {"knowledgeKey": "product", "urls": ["https://example.com/"]}
    assert client.post("/documents/webpages", json=body).status_code == 422
    response = client.post("/documents/webpages", params={"userId": OWNER}, json=body)
    assert response.status_code == 201
    assert response.json()["data"]["webpage"]["syncStatus"] == "queued"
    assert response.json()["data"]["webpage"]["autoSync"] is False
    assert repository.create.await_args.kwargs["user_id"] == OWNER
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
                 {"knowledgeKey": "product", "urls": ["https://example.com/"], "autoSync": "yes"}):
        assert client.post("/documents/webpages", params={"userId": OWNER}, json=body).status_code == 422
    repository.create.assert_not_awaited()
