from __future__ import annotations

import json
import logging
import traceback

import httpx
import pytest

from rag.ingest.embedder import (
    EmbeddingError,
    InternalEmbeddingCredentialProvider,
    OpenAIEmbedder,
)


INTERNAL_KEY = "test-only-internal-server-key-0123456789"
ADMIN_KEY = "sk-administrator-only-secret"


def credential_response(key=ADMIN_KEY):
    return httpx.Response(200, json={"data": {"provider": "openai", "apiKey": key}})


def embedding_response(request):
    inputs = json.loads(request.content)["input"]
    return httpx.Response(
        200,
        json={
            "object": "list",
            "model": "text-embedding-3-small",
            "data": [
                {"object": "embedding", "index": index, "embedding": [0.25, 0.75]}
                for index, _ in enumerate(inputs)
            ],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        },
    )


@pytest.fixture
async def make_embedder(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-environment-must-not-be-used")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    embedders = []

    def create(internal_handler, openai_handler, **kwargs):
        internal_client = httpx.AsyncClient(transport=httpx.MockTransport(internal_handler))
        openai_client = httpx.AsyncClient(transport=httpx.MockTransport(openai_handler))
        provider = InternalEmbeddingCredentialProvider(
            base_url="http://api.test/api/v1/",
            internal_server_key=INTERNAL_KEY,
            client=internal_client,
        )
        embedder = OpenAIEmbedder(
            credential_provider=provider,
            http_client=openai_client,
            initial_backoff_seconds=0,
            **kwargs,
        )
        embedders.append(embedder)
        return embedder, internal_client, openai_client

    yield create
    for embedder in embedders:
        await embedder.aclose()


async def test_ingest_and_search_use_rotated_admin_key_on_next_sdk_request(make_embedder):
    current_key = ADMIN_KEY
    authorizations = []

    def internal(request):
        assert request.method == "GET"
        assert str(request.url) == "http://api.test/api/v1/internal/rag/embedding-credential"
        assert request.headers["X-Internal-Server"] == INTERNAL_KEY
        return credential_response(current_key)

    def openai(request):
        assert request.url.path == "/v1/embeddings"
        assert "X-Internal-Server" not in request.headers
        authorizations.append(request.headers["Authorization"])
        return embedding_response(request)

    embedder, _, _ = make_embedder(internal, openai)
    assert await embedder.embed_texts(["document"]) == [[0.25, 0.75]]
    current_key = "sk-rotated-administrator-secret"
    assert await embedder.embed_query("query") == [0.25, 0.75]
    assert authorizations == [f"Bearer {ADMIN_KEY}", f"Bearer {current_key}"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, text=ADMIN_KEY),
        httpx.Response(403, text=ADMIN_KEY),
        httpx.Response(503, text=ADMIN_KEY),
        httpx.Response(200, text=f"not-json-{ADMIN_KEY}"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"data": None}),
        httpx.Response(200, json={"data": {"provider": "other", "apiKey": ADMIN_KEY}}),
        httpx.Response(200, json={"data": {"provider": "openai"}}),
        credential_response(None),
        credential_response(123),
        credential_response(""),
        credential_response("  "),
        credential_response(f" {ADMIN_KEY}"),
    ],
)
async def test_credential_failure_never_uses_environment_or_fake_vectors(
    make_embedder, response, caplog
):
    requests = []

    def openai(request):
        requests.append(request)
        return embedding_response(request)

    embedder, _, _ = make_embedder(lambda request: response, openai, max_attempts=1)
    with caplog.at_level(logging.DEBUG), pytest.raises(EmbeddingError) as error:
        await embedder.embed_query("query")
    assert requests == []
    assert error.value.code == "credential_unavailable"
    assert str(error.value) == "Embedding credentials are unavailable. Contact an administrator."
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert ADMIN_KEY not in str(error.value)
    assert ADMIN_KEY not in caplog.text
    assert INTERNAL_KEY not in caplog.text
    assert ADMIN_KEY not in "".join(traceback.format_exception(error.value))


async def test_internal_redirect_does_not_forward_shared_secret(make_embedder):
    urls = []

    def internal(request):
        urls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://untrusted.test/credential"})

    embedder, internal_client, _ = make_embedder(internal, embedding_response, max_attempts=1)
    internal_client.follow_redirects = True
    with pytest.raises(EmbeddingError):
        await embedder.embed_query("query")
    assert urls == ["http://api.test/api/v1/internal/rag/embedding-credential"]


