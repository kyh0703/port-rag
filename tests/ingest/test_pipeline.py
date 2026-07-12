from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from rag.db.models import DocumentStatus
from rag.ingest import IngestChunk
from rag.ingest import IngestJob
from rag.ingest import IngestPipeline
from rag.ingest import ParsedDocument
from rag.ingest import StaticFakeEmbedder


@dataclass
class StoredChunk:
    seq: int
    text: str
    metadata: dict[str, object]
    embedding: list[float]


class MemoryStore:
    def __init__(self) -> None:
        self.statuses: dict[uuid.UUID, str] = {}
        self.errors: dict[uuid.UUID, str] = {}
        self.chunks: dict[uuid.UUID, list[StoredChunk]] = {}

    async def replace_chunks_and_mark_ready(
        self,
        document_id: uuid.UUID,
        chunks: list[IngestChunk],
        embeddings: list[list[float]],
    ) -> None:
        self.chunks[document_id] = [
            StoredChunk(
                seq=chunk.seq,
                text=chunk.text,
                metadata=chunk.metadata,
                embedding=embedding,
            )
            for chunk, embedding in zip(chunks, embeddings, strict=True)
        ]
        self.statuses[document_id] = DocumentStatus.READY.value
        self.errors.pop(document_id, None)

    async def mark_failed(self, document_id: uuid.UUID, error: str) -> None:
        self.statuses[document_id] = DocumentStatus.FAILED.value
        self.errors[document_id] = error


class TextParser:
    async def parse(self, path: Path) -> ParsedDocument:
        return ParsedDocument(name=path.name, content=path.read_text())


class BrokenParser:
    async def parse(self, path: Path) -> ParsedDocument:
        raise ValueError(f"cannot parse {path.name}")


class SplitChunker:
    async def chunk(self, parsed: ParsedDocument) -> list[IngestChunk]:
        return [
            IngestChunk(seq=index, text=line, metadata={"source": parsed.name, "line": index + 1})
            for index, line in enumerate(str(parsed.content).splitlines())
            if line
        ]


@pytest.mark.asyncio
async def test_successful_md_ingest_stores_chunks_and_marks_ready(tmp_path: Path) -> None:
    path = tmp_path / "notes.md"
    path.write_text("alpha\nbeta\n")
    document_id = uuid.uuid4()
    store = MemoryStore()
    embedder = StaticFakeEmbedder(dimensions=3)
    pipeline = IngestPipeline(
        parser=TextParser(),
        chunker=SplitChunker(),
        embedder=embedder,
        store=store,
    )

    await pipeline.ingest(IngestJob(document_id=document_id, path=path))

    assert store.statuses[document_id] == DocumentStatus.READY.value
    assert not path.exists()
    assert [chunk.text for chunk in store.chunks[document_id]] == ["alpha", "beta"]
    assert [chunk.embedding for chunk in store.chunks[document_id]] == [
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
    ]


@pytest.mark.asyncio
async def test_broken_parser_marks_document_failed(tmp_path: Path) -> None:
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"not a real pdf")
    document_id = uuid.uuid4()
    store = MemoryStore()
    pipeline = IngestPipeline(
        parser=BrokenParser(),
        chunker=SplitChunker(),
        embedder=StaticFakeEmbedder(dimensions=3),
        store=store,
    )

    await pipeline.ingest(IngestJob(document_id=document_id, path=path))

    assert store.statuses[document_id] == DocumentStatus.FAILED.value
    assert not path.exists()
    assert "cannot parse broken.pdf" in store.errors[document_id]
    assert document_id not in store.chunks


@pytest.mark.asyncio
async def test_chunk_metadata_and_seq_are_preserved(tmp_path: Path) -> None:
    path = tmp_path / "source.txt"
    path.write_text("first\nsecond\nthird\n")
    document_id = uuid.uuid4()
    store = MemoryStore()
    pipeline = IngestPipeline(
        parser=TextParser(),
        chunker=SplitChunker(),
        embedder=StaticFakeEmbedder(dimensions=2),
        store=store,
    )

    await pipeline.ingest(IngestJob(document_id=document_id, path=path))

    assert [(chunk.seq, chunk.metadata) for chunk in store.chunks[document_id]] == [
        (0, {"source": "source.txt", "line": 1}),
        (1, {"source": "source.txt", "line": 2}),
        (2, {"source": "source.txt", "line": 3}),
    ]
