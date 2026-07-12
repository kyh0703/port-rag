from fastapi.testclient import TestClient

from rag.main import create_app


def test_metrics_endpoint_exposes_required_application_metrics() -> None:
    response = TestClient(create_app(metrics_enabled=True)).get("/metrics")

    assert response.status_code == 200
    for metric in (
        "rag_http_requests_total",
        "rag_http_request_duration_seconds",
        "rag_ingest_queue_depth",
        "rag_ingest_jobs_total",
        "rag_embedding_duration_seconds",
        "rag_db_query_duration_seconds",
    ):
        assert metric in response.text


def test_metrics_endpoint_can_be_disabled() -> None:
    response = TestClient(create_app(metrics_enabled=False)).get("/metrics")

    assert response.status_code == 404
