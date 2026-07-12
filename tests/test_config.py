"""Tests for rag.config.Settings."""

import pytest
from pydantic import ValidationError

from rag.config import Settings, get_settings

ENV_VARS = [
    "DATABASE_URL",
    "OPENAI_API_KEY",
    "EMBEDDER",
    "HTTP_PORT",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIM",
    "TOP_K_DEFAULT",
    "SENTRY_DSN",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Isolate each test from ambient env vars and cached settings."""
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def make_settings(**env):
    """Build Settings from explicit env only (ignore any .env file)."""
    return Settings(_env_file=None, **env)


def test_missing_database_url_raises():
    with pytest.raises(ValidationError) as exc_info:
        make_settings(OPENAI_API_KEY="sk-test")
    assert "DATABASE_URL" in str(exc_info.value)


def test_defaults_applied():
    settings = make_settings(
        DATABASE_URL="postgresql+asyncpg://port:port@localhost:5432/port",
        OPENAI_API_KEY="sk-test",
    )
    assert settings.EMBEDDER == "openai"
    assert settings.HTTP_PORT == 8000
    assert settings.EMBEDDING_MODEL == "text-embedding-3-small"
    assert settings.EMBEDDING_DIM == 1536
    assert settings.TOP_K_DEFAULT == 5
    assert settings.SENTRY_DSN is None


def test_sentry_dsn_is_optional():
    settings = make_settings(
        DATABASE_URL="postgresql+asyncpg://port:port@localhost:5432/port",
        EMBEDDER="fake",
        SENTRY_DSN="https://public@example.ingest.sentry.io/1",
    )
    assert settings.SENTRY_DSN == "https://public@example.ingest.sentry.io/1"


def test_embedder_fake_allows_missing_openai_api_key():
    settings = make_settings(
        DATABASE_URL="postgresql+asyncpg://port:port@localhost:5432/port",
        EMBEDDER="fake",
    )
    assert settings.EMBEDDER == "fake"
    assert settings.OPENAI_API_KEY is None


def test_embedder_openai_requires_openai_api_key():
    with pytest.raises(ValidationError) as exc_info:
        make_settings(
            DATABASE_URL="postgresql+asyncpg://port:port@localhost:5432/port",
        )
    assert "OPENAI_API_KEY" in str(exc_info.value)


def test_invalid_embedder_rejected():
    with pytest.raises(ValidationError):
        make_settings(
            DATABASE_URL="postgresql+asyncpg://port:port@localhost:5432/port",
            OPENAI_API_KEY="sk-test",
            EMBEDDER="bogus",
        )


@pytest.mark.parametrize("port", [0, -1, 70000])
def test_invalid_port_rejected(port):
    with pytest.raises(ValidationError):
        make_settings(
            DATABASE_URL="postgresql+asyncpg://port:port@localhost:5432/port",
            OPENAI_API_KEY="sk-test",
            HTTP_PORT=port,
        )


def test_get_settings_is_cached(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://port:port@localhost:5432/port")
    monkeypatch.setenv("EMBEDDER", "fake")
    first = get_settings()
    second = get_settings()
    assert first is second
