"""Deterministic embedding test double; never used by the RAG runtime."""

from rag.security.owner_erasure import OwnerDataErased


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


class MemoryOwnerAdmission:
    def __init__(self) -> None:
        self.erased: set[str] = set()

    async def assert_active(self, user_id: str) -> None:
        if user_id in self.erased:
            raise OwnerDataErased()
