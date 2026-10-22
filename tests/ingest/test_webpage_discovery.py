from __future__ import annotations

import asyncio
import socket
from unittest.mock import AsyncMock

import httpx
import pytest

from rag.webpages.fetch import SafeWebpageFetcher, WebpageFetchError


@pytest.fixture
def public_dns(monkeypatch):
    resolver = AsyncMock(return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
    ])
    monkeypatch.setattr("rag.webpages.fetch.resolve_addresses", resolver)
    return resolver


@pytest.fixture
def deadlines(monkeypatch):
    """Expire real asyncio deadlines by events, not elapsed wall-clock sleeps."""
    original_timeout = asyncio.timeout
    scopes = []

    def capture(delay):
        scope = original_timeout(delay)
        scopes.append((delay, scope))
        return scope

    monkeypatch.setattr("rag.webpages.fetch.asyncio.timeout", capture)
    return scopes


def html(body):
    return httpx.Response(200, headers={"content-type": "text/html"}, text=body)


async def test_discovery_follows_real_site_navigation_but_never_inert_template_links(public_dns):
    requested = []

    def respond(request):
        requested.append(request.url.path)
        if request.url.path == "/":
            return html("""
                <header><nav><a href="/guide">Guide</a></nav><a href="/en/">En</a>
                    <div hidden><a href="/products">Collapsed menu</a></div></header>
                <div role="navigation"><a href="/support">Support</a></div>
                <template><a href="/fake-template">Fake</a></template>
                <script type="text/template"><a href="/fake-script">Fake</a></script>
                <main><h1>Root content</h1></main>
            """)
        return html("<main>Readable destination</main>")

    urls = await SafeWebpageFetcher(transport=httpx.MockTransport(respond)).discover("example.com")
    assert urls == [
        "https://example.com/", "https://example.com/guide", "https://example.com/en/",
        "https://example.com/products", "https://example.com/support",
    ]
    assert requested == ["/", "/guide", "/en/", "/products", "/support"]


async def test_discovery_verifies_each_candidate_and_isolates_failures(public_dns):
    requested = []
    candidates = ["missing", "offline", "pdf", "empty", "attachment", "good", "rebound"]

    async def resolve(host, port):
        if host == "private.example.com":
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", port, 0, 0)),
            ]
        return public_dns.return_value

    public_dns.side_effect = resolve

    def respond(request):
        path = request.url.path
        requested.append(path)
        if path == "/":
            return html("<main>Root</main>" + "".join(
                f'<a href="/{name}">{name}</a>' for name in candidates
            ) + '<a href="https://docs.example.com/guide">Guide</a>')
        if path == "/missing":
            return httpx.Response(404, headers={"content-type": "text/html"}, text="Missing")
        if path == "/offline":
            raise httpx.ConnectError("unavailable", request=request)
        if path == "/pdf":
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")
        if path == "/empty":
            return html("<head><title>Metadata</title></head><script>render()</script>")
        if path == "/attachment":
            return httpx.Response(200, headers={
                "content-type": "text/html", "content-disposition": "attachment; filename=page.html",
            }, text="<main>Download only</main>")
        if path == "/rebound":
            return httpx.Response(302, headers={"location": "https://private.example.com/blocked"})
        return html('<main>Readable</main><a href="/not-a-root-anchor">Do not crawl</a>')

    urls = await SafeWebpageFetcher(transport=httpx.MockTransport(respond)).discover("example.com")
    assert urls == ["https://example.com/", "https://example.com/good", "https://docs.example.com/guide"]
    assert set(requested) == {"/", "/guide", *(f"/{name}" for name in candidates)}
    assert len(requested) == 9
    assert any(call.args[0] == "private.example.com" for call in public_dns.await_args_list)


