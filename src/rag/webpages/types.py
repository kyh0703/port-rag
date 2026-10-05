"""Immutable views of durable webpage state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from rag.db.models import DocumentWebpage


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
