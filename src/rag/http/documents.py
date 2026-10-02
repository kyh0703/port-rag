"""Internal document management HTTP router."""

from __future__ import annotations

import uuid
import re
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated
from typing import Protocol

import sqlalchemy as sa
from fastapi import APIRouter
from fastapi import File
from fastapi import Form
from fastapi import HTTPException
from fastapi import Query
from fastapi import UploadFile
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from rag.db.models import Document
from rag.db.models import DocumentStatus
from rag.http.responses import ApiResponse
from rag.http.responses import ok
from rag.ingest.types import IngestJob
from rag.ingest.types import ReindexFailedError
from rag.security.owner_erasure import OwnerAdmission
from rag.security.owner_erasure import lock_active_owner


class SessionFactory(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[AsyncSession]:
        pass


@dataclass(frozen=True)
class DocumentRecord:
    id: uuid.UUID
    user_id: str
    knowledge_key: str
    name: str
    mime: str
    status: str
    error: str | None
    created_at: datetime
    updated_at: datetime


class DocumentRepository(Protocol):
    async def create_processing_document(
        self,
        *,
        user_id: str,
        knowledge_key: str,
        name: str,
        mime: str,
    ) -> DocumentRecord:
        pass

    async def list_documents(self, *, user_id: str) -> Sequence[DocumentRecord]:
        pass

    async def get_document(
        self,
        *,
        document_id: uuid.UUID,
        user_id: str,
    ) -> DocumentRecord | None:
        pass

    async def delete_document(self, *, document_id: uuid.UUID, user_id: str) -> bool:
        pass


class IngestQueue(Protocol):
    async def enqueue(self, job: IngestJob) -> None:
        pass


class DocumentReindexer(Protocol):
    async def reindex(self, *, document_id: uuid.UUID, user_id: str) -> bool:
        pass


class UploadStorage(Protocol):
    async def save(self, upload: UploadFile, *, user_id: str) -> Path:
        pass


class SqlAlchemyDocumentRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create_processing_document(
        self,
        *,
        user_id: str,
        knowledge_key: str,
        name: str,
        mime: str,
    ) -> DocumentRecord:
        async with self._session_factory() as session:
            await lock_active_owner(session, user_id)
            document = Document(
                user_id=uuid.UUID(user_id),
                knowledge_key=knowledge_key,
                name=name,
                mime=mime,
                status=DocumentStatus.PROCESSING.value,
            )
            session.add(document)
            try:
                await session.flush()
            except IntegrityError as exc:
                await session.rollback()
                constraint = getattr(getattr(exc, "orig", None), "diag", None)
                if getattr(constraint, "constraint_name", None) == "uq_documents_user_knowledge_key":
                    raise DuplicateKnowledgeKey from exc
                raise
            await session.refresh(document)
            await session.commit()
            return _to_record(document)

    async def list_documents(self, *, user_id: str) -> list[DocumentRecord]:
        statement = (
            sa.select(Document)
            .where(Document.user_id == uuid.UUID(user_id))
            .order_by(Document.created_at.desc(), Document.id)
        )
        async with self._session_factory() as session:
            await lock_active_owner(session, user_id)
            documents = (await session.scalars(statement)).all()
        return [_to_record(document) for document in documents]

    async def get_document(
        self,
        *,
        document_id: uuid.UUID,
        user_id: str,
    ) -> DocumentRecord | None:
        statement = sa.select(Document).where(
            Document.id == document_id,
            Document.user_id == uuid.UUID(user_id),
        )
        async with self._session_factory() as session:
            await lock_active_owner(session, user_id)
            document = await session.scalar(statement)
        if document is None:
            return None
        return _to_record(document)

    async def delete_document(self, *, document_id: uuid.UUID, user_id: str) -> bool:
        statement = sa.delete(Document).where(
            Document.id == document_id,
            Document.user_id == uuid.UUID(user_id),
        )
        async with self._session_factory() as session:
            result = await session.execute(statement)
            await session.commit()
        return bool(result.rowcount)


class DocumentResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    user_id: str = Field(alias="userId")
    knowledge_key: str = Field(alias="knowledgeKey")
    name: str
    mime: str
    status: str
    error: str | None
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")


UserIdForm = Annotated[uuid.UUID, Form(alias="userId")]
UserIdQuery = Annotated[uuid.UUID, Query(alias="userId")]
DocumentUpload = Annotated[UploadFile, File()]
KnowledgeKeyForm = Annotated[str | None, Form(alias="knowledgeKey")]

_KNOWLEDGE_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,127}$")


class DuplicateKnowledgeKey(ValueError):
    pass


