"""Runtime settings for rag."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = Field(..., min_length=1)
    OPENAI_API_KEY: str | None = None
    EMBEDDER: Literal["openai", "fake"] = "openai"
    HTTP_PORT: int = Field(8000, ge=1, le=65535)
    METRICS_ENABLED: bool = True
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIM: int = 1536
    TOP_K_DEFAULT: int = 5
    SENTRY_DSN: str | None = None

    @model_validator(mode="after")
    def require_openai_key_for_openai_embedder(self) -> "Settings":
        if self.EMBEDDER == "openai" and not self.OPENAI_API_KEY:
            raise ValueError("OPENAI_API_KEY is required when EMBEDDER=openai")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
