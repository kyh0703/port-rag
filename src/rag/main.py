"""rag entrypoint for the internal FastAPI HTTP server.

Importing this module must not require any provider credentials; settings and
heavy provider imports are loaded lazily inside ``serve()``.
"""

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
from time import perf_counter
from typing import cast

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
from rag.security.internal_server import (
    InternalServerAuthMiddleware,
    scrub_internal_key_fields,
    validate_internal_server_key,
)


def create_app(*, metrics_enabled: bool = True, internal_server_key: str | None = None) -> FastAPI:
    if internal_server_key is not None:
        validate_internal_server_key(internal_server_key)
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

    # Added last so authentication executes before metrics middleware or body parsing.
    # The import-only app has no configured key and denies all business routes.
    app.add_middleware(InternalServerAuthMiddleware, key=internal_server_key)
    return app


app = create_app()


async def serve() -> None:
    """Run the internal HTTP server."""
    settings = get_settings()
    initialize_sentry(settings)
    runtime_app = create_app(
        metrics_enabled=settings.METRICS_ENABLED,
        internal_server_key=settings.INTERNAL_SERVER_KEY.get_secret_value(),
    )

    from rag.http.account_erasure import AccountErasureService
    from rag.http.account_erasure import create_account_erasure_router
    from rag.db.session import create_engine
    from rag.db.session import create_session_factory
    from rag.http.documents import SqlAlchemyDocumentRepository
    from rag.http.documents import create_documents_router
    from rag.http.knowledge_revisions import create_knowledge_revisions_router
    from rag.http.search import create_search_router
    from rag.http.webpages import create_webpages_router
    from rag.security.private_data import OpenBaoPrivateDataCipher
    from rag.ingest.chunker import HybridDoclingChunker
    from rag.ingest.parser import DoclingParser
    from rag.ingest.pipeline import IngestPipeline
    from rag.ingest.store import SqlAlchemyIngestStore
    from rag.ingest.uploads import LocalUploadStorage
    from rag.ingest.worker import IngestWorker
    from rag.knowledge.revisions import KnowledgeRevisionRepository
    from rag.search.repository import SearchRepository
    from rag.search.service import SearchService
    from rag.security.retrieval_capability import RetrievalCapabilityVerifier
    from rag.security.owner_erasure import SqlAlchemyOwnerErasure
    from rag.webpages.fetch import SafeWebpageFetcher
    from rag.webpages.repository import WebpageRepository
    from rag.webpages.worker import WebpageWorker

    metrics = runtime_app.state.metrics
    async with AsyncExitStack() as resources:
        engine = create_engine(settings.DATABASE_URL, metrics=metrics)
        resources.push_async_callback(engine.dispose)
        session_factory = create_session_factory(engine)
        embedder = _create_embedder(settings, metrics=metrics)
        resources.push_async_callback(embedder.aclose)
        owner_access = SqlAlchemyOwnerErasure(session_factory)
        private_cipher = OpenBaoPrivateDataCipher(
            address=settings.OPENBAO_ADDR,
            ca_file=settings.OPENBAO_CA_CERT_FILE,
            role_file=settings.OPENBAO_ROLE_ID_FILE,
            secret_file=settings.OPENBAO_SECRET_ID_FILE,
            key=settings.OPENBAO_DATA_TRANSIT_KEY,
            lookup_key=settings.OPENBAO_LOOKUP_TRANSIT_KEY,
        )
        resources.push_async_callback(private_cipher.aclose)
        # Multipart spooling and parser working files must remain in volatile memory.
        import tempfile

        volatile_root = LocalUploadStorage._require_volatile_root()
        if not Path(tempfile.gettempdir()).resolve().is_relative_to(volatile_root):
            raise RuntimeError("RAG multipart spooling requires TMPDIR on a memory filesystem")
        storage = LocalUploadStorage(
            owner_access=owner_access,
            cipher=private_cipher,
            staging_root=settings.RAG_UPLOAD_STAGING_ROOT,
            clean_legacy_uploads_on_start=settings.RAG_CLEAN_LEGACY_UPLOADS_ON_START,
        )
        resources.callback(storage.close)

        pipeline = IngestPipeline(
            parser=DoclingParser(),
            chunker=HybridDoclingChunker(),
            embedder=embedder,
            store=SqlAlchemyIngestStore(session_factory, private_cipher),
            storage=storage,
            owner_access=owner_access,
        )
        worker = IngestWorker(
            pipeline, owner_access=owner_access, storage=storage, metrics=metrics,
        )
        worker.start()
        resources.push_async_callback(worker.stop)

        webpage_repository = WebpageRepository(session_factory, private_cipher)
        webpage_fetcher = SafeWebpageFetcher()
        webpage_worker = WebpageWorker(
            repository=webpage_repository, fetcher=webpage_fetcher, embedder=embedder,
            cipher=private_cipher,
        )
        webpage_worker.start()
        resources.push_async_callback(webpage_worker.stop)
        runtime_app.include_router(create_webpages_router(
            repository=webpage_repository, fetcher=webpage_fetcher, owner_access=owner_access,
        ))

        runtime_app.include_router(
            create_documents_router(
                repository=SqlAlchemyDocumentRepository(session_factory, private_cipher),
                worker=worker,
                storage=storage,
                owner_access=owner_access,
                reindexer=pipeline,
            )
        )

        search_service = SearchService(
            embedder=embedder,
            repository=SearchRepository(session_factory, private_cipher),
            default_top_k=settings.TOP_K_DEFAULT,
        )
        capability_verifier = RetrievalCapabilityVerifier(settings.RAG_RETRIEVAL_CAPABILITY_SECRET)

        runtime_app.include_router(
            create_search_router(
                service=search_service,
                capability_verifier=capability_verifier,
                owner_access=owner_access,
            )
        )
        runtime_app.include_router(
            create_knowledge_revisions_router(
                repository=KnowledgeRevisionRepository(session_factory, private_cipher),
                capability_verifier=capability_verifier,
                owner_access=owner_access,
            )
        )
        runtime_app.include_router(create_account_erasure_router(
            service=AccountErasureService(
                repository=owner_access, worker=worker, storage=storage,
                webpage_worker=webpage_worker,
            ),
        ))

        http_config = uvicorn.Config(
            runtime_app,
            host="0.0.0.0",
            port=settings.HTTP_PORT,
            log_level="info",
        )
        http_server = uvicorn.Server(http_config)
        # uvicorn installs signal handlers and returns on SIGINT/SIGTERM.
        await http_server.serve()


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
    return cast(dict[str, object], scrub_internal_key_fields(event))


def _create_embedder(settings: Settings, *, metrics: Metrics) -> object:
    from rag.ingest.embedder import OpenAIEmbedder
    from rag.ingest.embedder import InternalEmbeddingCredentialProvider

    return OpenAIEmbedder(
        credential_provider=InternalEmbeddingCredentialProvider(
            base_url=settings.API_INTERNAL_BASE_URL,
            internal_server_key=settings.INTERNAL_SERVER_KEY.get_secret_value(),
        ),
        model=settings.EMBEDDING_MODEL,
        metrics=metrics,
    )


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
