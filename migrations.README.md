# 마이그레이션 원본

Alembic revision의 편집 원본은 sibling `../spec/migrations/rag/`이며 원격 저장소는 `kyh0703/port-spec`이다. `alembic/env.py`와 모델 metadata·실행 설정은 RAG가 소유한다.

`migration-source.lock.json`과 `vendor/migrations.tar.gz`가 spec의 특정 commit을 고정한다. Docker build·CI·pytest는 checksum을 검증한 실행 파일을 `alembic/versions/`에 준비한다. 이 디렉터리의 생성 파일은 Git에서 제외한다.

로컬 Alembic 명령 전에는 다음 준비 단계를 실행한다. 준비와 revision 목록 확인은 DB를 변경하지 않는다.

```bash
python3 scripts/prepare_migrations.py
uv run alembic heads
uv run alembic history
```

새 revision을 생성하면 해당 파일을 `../spec/migrations/rag/`로 이동해 manifest 등록·커밋 후 bundle을 갱신한다. 기존 revision ID와 down_revision을 변경하지 않는다.

spec에서 소비 버전 갱신:

```bash
python3 scripts/bundle_migrations.py rag --service-root ../rag
```

전체 절차와 복구 기준은 [spec migration 문서](../spec/migrations/README.md)를 따른다.
