from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
import pytest

from rag.webpages.fetch import (
    MAX_CRAWLER_RESPONSE_BYTES,
    MAX_PAGE_BYTES,
    CrawlerClient,
    open_crawler_client,
)
from rag.webpages.types import CrawlerUnavailableError, WebpageFetchError

INTERNAL_KEY = "test-only-internal-server-key-0123456789"
SERVICE_URL = "http://crawler:8000"


@asynccontextmanager
async def crawler(handler, *, follow_redirects=False):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        trust_env=False,
        follow_redirects=follow_redirects,
    ) as client:
        yield CrawlerClient(
            base_url=SERVICE_URL, internal_server_key=INTERNAL_KEY, client=client,
        )


@pytest.mark.parametrize("operation", ["fetch", "discover"])
async def test_service_redirect_never_leaks_authentication_to_an_upstream(operation):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(307, headers={"location": "https://source.example/private"})

    async with crawler(respond, follow_redirects=True) as client:
        with pytest.raises(CrawlerUnavailableError):
            await getattr(client, operation)("example.com")
    assert len(requests) == 1
    assert requests[0].url.host == "crawler"


@pytest.mark.parametrize("code", [
    "invalid_url", "unsafe_address", "upstream_error", "invalid_content",
    "too_large", "too_many_redirects", "no_readable_text",
])
async def test_known_source_errors_discard_untrusted_messages_and_exception_context(code, caplog):
    secret = "https://source.example/private?token=secret " + INTERNAL_KEY
    with caplog.at_level(logging.WARNING):
        async with crawler(lambda _: httpx.Response(422, json={
            "error": {"code": code, "message": secret},
        })) as client:
            with pytest.raises(WebpageFetchError) as caught:
                await client.fetch("example.com")
    assert not isinstance(caught.value, CrawlerUnavailableError)
    assert caught.value.code == code
    assert secret not in str(caught.value)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert secret not in caplog.text
    assert INTERNAL_KEY not in caplog.text


@pytest.mark.parametrize(("status", "payload"), [
    (401, {"error": {"code": "unauthorized", "message": INTERNAL_KEY}}),
    (429, {"error": {"code": "busy", "message": INTERNAL_KEY}}),
    (503, {"error": {"code": "browser_unavailable", "message": INTERNAL_KEY}}),
    (504, {"error": {"code": "timeout", "message": INTERNAL_KEY}}),
    (400, {"error": {"code": "invalid_request", "message": INTERNAL_KEY}}),
    (422, {"error": {"code": "unknown", "message": INTERNAL_KEY}}),
    (503, {"error": {"code": "invalid_url", "message": INTERNAL_KEY}}),
    (422, {"error": INTERNAL_KEY}),
    (422, {"error": {"code": ["invalid_url"], "message": INTERNAL_KEY}}),
    (200, {"error": {"code": "invalid_url", "message": INTERNAL_KEY}}),
])
async def test_service_failures_are_not_misclassified_as_url_validation(status, payload):
    async with crawler(lambda _: httpx.Response(status, json=payload)) as client:
        with pytest.raises(CrawlerUnavailableError) as caught:
            await client.discover("example.com")
    assert caught.value.code == "crawler_unavailable"
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("exception", [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError])
async def test_transport_errors_are_safe_and_do_not_retry(exception, caplog):
    requests = []

    def respond(request):
        requests.append(request)
        raise exception("private source content " + INTERNAL_KEY, request=request)

    with caplog.at_level(logging.WARNING):
        async with crawler(respond) as client:
            with pytest.raises(CrawlerUnavailableError) as caught:
                await client.fetch("example.com")
    assert len(requests) == 1
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert INTERNAL_KEY not in str(caught.value)
    assert INTERNAL_KEY not in caplog.text


@pytest.mark.parametrize("payload", [
    None,
    [],
    {"url": "https://example.com/", "text": "Useful text"},
    {"url": "http://127.0.0.1/", "text": "Useful text", "links": []},
    {"url": "https://user:secret@example.com/", "text": "Useful text", "links": []},
    {"url": "file:///etc/passwd", "text": "Useful text", "links": []},
    {"url": "https://example.com/", "text": 3, "links": []},
    {"url": "https://example.com/", "text": " \n ", "links": []},
    {"url": "https://example.com/", "text": "Useful text", "links": [7]},
    {"url": "https://example.com/", "text": "Useful text", "links": "not-a-list"},
    {"url": "https://example.com/", "text": "x" * (MAX_PAGE_BYTES + 1), "links": []},
])
async def test_malformed_or_unsafe_fetch_response_is_unavailable(payload):
    async with crawler(lambda _: httpx.Response(200, json=payload)) as client:
        with pytest.raises(CrawlerUnavailableError):
            await client.fetch("example.com")


