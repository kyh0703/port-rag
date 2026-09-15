from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_expand_migration_adds_backfill_validation_and_account_scope() -> None:
    migration = (
        ROOT / "alembic" / "versions" / "20260827_0004_document_knowledge_key_expand.py"
    ).read_text()

    assert "op.add_column(" in migration
    assert '"documents",' in migration
    assert '"knowledge_key"' in migration
    assert "knowledge_" in migration
    assert "^[a-z][a-z0-9_]{0,127}$" in migration
    assert "uq_documents_user_knowledge_key" in migration
    assert '["user_id", "knowledge_key"]' in migration


def test_contract_migration_refuses_invalid_rows_before_not_null() -> None:
    migration = (
        ROOT / "alembic" / "versions" / "20260827_0005_document_knowledge_key_contract.py"
    ).read_text()

    assert "knowledge_key IS NULL" in migration
    assert "GROUP BY user_id, knowledge_key" in migration
    assert "SET NOT NULL" in migration
    assert migration.split("def downgrade", 1)[0].count("op.execute(") == 3
