"""Application search service."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from reg.search.types import SearchHit


class InvalidSearchRequest(ValueError):
    """Raised when a Search request is missing required caller-scoped input."""


class QueryEmbedder(Protocol):
    async def embed_query(self, text: str) -> Sequence[float]:
        pass


class SearchBoundary(Protocol):
    async def search(
        self,
        *,
        user_id: str,
        embedding: Sequence[float],
        top_k: int,
    ) -> list[SearchHit]:
        pass


class SearchService:
    def __init__(
        self,
        *,
        embedder: QueryEmbedder,
        repository: SearchBoundary,
        default_top_k: int = 5,
    ) -> None:
        if default_top_k < 1:
            raise ValueError("default_top_k must be greater than zero")

        self._embedder = embedder
        self._repository = repository
        self._default_top_k = default_top_k

    async def search(self, *, user_id: str, query: str, top_k: int) -> list[SearchHit]:
        normalized_user_id = user_id.strip()
        normalized_query = query.strip()

        if not normalized_user_id:
            raise InvalidSearchRequest("user_id is required")
        if not normalized_query:
            raise InvalidSearchRequest("query is required")
        if top_k < 0:
            raise InvalidSearchRequest("top_k must be zero or greater")

        resolved_top_k = top_k or self._default_top_k
        embedding = await self._embedder.embed_query(normalized_query)

        return await self._repository.search(
            user_id=normalized_user_id,
            embedding=embedding,
            top_k=resolved_top_k,
        )
