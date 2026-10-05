"""Bounded public HTTP fetching with DNS pinning and plain server-HTML extraction."""

from __future__ import annotations

import asyncio
import ipaddress
import mimetypes
import re
import socket
import zlib
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

MAX_URLS = 100
MAX_PAGE_BYTES = 2 * 1024 * 1024
FETCH_TIMEOUT = 20.0
_NAT64_NETWORKS = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))


class WebpageFetchError(ValueError):
    """A public page cannot be fetched safely or has no usable server HTML."""


@dataclass(frozen=True)
class WebpagePage:
    url: str
    text: str
    links: list[str]


def _public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    # Reject transition/mapped forms rather than letting a transport reinterpret them.
    return bool(address.is_global and not address.is_multicast and not address.is_reserved
                and not getattr(address, "ipv4_mapped", None)
                and not getattr(address, "sixtofour", None)
                and not getattr(address, "teredo", None)
                and not any(address in network for network in _NAT64_NETWORKS))


def normalize_url(url: str) -> str:
    if not url or len(url) > 4096 or any(ord(char) < 33 or ord(char) == 127 for char in url):
        raise WebpageFetchError("invalid webpage URL")
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError
        if parsed.username is not None or parsed.password is not None or "\\" in url:
            raise ValueError
        hostname = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        if "%" in hostname or not hostname:
            raise ValueError
        port = parsed.port
        if port is not None and port not in {80, 443}:
            raise ValueError
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", hostname):
                raise ValueError
        else:
            if not _public_address(hostname):
                raise WebpageFetchError("webpage address must be public")
        authority = f"[{hostname}]" if ":" in hostname else hostname
        if port is not None and port != (443 if parsed.scheme == "https" else 80):
            authority += f":{port}"
        return urlunsplit((parsed.scheme, authority, parsed.path or "/", parsed.query, ""))
    except (UnicodeError, ValueError) as exc:
        if isinstance(exc, WebpageFetchError):
            raise
        raise WebpageFetchError("invalid public HTTP webpage URL") from exc


async def resolve_addresses(host: str, port: int) -> list:
    return await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)


