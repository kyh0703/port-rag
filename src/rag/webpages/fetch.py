"""Authenticated, bounded HTTP boundary to the standalone JavaScript crawler."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx

from rag.security.internal_server import validate_internal_server_key
from rag.webpages.types import CrawlerUnavailableError, WebpageFetchError, WebpagePage
from rag.webpages.urls import MAX_URLS, normalize_url, validate_crawler_url

MAX_PAGE_BYTES = 2 * 1024 * 1024
# Allow JSON escaping of a full page while bounding both transfer and parsing memory.
MAX_CRAWLER_RESPONSE_BYTES = 16 * 1024 * 1024
CRAWLER_TIMEOUT = 35.0
_MAX_ERROR_RESPONSE_BYTES = 4096
_SOURCE_ERRORS = {
    "invalid_url": "invalid public HTTP webpage URL",
    "unsafe_address": "webpage address must be public",
    "upstream_error": "webpage fetch failed",
    "invalid_content": "webpage must return HTML",
    "too_large": "webpage is too large",
    "too_many_redirects": "webpage has too many redirects",
    "no_readable_text": "webpage has no readable text",
}


class CrawlerClient:
    """Share a service-only HTTP client; never connect to the requested webpage."""

    def __init__(self, *, base_url: str, internal_server_key: str,
                 client: httpx.AsyncClient) -> None:
        self._base_url = validate_crawler_url(base_url)
        self._internal_server_key = validate_internal_server_key(internal_server_key)
        self._client = client

    async def fetch(self, url: str) -> WebpagePage:
        payload = await self._request("/v1/fetch", normalize_url(url))
        try:
            final_url = _response_url(payload.get("url"))
            text, links = payload.get("text"), payload.get("links")
            if (not isinstance(text, str) or not text.strip()
                    or len(text.encode("utf-8")) > MAX_PAGE_BYTES
                    or not isinstance(links, list) or len(links) > 10000
                    or any(not isinstance(link, str) for link in links)):
                raise ValueError
            return WebpagePage(url=final_url, text=text, links=links)
        except ValueError:
            pass
        raise CrawlerUnavailableError()

    async def discover(self, url: str) -> list[str]:
        url = normalize_url(url)
        payload = await self._request("/v1/discover", url)
        try:
            urls = payload.get("urls")
            if not isinstance(urls, list) or not 1 <= len(urls) <= MAX_URLS:
                raise ValueError
            root_host = urlsplit(url).hostname
            seen: set[str] = set()
            for candidate in urls:
                candidate = _response_url(candidate)
                host = urlsplit(candidate).hostname
                if (host != root_host and not host.endswith(f".{root_host}")) or candidate in seen:
                    raise ValueError
                seen.add(candidate)
            return urls
        except ValueError:
            pass
        raise CrawlerUnavailableError()

    async def _request(self, path: str, url: str) -> dict[str, object]:
        failure: WebpageFetchError = CrawlerUnavailableError()
        try:
            async with asyncio.timeout(CRAWLER_TIMEOUT):
                async with self._client.stream(
                    "POST", self._base_url + path,
                    json={"url": url},
                    headers={"X-Internal-Server": self._internal_server_key,
                             "Accept": "application/json", "Accept-Encoding": "identity"},
                    auth=None, follow_redirects=False, timeout=CRAWLER_TIMEOUT,
                ) as response:
                    # Service redirects must never forward the shared key to a source page.
                    if response.status_code not in {200, 422}:
                        raise ValueError
                    limit = (MAX_CRAWLER_RESPONSE_BYTES if response.status_code == 200
                             else _MAX_ERROR_RESPONSE_BYTES)
                    payload = await _read_payload(response, limit)
                    if response.status_code == 200:
                        return payload
                    error = payload.get("error")
                    code = error.get("code") if isinstance(error, dict) else None
                    if isinstance(code, str) and code in _SOURCE_ERRORS:
                        failure = WebpageFetchError(_SOURCE_ERRORS[code], code=code)
        except (httpx.HTTPError, OSError, TimeoutError, ValueError, RuntimeError):
            pass
        # Upstream exceptions can carry request URLs, bodies and authentication headers.
        # Raise only after leaving handlers, without retaining their exception context.
        raise failure


def _response_url(value: object) -> str:
    if (not isinstance(value, str) or not value.startswith(("http://", "https://"))
            or normalize_url(value) != value):
        raise ValueError
    return value


async def _read_payload(response: httpx.Response, max_bytes: int) -> dict[str, object]:
    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if (media_type != "application/json"
            or response.headers.get("content-encoding", "identity").lower() != "identity"):
        raise ValueError
    size = response.headers.get("content-length")
    if size is not None and (not size.isascii() or not size.isdecimal() or int(size) > max_bytes):
        raise ValueError
    body = bytearray()
    # Raw streaming prevents a hostile/incorrect service response from decompressing
    # before the byte limit. Pre-buffered responses occur in in-process transports.
    stream = (response.aiter_bytes(chunk_size=65536) if response.is_stream_consumed
              else response.aiter_raw(chunk_size=65536))
    async for part in stream:
        if len(body) + len(part) > max_bytes:
            raise ValueError
        body.extend(part)
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError
    return payload


@asynccontextmanager
async def open_crawler_client(*, base_url: str, internal_server_key: str) -> AsyncIterator[CrawlerClient]:
    """Close the shared pool after HTTP serving and all webpage workers have stopped."""
    async with httpx.AsyncClient(
        trust_env=False, follow_redirects=False, timeout=CRAWLER_TIMEOUT,
    ) as client:
        yield CrawlerClient(base_url=base_url, internal_server_key=internal_server_key, client=client)
