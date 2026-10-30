"""Pure URL validation for registration and the configured trusted crawler origin."""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

import httpx

from rag.webpages.types import WebpageFetchError

MAX_URLS = 100
_NAT64_NETWORKS = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))


def _public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    # Registration rejects mapped/transition forms; the crawler owns network validation.
    return bool(address.is_global and not address.is_multicast and not address.is_reserved
                and not getattr(address, "ipv4_mapped", None)
                and not getattr(address, "sixtofour", None)
                and not getattr(address, "teredo", None)
                and not any(address in network for network in _NAT64_NETWORKS))


def normalize_url(url: str) -> str:
    url = url.strip(" ")
    if not url or "\\" in url or any(ord(char) <= 32 or 127 <= ord(char) <= 159 for char in url):
        raise WebpageFetchError("invalid webpage URL")
    bare = not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.IGNORECASE)
    if bare:
        if url.startswith("//") or re.match(r"^https?:", url, re.IGNORECASE):
            raise WebpageFetchError("invalid public HTTP webpage URL")
        url = "https://" + url
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError
        if parsed.username is not None or parsed.password is not None or "%" in parsed.netloc:
            raise ValueError
        hostname = unicodedata.normalize("NFC", parsed.hostname.lower()).rstrip(".")
        if not hostname:
            raise ValueError
        port = parsed.port
        if port is not None and port not in {80, 443}:
            raise ValueError
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            hostname = httpx.URL(host=hostname).raw_host.decode("ascii").rstrip(".")
            if (not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", hostname)
                    or (bare and "." not in hostname)):
                raise ValueError
        else:
            if not _public_address(hostname):
                raise WebpageFetchError("webpage address must be public")
            hostname = address.compressed
        authority = f"[{hostname}]" if ":" in hostname else hostname
        if port is not None and port != (443 if parsed.scheme == "https" else 80):
            authority += f":{port}"
        normalized = urlunsplit((parsed.scheme, authority, parsed.path or "/", parsed.query, ""))
        if len(normalized.encode("utf-8")) > 4096:
            raise WebpageFetchError("webpage URL is too long")
        return normalized
    except WebpageFetchError:
        raise
    except (UnicodeError, ValueError, httpx.InvalidURL):
        pass
    # Do not retain URL-bearing parser errors in exception chains.
    raise WebpageFetchError("invalid public HTTP webpage URL")


def validate_crawler_url(value: str) -> str:
    """Accept an explicit service origin, including private service/loopback addresses."""
    try:
        if (not value or "\\" in value or "?" in value or "#" in value
                or any(ord(char) <= 32 or 127 <= ord(char) <= 159 for char in value)):
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or "%" in parsed.netloc or parsed.path not in {"", "/"}
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            raise ValueError
        service = httpx.URL(value)
        if not service.is_absolute_url or not service.host:
            raise ValueError
        return str(service.copy_with(path="")).rstrip("/")
    except (UnicodeError, ValueError, httpx.InvalidURL):
        pass
    raise ValueError("CRAWLER_URL must be an HTTP(S) service origin without credentials or a path")
