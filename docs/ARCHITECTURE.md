# 아키텍처

## 목적

- `reg`는 port 생태계의 **RAG 인제스트/검색 서비스**다. 사용자가 올린 파일을
  파싱 → 청킹 → 임베딩해 pgvector에 저장하고, `../voice-agent`가 대화 턴마다
  호출하는 벡터 검색을 gRPC로 제공한다.
- 모든 버전에 공통된 구조적 원리를 기록한다. 버전별 세부 디자인은
  `docs/vN/designs/`에 둔다.

## 공유 경계

- 코어 도메인:
  - Document: 업로드된 원본 파일의 메타데이터와 인제스트 상태
    (processing/ready/failed). 소유자는 `userId`(api가 인증한 사용자 id).
  - Chunk: 파싱된 문서에서 분할된 텍스트 조각 + 임베딩 벡터 + 원문 위치
    메타데이터.
  - Search: 쿼리 텍스트를 임베딩해 사용자 스코프 안에서 top-k 청크를
    반환하는 읽기 경로.
- 외부 통합:
  - `../api` (NestJS): 유일한 업로드 진입점. auth를 소유하고, 검증된
    `userId`와 파일을 reg 내부 HTTP로 전달한다. reg는 외부에 직접
    노출되지 않고 자체 인증을 구현하지 않는다.
  - `../voice-agent` (LiveKit Agents TS 워커): gRPC 클라이언트. 매 사용자
    발화마다 `Search`를 호출해 top-k 청크를 LLM 컨텍스트에 주입한다.
  - `../contracts` (buf): reg의 gRPC proto 정의 위치. TS(voice-agent용)와
    Python(reg 서버용) codegen을 모두 생성한다.
  - OpenAI Embeddings API: `text-embedding-3-small` (1536차원). 인제스트와
    검색 쿼리 임베딩 모두 이 모델 하나로 통일한다(차원/모델 불일치 금지).
  - Docling: PDF/docx/pptx/xlsx/md/txt → 구조화 마크다운 파싱. 서비스
    프로세스 내 라이브러리로 사용한다(v1은 별도 파싱 서비스 없음).
- 데이터 경계:
  - reg 전용 PostgreSQL(+pgvector) 인스턴스. api DB와 공유하지 않는다 —
    벡터 인덱스 부하를 격리하고 스키마 수명주기를 독립시킨다.
  - 원본 파일은 인제스트 완료 후 보존하지 않는다(v1). 재인제스트는
    재업로드로 처리한다.
  - 청크/벡터는 문서 삭제 시 함께 삭제된다(FK cascade).

## 공유 제약 조건

- 보안:
  - OpenAI/DB 자격 증명은 환경변수로만 주입하고 로그에 남기지 않는다.
  - 모든 조회/삭제는 `userId` 스코프를 강제한다. 스코프 없는 검색 경로를
    만들지 않는다.
  - reg는 클러스터 내부 트래픽만 받는다(외부 ingress 없음).
- 신뢰성:
  - 인제스트는 비동기다. 업로드 수락 즉시 `processing`으로 응답하고,
    파싱/임베딩 실패는 문서 상태 `failed`+사유로 기록한다. 실패가 서비스
    전체를 죽이지 않는다.
  - 임베딩 API 호출은 배치 + 재시도(backoff)로 감싼다.
- 성능:
  - Search는 대화 턴 경로에 있다 — p95 < 200ms 목표. 쿼리 임베딩 1회 +
    pgvector 인덱스(HNSW) 조회로 구성한다.
  - 인제스트 처리량은 v1에서 인프로세스 워커로 충분하다고 가정하고, 병목이
    확인되면 큐/워커 분리는 다음 버전에서 결정한다.
- 작동 제한:
  - 스택: Python 3.12+, FastAPI(내부 HTTP), grpcio(aio), Docling,
    SQLAlchemy(async) + pgvector.
  - gRPC 계약 변경은 반드시 `../contracts` proto를 먼저 수정하고 양쪽
    codegen을 재생성한다.
