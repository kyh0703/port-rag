# RAG 개인정보 저장 암호화

상태: feature worktree 구현 및 분리한 테스트 환경 검증. 운영 배포·운영 데이터 백필은 실행하지 않았다.

## 구현

- 문서 이름·오류, chunk 본문·메타데이터, 고정 revision의 문서 이름·본문·메타데이터를 OpenBao Transit으로 저장한다.
- 웹페이지 URL·수집 원문·오류도 암호화한다. 내용 변경 감지는 소유자·문서 범위의 OpenBao HMAC 고정 버전 1을 사용한다.
- owner/resource/field를 AAD에 포함한다. 다른 소유자나 문서에 붙인 암호문은 복호화할 수 없다. 앱에 암호화 키를 가져오지 않는다.
- 256 KiB를 초과하는 JSON은 chunk와 암호화된 manifest로 나눈다. manifest의 개수·길이·digest를 확인하고 나서 사용한다. 최대 값은 32 MiB다.
- 업로드 staging 파일은 64 KiB 단위의 암호문으로 저장한다. 최대 업로드는 128 MiB다. 업로드 오류 시 부분 파일을 제거한다.
- 파서 입력은 `/dev/shm` tmpfs에만 복호화한다. 종료·오류 시 파일을 제거한다. multipart spool도 RAM에 두기 위해 `TMPDIR=/dev/shm`이 필수다. 일반 디스크로 대체하지 않는다.
- SQL·HTTP에 돌아가는 문서 정보는 소유권 확인을 거친 복호화 결과다. managed ORM 객체에 평문을 덮어쓰지 않는다.

## 벡터 및 운영 필드

검색용 pgvector 임베딩, UUID/FK, 순서·상태·시각·MIME·knowledge_key 등 운영 필드는 검색 가능한 기존 표현을 유지한다. 본문·파일 암호화가 임베딩의 역추론 위험까지 제거했다고 말하면 안 된다. 외부 식별자·설정 텍스트·백업 등 전체 서비스 개인정보 범위의 추가 점검은 별도다.

## 운영 전환

1. RAG 전용 `rag-private-data` AppRole, `port-rag-private-data` (`chacha20-poly1305`), `port-rag-private-lookup` (`hmac`, 32 bytes)을 만든다. 키는 비반출이다. API의 PII 키나 API의 data key 권한을 RAG에 주지 않는다.
2. RAG UID 1000 소유·mode 0400 credential volume과 CA를 읽기 전용으로 마운트한다. infra의 전용 provisioning 명령은 default dry-run이다.
3. 이전 ingest 작업을 마친 뒤 모든 reader/writer를 맞춰 전환한다. old staging은 owner cleanup 절차로 정리한다. 새 parser는 legacy 평문 staging을 읽지 않는다.
4. 테이블 소유 유지보수 계정으로 기존 DB 표현을 암호문으로 변환한다. 임시 트리거 변경과 모든 행 변경은 한 트랜잭션으로 처리한다.

```sh
PYTHONPATH=src python -m rag.security.private_data_backfill
PYTHONPATH=src python -m rag.security.private_data_backfill --apply
```

default는 read-only inventory다. apply는 100행씩 읽고, 기존 암호문도 검증한다. 고정 revision의 표현 변경 때만 해당 테이블의 변경 방지 트리거를 잠깐 끄며 `ACCESS EXCLUSIVE`로 다른 writer를 막는다. commit 전에 다시 켠다. 실패 시 행과 DDL 모두 rollback한다. 이 절차는 문서 ID·revision·순서·임베딩을 바꾸지 않는다. 운영 worker를 실행하지 않으며 데이터 내용이나 상류 오류를 출력하지 않는다.

DB/WAL/backup 및 기존 임시 파일의 이전 평문은 새 쓰기 경로만으로 없어지지 않는다. 그 정리까지 끝나야 운영 저장 암호화 전환을 완료했다고 보고할 수 있다.

근거: https://openbao.org/docs/next/api/secret/transit/

## 암호화 장애 복구

일시적 OpenBao 오류는 영구 파싱 실패와 분리한다. 소유권을 확인한 DB 상태에는 사용자 본문 없는 고정 machine code `storage_encryption_unavailable`을 기록한다. 암호화된 staging 원본을 보존하고 worker가 0.1초부터 최대 30초 간격으로 재시도한다. 복구 후 저장이 성공하면 원본을 제거한다. 사용자 삭제는 해당 소유자의 지연 재시도를 취소하고 원본을 제거한다.

재시도는 살아 있는 worker의 in-process queue에서 동작한다. 프로세스 재시작 후 자동으로 복구하는 durable queue를 구현한 것은 아니다. worker 종료 시 일시적 오류의 암호화된 원본을 보존한다.

장애·복구·계정 삭제 회귀를 포함한 전체 테스트 178개 통과, 29개 DB·OpenBao 환경 조건으로 skip. 전체 source/test Ruff 검사 통과. 운영 배포·운영 백필은 실행하지 않았다.
