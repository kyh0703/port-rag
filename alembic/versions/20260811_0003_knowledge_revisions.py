"""add immutable knowledge revisions

Revision ID: 20260811_0003
Revises: 20260716_0002
Create Date: 2026-08-11 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql


revision: str = "20260811_0003"
down_revision: str | None = "20260716_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_knowledge_revisions_user_id",
        "knowledge_revisions",
        ["user_id"],
    )
    op.create_table(
        "knowledge_revision_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_name", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("embedding", Vector(1536), nullable=False),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            ["knowledge_revisions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_knowledge_revision_chunks_revision_seq",
        "knowledge_revision_chunks",
        ["revision_id", "source_document_id", "seq"],
    )
    op.create_index(
        "ix_knowledge_revision_chunks_embedding_hnsw_cosine",
        "knowledge_revision_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.execute(
        """
        CREATE FUNCTION knowledge_revision_rows_are_immutable()
        RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'knowledge revisions are immutable';
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER knowledge_revision_rows_are_immutable
        BEFORE UPDATE OR DELETE ON knowledge_revisions
        FOR EACH ROW EXECUTE FUNCTION knowledge_revision_rows_are_immutable();
        """
    )
    op.execute(
        """
        CREATE FUNCTION knowledge_revision_chunks_are_immutable()
        RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'knowledge revision chunks are immutable';
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER knowledge_revision_chunks_are_immutable
        BEFORE UPDATE OR DELETE ON knowledge_revision_chunks
        FOR EACH ROW EXECUTE FUNCTION knowledge_revision_chunks_are_immutable();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER knowledge_revision_chunks_are_immutable "
        "ON knowledge_revision_chunks"
    )
    op.execute("DROP FUNCTION knowledge_revision_chunks_are_immutable()")
    op.execute(
        "DROP TRIGGER knowledge_revision_rows_are_immutable ON knowledge_revisions"
    )
    op.execute("DROP FUNCTION knowledge_revision_rows_are_immutable()")
    op.drop_index(
        "ix_knowledge_revision_chunks_embedding_hnsw_cosine",
        table_name="knowledge_revision_chunks",
    )
    op.drop_index(
        "ix_knowledge_revision_chunks_revision_seq",
        table_name="knowledge_revision_chunks",
    )
    op.drop_table("knowledge_revision_chunks")
    op.drop_index("ix_knowledge_revisions_user_id", table_name="knowledge_revisions")
    op.drop_table("knowledge_revisions")
