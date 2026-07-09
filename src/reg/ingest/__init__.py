"""Document ingest pipeline components."""

from reg.ingest.embedder import OpenAIEmbedder
from reg.ingest.embedder import StaticFakeEmbedder
from reg.ingest.chunker import HybridDoclingChunker
from reg.ingest.pipeline import IngestPipeline
from reg.ingest.parser import DoclingParser
from reg.ingest.store import SqlAlchemyIngestStore
from reg.ingest.types import IngestChunk
from reg.ingest.types import IngestJob
from reg.ingest.types import ParsedDocument
from reg.ingest.worker import IngestWorker

__all__ = [
    "IngestChunk",
    "IngestJob",
    "IngestPipeline",
    "IngestWorker",
    "DoclingParser",
    "HybridDoclingChunker",
    "OpenAIEmbedder",
    "ParsedDocument",
    "SqlAlchemyIngestStore",
    "StaticFakeEmbedder",
]
