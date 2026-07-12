from __future__ import annotations

from dataclasses import dataclass

from rag.ingest.embedder import StaticFakeEmbedder
from rag.search.service import SearchService
from rag.search.types import SearchHit


class FakeEmbedder:
    async def embed_query(self, text: str) -> list[float]:
        return [float(len(text)), 0.0, 1.0]


@dataclass(frozen=True)
class ScopedRow:
    user_id: str
    hit: SearchHit


class InMemoryRepository:
    def __init__(self, rows: list[ScopedRow]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, list[float], int]] = []

    async def search(self, user_id: str, embedding: list[float], top_k: int) -> list[SearchHit]:
        self.calls.append((user_id, embedding, top_k))
        return [row.hit for row in self.rows if row.user_id == user_id][:top_k]


async def test_search_is_scoped_to_requested_user() -> None:
    repository = InMemoryRepository(
        [
            ScopedRow(
                user_id="user-a",
                hit=SearchHit(
                    chunk_id="chunk-a",
                    document_id="doc-a",
                    document_name="a.md",
                    text="alpha",
                    score=0.9,
                    metadata={},
                    seq=0,
                ),
            ),
            ScopedRow(
                user_id="user-b",
                hit=SearchHit(
                    chunk_id="chunk-b",
                    document_id="doc-b",
                    document_name="b.md",
                    text="beta",
                    score=0.99,
                    metadata={},
                    seq=0,
                ),
            ),
        ]
    )
    service = SearchService(embedder=FakeEmbedder(), repository=repository, default_top_k=5)

    results = await service.search(user_id="user-a", query="alpha", top_k=10)

    assert [result.chunk_id for result in results] == ["chunk-a"]
    assert repository.calls == [("user-a", [5.0, 0.0, 1.0], 10)]


async def test_top_k_zero_uses_default() -> None:
    repository = InMemoryRepository(
        [
            ScopedRow(
                user_id="user-a",
                hit=SearchHit(
                    chunk_id=f"chunk-{index}",
                    document_id="doc-a",
                    document_name="a.md",
                    text=str(index),
                    score=1.0,
                    metadata={},
                    seq=index,
                ),
            )
            for index in range(3)
        ]
    )
    service = SearchService(embedder=FakeEmbedder(), repository=repository, default_top_k=2)

    results = await service.search(user_id="user-a", query="alpha", top_k=0)

    assert [result.chunk_id for result in results] == ["chunk-0", "chunk-1"]
    assert repository.calls == [("user-a", [5.0, 0.0, 1.0], 2)]


async def test_search_accepts_shared_fake_embedder() -> None:
    repository = InMemoryRepository([])
    service = SearchService(
        embedder=StaticFakeEmbedder(dimensions=3),
        repository=repository,
        default_top_k=2,
    )

    await service.search(user_id="user-a", query="alpha", top_k=1)

    assert repository.calls == [("user-a", [1.0, 0.0, 0.0], 1)]
