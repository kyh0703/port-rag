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