@pytest.mark.parametrize("urls", [
    [], ["example.com"], ["http://127.0.0.1/"], ["file:///etc/passwd"],
    ["https://example.com.evil.test/"], ["https://foreign.example/"], [7],
    ["https://example.com/", "https://example.com/"],
    [f"https://example.com/{index}" for index in range(101)],
])
async def test_discovery_rejects_unsafe_out_of_scope_or_unbounded_service_results(urls):
    async with crawler(lambda _: httpx.Response(200, json={"urls": urls})) as client:
        with pytest.raises(CrawlerUnavailableError):
            await client.discover("example.com")


@pytest.mark.parametrize(("headers", "body"), [
    ({"content-type": "text/html"}, b"<main>private source body</main>"),
    ({"content-type": "application/json"}, b"not-json " + INTERNAL_KEY.encode()),
    ({"content-type": "application/json", "content-encoding": "gzip"}, b"compressed"),
    ({"content-type": "application/json", "content-length": "invalid"}, b"{}"),
    ({"content-type": "application/json", "content-length": str(MAX_CRAWLER_RESPONSE_BYTES + 1)}, b"{}"),
])
async def test_invalid_service_response_headers_and_json_never_escape(headers, body):
    async with crawler(lambda _: httpx.Response(200, headers=headers, content=body)) as client:
        with pytest.raises(CrawlerUnavailableError) as caught:
            await client.fetch("example.com")
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(("status", "limit"), [(200, MAX_CRAWLER_RESPONSE_BYTES), (422, 4096)])
async def test_streaming_limit_stops_reading_and_closes_response(status, limit):
    yielded = 0
    closed = False

    class OversizedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal yielded
            for _ in range(limit // 65536 + 3):
                yielded += 1
                yield b" " * 65536

        async def aclose(self):
            nonlocal closed
            closed = True

    async with crawler(lambda _: httpx.Response(
        status, headers={"content-type": "application/json"}, stream=OversizedBody(),
    )) as client:
        with pytest.raises(CrawlerUnavailableError):
            await client.fetch("example.com")
    assert yielded == limit // 65536 + 1
    assert closed


@pytest.mark.parametrize("extra_headers", [
    {"content-encoding": "gzip"},
    {"content-length": str(MAX_CRAWLER_RESPONSE_BYTES + 1)},
])
async def test_unsupported_compression_and_oversized_headers_reject_before_reading(extra_headers):
    read = closed = False

    class UnreadBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal read
            read = True
            yield b"body must not be decoded or buffered"

        async def aclose(self):
            nonlocal closed
            closed = True

    async with crawler(lambda _: httpx.Response(
        200, headers={"content-type": "application/json", **extra_headers}, stream=UnreadBody(),
    )) as client:
        with pytest.raises(CrawlerUnavailableError):
            await client.fetch("example.com")
    assert not read
    assert closed


@pytest.mark.parametrize("operation", ["fetch", "discover"])
async def test_caller_cancellation_closes_pending_response_without_converting_to_failure(operation):
    started, closed = asyncio.Event(), asyncio.Event()

    class PendingBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            await asyncio.Event().wait()
            yield b"{}"

        async def aclose(self):
            closed.set()

    async with crawler(lambda _: httpx.Response(
        200, headers={"content-type": "application/json"}, stream=PendingBody(),
    )) as client:
        task = asyncio.create_task(getattr(client, operation)("example.com"))
        try:
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert closed.is_set()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_total_deadline_cancels_pending_outgoing_request(monkeypatch):
    started, cancelled = asyncio.Event(), asyncio.Event()
    scopes = []
    original_timeout = asyncio.timeout

    def capture(delay):
        scope = original_timeout(delay)
        scopes.append((delay, scope))
        return scope

    async def respond(request):
        started.set()
        try:
            await asyncio.Event().wait()
            raise AssertionError("crawler request must be cancelled at its deadline")
        finally:
            cancelled.set()

    monkeypatch.setattr("rag.webpages.fetch.asyncio.timeout", capture)
    async with crawler(respond) as client:
        task = asyncio.create_task(client.discover("example.com"))
        try:
            await asyncio.wait_for(started.wait(), 1)
            scopes[0][1].reschedule(asyncio.get_running_loop().time())
            with pytest.raises(CrawlerUnavailableError) as caught:
                await asyncio.wait_for(task, 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert cancelled.is_set()
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("fail", [False, True])
async def test_shared_crawler_resource_closes_transport_on_normal_and_failed_exit(monkeypatch, fail):
    class TrackingTransport(httpx.MockTransport):
        closed = False

        async def aclose(self):
            self.closed = True

    transport = TrackingTransport(lambda _: httpx.Response(200, json={
        "url": "https://example.com/", "text": "Rendered product content", "links": [],
    }))
    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "rag.webpages.fetch.httpx.AsyncClient",
        lambda **kwargs: async_client(transport=transport, **kwargs),
    )

    class RuntimeStopped(Exception):
        pass

    try:
        async with open_crawler_client(
            base_url=SERVICE_URL, internal_server_key=INTERNAL_KEY,
        ) as client:
            await client.fetch("example.com")
            assert not transport.closed
            if fail:
                raise RuntimeStopped
    except RuntimeStopped:
        assert fail
    assert transport.closed
