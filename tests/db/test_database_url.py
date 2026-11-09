from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from rag.db.database_url import normalize_asyncpg_url


def test_normalize_asyncpg_url_translates_libpq_sslmode() -> None:
    url = normalize_asyncpg_url(
        "postgresql+asyncpg://user:password@database.example.com:5432/rag"
        "?sslmode=require&application_name=rag",
    )

    assert "sslmode=" not in url
    assert "ssl=require" in url
    assert "application_name=rag" in url


def test_normalize_asyncpg_url_leaves_urls_without_sslmode_unchanged() -> None:
    url = "postgresql+asyncpg://user:password@database.example.com:5432/rag"

    assert normalize_asyncpg_url(url) == url


def test_alembic_preserves_encoded_password_and_tls_options(monkeypatch) -> None:
    root = Path(__file__).resolve().parents[2]
    config = Config(output_buffer=StringIO())
    config.set_main_option("script_location", str(root / "alembic"))
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres.project:pa%40ss%25word%2F@pooler.example.com:5432/postgres"
        "?sslmode=require",
    )

    # Execute the real migration environment without connecting to an external database.
    command.upgrade(config, "base", sql=True)

    url = make_url(config.get_main_option("sqlalchemy.url"))
    assert url.password == "pa@ss%word/"
    assert url.username == "postgres.project"
    assert url.query["ssl"] == "require"
