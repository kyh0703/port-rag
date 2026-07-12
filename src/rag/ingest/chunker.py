"""HybridChunker adapter."""

from __future__ import annotations

import asyncio
from typing import Any

from docling.chunking import HybridChunker

from rag.ingest.types import IngestChunk
from rag.ingest.types import ParsedDocument


class HybridDoclingChunker:
    def __init__(self, chunker: HybridChunker | None = None) -> None:
        self._chunker = chunker or HybridChunker()

    async def chunk(self, parsed: ParsedDocument) -> list[IngestChunk]:
        return await asyncio.to_thread(self._chunk_sync, parsed)

    def _chunk_sync(self, parsed: ParsedDocument) -> list[IngestChunk]:
        chunks: list[IngestChunk] = []
        for raw_chunk in self._chunker.chunk(parsed.content):
            text = raw_chunk.text.strip()
            if not text:
                continue

            metadata = _json_metadata(raw_chunk.meta)
            metadata.setdefault("source", parsed.name)
            chunks.append(IngestChunk(seq=len(chunks), text=text, metadata=metadata))
        return chunks


def _json_metadata(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        return dumped if isinstance(dumped, dict) else {"value": dumped}
    if isinstance(value, dict):
        return value
    return {"value": str(value)}
