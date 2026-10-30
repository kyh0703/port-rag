# State

current_version: v1

## Active

- Version: v1
- Focus: RAG 인제스트/검색 서비스 MVP — 파일 업로드 → Docling 파싱 → 청킹 →
  임베딩 → pgvector 저장, voice-agent 대화 턴별 REST 검색 제공
- Current feature: rag-ingest-search-mvp

## 병렬 테스트

- `uv run pytest`는 기본 두 워커를 사용한다. `-n 0`은 단일 프로세스 실행이다.
- `TEST_RAG_DATABASE_URL`은 테스트 전용 PostgreSQL을 가리켜야 한다. 워커는
  `CREATEDB` 권한으로 독립 `rag_pytest_<UUID>` DB를 만들고 고정 Alembic
  migration을 적용한 후 삭제한다. 강제 종료 시 남은 테스트 DB를 확인한다.
- pgvector PostgreSQL 17에서 두 워커의 전체 회귀를 검증했다. 실제 OpenBao에
  의존하는 테스트는 별도 설정이 필요하다. CI도 격리 pgvector 서비스를 쓴다.

## 독립 크롤러

- 웹페이지 네트워크 수집과 JS 렌더링은 `../crawler`의 Rust/Chromium 서버로 분리했다.
- `CRAWLER_URL`은 호스트 개발에서 `http://localhost:8002`, Compose/Kubernetes에서는
  `http://crawler:8000`이다. 기존 내부 키를 사용하며 로컬 수집 fallback은 없다.
- RAG의 지식 등록·동기화 큐·임베딩·게시·삭제 경계는 유지했다.
- 격리 pgvector 전체 회귀 338개 통과, 별도 TLS OpenBao 1개 미설정으로 skip.
  실제 RAG HTTP 클라이언트 → macOS Rust 서버 및 sandbox 유지 Linux/arm64
  컨테이너 → Chromium의 JS 페이지 수집을 검증했다.

## Completed Versions

(없음)
