FROM python:3.12-slim

RUN pip install --no-cache-dir uv==0.10.4

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Install dependencies first for layer caching (docling/torch are heavy).
# Keep uv's package cache outside image layers.
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src ./src
COPY config ./config
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Run as non-root.
RUN useradd --create-home --uid 1000 rag
USER rag

EXPOSE 8000

CMD ["uv", "run", "--no-sync", "python", "-m", "rag.main"]
