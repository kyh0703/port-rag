"""Search DTOs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SearchHit:
    chunk_id: str
    document_id: str
    document_name: str
    text: str
    score: float
    metadata: dict[str, str]
    seq: int
