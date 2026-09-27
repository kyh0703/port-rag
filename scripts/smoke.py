"""End-to-end smoke check for local rag development.

The script starts docker compose with a temporary override, uploads a small
Markdown document, waits for ingest to become ready, and performs an HTTP search.
Real mode requires a reachable API with an administrator OpenAI credential.
Set SMOKE_EMBEDDER=fake explicitly only for isolated tests without that API.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import subprocess
import tempfile
import textwrap
import time
import uuid
from pathlib import Path

import httpx
from alembic import command
from alembic.config import Config

from rag.security.internal_server import validate_internal_server_key


ROOT = Path(__file__).resolve().parents[1]
USER_ID = str(uuid.uuid4())
COMPOSE_PROJECT = f"rag-smoke-{uuid.uuid4().hex[:12]}"
DOCUMENT_TEXT = "Port smoke document. The retrieval keyword is copper-pineapple."


def main() -> None:
    try:
        internal_server_key = _ensure_compose()
        _run_migrations()
        asyncio.run(_roundtrip(internal_server_key))
    finally:
        _run(
            [
                "docker",
                "compose",
                "-p",
                COMPOSE_PROJECT,
                "-f",
                "docker-compose.yml",
                "down",
                "-v",
                "--remove-orphans",
            ],
            check=False,
        )


def _ensure_compose() -> str:
    embedder = _embedder_mode()
    internal_server_key = os.environ.get("INTERNAL_SERVER_KEY", "")
    if not internal_server_key and embedder == "fake":
        internal_server_key = secrets.token_urlsafe(32)
    validate_internal_server_key(internal_server_key)
    compose_env = {
        **os.environ,
        "INTERNAL_SERVER_KEY": internal_server_key,
        "RAG_RETRIEVAL_CAPABILITY_SECRET": secrets.token_urlsafe(32),
    }
    override = textwrap.dedent(
        f"""
        services:
          postgres:
            ports: !override
              - "${{RAG_SMOKE_POSTGRES_PORT:-5432}}:5432"
          rag:
            environment:
              DATABASE_URL: postgresql+asyncpg://port:port@postgres:5432/port
              EMBEDDER: {embedder}
              API_INTERNAL_BASE_URL: "${{API_INTERNAL_BASE_URL:-http://api:8000/api/v1}}"
              INTERNAL_SERVER_KEY: "${{INTERNAL_SERVER_KEY}}"
              RAG_RETRIEVAL_CAPABILITY_SECRET: "${{RAG_RETRIEVAL_CAPABILITY_SECRET}}"
            ports: !override
              - "${{RAG_SMOKE_HTTP_PORT:-8000}}:8000"
        """
    )

    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as file:
        file.write(override)
        override_path = Path(file.name)

    try:
        _run(
            [
                "docker", "compose", "-p", COMPOSE_PROJECT,
                "-f", "docker-compose.yml", "-f", str(override_path),
                "up", "-d", "--build",
            ],
            env=compose_env,
        )
    finally:
        override_path.unlink(missing_ok=True)
    return internal_server_key


def _embedder_mode() -> str:
    configured = os.environ.get("SMOKE_EMBEDDER", "openai")
    if configured not in {"fake", "openai"}:
        raise ValueError("SMOKE_EMBEDDER must be openai or fake")
    return configured


def _run_migrations() -> None:
    postgres_port = os.environ.get("RAG_SMOKE_POSTGRES_PORT", "5432")
    database_url = os.environ.get(
        "RAG_SMOKE_DATABASE_URL",
        f"postgresql+asyncpg://port:port@localhost:{postgres_port}/port",
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    previous_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        command.upgrade(config, "head")
    finally:
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


async def _roundtrip(internal_server_key: str) -> None:
    http_base = os.environ.get("RAG_SMOKE_HTTP_BASE", "http://localhost:8000")

    async with httpx.AsyncClient(
        base_url=http_base,
        headers={"X-Internal-Server": internal_server_key},
        timeout=30.0,
    ) as client:
        await _wait_until_ready(client)
        document_id = await _upload(client)
        await _wait_for_document_ready(client, document_id)
        latency_ms, results_count = await _search(client)
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
    document_id = str(response.json()["data"]["id"])
    print(f"Uploaded document: {document_id}")
    return document_id


async def _wait_for_document_ready(client: httpx.AsyncClient, document_id: str) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        response = await client.get(f"/documents/{document_id}", params={"userId": USER_ID})
        response.raise_for_status()
        document = response.json()["data"]
        status = document["status"]
        if status == "ready":
            print(f"Document ready: {document_id}")
            return
        if status == "failed":
            raise RuntimeError(f"document ingest failed: {document.get('error')}")
        await asyncio.sleep(1)
    raise RuntimeError(f"document {document_id} did not become ready within 120s")


async def _search(client: httpx.AsyncClient) -> tuple[float, int]:
    started = time.perf_counter()
    response = await client.post(
        "/search",
        json={
            "userId": USER_ID,
            "query": "copper pineapple retrieval keyword",
            "topK": 3,
        },
    )
    latency_ms = (time.perf_counter() - started) * 1000
    response.raise_for_status()
    results = response.json()["data"]["results"]
    if not results:
        raise RuntimeError("Search returned no results")
    return latency_ms, len(results)


def _run(
    command_line: list[str], *, check: bool = True, env: dict[str, str] | None = None
) -> None:
    print("+", " ".join(command_line))
    subprocess.run(command_line, cwd=ROOT, check=check, env=env)


if __name__ == "__main__":
    main()
