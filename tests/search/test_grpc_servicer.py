from __future__ import annotations

from dataclasses import dataclass

import grpc
import pytest
from port.reg.v1 import reg_pb2

from reg.grpc.servicer import RegSearchServicer
from reg.search.service import SearchService
from reg.search.types import SearchHit


class FakeEmbedder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def embed_query(self, text: str) -> list[float]:
        self.calls.append(text)
        return [0.1, 0.2, 0.3]


class FakeRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[float], int]] = []

    async def search(self, user_id: str, embedding: list[float], top_k: int) -> list[SearchHit]:
        self.calls.append((user_id, embedding, top_k))
        return [
            SearchHit(
                chunk_id="chunk-1",
                document_id="doc-1",
                document_name="handbook.md",
                text="port search notes",
                score=0.82,
                metadata={"page": "1"},
                seq=2,
            )
        ]


@dataclass
class AbortError(Exception):
    code: grpc.StatusCode
    details: str


class FakeContext:
    async def abort(self, code: grpc.StatusCode, details: str) -> None:
        raise AbortError(code, details)


@pytest.mark.parametrize("user_id", ["", "   "])
async def test_search_missing_or_blank_user_id_returns_invalid_argument(user_id: str) -> None:
    embedder = FakeEmbedder()
    repository = FakeRepository()
    service = SearchService(embedder=embedder, repository=repository)
    servicer = RegSearchServicer(service)

    with pytest.raises(AbortError) as exc_info:
        await servicer.Search(
            reg_pb2.SearchRequest(user_id=user_id, query="hello", top_k=3),
            FakeContext(),
        )

    assert exc_info.value.code == grpc.StatusCode.INVALID_ARGUMENT
    assert "user_id" in exc_info.value.details
    assert embedder.calls == []
    assert repository.calls == []


async def test_search_response_maps_hits_to_generated_contract() -> None:
    embedder = FakeEmbedder()
    repository = FakeRepository()
    service = SearchService(embedder=embedder, repository=repository, default_top_k=4)
    servicer = RegSearchServicer(service)

    response = await servicer.Search(
        reg_pb2.SearchRequest(user_id="user-a", query="notes", top_k=0),
        FakeContext(),
    )

    assert len(response.results) == 1
    result = response.results[0]
    assert result.chunk_id == "chunk-1"
    assert result.document_id == "doc-1"
    assert result.document_name == "handbook.md"
    assert result.text == "port search notes"
    assert result.score == pytest.approx(0.82)
    assert dict(result.metadata) == {"page": "1"}
    assert result.seq == 2
    assert repository.calls == [("user-a", [0.1, 0.2, 0.3], 4)]
