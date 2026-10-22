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


@pytest.mark.parametrize(("opening", "closing"), [
    ("<main>", "</main>"),
    ('<div role="main">', "</div>"),
])
def test_prefers_main_content_without_losing_its_headings(opening, closing):
    page = parse_html("https://example.com/", f"""
        <body><p>Shared promotion before the content</p>
        {opening}
            <header><h1>Product guide</h1></header>
            <article><header><h2>Installation</h2></header><p>Install the product.</p></article>
        {closing}
        <p>Shared promotion after the content</p></body>
    """)
    assert page.text == "Product guide\nInstallation\nInstall the product."


def test_body_fallback_removes_site_chrome_but_preserves_article_and_section_headers():
    page = parse_html("https://example.com/", """
        <body><header><a href="/">Kr</a><a href="/en/">En</a></header>
        <div role="banner">Company slogan</div>
        <div role="navigation"><a href="/products">Global products menu</a></div>
        <article><header><h1>Release notes</h1><p>Published today</p></header>
            <section><header><h2>New features</h2></header><p>Useful product details.</p></section>
        </article>
        <footer>Shared copyright</footer><div role="contentinfo">Site contact information</div>
        </body>
    """)
    assert page.text == "Release notes\nPublished today\nNew features\nUseful product details."


@pytest.mark.parametrize("role", ["navigation", "banner", "contentinfo"])
def test_main_content_excludes_explicit_site_landmarks(role):
    page = parse_html("https://example.com/", f"""
        <main><h1>Useful guide</h1><div role="{role}"><p>Shared site information</p></div>
        <p>Actual instructions</p></main>
    """)
    assert page.text == "Useful guide\nActual instructions"


@pytest.mark.parametrize("attributes", [
    "hidden",
    'aria-hidden="true"',
    'aria-hidden=" TRUE "',
    'style="display:none"',
    'style="color: red; DISPLAY : NoNe !important; width: 10px"',
    'style="visibility: hidden;"',
    'style="color: red; VISIBILITY : HIDDEN !important"',
])
def test_hidden_subtrees_never_contribute_text_or_displace_body_fallback(attributes):
    page = parse_html("https://example.com/", f"""
        <body><div {attributes}><main><h1>Hidden duplicate</h1><p>Hidden text</p></main></div>
        <article><h1>Visible guide</h1><p>Visible details</p></article></body>
    """)
    assert page.text == "Visible guide\nVisible details"


@pytest.mark.parametrize("html", [
    "<head><title>Not content</title></head><header>Kr En</header><footer>Copyright</footer>",
    '<head><title>Not content</title></head><main><div style="display:none">Hidden</div></main>',
    "<body><p>Shared promotion</p><main><nav>Menu only</nav></main></body>",
])
def test_no_readable_content_raises_without_metadata_or_chrome_fallback(html):
    with pytest.raises(WebpageFetchError, match="readable"):
        parse_html("https://example.com/", html)


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


def test_discovery_keeps_real_navigation_links_without_template_or_script_links():
    page = parse_html("https://example.com/", '''
        <header><nav><a href="/docs">Navigation</a></nav><a href="/en/">En</a>
            <div hidden><nav><a href="/expanded">Collapsed navigation</a></nav></div>
        </header>
        <div role="navigation"><a href="/products">Products</a></div>
        <template><nav><a href="/fake-template">Fake</a></nav></template>
        <script type="text/template"><a href="/fake-script">Fake</a></script>
        <main>Actual page content</main><footer><a href="/contact">Contact</a></footer>
    ''')
    assert page.text == "Actual page content"
    assert page.links == ["/docs", "/en/", "/expanded", "/products", "/contact"]


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


@pytest.mark.parametrize(("value", "expected"), [
    (" example.com./list?reqPageNo=3&sig=a%2fb%2Bc#top ",
     "https://example.com/list?reqPageNo=3&sig=a%2fb%2Bc"),
    ("HTTPS://EXAMPLE.COM...:443", "https://example.com/"),
    ("example.com:443/a", "https://example.com/a"),
    ("http://example.com:80/a", "http://example.com/a"),
    ("http://example.com:443/a", "http://example.com:443/a"),
    ("https://example.com:80/a", "https://example.com:80/a"),
    ("müller.de/안내?q=한글&sig=%2f%2F&key=1&key=2",
     "https://xn--mller-kva.de/안내?q=한글&sig=%2f%2F&key=1&key=2"),
    ("faß.de/", "https://xn--fa-hia.de/"),
    ("cafe\u0301.fr/", "https://xn--caf-dma.fr/"),
    ("example.com/?q=one+two&empty=&q=%2B#section",
     "https://example.com/?q=one+two&empty=&q=%2B"),
    ("example.com/?#top", "https://example.com/"),
    ("[2606:4700:4700:0000:0000:0000:0000:1111]",
     "https://[2606:4700:4700::1111]/"),
])
def test_normalizes_only_authority_without_rewriting_path_or_query(value, expected):
    assert normalize_url(value) == expected
    assert normalize_url(expected) == expected


