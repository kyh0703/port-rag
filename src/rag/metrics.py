"""Prometheus metrics for the RAG service."""

from time import perf_counter

from prometheus_client import CollectorRegistry
from prometheus_client import Counter
from prometheus_client import Gauge
from prometheus_client import Histogram


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.http_requests = Counter(
            "rag_http_requests_total",
            "HTTP requests processed.",
            ["route", "status"],
            registry=self.registry,
        )
        self.http_request_duration = Histogram(
            "rag_http_request_duration_seconds",
            "HTTP request duration.",
            ["route"],
            registry=self.registry,
        )
        self.ingest_queue_depth = Gauge(
            "rag_ingest_queue_depth",
            "Current ingest queue depth.",
            registry=self.registry,
        )
        self.ingest_jobs = Counter(
            "rag_ingest_jobs_total",
            "Ingest jobs processed.",
            ["result"],
            registry=self.registry,
        )
        self.embedding_duration = Histogram(
            "rag_embedding_duration_seconds",
            "Embedding operation duration.",
            ["operation"],
            registry=self.registry,
        )
        self.db_query_duration = Histogram(
            "rag_db_query_duration_seconds",
            "Database query duration.",
            ["operation"],
            registry=self.registry,
        )

    def observe_http(self, *, route: str, status: int, started_at: float) -> None:
        self.http_requests.labels(route=route, status=str(status)).inc()
        self.http_request_duration.labels(route=route).observe(perf_counter() - started_at)
