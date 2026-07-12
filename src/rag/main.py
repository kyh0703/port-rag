"""rag entrypoint for the internal FastAPI HTTP server.

Importing this module must not require any provider credentials; settings and
heavy provider imports are loaded lazily inside ``serve()``.
"""

import asyncio
import uvicorn
import sentry_sdk
from fastapi import FastAPI
from sentry_sdk.integrations.fastapi import FastApiIntegration

from rag.config import Settings
from rag.config import get_settings
from rag.http.responses import ok
from rag.http.responses import register_exception_handlers


def create_app() -> FastAPI:
    app = FastAPI(title="rag")
    register_exception_handlers(app)

    @app.get("/healthz")
    async def healthz():
        return ok({"status": "ok"})

    return app


app = create_app()


async def serve() -> None:
    """Run the internal HTTP server."""
    settings = get_settings()
    initialize_sentry(settings)
    runtime_app = create_app()
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

    engine = create_engine(settings.DATABASE_URL)
    session_factory = create_session_factory(engine)
    embedder = _create_embedder(settings)

    worker = IngestWorker(
        IngestPipeline(
            parser=DoclingParser(),
            chunker=HybridDoclingChunker(),
            embedder=embedder,
            store=SqlAlchemyIngestStore(session_factory),
        )
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


def _create_embedder(settings: Settings) -> object:
    if settings.EMBEDDER == "fake":
        from rag.ingest.embedder import StaticFakeEmbedder

        return StaticFakeEmbedder(dimensions=settings.EMBEDDING_DIM)

    from rag.ingest.embedder import OpenAIEmbedder

    return OpenAIEmbedder(
        api_key=settings.OPENAI_API_KEY or "",
        model=settings.EMBEDDING_MODEL,
    )


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