def create_documents_router(
    *,
    repository: DocumentRepository,
    worker: IngestQueue,
    storage: UploadStorage,
    owner_access: OwnerAdmission,
    reindexer: DocumentReindexer | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/documents",
        status_code=201,
        response_model=ApiResponse[DocumentResponse],
        response_model_exclude={"error"},
    )
    async def upload_document(
        user_id: UserIdForm,
        file: DocumentUpload,
        knowledge_key: KnowledgeKeyForm = None,
    ) -> ApiResponse[DocumentResponse]:
        normalized_user_id = str(user_id)
        resolved_knowledge_key = knowledge_key or f"knowledge_{uuid.uuid4().hex}"
        if not _KNOWLEDGE_KEY_PATTERN.fullmatch(resolved_knowledge_key):
            raise HTTPException(status_code=422, detail="invalid knowledge key")
        await owner_access.assert_active(normalized_user_id)
        path = await storage.save(file, user_id=normalized_user_id)
        document: DocumentRecord | None = None
        try:
            await owner_access.assert_active(normalized_user_id)
            document = await repository.create_processing_document(
                user_id=normalized_user_id,
                knowledge_key=resolved_knowledge_key,
                name=file.filename or "upload",
                mime=file.content_type or "application/octet-stream",
            )
            await worker.enqueue(IngestJob(
                document_id=document.id, path=path, user_id=normalized_user_id,
            ))
            await owner_access.assert_active(normalized_user_id)
        except DuplicateKnowledgeKey as exc:
            path.unlink(missing_ok=True)
            raise HTTPException(status_code=409, detail="knowledge key already exists") from exc
        except Exception:
            path.unlink(missing_ok=True)
            if document is not None:
                await repository.delete_document(
                    document_id=document.id,
                    user_id=normalized_user_id,
                )
            raise
        return ok(_to_response(document), status_code=201)

    @router.post(
        "/documents/{document_id}/reindex",
        response_model=ApiResponse[DocumentResponse],
        response_model_exclude={"error"},
    )
    async def reindex_document(
        document_id: uuid.UUID,
        user_id: UserIdQuery,
    ) -> ApiResponse[DocumentResponse]:
        normalized_user_id = str(user_id)
        await owner_access.assert_active(normalized_user_id)
        document = await repository.get_document(
            document_id=document_id,
            user_id=normalized_user_id,
        )
        if document is None:
            raise HTTPException(status_code=404, detail="document not found")
        if document.status == DocumentStatus.PROCESSING.value:
            raise HTTPException(status_code=409, detail="document is still processing")
        if reindexer is None:
            raise RuntimeError("document reindexer is not configured")
        try:
            reindexed = await reindexer.reindex(
                document_id=document_id,
                user_id=normalized_user_id,
            )
        except ReindexFailedError as exc:
            raise HTTPException(status_code=422, detail="document reindex failed") from exc
        if not reindexed:
            raise HTTPException(status_code=404, detail="document not found")
        document = await repository.get_document(
            document_id=document_id,
            user_id=normalized_user_id,
        )
        if document is None:
            raise HTTPException(status_code=404, detail="document not found")
        await owner_access.assert_active(normalized_user_id)
        return ok(_to_response(document))

    @router.get(
        "/documents",
        response_model=ApiResponse[list[DocumentResponse]],
        response_model_exclude={"error"},
    )
    async def list_documents(user_id: UserIdQuery) -> ApiResponse[list[DocumentResponse]]:
        await owner_access.assert_active(str(user_id))
        documents = await repository.list_documents(user_id=str(user_id))
        await owner_access.assert_active(str(user_id))
        return ok([_to_response(document) for document in documents])

    @router.get(
        "/documents/{document_id}",
        response_model=ApiResponse[DocumentResponse],
        response_model_exclude={"error"},
    )
    async def get_document(
        document_id: uuid.UUID,
        user_id: UserIdQuery,
    ) -> ApiResponse[DocumentResponse]:
        await owner_access.assert_active(str(user_id))
        document = await repository.get_document(
            document_id=document_id,
            user_id=str(user_id),
        )
        if document is None:
            raise HTTPException(status_code=404, detail="document not found")
        await owner_access.assert_active(str(user_id))
        return ok(_to_response(document))

    @router.delete(
        "/documents/{document_id}",
        response_model=ApiResponse[None],
        response_model_exclude={"error"},
    )
    async def delete_document(document_id: uuid.UUID, user_id: UserIdQuery) -> ApiResponse[None]:
        await owner_access.assert_active(str(user_id))
        deleted = await repository.delete_document(
            document_id=document_id,
            user_id=str(user_id),
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="document not found")
        return ok(None)

    return router


def _to_record(document: Document) -> DocumentRecord:
    return DocumentRecord(
        id=document.id,
        user_id=str(document.user_id),
        knowledge_key=document.knowledge_key,
        name=document.name,
        mime=document.mime,
        status=_status_value(document.status),
        error=document.error,
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


def _to_response(document: DocumentRecord) -> DocumentResponse:
    return DocumentResponse(
        id=str(document.id),
        user_id=document.user_id,
        knowledge_key=document.knowledge_key,
        name=document.name,
        mime=document.mime,
        status=_status_value(document.status),
        error=document.error,
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


def _status_value(status: DocumentStatus | str) -> str:
    if isinstance(status, DocumentStatus):
        return status.value
    return str(status)
