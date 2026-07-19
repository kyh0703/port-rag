"""Shared ingest data types and protocols."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Protocol


@dataclass(frozen=True)
class IngestJob:
    document_id: uuid.UUID
    path: Path


@dataclass(frozen=True)
class ParsedDocument:
    name: str
    content: Any


@dataclass(frozen=True)
class IngestChunk:
    seq: int
    text: str
    metadata: dict[str, Any]


class DocumentParser(Protocol):
    async def parse(self, path: Path) -> ParsedDocument: ...


class DocumentChunker(Protocol):
    async def chunk(self, parsed: ParsedDocument) -> list[IngestChunk]: ...


class TextEmbedder(Protocol):
    async def embed_texts(self, texts: list[str]) -> list[list[float]]: ...


class ReindexFailedError(Exception):
    """Raised after a reindex failure has been persisted on its document."""


class IngestStore(Protocol):
    async def replace_chunks_and_mark_ready(
        self,
        document_id: uuid.UUID,
        chunks: list[IngestChunk],
        embeddings: list[list[float]],
    ) -> None: ...

    async def mark_failed(self, document_id: uuid.UUID, error: str) -> None: ...

    async def get_chunks_for_reindex(
        self,
        document_id: uuid.UUID,
        user_id: str,
    ) -> list[IngestChunk] | None: ...

    async def replace_embeddings_and_mark_ready(
        self,
        document_id: uuid.UUID,
        user_id: str,
        embeddings: list[list[float]],
    ) -> bool: ...

    async def mark_reindex_failed(
        self,
        document_id: uuid.UUID,
        user_id: str,
        error: str,
    ) -> bool: ...
