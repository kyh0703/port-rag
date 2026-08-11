"""Internal immutable knowledge revision HTTP router."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated
from typing import Protocol

from fastapi import APIRouter
from fastapi import Header
from fastapi import HTTPException
from fastapi import Query
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from rag.http.responses import ApiResponse
from rag.http.responses import ok
from rag.knowledge.revisions import KnowledgeRevisionNotFound
from rag.knowledge.revisions import KnowledgeRevisionRecord
from rag.security.retrieval_capability import InvalidRetrievalCapability


class RevisionRepository(Protocol):
    async def create(
        self,
        *,
        user_id: str,
        document_ids: Sequence[uuid.UUID] | None,
        revision_id: uuid.UUID | None,
    ) -> KnowledgeRevisionRecord:
        pass

    async def get(
        self,
        *,
        revision_id: uuid.UUID,
        user_id: str,
    ) -> KnowledgeRevisionRecord | None:
        pass


class RetrievalCapabilityBoundary(Protocol):
    def verify(self, token: str, *, knowledge_revision_id: str) -> str:
        pass


class CreateKnowledgeRevisionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    user_id: uuid.UUID = Field(alias="userId")
    document_ids: list[uuid.UUID] | None = Field(None, alias="documentIds")
    revision_id: uuid.UUID = Field(alias="revisionId")


class KnowledgeRevisionResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: uuid.UUID
    user_id: uuid.UUID = Field(alias="userId")
    chunk_count: int = Field(alias="chunkCount")
    created_at: datetime = Field(alias="createdAt")


UserIdQuery = Annotated[uuid.UUID, Query(alias="userId")]


def create_knowledge_revisions_router(
    *,
    repository: RevisionRepository,
    capability_verifier: RetrievalCapabilityBoundary,
) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/knowledge-revisions",
        status_code=201,
        response_model=ApiResponse[KnowledgeRevisionResponse],
        response_model_exclude={"error"},
    )
    async def create_revision(
        request: CreateKnowledgeRevisionRequest,
        authorization: str | None = Header(default=None),
    ) -> ApiResponse[KnowledgeRevisionResponse]:
        token = _bearer_token(authorization)
        try:
            authorized_user_id = capability_verifier.verify(
                token,
                knowledge_revision_id=str(request.revision_id),
            )
            if authorized_user_id != str(request.user_id):
                raise InvalidRetrievalCapability("invalid retrieval capability")
        except InvalidRetrievalCapability as exc:
            raise HTTPException(
                status_code=401,
                detail="Invalid retrieval capability",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        existing = await repository.get(
            revision_id=request.revision_id,
            user_id=str(request.user_id),
        )
        if existing is not None:
            return ok(_to_response(existing), status_code=201)
        try:
            revision = await repository.create(
                user_id=str(request.user_id),
                document_ids=request.document_ids,
                revision_id=request.revision_id,
            )
        except KnowledgeRevisionNotFound as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return ok(_to_response(revision), status_code=201)

    @router.get(
        "/knowledge-revisions/{revision_id}",
        response_model=ApiResponse[KnowledgeRevisionResponse],
        response_model_exclude={"error"},
    )
    async def get_revision(
        revision_id: uuid.UUID,
        user_id: UserIdQuery,
    ) -> ApiResponse[KnowledgeRevisionResponse]:
        revision = await repository.get(
            revision_id=revision_id,
            user_id=str(user_id),
        )
        if revision is None:
            raise HTTPException(status_code=404, detail="knowledge revision not found")
        return ok(_to_response(revision))

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


def _to_response(revision: KnowledgeRevisionRecord) -> KnowledgeRevisionResponse:
    return KnowledgeRevisionResponse(
        id=revision.id,
        user_id=revision.user_id,
        chunk_count=revision.chunk_count,
        created_at=revision.created_at,
    )
