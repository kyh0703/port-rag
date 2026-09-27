"""Embedding providers for ingest."""

from __future__ import annotations

import asyncio
from time import perf_counter

import httpx
from openai import AsyncOpenAI

from rag.metrics import Metrics


class EmbeddingError(RuntimeError):
    """Safe embedding failure that can be persisted or returned to callers."""


class InternalEmbeddingCredentialProvider:
    """Fetch the current administrator credential for each SDK request."""

    def __init__(
        self,
        *,
        base_url: str,
        internal_server_key: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/internal/rag/embedding-credential"
        self._internal_server_key = internal_server_key
        self._client = client or httpx.AsyncClient(timeout=10.0, follow_redirects=False)

    async def __call__(self) -> str:
        try:
            response = await self._client.get(
                self._url,
                headers={"X-Internal-Server": self._internal_server_key},
                follow_redirects=False,
            )
            if response.status_code == 200:
                payload = response.json()
                data = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(data, dict) and data.get("provider") == "openai":
                    key = data.get("apiKey")
                    if isinstance(key, str) and key.strip() and key == key.strip():
                        return key
        except Exception:
            pass
        # Raise outside the handler: the original exception may contain credentials.
        raise EmbeddingError("Embedding credential unavailable")

    async def aclose(self) -> None:
        await self._client.aclose()


class StaticFakeEmbedder:
    def __init__(self, *, dimensions: int = 1536) -> None:
        self._dimensions = dimensions

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(index) for index, _ in enumerate(texts)]

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_texts([text]))[0]

    def _vector(self, index: int) -> list[float]:
        vector = [0.0] * self._dimensions
        if vector:
            vector[index % self._dimensions] = 1.0
        return vector


class OpenAIEmbedder:
    def __init__(
        self,
        *,
        credential_provider: InternalEmbeddingCredentialProvider,
        model: str = "text-embedding-3-small",
        batch_size: int = 128,
        max_attempts: int = 3,
        initial_backoff_seconds: float = 0.5,
        http_client: httpx.AsyncClient | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self._credential_provider = credential_provider
        self._client = AsyncOpenAI(
            api_key=credential_provider, max_retries=0, http_client=http_client
        )
        self._model = model
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._initial_backoff_seconds = initial_backoff_seconds
        self._metrics = metrics

    async def aclose(self) -> None:
        try:
            await self._client.close()
        finally:
            await self._credential_provider.aclose()

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts, operation="ingest")

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([text], operation="search"))[0]

    async def _embed(self, texts: list[str], *, operation: str) -> list[list[float]]:
        started_at = perf_counter()
        embeddings: list[list[float]] = []
        try:
            for start in range(0, len(texts), self._batch_size):
                batch = texts[start : start + self._batch_size]
                embeddings.extend(await self._embed_batch_with_retry(batch))
            return embeddings
        finally:
            if self._metrics is not None:
                self._metrics.embedding_duration.labels(operation=operation).observe(
                    perf_counter() - started_at
                )

    async def _embed_batch_with_retry(self, texts: list[str]) -> list[list[float]]:
        delay = self._initial_backoff_seconds
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.embeddings.create(model=self._model, input=texts)
                return [item.embedding for item in response.data]
            except Exception:
                if attempt < self._max_attempts:
                    await asyncio.sleep(delay)
                    delay *= 2
        # Do not retain upstream exceptions: OpenAI errors can echo the API key.
        raise EmbeddingError("Embedding request failed")
