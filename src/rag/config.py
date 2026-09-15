"""Runtime settings for rag."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
import yaml
from rag.security.internal_server import validate_internal_server_key

CONFIG_ROOT = Path(__file__).resolve().parents[2]


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(
    loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"duplicate YAML key {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


class CategorizedYamlSettingsSource(PydanticBaseSettingsSource):
    """Load categorized YAML while keeping existing environment variable names."""

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self.data: dict[str, object] = {}
        self.field_names = set(settings_cls.model_fields)
        for path in (CONFIG_ROOT / "config/default.yaml", CONFIG_ROOT / "config/local.yaml"):
            if path.is_file():
                with path.open(encoding="utf-8") as stream:
                    document = yaml.load(stream, Loader=UniqueKeyLoader) or {}
                flattened: dict[str, object] = {}
                self._flatten(document, flattened)
                self.data.update(flattened)

    def _flatten(self, value: object, result: dict[str, object]) -> None:
        if not isinstance(value, dict):
            raise ValueError("YAML categories must contain a mapping")
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("YAML leaf names must be strings")
            if isinstance(child, dict):
                if key in self.field_names:
                    raise ValueError(f"YAML leaf {key} must be scalar")
                self._flatten(child, result)
            elif isinstance(child, (list, tuple)):
                raise ValueError(f"YAML leaf {key} must be scalar")
            else:
                if key in result:
                    raise ValueError(f"duplicate YAML leaf {key}")
                result[key] = child

    def __call__(self) -> dict[str, object]:
        return self.data

    def get_field_value(self, field: object, field_name: str) -> tuple[object, str, bool]:
        return self.data.get(field_name), field_name, False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    DATABASE_URL: str = Field(..., min_length=1)
    INTERNAL_SERVER_KEY: SecretStr
    RAG_RETRIEVAL_CAPABILITY_SECRET: str = Field(..., min_length=32)
    OPENAI_API_KEY: str | None = None
    EMBEDDER: Literal["openai", "fake"] = "openai"
    HTTP_PORT: int = Field(8000, ge=1, le=65535)
    METRICS_ENABLED: bool = True
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIM: int = 1536
    TOP_K_DEFAULT: int = 5
    SENTRY_DSN: str | None = None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            CategorizedYamlSettingsSource(settings_cls),
            file_secret_settings,
        )

    @model_validator(mode="after")
    def require_openai_key_for_openai_embedder(self) -> "Settings":
        validate_internal_server_key(self.INTERNAL_SERVER_KEY.get_secret_value())
        if self.EMBEDDER == "openai" and not self.OPENAI_API_KEY:
            raise ValueError("OPENAI_API_KEY is required when EMBEDDER=openai")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
