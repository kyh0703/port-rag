"""Internal document management HTTP router."""

from __future__ import annotations

import tempfile
import uuid
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
from fastapi import Response
from fastapi import UploadFile
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from reg.db.models import Document
from reg.db.models import DocumentStatus
from reg.ingest.types import IngestJob


class SessionFactory(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[AsyncSession]:
        pass


@dataclass(frozen=True)
class DocumentRecord:
    id: uuid.UUID
    user_id: str
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


class UploadStorage(Protocol):
    async def save(self, upload: UploadFile) -> Path:
        pass


class LocalUploadStorage:
    def __init__(self, upload_dir: Path | None = None) -> None:
        self._upload_dir = upload_dir or Path(tempfile.mkdtemp(prefix="reg-uploads-"))
        self._upload_dir.mkdir(parents=True, exist_ok=True)

    async def save(self, upload: UploadFile) -> Path:
        suffix = Path(upload.filename or "").suffix
        path = self._upload_dir / f"{uuid.uuid4()}{suffix}"
        with path.open("wb") as output:
            while chunk := await upload.read(1024 * 1024):
                output.write(chunk)
        return path


class SqlAlchemyDocumentRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create_processing_document(
        self,
        *,
        user_id: str,
        name: str,
        mime: str,
    ) -> DocumentRecord:
        async with self._session_factory() as session:
            document = Document(
                user_id=user_id,
                name=name,
                mime=mime,
                status=DocumentStatus.PROCESSING.value,
            )
            session.add(document)
            await session.flush()
            await session.refresh(document)
            await session.commit()
            return _to_record(document)

    async def list_documents(self, *, user_id: str) -> list[DocumentRecord]:
        statement = (
            sa.select(Document)
            .where(Document.user_id == user_id)
            .order_by(Document.created_at.desc(), Document.id)
        )
        async with self._session_factory() as session:
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
            Document.user_id == user_id,
        )
        async with self._session_factory() as session:
            document = await session.scalar(statement)
        if document is None:
            return None
        return _to_record(document)

    async def delete_document(self, *, document_id: uuid.UUID, user_id: str) -> bool:
        statement = sa.delete(Document).where(
            Document.id == document_id,
            Document.user_id == user_id,
        )
        async with self._session_factory() as session:
            result = await session.execute(statement)
            await session.commit()
        return bool(result.rowcount)


class DocumentResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    user_id: str = Field(alias="userId")
    name: str
    mime: str
    status: str
    error: str | None
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")


UserIdForm = Annotated[str, Form(alias="userId", min_length=1)]
UserIdQuery = Annotated[str, Query(alias="userId", min_length=1)]
DocumentUpload = Annotated[UploadFile, File()]


def create_documents_router(
    *,
    repository: DocumentRepository,
    worker: IngestQueue,
    storage: UploadStorage | None = None,
) -> APIRouter:
    router = APIRouter()
    upload_storage = storage or LocalUploadStorage()

    @router.post("/documents", status_code=201, response_model=DocumentResponse)
    async def upload_document(user_id: UserIdForm, file: DocumentUpload) -> DocumentResponse:
        normalized_user_id = _normalize_user_id(user_id)
        path = await upload_storage.save(file)
        document: DocumentRecord | None = None
        try:
            document = await repository.create_processing_document(
                user_id=normalized_user_id,
                name=file.filename or "upload",
                mime=file.content_type or "application/octet-stream",
            )
            await worker.enqueue(IngestJob(document_id=document.id, path=path))
        except Exception:
            path.unlink(missing_ok=True)
            if document is not None:
                await repository.delete_document(
                    document_id=document.id,
                    user_id=normalized_user_id,
                )
            raise
        return _to_response(document)

    @router.get("/documents", response_model=list[DocumentResponse])
    async def list_documents(user_id: UserIdQuery) -> list[DocumentResponse]:
        documents = await repository.list_documents(user_id=_normalize_user_id(user_id))
        return [_to_response(document) for document in documents]

    @router.get("/documents/{document_id}", response_model=DocumentResponse)
    async def get_document(document_id: uuid.UUID, user_id: UserIdQuery) -> DocumentResponse:
        document = await repository.get_document(
            document_id=document_id,
            user_id=_normalize_user_id(user_id),
        )
        if document is None:
            raise HTTPException(status_code=404, detail="document not found")
        return _to_response(document)

    @router.delete("/documents/{document_id}", status_code=204)
    async def delete_document(document_id: uuid.UUID, user_id: UserIdQuery) -> Response:
        deleted = await repository.delete_document(
            document_id=document_id,
            user_id=_normalize_user_id(user_id),
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="document not found")
        return Response(status_code=204)

    return router


def _to_record(document: Document) -> DocumentRecord:
    return DocumentRecord(
        id=document.id,
        user_id=document.user_id,
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
        name=document.name,
        mime=document.mime,
        status=_status_value(document.status),
        error=document.error,
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


def _normalize_user_id(user_id: str) -> str:
    normalized = user_id.strip()
    if not normalized:
        raise HTTPException(status_code=422, detail="userId is required")
    return normalized


def _status_value(status: DocumentStatus | str) -> str:
    if isinstance(status, DocumentStatus):
        return status.value
    return str(status)
