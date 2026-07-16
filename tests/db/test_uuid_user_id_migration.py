from __future__ import annotations

from pathlib import Path

from sqlalchemy.dialects.postgresql import UUID

from rag.db.models import metadata


def test_documents_user_id_uses_postgresql_uuid() -> None:
    assert isinstance(metadata.tables["documents"].c.user_id.type, UUID)


def test_uuid_user_id_forward_migration_exists() -> None:
    migration = (
        Path(__file__).parents[2]
        / "alembic"
        / "versions"
        / "20260716_0002_documents_user_id_uuid.py"
    )

    assert migration.read_text().count("postgresql_using=\"user_id::uuid\"") == 1
