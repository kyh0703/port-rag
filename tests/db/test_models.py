import enum

import sqlalchemy as sa
from pgvector.sqlalchemy import VECTOR
from sqlalchemy.dialects.postgresql import JSONB

from rag.db.models import DocumentStatus
from rag.db.models import metadata


def test_document_status_values_are_fixed() -> None:
    assert issubclass(DocumentStatus, enum.StrEnum)
    assert {status.value for status in DocumentStatus} == {
        "processing",
        "ready",
        "failed",
    }


def test_documents_table_metadata() -> None:
    documents = metadata.tables["documents"]

    assert set(documents.columns.keys()) >= {
        "id",
        "user_id",
        "name",
        "mime",
        "status",
        "error",
        "created_at",
        "updated_at",
    }
    assert documents.c.id.primary_key
    assert not documents.c.user_id.nullable
    assert not documents.c.name.nullable
    assert not documents.c.mime.nullable
    assert not documents.c.status.nullable
    assert documents.c.error.nullable
    assert not documents.c.created_at.nullable
    assert not documents.c.updated_at.nullable

    status_checks = [
        constraint
        for constraint in documents.constraints
        if isinstance(constraint, sa.CheckConstraint)
        and constraint.name == "ck_documents_status"
    ]
    assert len(status_checks) == 1
    assert all(value in str(status_checks[0].sqltext) for value in ("processing", "ready", "failed"))

    index_columns = {
        index.name: [column.name for column in index.columns]
        for index in documents.indexes
    }
    assert index_columns["ix_documents_user_id"] == ["user_id"]


def test_chunks_table_metadata() -> None:
    chunks = metadata.tables["chunks"]

    assert set(chunks.columns.keys()) >= {
        "id",
        "document_id",
        "seq",
        "text",
        "metadata",
        "embedding",
    }
    assert chunks.c.id.primary_key
    assert not chunks.c.document_id.nullable
    assert not chunks.c.seq.nullable
    assert not chunks.c.text.nullable
    assert not chunks.c.metadata.nullable
    assert not chunks.c.embedding.nullable
    assert isinstance(chunks.c.metadata.type, JSONB)
    assert isinstance(chunks.c.embedding.type, VECTOR)
    assert chunks.c.embedding.type.dim == 1536

    [document_fk] = chunks.c.document_id.foreign_keys
    assert document_fk.column.table.name == "documents"
    assert document_fk.column.name == "id"
    assert document_fk.ondelete == "CASCADE"

    index_columns = {
        index.name: [column.name for column in index.columns]
        for index in chunks.indexes
    }
    assert index_columns["ix_chunks_document_id_seq"] == ["document_id", "seq"]

    [embedding_index] = [
        index for index in chunks.indexes if index.name == "ix_chunks_embedding_hnsw_cosine"
    ]
    assert embedding_index.dialect_options["postgresql"]["using"] == "hnsw"
    assert embedding_index.dialect_options["postgresql"]["ops"] == {
        "embedding": "vector_cosine_ops"
    }
