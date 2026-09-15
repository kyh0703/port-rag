"""enforce document knowledge keys after all writers are deployed

Revision ID: 20260827_0005
Revises: 20260827_0004
Create Date: 2026-08-27 12:10:00.000000
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260827_0005"
down_revision: str | None = "20260827_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM documents WHERE knowledge_key IS NULL) THEN
            RAISE EXCEPTION 'documents.knowledge_key contains NULL rows';
          END IF;
          IF EXISTS (
            SELECT 1 FROM documents
            WHERE knowledge_key !~ '^[a-z][a-z0-9_]{0,127}$'
          ) THEN
            RAISE EXCEPTION 'documents.knowledge_key contains invalid rows';
          END IF;
          IF EXISTS (
            SELECT 1 FROM documents
            GROUP BY user_id, knowledge_key
            HAVING count(*) > 1
          ) THEN
            RAISE EXCEPTION 'documents.knowledge_key contains duplicate rows';
          END IF;
        END $$;
        """
    )
    op.execute(
        "ALTER TABLE documents VALIDATE CONSTRAINT ck_documents_knowledge_key"
    )
    op.execute(
        "ALTER TABLE documents ALTER COLUMN knowledge_key SET NOT NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE documents ALTER COLUMN knowledge_key DROP NOT NULL")
