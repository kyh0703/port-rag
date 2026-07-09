# RAG Ingest + Search MVP

## Goal

- 파일 업로드 → Docling 파싱 → 청킹 → 임베딩 → pgvector 저장 파이프라인과
  사용자 스코프 REST Search를 갖춘 reg 서비스 수직 슬라이스를 완성하고
  검증 가능하게 만든다.

## References

- docs/STATE.md
- docs/ROADMAP.md
- docs/ARCHITECTURE.md
- docs/v1/designs/2026-07-09-v1-rag-ingest-search-mvp.md
- docs/v1/research/2026-07-09-v1-parsing-stack-selection.md

## Workspace

- Branch: chore/remove-grpc-rest-response
- Base: main
- Isolation: required
- Created by: exec-plan via git-worktree

## Task Graph

### Task T1
- [x] Complete
- Goal: REST-only 결정으로 `../contracts` reg gRPC 계약/codegen 의존성을
  제거한다. 검색 계약은 내부 HTTP `POST /search` JSON schema와 공통
  `ApiResponse` envelope로 관리한다.
- Depends on:
  - none
- Write Scope:
  - src/reg/http/search.py
  - src/reg/http/responses.py
  - src/reg/main.py
  - pyproject.toml
  - uv.lock
- Read Context:
  - ../api/src/shared/api/types/api-response.type.ts
  - ../api/src/shared/api/interceptors/transform.interceptor.ts
  - ../api/src/shared/api/filters/http-exception.filter.ts
- Checks:
  - uv run pytest tests/http/test_search.py
  - uv run ruff check src/reg/http
- Parallel-safe: yes

### Task T2
- [x] Complete
- Goal: reg Python 서비스 스캐폴딩 — pyproject.toml(uv, Python 3.12,
  fastapi/docling/sqlalchemy[asyncio]/alembic/pgvector/openai/pytest/ruff),
  `src/reg/config.py`(환경변수 검증: DATABASE_URL, OPENAI_API_KEY,
  HTTP_PORT), `src/reg/main.py`(FastAPI HTTP 기동 뼈대, healthz),
  Dockerfile, docker-compose.yml(pgvector 포함),
  .env.example.
- Depends on:
  - none
- Write Scope:
  - pyproject.toml
  - src/reg/config.py
  - src/reg/main.py
  - Dockerfile
  - docker-compose.yml
  - .env.example
  - tests/test_config.py
- Read Context:
  - docs/ARCHITECTURE.md
- Checks:
  - uv sync
  - uv run ruff check .
  - uv run pytest tests/test_config.py
- Parallel-safe: no

### Task T3
- [x] Complete
- Goal: DB 스키마 + 마이그레이션 — documents(id, user_id, name, mime,
  status[processing|ready|failed], error, timestamps), chunks(id,
  document_id FK cascade, seq, text, metadata jsonb, embedding vector(1536)),
  user_id 인덱스 + embedding HNSW(cosine) 인덱스. SQLAlchemy async 모델과
  Alembic 초기 마이그레이션.
- Depends on:
  - T2
- Write Scope:
  - src/reg/db/
  - alembic/
  - alembic.ini
  - tests/db/
- Read Context:
  - docs/v1/designs/2026-07-09-v1-rag-ingest-search-mvp.md
  - src/reg/config.py
- Checks:
  - docker compose up -d postgres && uv run alembic upgrade head
  - uv run pytest tests/db
- Parallel-safe: no

### Task T4
- [x] Complete
- Goal: 인제스트 파이프라인 — Docling 파싱(PDF/docx/pptx/xlsx/md/txt) →
  HybridChunker 청킹 → OpenAI text-embedding-3-small 배치 임베딩
  (backoff 재시도, embedder는 fake 주입 가능 인터페이스) → chunks 저장 →
  문서 상태 ready/failed 전이. asyncio 기반 인프로세스 워커.
- Depends on:
  - T3
- Write Scope:
  - src/reg/ingest/
  - tests/ingest/
- Read Context:
  - src/reg/db/
  - docs/v1/research/2026-07-09-v1-parsing-stack-selection.md
- Checks:
  - uv run pytest tests/ingest (fake embedder, 샘플 pdf/md/docx fixture,
    깨진 파일 → failed 전이 포함)
  - uv run ruff check src/reg/ingest
- Parallel-safe: yes

### Task T5
- [x] Complete
- Goal: 검색 경로 + REST 서버 — `POST /search`에서 쿼리 임베딩 →
  user_id 스코프 pgvector cosine top-k 조회. userId/query 누락은 공통
  `ApiResponse` 오류 envelope로 반환한다.
- Depends on:
  - T3
- Write Scope:
  - src/reg/search/
  - src/reg/http/search.py
  - tests/search/
  - tests/http/test_search.py
- Read Context:
  - src/reg/db/
  - ../api/src/shared/api/
- Checks:
  - uv run pytest tests/search tests/http/test_search.py
  - uv run ruff check src/reg/search src/reg/http
- Parallel-safe: yes

### Task T6
- [x] Complete
- Goal: 내부 HTTP API — POST /documents(멀티파트 업로드, userId 필수,
  즉시 processing 응답 후 T4 워커 위임), GET /documents?userId=,
  GET /documents/{id}, DELETE /documents/{id}(cascade). FastAPI 라우터.
  모든 JSON 응답은 `../api`와 같은 ApiResponse envelope를 사용한다.
- Depends on:
  - T4
- Write Scope:
  - src/reg/http/
  - tests/http/
- Read Context:
  - src/reg/ingest/
  - src/reg/db/
- Checks:
  - uv run pytest tests/http
  - uv run ruff check src/reg/http
- Parallel-safe: yes

### Task T7
- [x] Complete
- Goal: 기동 배선 통합 + e2e 스모크 — main.py에 HTTP 라우터/워커 배선,
  docker compose로 업로드→ready→REST Search 왕복 스모크
  스크립트(`scripts/smoke.py`, 실키 없으면 fake embedder 모드), Search
  지연 간이 측정 출력.
- Depends on:
  - T5
  - T6
- Write Scope:
  - src/reg/main.py
  - scripts/
  - README.md
- Read Context:
  - src/reg/http/
  - src/reg/ingest/
- Checks:
  - docker compose up -d && uv run python scripts/smoke.py
  - uv run pytest
  - uv run ruff check .
- Parallel-safe: no

## Notes

- 레포가 아직 git 저장소가 아님 — exec-plan 시작 전 `git init` + 초기 커밋
  필요.
- REST-only 결정으로 `../contracts` 교차 레포 쓰기는 범위에서 제거한다.
- 청크 크기/오버랩 기본값은 HybridChunker 기본 + max_tokens 512로 시작,
  T4에서 fixture 기반으로 조정 여지.
