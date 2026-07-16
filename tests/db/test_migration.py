from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_alembic_initial_migration_contains_required_schema() -> None:
    versions = ROOT / "alembic" / "versions"
    migration = (versions / "20260709_0001_initial_documents_chunks.py").read_text()
    assert "CREATE EXTENSION IF NOT EXISTS vector" in migration
    assert "create_table(\n        \"documents\"" in migration
    assert "create_table(\n        \"chunks\"" in migration
    assert "ck_documents_status" in migration
    assert "processing" in migration
    assert "ready" in migration
    assert "failed" in migration
    assert "ForeignKeyConstraint([\"document_id\"], [\"documents.id\"], ondelete=\"CASCADE\")" in migration
    assert "Vector(1536)" in migration
    assert "postgresql_using=\"hnsw\"" in migration
    assert "vector_cosine_ops" in migration