class _TextParser(HTMLParser):
    _ignored = {"head", "title", "script", "style", "nav", "noscript", "template", "svg", "canvas"}
    _blocks = {"p", "div", "section", "article", "main", "h1", "h2", "h3", "h4", "h5", "h6",
               "br", "li", "ul", "ol", "tr", "table", "blockquote", "pre", "hr"}
    _void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
             "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.links: list[str] = []
        self.hidden_stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        # Navigation text is excluded, but real navigation links remain discoverable.
        if (tag == "a" and attributes.get("href") and len(self.links) < 10000
                and not any(parent in self._ignored - {"nav"} for parent in self.hidden_stack)):
            self.links.append(attributes["href"])
        hidden = (tag in self._ignored or "hidden" in attributes
                  or attributes.get("aria-hidden", "").lower() == "true")
        if self.hidden_stack or hidden:
            if tag not in self._void:
                if len(self.hidden_stack) >= 256:
                    raise WebpageFetchError("webpage HTML nesting is too deep")
                self.hidden_stack.append(tag)
            return
        if tag in self._blocks:
            self.text.append("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self._void:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.hidden_stack:
            if tag in self.hidden_stack:
                index = len(self.hidden_stack) - 1 - self.hidden_stack[::-1].index(tag)
                del self.hidden_stack[index:]
            return
        if tag in self._blocks:
            self.text.append("\n")

    def handle_data(self, data):
        if not self.hidden_stack:
            self.text.append(data.replace("\x00", ""))


def parse_html(url: str, html: str) -> WebpagePage:
    parser = _TextParser()
    parser.feed(html)
    parser.close()
    text = "\n".join(line for raw in "".join(parser.text).splitlines()
                     if (line := " ".join(raw.split())))
    if not text:
        raise WebpageFetchError("webpage has no readable server-rendered text")
    return WebpagePage(url=url, text=text, links=parser.links)


class SafeWebpageFetcher:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None,
                 max_bytes: int = MAX_PAGE_BYTES) -> None:
        self._transport = transport
        self._max_bytes = max_bytes

    async def fetch(self, url: str) -> WebpagePage:
        try:
            async with asyncio.timeout(FETCH_TIMEOUT):
                return await self._fetch(normalize_url(url))
        except WebpageFetchError:
            raise
        except (TimeoutError, httpx.HTTPError, OSError, UnicodeError, ValueError, zlib.error) as exc:
            raise WebpageFetchError("webpage fetch failed or timed out") from exc

    async def _fetch(self, url: str) -> WebpagePage:
        for _ in range(6):
            target = urlsplit(url)
            port = target.port or (443 if target.scheme == "https" else 80)
            addresses = await resolve_addresses(target.hostname, port)
            if not addresses or any(not _public_address(address[4][0]) for address in addresses):
                raise WebpageFetchError("webpage DNS addresses must all be public")
            address = addresses[0][4][0]
            pinned_url = httpx.URL(url).copy_with(host=address)
            # Connect only to the validated numeric address. TLS SNI AND certificate
            # verification retain the original hostname (httpcore's sni_hostname).
            # A new client per hop prevents cookies/connection reuse across hostnames.
            async with httpx.AsyncClient(
                transport=self._transport, trust_env=False, follow_redirects=False,
                timeout=httpx.Timeout(10.0),
            ) as client:
                async with client.stream(
                    "GET", pinned_url,
                    headers={"Host": target.netloc, "Accept": "text/html,application/xhtml+xml",
                             "Accept-Encoding": "identity", "User-Agent": "PortKnowledge/1.0"},
                    extensions={"sni_hostname": target.hostname},
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise WebpageFetchError("webpage redirect has no location")
                        url = normalize_url(urljoin(url, location))
                        continue
                    if response.status_code != 200:
                        raise WebpageFetchError(f"webpage returned HTTP {response.status_code}")
                    media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if media_type not in {"text/html", "application/xhtml+xml"}:
                        raise WebpageFetchError("webpage must return HTML")
                    coding = response.headers.get("content-encoding", "identity").lower()
                    if coding not in {"identity", "gzip", "deflate"}:
                        raise WebpageFetchError("unsupported webpage compression")
                    size = response.headers.get("content-length")
                    if size is not None and int(size) > self._max_bytes:
                        raise WebpageFetchError("webpage is too large")
                    body = bytearray()
                    decoder = (zlib.decompressobj(16 + zlib.MAX_WBITS if coding == "gzip"
                                                  else zlib.MAX_WBITS)
                               if coding != "identity" else None)
                    transferred = 0
                    # Raw streaming avoids an HTTP client's unbounded decompressor.
                    stream = (response.aiter_bytes(chunk_size=65536) if response.is_stream_consumed
                              else response.aiter_raw(chunk_size=65536))
                    async for part in stream:
                        transferred += len(part)
                        if transferred > self._max_bytes:
                            raise WebpageFetchError("webpage is too large")
                        if decoder is not None:
                            part = decoder.decompress(part, self._max_bytes - len(body) + 1)
                        if len(body) + len(part) > self._max_bytes:
                            raise WebpageFetchError("webpage is too large")
                        body.extend(part)
                    if decoder is not None and (not decoder.eof or decoder.unused_data):
                        raise WebpageFetchError("invalid compressed webpage")
                    encoding = response.encoding or "utf-8"
                    try:
                        html = body.decode(encoding, errors="replace")
                    except LookupError:
                        html = body.decode("utf-8", errors="replace")
                    return parse_html(url, html)
        raise WebpageFetchError("webpage has too many redirects")

    async def discover(self, url: str) -> list[str]:
        url = normalize_url(url)
        hostname = urlsplit(url).hostname
        page = await self.fetch(url)
        urls = [url]
        seen = {url}
        for link in page.links:
            try:
                candidate = normalize_url(urljoin(page.url, link))
            except WebpageFetchError:
                continue
            parsed = urlsplit(candidate)
            host = parsed.hostname
            media_type, _ = mimetypes.guess_type(parsed.path)
            # Download/index links are not HTML pages and otherwise make the
            # default all-selected registration fail as a whole.
            if media_type is not None and media_type not in {"text/html", "application/xhtml+xml"}:
                continue
            if host != hostname and not host.endswith(f".{hostname}"):
                continue
            if candidate not in seen:
                seen.add(candidate)
                urls.append(candidate)
                if len(urls) == MAX_URLS:
                    break
        return urls
