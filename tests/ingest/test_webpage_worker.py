from __future__ import annotations
from tests.private_data_fixture import private_data_cipher

import asyncio
import logging
import uuid
from unittest.mock import AsyncMock

import pytest

from rag.ingest.embedder import EmbeddingError
from rag.webpages.types import WebpagePage
from rag.webpages.repository import WebpageClaim
from rag.webpages.worker import WebpageWorker


def make_claim():
    return WebpageClaim(document_id=uuid.uuid4(), user_id=str(uuid.uuid4()), token=uuid.uuid4(),
                        urls=("https://example.com/one", "https://example.com/two"),
                        content_hash=None, reason="manual",
    )


async def test_erasure_cancels_and_drains_active_network_before_returning():
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def fetch(url):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    repository = AsyncMock()
    repository.claim_is_current.return_value = True
    fetcher, embedder = AsyncMock(), AsyncMock()
    fetcher.fetch.side_effect = fetch
    worker = WebpageWorker(repository=repository, fetcher=fetcher, embedder=embedder, cipher=private_data_cipher
    )
    claim = make_claim()
    task = asyncio.create_task(worker.process(claim))
    await asyncio.wait_for(started.wait(), timeout=1)
    await worker.erase_user(claim.user_id)
    assert cancelled.is_set()
    await asyncio.wait_for(task, timeout=1)
    await worker.process(claim)
    assert fetcher.fetch.await_count == 1
    embedder.embed_texts.assert_not_awaited()
    repository.finish.assert_not_awaited()
    repository.fail.assert_not_awaited()


async def test_whole_job_timeout_bounds_multiple_pages_and_preserves_previous_chunks(monkeypatch):
    async def fetch(url):
        await asyncio.sleep(0.04)
        return WebpagePage(url=url, text="Useful text", links=[])

    monkeypatch.setattr("rag.webpages.worker.JOB_TIMEOUT", 0.06)
    repository, fetcher, embedder = AsyncMock(), AsyncMock(), AsyncMock()
    repository.claim_is_current.return_value = True
    fetcher.fetch.side_effect = fetch
    worker = WebpageWorker(repository=repository, fetcher=fetcher, embedder=embedder, cipher=private_data_cipher
    )
    claim = make_claim()
    await worker.process(claim)
    repository.fail.assert_awaited_once_with(claim, "webpage synchronization timed out")
    repository.finish.assert_not_awaited()
    embedder.embed_texts.assert_not_awaited()


async def test_fenced_remote_claim_stops_before_another_external_request():
    repository, fetcher, embedder = AsyncMock(), AsyncMock(), AsyncMock()
    repository.claim_is_current.side_effect = [True, False]
    fetcher.fetch.return_value = WebpagePage(url="https://example.com/one", text="Useful text", links=[])
    worker = WebpageWorker(repository=repository, fetcher=fetcher, embedder=embedder, cipher=private_data_cipher
    )
    await worker.process(make_claim())
    assert fetcher.fetch.await_count == 1
    repository.finish.assert_not_awaited()
    embedder.embed_texts.assert_not_awaited()


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (
            "authentication_failed",
            "Embedding provider authentication failed. "
            "Contact an administrator to check the API key.",
        ),
        (
            "permission_denied",
            "Embedding provider access was denied. "
            "Contact an administrator to check permissions.",
        ),
        (
            "rate_limit_exceeded",
            "Embedding provider rate limit or quota was exceeded. Please try again later.",
        ),
        (
            "provider_unavailable",
            "Embedding provider is temporarily unavailable. Please try again later.",
        ),
        (
            "credential_unavailable",
            "Embedding credentials are unavailable. Contact an administrator.",
        ),
    ],
)
async def test_embedding_failure_preserves_previous_chunks_and_persists_safe_reason(
    code, message
):
    repository, fetcher, embedder = AsyncMock(), AsyncMock(), AsyncMock()
    repository.claim_is_current.return_value = True
    fetcher.fetch.return_value = WebpagePage(
        url="https://example.com/one", text="Useful text", links=[]
    )
    embedder.embed_texts.side_effect = EmbeddingError(code)
    worker = WebpageWorker(
        repository=repository, fetcher=fetcher, embedder=embedder, cipher=private_data_cipher
    )
    claim = make_claim()

    await worker.process(claim)

    repository.fail.assert_awaited_once_with(claim, message)
    repository.finish.assert_not_awaited()


@pytest.mark.parametrize("exception_type", [ValueError, RuntimeError])
@pytest.mark.parametrize("failure_stage", ["fetch", "embed"])
async def test_unexpected_failure_logs_only_exception_class_and_preserves_previous_chunks(
    caplog, exception_type, failure_stage
):
    repository, fetcher, embedder = AsyncMock(), AsyncMock(), AsyncMock()
    repository.claim_is_current.return_value = True
    fetcher.fetch.return_value = WebpagePage(
        url="https://example.com/one", text="Useful text", links=[]
    )
    raw_message = "secret sk-private-provider-key https://provider.test/private"
    if failure_stage == "fetch":
        fetcher.fetch.side_effect = exception_type(raw_message)
    else:
        embedder.embed_texts.side_effect = exception_type(raw_message)
    worker = WebpageWorker(
        repository=repository, fetcher=fetcher, embedder=embedder, cipher=private_data_cipher
    )
    claim = make_claim()

    with caplog.at_level(logging.WARNING, logger="rag.webpages.worker"):
        await worker.process(claim)

    repository.fail.assert_awaited_once_with(claim, "webpage synchronization failed")
    repository.finish.assert_not_awaited()
    assert exception_type.__name__ in caplog.text
    assert "sk-private-provider-key" not in caplog.text
    assert "https://provider.test/private" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
