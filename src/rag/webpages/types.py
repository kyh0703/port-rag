"""Webpage collection results, safe failures, and durable-state views."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from rag.db.models import DocumentWebpage


class WebpageFetchError(ValueError):
    """A safe collection failure that can be persisted or returned to callers."""

    def __init__(self, message: str, *, code: str = "fetch_failed") -> None:
        super().__init__(message)
        self.code = code


class CrawlerUnavailableError(WebpageFetchError):
    """The trusted crawler could not supply a valid response."""

    def __init__(self) -> None:
        super().__init__(
            "webpage crawler is unavailable; try again later", code="crawler_unavailable",
        )


@dataclass(frozen=True)
class WebpagePage:
    url: str
    text: str
    links: list[str]


@dataclass(frozen=True)
class WebpageState:
    urls: list[str]
    auto_sync: bool
    sync_status: Literal["idle", "queued", "running", "failed"]
    last_synced_at: datetime | None
    last_checked_at: datetime | None
    next_sync_at: datetime | None
    sync_error: str | None
    last_sync_changed: bool | None


def webpage_state(page: DocumentWebpage) -> WebpageState:
    return WebpageState(
        urls=list(page.urls), auto_sync=page.auto_sync, sync_status=page.sync_status,
        last_synced_at=page.last_synced_at, last_checked_at=page.last_checked_at,
        next_sync_at=page.next_sync_at, sync_error=page.sync_error,
        last_sync_changed=page.last_sync_changed,
    )
