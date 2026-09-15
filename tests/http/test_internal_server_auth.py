import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from rag.config import Settings
from rag.main import create_app, scrub_sentry_event
from rag.http.search import create_search_router
from rag.security.retrieval_capability import InvalidRetrievalCapability

TEST_KEY = "test-only-internal-server-key-0123456789"


@pytest.fixture(autouse=True)
def clear_internal_key(monkeypatch):
    monkeypatch.delenv("INTERNAL_SERVER_KEY", raising=False)


def make_app(key=TEST_KEY):
    app = create_app(internal_server_key=key)
    calls = []

    @app.post("/documents")
    async def document(body: dict):
        calls.append(body)
        return {"ok": True}

    @app.get("/knowledge-revisions")
    async def revisions():
        calls.append("list")
        return {"data": []}

    return app, calls


def test_unconfigured_app_never_allows_protected_routes():
    app = create_app()

    @app.get("/documents")
    async def documents():
        return {"data": []}

    assert TestClient(app).get("/documents").status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"x-internal-server": "wrong-internal-server-key-0123456789"},
        [("x-internal-server", TEST_KEY), ("x-internal-server", TEST_KEY)],
    ],
)
def test_missing_wrong_and_duplicate_keys_are_rejected_before_body_parsing(headers):
    app, calls = make_app()
    response = TestClient(app).post("/documents", headers=headers, content="not JSON")
    assert response.status_code == 401
    assert response.json()["error"] == "Unauthorized"
    assert TEST_KEY not in response.text
    assert calls == []


def test_valid_key_reaches_business_routes_and_preserves_other_authorization():
    app, calls = make_app()
    client = TestClient(app)
    headers = {"X-Internal-Server": TEST_KEY, "Authorization": "Bearer scoped-token"}
    response = client.post("/documents", headers=headers, json={"name": "test"})
    assert response.status_code == 200
    assert calls == [{"name": "test"}]


@pytest.mark.parametrize("path", ["/healthz", "/metrics", "/metrics/"])
def test_health_and_metrics_remain_available_without_keys(path):
    app, _ = make_app()
    assert TestClient(app).get(path).status_code == 200


@pytest.mark.parametrize("key", ["", "short", "x" * 257, "\n" + "x" * 40, "가" * 40])
def test_invalid_server_configuration_fails_closed(key):
    with pytest.raises(ValueError, match="INTERNAL_SERVER_KEY"):
        create_app(internal_server_key=key)


@pytest.mark.asyncio
async def test_unauthorized_body_is_not_consumed():
    app, calls = make_app()
    sent = []

    async def receive():
        raise AssertionError("unauthorized request body was read")

    async def send(message):
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/documents",
            "raw_path": b"/documents",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"content-type", b"application/json")],
            "server": ("test", 80),
            "client": ("test", 1),
        },
        receive,
        send,
    )
    assert sent[0]["status"] == 401
    assert calls == []


def test_internal_key_is_required_and_hidden_in_settings_errors():
    base = {
        "_env_file": None,
        "DATABASE_URL": "postgresql+asyncpg://port:port@localhost:5432/port",
        "RAG_RETRIEVAL_CAPABILITY_SECRET": "a-32-byte-minimum-retrieval-secret",
        "EMBEDDER": "fake",
    }
    with pytest.raises(ValidationError, match="INTERNAL_SERVER_KEY"):
        Settings(**base)
    settings = Settings(**base, INTERNAL_SERVER_KEY=TEST_KEY)
    assert TEST_KEY not in repr(settings)
    assert settings.INTERNAL_SERVER_KEY.get_secret_value() == TEST_KEY


def test_sentry_scrubs_internal_key_fields_outside_request_headers():
    event = {
        "request": {"headers": {"x-internal-server": TEST_KEY}},
        "extra": {"config": {"INTERNAL_SERVER_KEY": TEST_KEY}, "safe": "kept"},
    }
    result = scrub_sentry_event(event, {})
    assert TEST_KEY not in json.dumps(result)
    assert result["extra"]["safe"] == "kept"


def test_service_key_does_not_replace_revision_capability():
    capability_calls = []
    search_calls = []

    class Capability:
        def verify(self, token, *, knowledge_revision_id):
            capability_calls.append(token)
            if token != "valid-capability":
                raise InvalidRetrievalCapability("invalid capability")
            return "00000000-0000-0000-0000-000000000000"

    class Search:
        async def search_revision(self, **kwargs):
            search_calls.append(kwargs)
            return []

    app = create_app(internal_server_key=TEST_KEY)
    app.include_router(create_search_router(service=Search(), capability_verifier=Capability()))
    client = TestClient(app)
    url = "/knowledge-revisions/00000000-0000-0000-0000-000000000000/search"
    assert (
        client.post(
            url, json={"query": "test"}, headers={"Authorization": "Bearer valid-capability"}
        ).status_code
        == 401
    )
    assert capability_calls == []
    headers = {"x-internal-server": TEST_KEY, "Authorization": "Bearer invalid"}
    assert client.post(url, json={"query": "test"}, headers=headers).status_code == 401
    assert search_calls == []
    headers["Authorization"] = "Bearer valid-capability"
    assert client.post(url, json={"query": "test"}, headers=headers).status_code == 200
    assert len(search_calls) == 1