async def test_network_failure_is_safe_and_does_not_reuse_previous_key(make_embedder, caplog):
    available = True
    openai_calls = []

    def internal(request):
        if available:
            return credential_response()
        raise httpx.ConnectError(f"credential unavailable: {ADMIN_KEY}", request=request)

    def openai(request):
        openai_calls.append(request)
        return embedding_response(request)

    embedder, _, _ = make_embedder(internal, openai, max_attempts=1)
    assert await embedder.embed_query("first") == [0.25, 0.75]
    available = False
    with caplog.at_level(logging.DEBUG), pytest.raises(EmbeddingError) as error:
        await embedder.embed_query("second")
    assert len(openai_calls) == 1
    assert error.value.code == "credential_unavailable"
    assert error.value.__context__ is None
    assert ADMIN_KEY not in "".join(traceback.format_exception(error.value))
    assert ADMIN_KEY not in caplog.text
    assert INTERNAL_KEY not in caplog.text


@pytest.mark.parametrize(
    ("status_code", "code", "message"),
    [
        (
            401,
            "authentication_failed",
            "Embedding provider authentication failed. "
            "Contact an administrator to check the API key.",
        ),
        (
            403,
            "permission_denied",
            "Embedding provider access was denied. "
            "Contact an administrator to check permissions.",
        ),
        (
            429,
            "rate_limit_exceeded",
            "Embedding provider rate limit or quota was exceeded. Please try again later.",
        ),
        (
            500,
            "provider_unavailable",
            "Embedding provider is temporarily unavailable. Please try again later.",
        ),
        (
            503,
            "provider_unavailable",
            "Embedding provider is temporarily unavailable. Please try again later.",
        ),
        (
            400,
            "request_failed",
            "Embedding request failed. Please try again later.",
        ),
    ],
)
async def test_upstream_status_has_safe_reason_without_retaining_key_echo(
    make_embedder, caplog, status_code, code, message
):
    attempts = 0
    raw_message = f"Invalid API key: {ADMIN_KEY}; https://provider.test/private"

    def openai(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(status_code, json={"error": {"message": raw_message}})

    embedder, _, _ = make_embedder(
        lambda request: credential_response(), openai, max_attempts=3
    )
    with caplog.at_level(logging.DEBUG), pytest.raises(EmbeddingError) as error:
        await embedder.embed_query("query")
    assert attempts == 3
    assert error.value.code == code
    assert str(error.value) == message
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert ADMIN_KEY not in "".join(traceback.format_exception(error.value))
    assert ADMIN_KEY not in repr(vars(error.value))
    assert ADMIN_KEY not in caplog.text
    assert raw_message not in caplog.text


@pytest.mark.parametrize("exception_type", [httpx.ConnectError, httpx.ReadTimeout])
async def test_provider_network_failure_does_not_log_upstream_exception_or_traceback(
    make_embedder, caplog, exception_type
):
    attempts = 0
    raw_message = f"Provider unavailable: {ADMIN_KEY}; https://provider.test/private"

    def openai(request):
        nonlocal attempts
        attempts += 1
        raise exception_type(raw_message, request=request)

    embedder, _, _ = make_embedder(
        lambda request: credential_response(), openai, max_attempts=3
    )
    # Isolate application handling from the SDK's own opt-in debug tracebacks.
    with (
        caplog.at_level(logging.DEBUG),
        caplog.at_level(logging.WARNING, logger="openai"),
        pytest.raises(EmbeddingError) as error,
    ):
        await embedder.embed_query("query")
    assert attempts == 3
    assert error.value.code == "provider_unavailable"
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert ADMIN_KEY not in "".join(traceback.format_exception(error.value))
    assert ADMIN_KEY not in repr(vars(error.value))
    assert ADMIN_KEY not in caplog.text
    assert raw_message not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


async def test_batch_retry_fetches_current_key_and_closes_both_http_clients(make_embedder):
    batches = []
    credential_requests = []

    def internal(request):
        credential_requests.append(request)
        return credential_response()

    def openai(request):
        batches.append(json.loads(request.content)["input"])
        if len(batches) == 1:
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return embedding_response(request)

    embedder, internal_client, openai_client = make_embedder(
        internal, openai, batch_size=2, max_attempts=3
    )
    assert await embedder.embed_texts(["a", "b", "c"]) == [[0.25, 0.75]] * 3
    assert batches == [["a", "b"], ["a", "b"], ["c"]]
    assert len(credential_requests) == 3
    await embedder.aclose()
    assert internal_client.is_closed
    assert openai_client.is_closed


async def test_stale_fake_environment_cannot_bypass_administrator_credentials(monkeypatch):
    from rag.config import Settings
    from rag.main import _create_embedder
    from rag.metrics import Metrics

    monkeypatch.setenv("EMBEDDER", "fake")

    async def unavailable(self, request, **kwargs):
        return httpx.Response(503, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", unavailable)
    settings = Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://test:test@localhost/test",
        INTERNAL_SERVER_KEY=INTERNAL_KEY,
        RAG_RETRIEVAL_CAPABILITY_SECRET="test-retrieval-secret-at-least-32-bytes",
    )
    embedder = _create_embedder(settings, metrics=Metrics())
    try:
        with pytest.raises(EmbeddingError):
            await embedder.embed_query("Do not silently return a fake vector")
    finally:
        close = getattr(embedder, "aclose", None)
        if close is not None:
            await close()
