import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_knowledge_revision_migration_copies_chunks_and_blocks_mutation() -> None:
    migration = (
        ROOT
        / "alembic"
        / "versions"
        / "20260811_0003_knowledge_revisions.py"
    ).read_text()

    assert '"knowledge_revisions"' in migration
    assert '"knowledge_revision_chunks"' in migration
    assert 'ondelete="RESTRICT"' in migration
    assert 'Vector(1536)' in migration
    assert "knowledge_revision_rows_are_immutable" in migration
    assert "knowledge_revision_chunks_are_immutable" in migration
    assert "BEFORE UPDATE OR DELETE" in migration

    execute_sql = [
        ast.literal_eval(node.args[0])
        for node in ast.walk(ast.parse(migration))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "op"
        and node.func.attr == "execute"
        and node.args
    ]
    assert all(
        not ("CREATE FUNCTION" in statement and "CREATE TRIGGER" in statement)
        for statement in execute_sql
    )


def test_container_packages_alembic_migrations() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert "COPY alembic.ini ./alembic.ini" in dockerfile
    assert "COPY alembic ./alembic" in dockerfile
