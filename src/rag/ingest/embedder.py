"""Embedding providers for ingest."""

from __future__ import annotations

import asyncio

from openai import AsyncOpenAI


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
        api_key: str,
        model: str = "text-embedding-3-small",
        batch_size: int = 128,
        max_attempts: int = 3,
        initial_backoff_seconds: float = 0.5,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self._client = client or AsyncOpenAI(api_key=api_key, max_retries=0)
        self._model = model
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._initial_backoff_seconds = initial_backoff_seconds

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        embeddings: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            embeddings.extend(await self._embed_batch_with_retry(batch))
        return embeddings

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_texts([text]))[0]

    async def _embed_batch_with_retry(self, texts: list[str]) -> list[list[float]]:
        delay = self._initial_backoff_seconds
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.embeddings.create(model=self._model, input=texts)
                return [item.embedding for item in response.data]
            except Exception:
                if attempt == self._max_attempts:
                    raise
                await asyncio.sleep(delay)
                delay *= 2
        raise RuntimeError("embedding retry loop exhausted")
