"""Internal search HTTP router."""

from __future__ import annotations

import uuid
from typing import Protocol

from fastapi import APIRouter
from fastapi import HTTPException
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from rag.http.responses import ApiResponse
from rag.http.responses import ok
from rag.search.service import InvalidSearchRequest
from rag.search.types import SearchHit


class SearchBoundary(Protocol):
    async def search(self, *, user_id: str, query: str, top_k: int) -> list[SearchHit]:
        pass


class SearchRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    user_id: uuid.UUID = Field(alias="userId")
    query: str = Field(min_length=1)
    top_k: int = Field(0, alias="topK", ge=0)


class SearchResultResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    chunk_id: str = Field(alias="chunkId")
    document_id: str = Field(alias="documentId")
    document_name: str = Field(alias="documentName")
    text: str
    score: float
    metadata: dict[str, str]
    seq: int


class SearchResponse(BaseModel):
    results: list[SearchResultResponse]


def create_search_router(*, service: SearchBoundary) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/search",
        response_model=ApiResponse[SearchResponse],
        response_model_exclude={"error"},
    )
    async def search_documents(request: SearchRequest) -> ApiResponse[SearchResponse]:
        try:
            hits = await service.search(
                user_id=str(request.user_id),
                query=request.query,
                top_k=request.top_k,
            )
        except InvalidSearchRequest as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        return ok(SearchResponse(results=[_to_response(hit) for hit in hits]))

    return router


def _to_response(hit: SearchHit) -> SearchResultResponse:
    return SearchResultResponse(
        chunk_id=hit.chunk_id,
        document_id=hit.document_id,
        document_name=hit.document_name,
        text=hit.text,
        score=hit.score,
        metadata=hit.metadata,
        seq=hit.seq,
    )
