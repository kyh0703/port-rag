"""Tests for rag.config.Settings."""

import pytest
from pydantic import ValidationError

import rag.config as config
from rag.config import Settings, get_settings

ENV_VARS = [
    "DATABASE_URL",
    "RAG_RETRIEVAL_CAPABILITY_SECRET",
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
    return Settings(
        _env_file=None,
        RAG_RETRIEVAL_CAPABILITY_SECRET="a-32-byte-minimum-retrieval-secret",
        **env,
    )


def test_missing_database_url_raises():
    with pytest.raises(ValidationError) as exc_info:
        make_settings(OPENAI_API_KEY="sk-test")
    assert "DATABASE_URL" in str(exc_info.value)


def test_short_retrieval_capability_secret_is_rejected():
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            DATABASE_URL="postgresql+asyncpg://port:port@localhost:5432/port",
            EMBEDDER="fake",
            RAG_RETRIEVAL_CAPABILITY_SECRET="short",
        )
    assert "RAG_RETRIEVAL_CAPABILITY_SECRET" in str(exc_info.value)


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
    monkeypatch.setenv(
        "RAG_RETRIEVAL_CAPABILITY_SECRET",
        "a-32-byte-minimum-retrieval-secret",
    )
    first = get_settings()
    second = get_settings()
    assert first is second


def test_categorized_local_yaml_is_loaded_as_flat_settings(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "local.yaml").write_text(
        """
database:
  DATABASE_URL: postgresql+asyncpg://yaml:y@localhost:5432/yaml
auth:
  RAG_RETRIEVAL_CAPABILITY_SECRET: yaml-secret-that-is-long-enough-123
embedding:
  EMBEDDER: fake
  EMBEDDING_DIM: 768
"""
    )
    monkeypatch.setattr(config, "CONFIG_ROOT", tmp_path)

    settings = Settings(_env_file=None)

    assert settings.DATABASE_URL.endswith("/yaml")
    assert settings.EMBEDDER == "fake"
    assert settings.EMBEDDING_DIM == 768


def test_environment_overrides_categorized_yaml(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "local.yaml").write_text(
        """
database:
  DATABASE_URL: postgresql+asyncpg://yaml:y@localhost:5432/yaml
auth:
  RAG_RETRIEVAL_CAPABILITY_SECRET: yaml-secret-that-is-long-enough-123
embedding:
  EMBEDDER: fake
"""
    )
    monkeypatch.setattr(config, "CONFIG_ROOT", tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://env:e@localhost:5432/env")

    settings = Settings(_env_file=None)

    assert settings.DATABASE_URL.endswith("/env")


def test_duplicate_yaml_leaf_is_rejected(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "local.yaml").write_text(
        """
database:
  DATABASE_URL: postgresql+asyncpg://yaml:y@localhost:5432/yaml
other:
  DATABASE_URL: postgresql+asyncpg://other:o@localhost:5432/other
"""
    )
    monkeypatch.setattr(config, "CONFIG_ROOT", tmp_path)

    with pytest.raises(ValueError, match="duplicate YAML leaf DATABASE_URL"):
        Settings(_env_file=None)


def test_non_scalar_yaml_leaf_is_rejected(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "local.yaml").write_text(
        """
database:
  DATABASE_URL:
    nested: value
"""
    )
    monkeypatch.setattr(config, "CONFIG_ROOT", tmp_path)

    with pytest.raises(ValueError, match="must be scalar"):
        Settings(_env_file=None)


def test_duplicate_yaml_key_in_same_mapping_is_rejected(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "local.yaml").write_text(
        """
database:
  DATABASE_URL: first
  DATABASE_URL: second
"""
    )
    monkeypatch.setattr(config, "CONFIG_ROOT", tmp_path)

    with pytest.raises(ValueError, match="duplicate YAML key DATABASE_URL"):
        Settings(_env_file=None)
