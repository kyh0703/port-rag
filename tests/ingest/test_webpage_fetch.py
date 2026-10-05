from __future__ import annotations

import socket
import gzip
from unittest.mock import AsyncMock

import httpx
import pytest

from rag.webpages.fetch import SafeWebpageFetcher, WebpageFetchError, normalize_url, parse_html


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "http://user:secret@example.com/", "http://127.0.0.1/",
    "http://[::1]/", "http://169.254.169.254/", "http://10.0.0.1/",
    "http://[::ffff:127.0.0.1]/", "https://example.com:22/", "https://example.com/\nsecret",
])
def test_rejects_unsafe_urls(url):
    with pytest.raises(WebpageFetchError):
        normalize_url(url)


def test_extracts_server_html_without_script_style_navigation_or_metadata():
    page = parse_html("https://tryvox.com/", """
    <html><head><title>Not body</title><meta name="description" content="fake body"></head>
    <body><nav>Navigation only</nav><script>window.data = 'secret';</script>
    <style>.hidden {color:red}</style><main><h1>Voice agents</h1>
    <p>Real server-rendered product information.</p><a href="/pricing">Pricing</a></main>
    <div hidden>Hidden text</div></body></html>
    """)
    assert page.text == "Voice agents\nReal server-rendered product information.\nPricing"
    assert "/pricing" in page.links
    with pytest.raises(WebpageFetchError, match="text"):
        parse_html("https://example.com/", "<head><title>Metadata only</title></head><script>app()</script>")


async def test_pins_validated_address_preserving_host_and_tls_hostname(monkeypatch):
    resolver = AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    monkeypatch.setattr("rag.webpages.fetch.resolve_addresses", resolver)
    seen = []

    async def respond(request):
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<p>Public content</p>")

    page = await SafeWebpageFetcher(transport=httpx.MockTransport(respond)).fetch("https://example.com/page")
    assert page.text == "Public content"
    assert seen[0].url.host == "93.184.216.34"
    assert seen[0].headers["host"] == "example.com"
    assert seen[0].extensions["sni_hostname"] == "example.com"
    assert resolver.await_count == 1


async def test_revalidates_redirect_dns_and_never_connects_private_address(monkeypatch):
    resolver = AsyncMock(side_effect=[
        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))],
    ])
    monkeypatch.setattr("rag.webpages.fetch.resolve_addresses", resolver)
    calls = []

    async def respond(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://redirect.example.com/"})

    with pytest.raises(WebpageFetchError, match="public"):
        await SafeWebpageFetcher(transport=httpx.MockTransport(respond)).fetch("https://example.com/")
    assert len(calls) == 1


async def test_rejects_mixed_public_private_dns_and_oversized_html(monkeypatch):
    resolver = AsyncMock(return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.2", 443)),
    ])
    monkeypatch.setattr("rag.webpages.fetch.resolve_addresses", resolver)
    with pytest.raises(WebpageFetchError, match="public"):
        await SafeWebpageFetcher().fetch("https://example.com/")
    resolver.return_value = resolver.return_value[:1]
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "text/html"}, content=b"x" * 33,
    ))
    with pytest.raises(WebpageFetchError, match="large"):
        await SafeWebpageFetcher(transport=transport, max_bytes=32).fetch("https://example.com/")


async def test_discovery_returns_only_unique_same_host_or_subdomain_links(monkeypatch):
    monkeypatch.setattr("rag.webpages.fetch.resolve_addresses", AsyncMock(return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
    ]))
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "text/html"}, content=b'''<p>Discover pages</p>
        <a href="/pricing#top">Pricing</a><a href="/pricing">Pricing again</a>
        <a href="https://docs.example.com/guide">Docs</a>
        <a href="/llms.txt">Text index</a><a href="/brochure.PDF">Download</a>
        <a href="/sitemap.xml">Sitemap</a><a href="/guide.html">HTML guide</a>
        <a href="https://example.com.evil.test/">External</a>
        <a href="http://127.0.0.1/">Private</a>''',
    ))
    assert await SafeWebpageFetcher(transport=transport).discover("https://example.com/") == [
        "https://example.com/", "https://example.com/pricing", "https://docs.example.com/guide",
        "https://example.com/guide.html",
    ]


def test_discovery_keeps_navigation_links_but_not_navigation_or_template_text():
    page = parse_html("https://example.com/", '''
        <nav><a href="/docs">Navigation</a></nav>
        <template><a href="/fake">Fake</a></template><main>Actual page content</main>
    ''')
    assert page.text == "Actual page content"
    assert page.links == ["/docs"]


async def test_gzip_decoding_is_bounded_before_allocation(monkeypatch):
    monkeypatch.setattr("rag.webpages.fetch.resolve_addresses", AsyncMock(return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
    ]))

    class CompressedStream(httpx.AsyncByteStream):
        def __init__(self, body):
            self.body = gzip.compress(body)

        async def __aiter__(self):
            yield self.body

    def transport(body):
        return httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"content-type": "text/html", "content-encoding": "gzip"},
            stream=CompressedStream(body),
        ))

    page = await SafeWebpageFetcher(transport=transport(b"<p>Useful body</p>")).fetch(
        "https://example.com/",
    )
    assert page.text == "Useful body"
    with pytest.raises(WebpageFetchError, match="large"):
        await SafeWebpageFetcher(transport=transport(b"x" * 100000), max_bytes=256).fetch(
            "https://example.com/",
        )
