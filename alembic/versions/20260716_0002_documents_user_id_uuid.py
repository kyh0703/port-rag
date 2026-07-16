"""store document owner identifiers as UUID

Revision ID: 20260716_0002
Revises: 20260709_0001
Create Date: 2026-07-16 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260716_0002"
down_revision: str | None = "20260709_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "documents",
        "user_id",
        existing_type=sa.Text(),
        type_=postgresql.UUID(as_uuid=True),
        postgresql_using="user_id::uuid",
    )


def downgrade() -> None:
    op.alter_column(
        "documents",
        "user_id",
        existing_type=postgresql.UUID(as_uuid=True),
        type_=sa.Text(),
        postgresql_using="user_id::text",
    )
