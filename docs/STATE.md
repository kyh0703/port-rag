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

## Completed Versions

(없음)
