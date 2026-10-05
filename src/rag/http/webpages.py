"""Internal owner-scoped webpage registration, discovery, and synchronization."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from rag.http.documents import DocumentResponse, DuplicateKnowledgeKey, _to_response
from rag.http.responses import ApiResponse, ok
from rag.security.owner_erasure import OwnerAdmission
from rag.webpages.fetch import SafeWebpageFetcher, WebpageFetchError
from rag.webpages.repository import SyncConflict, WebpageDetail, WebpageRepository

UserIdQuery = Annotated[uuid.UUID, Query(alias="userId")]
Url = Annotated[str, Field(min_length=1, max_length=4096)]


class DiscoverWebpagesRequest(BaseModel):
    url: Url


class DiscoveredWebpagesResponse(BaseModel):
    urls: list[str]


class RegisterWebpagesRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str | None = Field(None, min_length=1, max_length=255)
    knowledge_key: str = Field(alias="knowledgeKey", pattern=r"^[a-z][a-z0-9_]{0,127}$")
    urls: list[Url] = Field(min_length=1, max_length=100)
    auto_sync: StrictBool = Field(False, alias="autoSync")


class UpdateWebpageRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    auto_sync: StrictBool = Field(alias="autoSync")


class WebpageDetailResponse(BaseModel):
    document: DocumentResponse
    content: str


def _detail(detail: WebpageDetail | None) -> WebpageDetailResponse:
    if detail is None:
        raise HTTPException(status_code=404, detail="webpage document not found")
    return WebpageDetailResponse(document=_to_response(detail.document), content=detail.content)


def create_webpages_router(*, repository: WebpageRepository, fetcher: SafeWebpageFetcher,
                           owner_access: OwnerAdmission) -> APIRouter:
    router = APIRouter()

    @router.post("/documents/webpages/discover", response_model=ApiResponse[DiscoveredWebpagesResponse],
                 response_model_exclude={"error"})
    async def discover(request: DiscoverWebpagesRequest, user_id: UserIdQuery):
        await owner_access.assert_active(str(user_id))
        try:
            urls = await fetcher.discover(request.url)
        except WebpageFetchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        await owner_access.assert_active(str(user_id))
        return ok(DiscoveredWebpagesResponse(urls=urls))

    @router.post("/documents/webpages", status_code=201, response_model=ApiResponse[DocumentResponse],
                 response_model_exclude={"error"})
    async def register(request: RegisterWebpagesRequest, user_id: UserIdQuery):
        await owner_access.assert_active(str(user_id))
        try:
            document = await repository.create(
                user_id=str(user_id), knowledge_key=request.knowledge_key, name=request.name,
                urls=request.urls, auto_sync=request.auto_sync,
            )
        except DuplicateKnowledgeKey as exc:
            raise HTTPException(status_code=409, detail="knowledge key already exists") from exc
        except WebpageFetchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        await owner_access.assert_active(str(user_id))
        return ok(_to_response(document), status_code=201)

    @router.get("/documents/{document_id}/webpage", response_model=ApiResponse[WebpageDetailResponse],
                response_model_exclude={"error"})
    async def get_webpage(document_id: uuid.UUID, user_id: UserIdQuery):
        await owner_access.assert_active(str(user_id))
        response = _detail(await repository.get(document_id=document_id, user_id=str(user_id)))
        await owner_access.assert_active(str(user_id))
        return ok(response)

    @router.patch("/documents/{document_id}/webpage", response_model=ApiResponse[WebpageDetailResponse],
                  response_model_exclude={"error"})
    async def update_webpage(document_id: uuid.UUID, request: UpdateWebpageRequest,
                             user_id: UserIdQuery):
        await owner_access.assert_active(str(user_id))
        response = _detail(await repository.set_auto_sync(
            document_id=document_id, user_id=str(user_id), auto_sync=request.auto_sync,
        ))
        await owner_access.assert_active(str(user_id))
        return ok(response)

    @router.post("/documents/{document_id}/sync", status_code=202,
                 response_model=ApiResponse[WebpageDetailResponse], response_model_exclude={"error"})
    async def sync_webpage(document_id: uuid.UUID, user_id: UserIdQuery):
        await owner_access.assert_active(str(user_id))
        try:
            response = _detail(await repository.enqueue(document_id=document_id, user_id=str(user_id)))
        except SyncConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await owner_access.assert_active(str(user_id))
        return ok(response, status_code=202)

    return router
