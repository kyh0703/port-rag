FROM python:3.12-slim

# uv binary from the official distroless image.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Install dependencies first for layer caching (docling/torch are heavy).
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
RUN uv sync --frozen --no-dev

# Run as non-root.
RUN useradd --create-home --uid 1000 rag
USER rag

EXPOSE 8000

CMD ["uv", "run", "--no-sync", "python", "-m", "rag.main"]
