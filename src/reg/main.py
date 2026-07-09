"""reg entrypoint: FastAPI (internal HTTP) + gRPC aio server in one asyncio loop.

Importing this module must not require any provider credentials; settings and
heavy provider imports are loaded lazily inside ``serve()``.
"""

import asyncio
import sys
from pathlib import Path

import grpc
import uvicorn
from fastapi import FastAPI

from reg.config import Settings
from reg.config import get_settings


def create_app() -> FastAPI:
    app = FastAPI(title="reg")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()


def register_grpc_servicers(server: grpc.aio.Server, search_service: object) -> None:
    """Register generated gRPC servicers.

    Generated contracts are expected on PYTHONPATH. Docker uses
    ``/app/contracts-gen``; local development can use PYTHONPATH or the adjacent
    ``../contracts/gen/python`` checkout.
    """
    _ensure_contracts_path()

    from reg.grpc.servicer import add_reg_search_servicer

    add_reg_search_servicer(server, search_service)


async def serve() -> None:
    """Run the HTTP and gRPC servers concurrently in the current event loop."""
    settings = get_settings()
    runtime_app = create_app()
    engine = None
    worker = None

    from reg.db.session import create_engine
    from reg.db.session import create_session_factory
    from reg.http.documents import SqlAlchemyDocumentRepository
    from reg.http.documents import create_documents_router
    from reg.ingest.chunker import HybridDoclingChunker
    from reg.ingest.parser import DoclingParser
    from reg.ingest.pipeline import IngestPipeline
    from reg.ingest.store import SqlAlchemyIngestStore
    from reg.ingest.worker import IngestWorker
    from reg.search.repository import SearchRepository
    from reg.search.service import SearchService

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

    grpc_server = grpc.aio.server()
    register_grpc_servicers(grpc_server, search_service)
    grpc_server.add_insecure_port(f"[::]:{settings.GRPC_PORT}")
    await grpc_server.start()

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
        await grpc_server.stop(grace=5)
        if worker is not None:
            await worker.stop()
        if engine is not None:
            await engine.dispose()


def _create_embedder(settings: Settings) -> object:
    if settings.EMBEDDER == "fake":
        from reg.ingest.embedder import StaticFakeEmbedder

        return StaticFakeEmbedder(dimensions=settings.EMBEDDING_DIM)

    from reg.ingest.embedder import OpenAIEmbedder

    return OpenAIEmbedder(
        api_key=settings.OPENAI_API_KEY or "",
        model=settings.EMBEDDING_MODEL,
    )


def _ensure_contracts_path() -> None:
    candidates = [Path("/app/contracts-gen")]
    for parent in Path(__file__).resolve().parents:
        candidates.append(parent / "contracts" / "gen" / "python")

    for candidate in candidates:
        if candidate.exists() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