async def test_discovery_skips_oversized_or_unsafe_links_without_poisoning_root(public_dns):
    requested = []

    def respond(request):
        requested.append(request.url.path)
        if request.url.path == "/":
            return html('<main>Root</main>'
                        + f'<a href="/{"가" * 1400}">Too long</a>'
                        + '<a href="/unsafe&#10;path">Control</a>'
                        + '<a href="/good?reqPageNo=1&amp;sig=%2f%2F&amp;k=1&amp;k=2#first">One</a>'
                        + '<a href="/good?reqPageNo=1&amp;sig=%2f%2F&amp;k=1&amp;k=2#again">Duplicate</a>'
                        + '<a href="/good?reqPageNo=2">Two</a>')
        return html("<main>Readable page</main>")

    urls = await SafeWebpageFetcher(transport=httpx.MockTransport(respond)).discover("example.com")
    assert urls == [
        "https://example.com/",
        "https://example.com/good?reqPageNo=1&sig=%2f%2F&k=1&k=2",
        "https://example.com/good?reqPageNo=2",
    ]
    assert requested == ["/", "/good", "/good"]


async def test_discovery_uses_redirected_root_as_link_base_without_broadening_host_scope(public_dns):
    requested = []

    def respond(request):
        requested.append((request.headers["host"], request.url.path))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "https://docs.example.com/manual/"})
        if request.url.path == "/manual/":
            return html('''<main>Manual</main><a href="chapter">Chapter</a>
                <a href="https://example.com/help">Help</a>
                <a href="https://example.com.evil.test/trap">Wrong host</a>
                <a href="https://other.test/foreign">Other domain</a>''')
        return html('<main>Readable</main><a href="/child-only">No recursion</a>')

    urls = await SafeWebpageFetcher(transport=httpx.MockTransport(respond)).discover(
        "https://example.com/start",
    )
    assert urls == [
        "https://example.com/start", "https://docs.example.com/manual/chapter", "https://example.com/help",
    ]
    assert requested == [
        ("example.com", "/start"), ("docs.example.com", "/manual/"),
        ("docs.example.com", "/manual/chapter"), ("example.com", "/help"),
    ]


@pytest.mark.parametrize("fail_every", [0, 10])
async def test_discovery_caps_verifications_at_100_even_when_candidates_fail(public_dns, fail_every):
    requested = []

    def respond(request):
        path = request.url.path
        requested.append(path)
        if path == "/":
            return html("<main>Root</main>" + "".join(
                f'<a href="/page-{index}">Page {index}</a>' for index in range(120)
            ))
        index = int(path.removeprefix("/page-"))
        if fail_every and index % fail_every == 0:
            return httpx.Response(404)
        return html("<main>Readable page</main>")

    urls = await SafeWebpageFetcher(transport=httpx.MockTransport(respond)).discover("example.com")
    assert requested == ["/"] + [f"/page-{index}" for index in range(99)]
    assert urls == ["https://example.com/"] + [
        f"https://example.com/page-{index}" for index in range(99)
        if not fail_every or index % fail_every
    ]
    assert len(urls) <= 100


