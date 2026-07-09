# RAG Ingest + Search MVP

## Goal

- 파일 업로드 → Docling 파싱 → 청킹 → 임베딩 → pgvector 저장 파이프라인과
  사용자 스코프 gRPC Search를 갖춘 reg 서비스 수직 슬라이스를 완성하고
  검증 가능하게 만든다.

## References

- docs/STATE.md
- docs/ROADMAP.md
- docs/ARCHITECTURE.md
- docs/v1/designs/2026-07-09-v1-rag-ingest-search-mvp.md
- docs/v1/research/2026-07-09-v1-parsing-stack-selection.md

## Workspace

- Branch: feat/v1-rag-ingest-search-mvp
- Base: main
- Isolation: required
- Created by: exec-plan via git-worktree

## Task Graph

### Task T1
- [ ] Complete
- Goal: `../contracts`에 reg 검색 계약 proto(`proto/port/reg/v1/reg.proto`,
  `RegService.Search`: userId/query/topK/conversationId? → chunks[text,
  documentId, documentName, score, metadata])를 정의하고 Python codegen
  (remote plugin `buf.build/protocolbuffers/python` + `buf.build/grpc/python`,
  out `gen/python`)을 buf.gen.yaml에 추가한 뒤 전체 재생성한다.
- Depends on:
  - none
- Write Scope:
  - ../contracts/proto/port/reg/v1/
  - ../contracts/buf.gen.yaml
  - ../contracts/gen/ (재생성 산출물)
- Read Context:
  - ../contracts/proto/port/api/v1/gateway_events.proto (컨벤션 참고)
  - docs/v1/designs/2026-07-09-v1-rag-ingest-search-mvp.md
- Checks:
  - cd ../contracts && buf lint
  - cd ../contracts && buf generate
- Parallel-safe: yes

### Task T2
- [ ] Complete
- Goal: reg Python 서비스 스캐폴딩 — pyproject.toml(uv, Python 3.12,
  fastapi/grpcio/docling/sqlalchemy[asyncio]/alembic/pgvector/openai/pytest/ruff),
  `src/reg/config.py`(환경변수 검증: DATABASE_URL, OPENAI_API_KEY,
  GRPC_PORT, HTTP_PORT), `src/reg/main.py`(FastAPI + gRPC aio 동시 기동
  뼈대, healthz), Dockerfile, docker-compose.yml(pgvector 포함),
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
- [ ] Complete
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
- [ ] Complete
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
- [ ] Complete
- Goal: 검색 경로 + gRPC 서버 — 쿼리 임베딩 → user_id 스코프 pgvector
  cosine top-k 조회, T1 codegen 기반 `RegService.Search` grpcio(aio)
  서비서 구현. userId 누락/불일치 시 빈 결과가 아닌 INVALID_ARGUMENT.
- Depends on:
  - T1
  - T3
- Write Scope:
  - src/reg/search/
  - src/reg/grpc/
  - tests/search/
- Read Context:
  - ../contracts/gen/python/
  - src/reg/db/
- Checks:
  - uv run pytest tests/search (스코프 격리 테스트 포함)
  - uv run ruff check src/reg/search src/reg/grpc
- Parallel-safe: yes

### Task T6
- [ ] Complete
- Goal: 내부 HTTP API — POST /documents(멀티파트 업로드, userId 필수,
  즉시 processing 응답 후 T4 워커 위임), GET /documents?userId=,
  GET /documents/{id}, DELETE /documents/{id}(cascade). FastAPI 라우터.
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
- [ ] Complete
- Goal: 기동 배선 통합 + e2e 스모크 — main.py에 HTTP 라우터/gRPC 서비서/
  워커 배선, docker compose로 업로드→ready→gRPC Search 왕복 스모크
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
  - src/reg/grpc/
  - src/reg/ingest/
- Checks:
  - docker compose up -d && uv run python scripts/smoke.py
  - uv run pytest
  - uv run ruff check .
- Parallel-safe: no

## Notes

- 레포가 아직 git 저장소가 아님 — exec-plan 시작 전 `git init` + 초기 커밋
  필요.
- T1은 교차 레포(../contracts) 쓰기. contracts는 별도 git 저장소이므로
  worktree 격리 대상이 아니며, 해당 변경은 contracts 레포에서 직접 커밋한다.
- 청크 크기/오버랩 기본값은 HybridChunker 기본 + max_tokens 512로 시작,
  T4에서 fixture 기반으로 조정 여지.
