"""Internally authenticated operating-data erasure boundary."""

from __future__ import annotations

import uuid
from typing import Literal
from typing import Protocol

from fastapi import APIRouter
from fastapi import HTTPException
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from rag.http.responses import ApiResponse
from rag.http.responses import ok
from rag.ingest.uploads import LegacyUploadCleanupRequired
from rag.ingest.uploads import LocalUploadStorage
from rag.ingest.worker import IngestWorker


class OwnerErasureRepository(Protocol):
    async def fence(self, user_id: str) -> None: ...

    async def erase(self, user_id: str) -> None: ...


class AccountErasureService:
    def __init__(
        self,
        *,
        repository: OwnerErasureRepository,
        worker: IngestWorker,
        storage: LocalUploadStorage,
    ) -> None:
        self._repository = repository
        self._worker = worker
        self._storage = storage

    async def erase(self, user_id: str) -> None:
        # The fence commits before waiting on local work or filesystem cleanup.
        # A cleanup failure leaves the owner blocked and all SQL retry handles.
        await self._repository.fence(user_id)
        self._storage.require_legacy_cleanup()
        await self._worker.erase_user(user_id)
        await self._storage.erase_owner(user_id)
        await self._repository.erase(user_id)


class AccountErasureResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    user_id: str = Field(alias="userId")
    erased: Literal[True] = True


def create_account_erasure_router(*, service: AccountErasureService) -> APIRouter:
    router = APIRouter()

    @router.delete(
        "/users/{user_id}/data",
        response_model=ApiResponse[AccountErasureResponse],
        response_model_exclude={"error"},
    )
    async def erase_user_data(user_id: uuid.UUID) -> ApiResponse[AccountErasureResponse]:
        normalized_user_id = str(user_id)
        try:
            await service.erase(normalized_user_id)
        except LegacyUploadCleanupRequired as exc:
            raise HTTPException(
                status_code=409, detail="RAG_LEGACY_UPLOAD_CLEANUP_REQUIRED",
            ) from exc
        return ok(AccountErasureResponse(user_id=normalized_user_id))

    return router