async def test_discovery_is_bounded_to_four_and_keeps_input_order_when_completion_reverses(public_dns):
    gates = {index: asyncio.Event() for index in range(1, 10)}
    for index in range(5, 10):
        gates[index].set()
    started = asyncio.Queue()
    completed = asyncio.Queue()
    active = peak = 0
    completion_order = []

    async def respond(request):
        nonlocal active, peak
        if request.url.path == "/":
            return html("<main>Root</main>" + "".join(
                f'<a href="/{index}">Page {index}</a>' for index in range(1, 10)
            ))
        index = int(request.url.path[1:])
        active += 1
        peak = max(peak, active)
        started.put_nowait(index)
        try:
            await gates[index].wait()
            completion_order.append(index)
            completed.put_nowait(index)
            return html('<main>Readable</main><a href="/child-only">No recursion</a>')
        finally:
            active -= 1

    task = asyncio.create_task(SafeWebpageFetcher(transport=httpx.MockTransport(respond)).discover(
        "example.com",
    ))
    try:
        initial = [await asyncio.wait_for(started.get(), 1) for _ in range(4)]
        assert initial == [1, 2, 3, 4]
        assert active == 4
        gates[4].set()
        for index in range(4, 10):
            assert await asyncio.wait_for(completed.get(), 1) == index
        for index in (3, 2, 1):
            gates[index].set()
            assert await asyncio.wait_for(completed.get(), 1) == index
        urls = await asyncio.wait_for(task, 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert peak == 4
    assert completion_order == [4, 5, 6, 7, 8, 9, 3, 2, 1]
    assert urls == ["https://example.com/"] + [f"https://example.com/{index}" for index in range(1, 10)]


async def test_overall_deadline_returns_only_verified_pages_and_drains_pending_fetches(public_dns, deadlines):
    all_pending = asyncio.Event()
    pending = set()
    closed = set()
    requested = []

    class PendingBody(httpx.AsyncByteStream):
        def __init__(self, path):
            self.path = path

        async def __aiter__(self):
            pending.add(self.path)
            if len(pending) == 4:
                all_pending.set()
            await asyncio.Event().wait()
            yield b"<main>Not verified before the deadline</main>"

        async def aclose(self):
            closed.add(self.path)

    def respond(request):
        path = request.url.path
        requested.append(path)
        if path == "/":
            return html('''<main>Root</main><a href="/good">Good</a>
                <a href="/slow-1">Slow</a><a href="/slow-2">Slow</a>
                <a href="/slow-3">Slow</a><a href="/slow-4">Slow</a><a href="/never">Queued</a>''')
        if path == "/good":
            return html("<main>Confirmed</main>")
        return httpx.Response(200, headers={"content-type": "text/html"}, stream=PendingBody(path))

    task = asyncio.create_task(SafeWebpageFetcher(transport=httpx.MockTransport(respond)).discover(
        "example.com",
    ))
    try:
        await asyncio.wait_for(all_pending.wait(), 1)
        overall = [scope for delay, scope in deadlines if delay == 30.0]
        assert len(overall) == 1
        overall[0].reschedule(asyncio.get_running_loop().time())
        assert await asyncio.wait_for(task, 1) == ["https://example.com/", "https://example.com/good"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert closed == pending == {f"/slow-{index}" for index in range(1, 5)}
    assert "/never" not in requested


async def test_overall_deadline_already_covers_root_and_root_timeout_is_explicit(public_dns, deadlines):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def respond(request):
        started.set()
        try:
            await asyncio.Event().wait()
            raise AssertionError("the root request must be cancelled by its deadline")
        finally:
            cancelled.set()

    task = asyncio.create_task(SafeWebpageFetcher(transport=httpx.MockTransport(respond)).discover(
        "example.com",
    ))
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert [delay for delay, _ in deadlines] == [30.0, 20.0]
        deadlines[0][1].reschedule(asyncio.get_running_loop().time())
        with pytest.raises(WebpageFetchError, match="timed out"):
            await asyncio.wait_for(task, 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert cancelled.is_set()


@pytest.mark.parametrize(("status", "headers", "body"), [
    (404, {"content-type": "text/html"}, "<p>Not found</p>"),
    (200, {"content-type": "application/pdf"}, "%PDF"),
    (200, {"content-type": "text/html"}, "<script>render()</script>"),
    (200, {"content-type": "text/html", "content-disposition": "attachment"}, "<p>File</p>"),
])
async def test_root_failure_raises_instead_of_returning_an_unverified_root(public_dns, status, headers, body):
    transport = httpx.MockTransport(lambda request: httpx.Response(status, headers=headers, text=body))
    with pytest.raises(WebpageFetchError):
        await SafeWebpageFetcher(transport=transport).discover("example.com")
    assert public_dns.await_count == 1


async def test_twenty_second_fetch_deadline_includes_dns(monkeypatch, deadlines):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def resolve(host, port):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr("rag.webpages.fetch.resolve_addresses", resolve)
    task = asyncio.create_task(SafeWebpageFetcher().fetch("https://example.com/"))
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert [delay for delay, _ in deadlines] == [20.0]
        deadlines[0][1].reschedule(asyncio.get_running_loop().time())
        with pytest.raises(WebpageFetchError, match="timed out"):
            await asyncio.wait_for(task, 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert cancelled.is_set()
