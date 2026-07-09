"""End-to-end smoke check for local reg development.

The script starts docker compose with a temporary override, uploads a small
Markdown document, waits for ingest to become ready, and performs a gRPC Search.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import grpc
import httpx
from alembic import command
from alembic.config import Config


ROOT = Path(__file__).resolve().parents[1]
USER_ID = "smoke-user"
DOCUMENT_TEXT = "Port smoke document. The retrieval keyword is copper-pineapple."


def main() -> None:
    contracts_gen = _contracts_gen_path()
    sys.path.insert(0, str(contracts_gen))

    try:
        _ensure_compose(contracts_gen)
        _run_migrations()
        asyncio.run(_roundtrip())
    finally:
        _run(
            [
                "docker",
                "compose",
                "down",
                "-v",
                "--remove-orphans",
            ],
            check=False,
        )


def _contracts_gen_path() -> Path:
    for parent in (ROOT, *ROOT.parents):
        candidate = parent / "contracts" / "gen" / "python"
        if (candidate / "port" / "reg" / "v1" / "reg_pb2.py").exists():
            return candidate
    raise RuntimeError("contracts/gen/python with port.reg.v1 generated code was not found")


def _ensure_compose(contracts_gen: Path) -> None:
    embedder = _embedder_mode()
    api_key = os.environ.get("OPENAI_API_KEY", "") if embedder == "openai" else ""
    override = textwrap.dedent(
        f"""
        services:
          postgres:
            ports: !override
              - "${{REG_SMOKE_POSTGRES_PORT:-5432}}:5432"
          reg:
            environment:
              DATABASE_URL: postgresql+asyncpg://reg:reg@postgres:5432/reg
              EMBEDDER: {embedder}
              OPENAI_API_KEY: "{api_key}"
            ports: !override
              - "${{REG_SMOKE_HTTP_PORT:-8000}}:8000"
              - "${{REG_SMOKE_GRPC_PORT:-50051}}:50051"
            volumes:
              - {contracts_gen}:/app/contracts-gen:ro
        """
    )

    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as file:
        file.write(override)
        override_path = Path(file.name)

    try:
        _run(["docker", "compose", "-f", "docker-compose.yml", "-f", str(override_path), "up", "-d", "--build"])
    finally:
        override_path.unlink(missing_ok=True)


def _embedder_mode() -> str:
    configured = os.environ.get("SMOKE_EMBEDDER")
    if configured in {"fake", "openai"}:
        return configured

    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if key and not key.startswith("sk-your-"):
        return "openai"
    return "fake"


def _run_migrations() -> None:
    postgres_port = os.environ.get("REG_SMOKE_POSTGRES_PORT", "5432")
    database_url = os.environ.get(
        "REG_SMOKE_DATABASE_URL",
        f"postgresql+asyncpg://reg:reg@localhost:{postgres_port}/reg",
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


async def _roundtrip() -> None:
    http_base = os.environ.get("REG_SMOKE_HTTP_BASE", "http://localhost:8000")
    grpc_target = os.environ.get("REG_SMOKE_GRPC_TARGET", "localhost:50051")

    async with httpx.AsyncClient(base_url=http_base, timeout=30.0) as client:
        await _wait_until_ready(client)
        document_id = await _upload(client)
        await _wait_for_document_ready(client, document_id)

    latency_ms, results_count = await _search(grpc_target)
    print(f"Search latency: {latency_ms:.2f} ms ({results_count} result(s))")


async def _wait_until_ready(client: httpx.AsyncClient) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            response = await client.get("/healthz")
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(1)
    raise RuntimeError("HTTP healthz did not become ready within 60s")


async def _upload(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/documents",
        data={"userId": USER_ID},
        files={"file": ("smoke.md", DOCUMENT_TEXT.encode(), "text/markdown")},
    )
    response.raise_for_status()
    document_id = str(response.json()["id"])
    print(f"Uploaded document: {document_id}")
    return document_id


async def _wait_for_document_ready(client: httpx.AsyncClient, document_id: str) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        response = await client.get(f"/documents/{document_id}", params={"userId": USER_ID})
        response.raise_for_status()
        document = response.json()
        status = document["status"]
        if status == "ready":
            print(f"Document ready: {document_id}")
            return
        if status == "failed":
            raise RuntimeError(f"document ingest failed: {document.get('error')}")
        await asyncio.sleep(1)
    raise RuntimeError(f"document {document_id} did not become ready within 120s")


async def _search(grpc_target: str) -> tuple[float, int]:
    from port.reg.v1 import reg_pb2
    from port.reg.v1 import reg_pb2_grpc

    async with grpc.aio.insecure_channel(grpc_target) as channel:
        await asyncio.wait_for(channel.channel_ready(), timeout=30)
        stub = reg_pb2_grpc.RegServiceStub(channel)
        request = reg_pb2.SearchRequest(
            user_id=USER_ID,
            query="copper pineapple retrieval keyword",
            top_k=3,
        )
        started = time.perf_counter()
        response = await stub.Search(request, timeout=30)
        latency_ms = (time.perf_counter() - started) * 1000

    if not response.results:
        raise RuntimeError("Search returned no results")
    return latency_ms, len(response.results)


def _run(command_line: list[str], *, check: bool = True) -> None:
    print("+", " ".join(command_line))
    subprocess.run(command_line, cwd=ROOT, check=check)


if __name__ == "__main__":
    main()
