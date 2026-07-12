"""rag entrypoint for the internal FastAPI HTTP server.

Importing this module must not require any provider credentials; settings and
heavy provider imports are loaded lazily inside ``serve()``.
"""

import asyncio
from time import perf_counter

import uvicorn
import sentry_sdk
from fastapi import FastAPI
from prometheus_client import make_asgi_app
from sentry_sdk.integrations.fastapi import FastApiIntegration

from rag.config import Settings
from rag.config import get_settings
from rag.metrics import Metrics
from rag.http.responses import ok
from rag.http.responses import register_exception_handlers


def create_app(*, metrics_enabled: bool = True) -> FastAPI:
    app = FastAPI(title="rag")
    register_exception_handlers(app)
    metrics = Metrics()
    app.state.metrics = metrics

    @app.middleware("http")
    async def observe_http_requests(request, call_next):
        started_at = perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            metrics.observe_http(route=request.url.path, status=500, started_at=started_at)
            raise

        route = request.scope.get("route")
        route_path = getattr(route, "path", request.url.path)
        metrics.observe_http(route=route_path, status=response.status_code, started_at=started_at)
        return response

    if metrics_enabled:
        app.mount("/metrics", make_asgi_app(registry=metrics.registry))

    @app.get("/healthz")
    async def healthz():
        return ok({"status": "ok"})

    return app


app = create_app()


async def serve() -> None:
    """Run the internal HTTP server."""
    settings = get_settings()
    initialize_sentry(settings)
    runtime_app = create_app(metrics_enabled=settings.METRICS_ENABLED)
    engine = None
    worker = None

    from rag.db.session import create_engine
    from rag.db.session import create_session_factory
    from rag.http.documents import SqlAlchemyDocumentRepository
    from rag.http.documents import create_documents_router
    from rag.http.search import create_search_router
    from rag.ingest.chunker import HybridDoclingChunker
    from rag.ingest.parser import DoclingParser
    from rag.ingest.pipeline import IngestPipeline
    from rag.ingest.store import SqlAlchemyIngestStore
    from rag.ingest.worker import IngestWorker
    from rag.search.repository import SearchRepository
    from rag.search.service import SearchService

    metrics = runtime_app.state.metrics
    engine = create_engine(settings.DATABASE_URL, metrics=metrics)
    session_factory = create_session_factory(engine)
    embedder = _create_embedder(settings, metrics=metrics)

    worker = IngestWorker(
        IngestPipeline(
            parser=DoclingParser(),
            chunker=HybridDoclingChunker(),
            embedder=embedder,
            store=SqlAlchemyIngestStore(session_factory),
        ),
        metrics=metrics,
    )
    worker.start()

    runtime_app.include_router(
        create_documents_router(
            repository=SqlAlchemyDocumentRepository(session_factory),
            worker=worker,
        )
    )

    search_service = SearchService(
        embedder=embedder,
        repository=SearchRepository(session_factory),
        default_top_k=settings.TOP_K_DEFAULT,
    )

    runtime_app.include_router(create_search_router(service=search_service))

    http_config = uvicorn.Config(
        runtime_app,
        host="0.0.0.0",
        port=settings.HTTP_PORT,
        log_level="info",
    )
    http_server = uvicorn.Server(http_config)

    try:
        # uvicorn installs signal handlers and returns on SIGINT/SIGTERM.
        await http_server.serve()
    finally:
        if worker is not None:
            await worker.stop()
        if engine is not None:
            await engine.dispose()


def initialize_sentry(settings: Settings) -> None:
    """Enable Sentry error reporting when a DSN is configured."""
    if settings.SENTRY_DSN:
        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            integrations=[FastApiIntegration()],
            send_default_pii=False,
            max_request_body_size="never",
            include_local_variables=False,
            before_send=scrub_sentry_event,
        )


def scrub_sentry_event(event: dict[str, object], hint: dict[str, object]) -> dict[str, object]:
    """Remove sensitive HTTP request data before sending an event."""
    request = event.get("request")
    if isinstance(request, dict):
        for key in ("data", "query_string", "headers"):
            request.pop(key, None)
    return event


def _create_embedder(settings: Settings, *, metrics: Metrics) -> object:
    if settings.EMBEDDER == "fake":
        from rag.ingest.embedder import StaticFakeEmbedder

        return StaticFakeEmbedder(dimensions=settings.EMBEDDING_DIM)

    from rag.ingest.embedder import OpenAIEmbedder

    return OpenAIEmbedder(
        api_key=settings.OPENAI_API_KEY or "",
        model=settings.EMBEDDING_MODEL,
        metrics=metrics,
    )


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
