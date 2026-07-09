---
feature: rag-ingest-search-mvp
created_at: 2026-07-09T15:54:43+09:00
---

# RAG 인제스트 + 검색 MVP

## Goal

사용자가 올린 파일(PDF/txt·md/Office)을 파싱 → 청킹 → 임베딩해 pgvector에
저장하고, voice-agent가 대화 턴마다 호출하는 사용자 스코프 벡터 검색을
gRPC로 제공하는 reg 서비스 v1을 구축한다.

## Context / Inputs

- Source docs:
  - `docs/ARCHITECTURE.md` (경계/제약)
  - `docs/v1/research/2026-07-09-v1-parsing-stack-selection.md` (스택 근거)
- Existing system facts:
  - `../api`(NestJS, pnpm)가 auth·대화 상태·LiveKit 컨트롤 플레인 소유.
    업로드는 api를 경유해 검증된 `userId`와 함께 reg로 전달된다.
  - `../voice-agent`(LiveKit Agents TS 워커)가 STT→LLM→TTS 세션을 실행.
    매 사용자 발화 후 LLM 호출 전에 RAG 조회 결과를 컨텍스트로 주입해야
    한다(주입 코드 자체는 voice-agent 레포 작업이며 본 기능의 Out).
  - `../contracts`(buf)가 proto 소유. 현재 Go+TS codegen 존재, Python 없음.
- External constraints:
  - 임베딩: OpenAI `text-embedding-3-small`(1536차원), API 키는 환경변수.
  - reg는 외부 미노출(클러스터 내부 전용).

## Problem Statement

voice-agent는 현재 사용자 개인 지식 없이 응답한다. 사용자가 문서를 올려도
저장·검색할 곳이 없어 대화 품질이 일반 지식 수준에 머문다. 파일을 벡터
청크로 관리하는 인제스트 파이프라인과, 대화 턴 지연 예산(p95 < 200ms) 안에
동작하는 사용자 스코프 검색 API가 필요하다.

## Decision Drivers

- 파싱/청킹 품질이 RAG 검색 품질의 상한선 — Office 문서 포함 범위.
- Search는 대화 턴 경로에 있어 지연이 사용자 체감에 직결.
- 운영 단순성: 단일 서비스 + 단일 DB로 v1 완결.
- 기존 경계 준수: auth는 api, 계약은 contracts, voice-agent는 소비만.

## Options Considered

### Option A — Python(FastAPI + grpcio) + Docling + pgvector (채택)

- Summary: 단일 Python 서비스. FastAPI 내부 HTTP로 업로드 수락, 인프로세스
  비동기 워커가 Docling 파싱→청킹→임베딩→저장, grpcio(aio)로 Search 제공.
- Pros: 파싱·청킹·임베딩 생태계 최강(Docling HybridChunker), 단일 서비스,
  단일 DB로 운영 단순.
- Cons: 생태계 내 스택 이질성(TS 중심), contracts에 Python codegen 추가 필요.
- Risks: 팀 Python 숙련도. 인프로세스 워커의 처리량 한계(문서량 증가 시).

### Option B — NestJS 단일 스택 + node 파서

- Summary: api와 동일 스택. pdf-parse/mammoth/officeparser로 파싱.
- Pros: 팀 익숙, contracts TS codegen 재사용, 코드 컨벤션 통일.
- Cons: 테이블/레이아웃 이해 없는 파싱 — Office/PDF 품질 저하가 검색 품질
  한계로 직결. 고품질은 LlamaParse 유료 API 의존.
- Risks: 파싱 품질 문제를 나중에 발견하면 스택 재작성 비용.

### Option C — NestJS 본체 + Docling 사이드카(docling-serve)

- Summary: TS 서비스가 파싱만 별도 Docling HTTP 서비스에 위임.
- Pros: 스택 통일 + 파싱 품질 확보.
- Cons: v1에 서비스 2개 배포/운영. 파싱-청킹 경계가 네트워크로 갈라져
  복잡도 증가.
- Risks: MVP에 과한 구조.

## Recommended Option

- Choice: Option A.
- Why now: 업로드 범위(PDF+Office)가 잠긴 시점에서 파싱 품질이 결정 요인.
  Docling을 라이브러리로 쓰는 단일 Python 서비스가 품질과 운영 단순성을
  동시에 만족한다.
- Rejected alternatives: B(품질 한계), C(v1 과잉 구조 — 처리량 병목 확인
  후 v3 후보로 유지).

## Scope Decision

