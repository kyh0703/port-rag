"""add document knowledge keys without enforcing not-null

Revision ID: 20260827_0004
Revises: 20260811_0003
Create Date: 2026-08-27 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260827_0004"
down_revision: str | None = "20260811_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("knowledge_key", sa.String(length=128), nullable=True),
    )
    op.execute(
        "UPDATE documents "
        "SET knowledge_key = 'knowledge_' || replace(id::text, '-', '') "
        "WHERE knowledge_key IS NULL"
    )
    op.create_check_constraint(
        "ck_documents_knowledge_key",
        "documents",
        "knowledge_key ~ '^[a-z][a-z0-9_]{0,127}$'",
        postgresql_not_valid=True,
    )
    op.create_index(
        "uq_documents_user_knowledge_key",
        "documents",
        ["user_id", "knowledge_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_documents_user_knowledge_key", table_name="documents")
    op.drop_constraint("ck_documents_knowledge_key", "documents", type_="check")
    op.drop_column("documents", "knowledge_key")
