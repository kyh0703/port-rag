"""SQLAlchemy models for ingested documents and vector chunks."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from rag.db.base import Base

metadata = Base.metadata


class DocumentStatus(enum.StrEnum):
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('processing', 'ready', 'failed')",
            name="ck_documents_status",
        ),
        sa.CheckConstraint(
            "knowledge_key ~ '^[a-z][a-z0-9_]{0,127}$'",
            name="ck_documents_knowledge_key",
        ),
        sa.Index("ix_documents_user_id", "user_id"),
        sa.Index(
            "uq_documents_user_knowledge_key",
            "user_id",
            "knowledge_key",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    knowledge_key: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    mime: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[DocumentStatus] = mapped_column(
        sa.String(16),
        nullable=False,
        default=DocumentStatus.PROCESSING,
        server_default=DocumentStatus.PROCESSING.value,
    )
    error: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )

    chunks: Mapped[list[DocumentChunk]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    webpage: Mapped[DocumentWebpage | None] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )


class DocumentChunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        sa.Index("ix_chunks_document_id_seq", "document_id", "seq"),
        sa.Index(
            "ix_chunks_embedding_hnsw_cosine",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=sa.text("'{}'::jsonb"),
    )
    embedding: Mapped[list[float]] = mapped_column(Vector(1536), nullable=False)

    document: Mapped[Document] = relationship(back_populates="chunks")


class KnowledgeRevision(Base):
    __tablename__ = "knowledge_revisions"
    __table_args__ = (sa.Index("ix_knowledge_revisions_user_id", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )


class KnowledgeRevisionChunk(Base):
    __tablename__ = "knowledge_revision_chunks"
    __table_args__ = (
        sa.Index(
            "ix_knowledge_revision_chunks_revision_seq",
            "revision_id",
            "source_document_id",
            "seq",
        ),
        sa.Index(
            "ix_knowledge_revision_chunks_embedding_hnsw_cosine",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("knowledge_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    document_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    seq: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=sa.text("'{}'::jsonb"),
    )
    embedding: Mapped[list[float]] = mapped_column(Vector(1536), nullable=False)


class DocumentWebpage(Base):
    __tablename__ = "document_webpages"
    __table_args__ = (
        sa.CheckConstraint(
            "sync_status IN ('idle', 'queued', 'running', 'failed')",
            name="ck_document_webpages_sync_status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(urls) = 'array' AND jsonb_array_length(urls) BETWEEN 1 AND 100",
            name="ck_document_webpages_urls",
        ),
        sa.CheckConstraint(
            "queue_reason IN ('initial', 'manual', 'automatic')",
            name="ck_document_webpages_queue_reason",
        ),
        sa.CheckConstraint(
            "(sync_status = 'running') = (claim_token IS NOT NULL AND claimed_at IS NOT NULL)",
            name="ck_document_webpages_claim",
        ),
        sa.CheckConstraint("auto_sync OR next_sync_at IS NULL", name="ck_document_webpages_schedule"),
        sa.Index("ix_document_webpages_due", "next_sync_at",
                 postgresql_where=sa.text("auto_sync = true")),
        sa.Index("ix_document_webpages_queue", "sync_status", "claimed_at"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True,
    )
    urls: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    auto_sync: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False,
                                          server_default=sa.false())
    sync_status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="queued",
                                            server_default="queued")
    queue_reason: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="initial",
                                             server_default="initial")
    content: Mapped[str] = mapped_column(sa.Text, nullable=False, default="", server_default="")
    content_hash: Mapped[str | None] = mapped_column(sa.String(64))
    last_synced_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    next_sync_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    sync_error: Mapped[str | None] = mapped_column(sa.Text)
    last_sync_changed: Mapped[bool | None] = mapped_column(sa.Boolean)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    claimed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    document: Mapped[Document] = relationship(back_populates="webpage")


class KnowledgeRevisionWebpage(Base):
    """Immutable membership; no document FK so deleting a source cannot rewrite history."""

    __tablename__ = "knowledge_revision_webpages"
    revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("knowledge_revisions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
