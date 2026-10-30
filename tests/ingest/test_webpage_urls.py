"""Registration URL normalization remains local; collection safety belongs to the crawler."""

import pytest

from rag.webpages.types import WebpageFetchError
from rag.webpages.urls import normalize_url


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "http://user:secret@example.com/", "http://127.0.0.1/",
    "http://[::1]/", "http://169.254.169.254/", "http://10.0.0.1/",
    "http://[::ffff:127.0.0.1]/", "https://example.com:22/", "https://example.com/\nsecret",
])
def test_rejects_unsafe_registration_urls(url):
    with pytest.raises(WebpageFetchError):
        normalize_url(url)


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
