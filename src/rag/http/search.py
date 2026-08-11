"""Internal search HTTP router."""

from __future__ import annotations

import uuid
from typing import Protocol

from fastapi import APIRouter
from fastapi import Header
from fastapi import HTTPException
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from rag.http.responses import ApiResponse
from rag.http.responses import ok
from rag.search.service import InvalidSearchRequest
from rag.search.types import SearchHit
from rag.security.retrieval_capability import InvalidRetrievalCapability


class SearchBoundary(Protocol):
    async def search(self, *, user_id: str, query: str, top_k: int) -> list[SearchHit]:
        pass

    async def search_revision(
        self,
        *,
        user_id: str,
        knowledge_revision_id: str,
        query: str,
        top_k: int,
    ) -> list[SearchHit]:
        pass


class RetrievalCapabilityBoundary(Protocol):
    def verify(self, token: str, *, knowledge_revision_id: str) -> str:
        pass


class SearchRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    user_id: uuid.UUID = Field(alias="userId")
    query: str = Field(min_length=1)
    top_k: int = Field(0, alias="topK", ge=0)


class RevisionSearchRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

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


def create_search_router(
    *,
    service: SearchBoundary,
    capability_verifier: RetrievalCapabilityBoundary,
) -> APIRouter:
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

    @router.post(
        "/knowledge-revisions/{knowledge_revision_id}/search",
        response_model=ApiResponse[SearchResponse],
        response_model_exclude={"error"},
    )
    async def search_knowledge_revision(
        knowledge_revision_id: uuid.UUID,
        request: RevisionSearchRequest,
        authorization: str | None = Header(default=None),
    ) -> ApiResponse[SearchResponse]:
        token = _bearer_token(authorization)
        try:
            user_id = capability_verifier.verify(
                token,
                knowledge_revision_id=str(knowledge_revision_id),
            )
        except InvalidRetrievalCapability as exc:
            raise HTTPException(
                status_code=401,
                detail="Invalid retrieval capability",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        try:
            hits = await service.search_revision(
                user_id=user_id,
                knowledge_revision_id=str(knowledge_revision_id),
                query=request.query,
                top_k=request.top_k,
            )
        except InvalidSearchRequest as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        return ok(SearchResponse(results=[_to_response(hit) for hit in hits]))

    return router


def _bearer_token(authorization: str | None) -> str:
    if authorization is None:
        raise HTTPException(
            status_code=401,
            detail="Retrieval capability is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=401,
            detail="Invalid retrieval capability",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token.strip()


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
