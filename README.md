# reg

Python RAG ingest/search service for Port. It exposes internal HTTP document
management and search endpoints.

## Local Setup

```bash
uv sync
cp .env.example .env
```

For local smoke checks without a real OpenAI key, set:

```bash
EMBEDDER=fake
```

With a real key, use:

```bash
EMBEDDER=openai
OPENAI_API_KEY=sk-...
```

## Environment

- `DATABASE_URL`: async SQLAlchemy URL, for example
  `postgresql+asyncpg://reg:reg@localhost:5432/reg`
- `EMBEDDER`: `openai` or `fake`
- `OPENAI_API_KEY`: required only when `EMBEDDER=openai`
- `HTTP_PORT`: default `8000`
- `EMBEDDING_MODEL`: default `text-embedding-3-small`
- `EMBEDDING_DIM`: default `1536`
- `TOP_K_DEFAULT`: default `5`
- `SENTRY_DSN`: optional Sentry DSN; enables unhandled FastAPI exception reporting

## Checks

```bash
docker compose up -d
uv run alembic upgrade head
uv run python scripts/smoke.py
uv run pytest
uv run ruff check .
```

`scripts/smoke.py` also selects `EMBEDDER=fake` when `OPENAI_API_KEY` is not set.
If host port `5432` is already occupied, run the smoke check with alternate
ports:

```bash
REG_SMOKE_POSTGRES_PORT=55435 \
REG_SMOKE_HTTP_PORT=18082 \
REG_SMOKE_HTTP_BASE=http://localhost:18082 \
SMOKE_EMBEDDER=fake \
uv run python scripts/smoke.py
```