@pytest.mark.parametrize("value", [
    "\texample.com", "example.com\n", "example.com/\x00", "example.com/\x7f",
    "example.com/\x85", "example.com/a b", "example.com\\private",
    "//example.com", "http:/example.com", "https:example.com", "example",
    "https://@example.com/", "https://user@example.com/", "https://%65xample.com/",
    "https://127.0.0.1./", "127.0.0.1", "https://[64:ff9b::7f00:1]/",
    "https://[2002:7f00:1::]/", "example.com:8080", "https://example.com/\ud800",
])
def test_normalization_does_not_repair_unsafe_or_ambiguous_input(value):
    with pytest.raises(WebpageFetchError):
        normalize_url(value)


def test_url_limit_counts_canonical_utf8_bytes_not_characters_or_discarded_fragment():
    prefix = "https://example.com/"
    remaining = 4096 - len(prefix)
    exact = prefix + "가" * (remaining // 3) + "a" * (remaining % 3)
    assert len(exact.encode("utf-8")) == 4096
    assert normalize_url(" " + exact + "#ignored" * 100 + " ") == exact
    with pytest.raises(WebpageFetchError):
        normalize_url(exact + "a")
    with pytest.raises(WebpageFetchError):
        normalize_url("example.com/" + "가" * 1360)


def test_url_limit_applies_after_idna_expansion():
    prefix = "https://xn--fa-hia.de/"
    exact = "faß.de/" + "a" * (4096 - len(prefix))
    assert normalize_url(exact) == prefix + "a" * (4096 - len(prefix))
    with pytest.raises(WebpageFetchError):
        normalize_url(exact + "a")


@pytest.mark.parametrize("dispositions", [
    ["attachment"], ['AtTaChMeNt ; filename="guide.html"'],
    ['attachment; filename*=UTF-8\'\'guide.html'],
    ["inline", 'attachment; filename="guide.html"'],
])
async def test_rejects_html_attachments_before_reading_body(monkeypatch, dispositions):
    monkeypatch.setattr("rag.webpages.fetch.resolve_addresses", AsyncMock(return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
    ]))
    read_body = False

    class AttachmentStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal read_body
            read_body = True
            yield b"<main>This is a downloadable HTML file</main>"

    headers = [("content-type", "text/html")]
    headers.extend(("content-disposition", value) for value in dispositions)
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, headers=headers, stream=AttachmentStream(),
    ))
    with pytest.raises(WebpageFetchError, match="attachment"):
        await SafeWebpageFetcher(transport=transport).fetch("https://example.com/")
    assert not read_body


async def test_inline_html_is_still_readable(monkeypatch):
    monkeypatch.setattr("rag.webpages.fetch.resolve_addresses", AsyncMock(return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
    ]))
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "text/html", "content-disposition": "inline"},
        content=b"<p>Read me</p>",
    ))
    assert (await SafeWebpageFetcher(transport=transport).fetch("example.com")).text == "Read me"


@pytest.mark.parametrize("location", [
    "http://127.0.0.1/", "http://[::1]/", "https://example.com:8080/",
    "file:///etc/passwd", "https://user:secret@example.com/",
    "https://example.com/\nunsafe", "https://example.com\\@evil.test/",
])
async def test_redirect_cannot_bypass_url_safety(monkeypatch, location):
    resolver = AsyncMock(return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
    ])
    monkeypatch.setattr("rag.webpages.fetch.resolve_addresses", resolver)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": location})

    with pytest.raises(WebpageFetchError):
        await SafeWebpageFetcher(transport=httpx.MockTransport(respond)).fetch("example.com")
    assert len(requests) == 1
    assert resolver.await_count == 1


@pytest.mark.parametrize("redirects", [5, 6])
async def test_redirect_limit_and_every_hop_are_still_dns_pinned(monkeypatch, redirects):
    resolver = AsyncMock(return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
    ])
    monkeypatch.setattr("rag.webpages.fetch.resolve_addresses", resolver)
    requests = []

    def respond(request):
        requests.append(request)
        hop = int(request.url.path.removeprefix("/"))
        if hop < redirects:
            return httpx.Response(302, headers={"location": f"https://hop.example.com/{hop + 1}"})
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<p>Done</p>")

    fetcher = SafeWebpageFetcher(transport=httpx.MockTransport(respond))
    if redirects == 5:
        assert (await fetcher.fetch("https://example.com/0")).text == "Done"
    else:
        with pytest.raises(WebpageFetchError, match="redirects"):
            await fetcher.fetch("https://example.com/0")
    assert len(requests) == resolver.await_count == 6
    assert all(request.url.host == "93.184.216.34" for request in requests)
    assert requests[0].headers["host"] == "example.com"
    assert all(request.headers["host"] == "hop.example.com" for request in requests[1:])
    assert all(request.extensions["sni_hostname"] == "hop.example.com" for request in requests[1:])