- In:
  - `../contracts`에 reg proto 정의(Search RPC + 내부 인제스트 계약에
    필요한 메시지) 및 Python codegen 추가, TS codegen 재생성.
  - Python 서비스 스캐폴딩: FastAPI(내부 HTTP) + grpcio(aio) 동시 기동,
    config(환경변수 검증), Dockerfile, healthz.
  - DB 스키마: documents(상태 포함) + chunks(vector(1536), HNSW 인덱스),
    마이그레이션(Alembic).
  - 인제스트 파이프라인: 업로드 수락(멀티파트, userId 필수) → 즉시
    `processing` 응답 → 비동기 워커에서 Docling 파싱 → HybridChunker 청킹
    → OpenAI 임베딩 배치 호출(재시도) → 저장 → `ready`/`failed` 전이.
  - gRPC `Search(userId, query, topK, conversationId?)`: 쿼리 임베딩 →
    userId 스코프 pgvector top-k → 청크 텍스트+문서 메타+스코어 반환.
  - 문서 관리 최소 API(내부 HTTP): 목록, 상태 조회, 삭제(cascade).
- Out:
  - voice-agent 쪽 gRPC 클라이언트/컨텍스트 주입 구현(voice-agent 레포
    별도 기능).
  - api 쪽 업로드 프록시 엔드포인트 구현(api 레포 별도 기능).
  - OCR/오디오/이미지, 하이브리드 BM25, 리랭커, MCP tool, 진행률 알림, 인증.
- Deferred:
  - 하이브리드 검색+리랭킹(v2), 인제스트 큐 분리/docling-serve(v3 후보),
    대화 스코프 문서, 원본 파일 보존·재인제스트.

## Open Questions

- contracts Python codegen 방식: buf remote plugin
  (`protocolbuffers/python` + `grpc/python`) 권장 — 계획 단계에서 buf.gen.yaml
  구조 확인 후 확정.
- Docling 모델 웜업 비용 — 프로세스 기동 시 프리로드 vs 첫 요청 lazy 로드.
- 청크 크기/오버랩 기본값(HybridChunker 토큰 상한) — 구현 중 튜닝 여지.

## Plan Handoff

### Source of Truth Docs

- `docs/ARCHITECTURE.md`
- `docs/v1/research/2026-07-09-v1-parsing-stack-selection.md`
- 이 문서

### Scope for Planning

- 위 `Scope Decision > In` 항목 전체를 하나의 계획으로 전환한다.
- 신규 코드 루트: 레포 루트(`/home/overthinker/project/port/reg`) —
  Python 패키지 `src/reg/` 구조 권장.
- `../contracts` proto 추가는 이 계획의 태스크에 포함된다(교차 레포 쓰기).

### Fixed Constraints

- Python 3.12+, FastAPI, grpcio(aio), Docling, SQLAlchemy(async), Alembic,
  pgvector(HNSW), OpenAI `text-embedding-3-small`(1536차원).
- gRPC 계약은 `../contracts` proto가 유일한 원천. reg 레포에 proto 사본
  정의 금지.
- 모든 검색/조회/삭제는 userId 스코프 강제. 시크릿은 환경변수만, 로그 금지.
- reg 전용 Postgres. api DB 접속 금지.

### Success Criteria

- PDF/md/docx 각 1개 업로드 → 상태가 `processing`→`ready`로 전이되고
  chunks에 벡터가 저장된다(로컬 Postgres+pgvector로 확인).
- 고의로 깨진 파일 업로드 → `failed` + 사유 기록, 서비스는 계속 동작.
- gRPC Search가 업로드한 문서 내용 관련 쿼리에 해당 청크를 top-k로
  반환하고, 다른 userId로는 반환하지 않는다.
- 로컬 측정 기준 Search p95 < 200ms(임베딩 API 왕복 포함).
- `../contracts`에서 buf generate가 Python/TS 산출물을 오류 없이 생성.

### Non-Goals

- voice-agent/api/web 레포의 소비자 측 구현.
- 검색 품질 튜닝(리랭커, 하이브리드), 수평 확장, 멀티테넌트 격리 이상의
  보안 강화.

### Open Questions

- 위 `Open Questions` 절 참조(codegen 방식, Docling 웜업, 청크 파라미터).

### Suggested Validation

- 단위: 청킹 경계, 상태 전이, userId 스코프 필터(pytest).
- 통합: docker-compose로 pgvector 기동 → 업로드→ready→Search 왕복
  스모크 스크립트. OpenAI 호출은 통합에서 실키 또는 fake 스위치.
- 계약: `buf lint` + `buf generate` 후 TS/Python 산출물 컴파일 확인.

### Parallelization Hints

- Candidate write boundaries:
  - `../contracts/proto/**` (proto + codegen 설정)
  - `src/reg/db/**` + Alembic (스키마/마이그레이션)
  - `src/reg/ingest/**` (파싱/청킹/임베딩 파이프라인)
  - `src/reg/search/**` + gRPC 서버 (검색 경로)
  - `src/reg/http/**` (업로드/문서 관리 API)
- Shared files to avoid touching in parallel: `src/reg/config.py`,
  `pyproject.toml`, `src/reg/main.py`(기동 배선) — 스캐폴딩 태스크가 먼저
  단독 소유.
- Likely sequential dependencies: proto/codegen → gRPC 서버 구현,
  스키마 → 인제스트/검색, 스캐폴딩 → 나머지 전부.
