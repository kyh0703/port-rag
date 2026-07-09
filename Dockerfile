FROM python:3.12-slim

# uv binary from the official distroless image.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Python codegen for the gRPC contracts lives in ../contracts and is generated
# outside this build. It must be importable at runtime from /app/contracts-gen:
# either bind-mount it (docker-compose volume) or COPY it in with a build arg
# once contract codegen packaging is decided. Kept as a PYTHONPATH placeholder
# for now — T7 wires the actual servicers.
ENV PYTHONPATH=/app/contracts-gen

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Install dependencies first for layer caching (docling/torch are heavy).
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
RUN uv sync --frozen --no-dev

# Run as non-root.
RUN useradd --create-home --uid 1000 reg
USER reg

EXPOSE 8000 50051

CMD ["uv", "run", "--no-sync", "python", "-m", "reg.main"]
