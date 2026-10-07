import uuid
import os

import pytest
import sqlalchemy as sa
from rag.db.session import create_engine
from rag.security.private_data_backfill import backfill
from rag.security.owner_erasure import SqlAlchemyOwnerErasure
from rag.db.session import create_session_factory
from tests.private_data_fixture import PrivateDataCipherFake


@pytest.mark.skipif(
    not os.getenv("TEST_RAG_DATABASE_URL"), reason="requires disposable migrated pgvector"
)
async def test_backfill_preserves_frozen_data_and_reenables_immutability_after_success():
    engine = create_engine(os.environ["TEST_RAG_DATABASE_URL"])
    user_id, document_id, chunk_id, revision_id = [uuid.uuid4() for _ in range(4)]
    cipher = PrivateDataCipherFake()
    try:
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "insert into documents(id,user_id,knowledge_key,name,mime,status) values (:id,:owner,'private_fixture','김민수.txt','text/plain','ready')"
                ),
                {"id": document_id, "owner": user_id},
            )
            await connection.execute(
                sa.text(
                    "insert into chunks(id,document_id,seq,text,metadata,embedding) values (:id,:doc,0,'김민수 010-1234-5678','{\"email\":\"private@example.test\"}',:embedding)"
                ),
                {
                    "id": chunk_id,
                    "doc": document_id,
                    "embedding": "[" + ",".join(["1"] + ["0"] * 1535) + "]",
                },
            )
            await connection.execute(
                sa.text("insert into knowledge_revisions(id,user_id) values (:id,:owner)"),
                {"id": revision_id, "owner": user_id},
            )
            await connection.execute(
                sa.text(
                    "insert into knowledge_revision_chunks(id,revision_id,source_document_id,document_name,seq,text,metadata,embedding) select gen_random_uuid(),:revision,document_id,'김민수.txt',seq,text,metadata,embedding from chunks where id=:id"
                ),
                {"revision": revision_id, "id": chunk_id},
            )
            reports = await backfill(connection, cipher, True)
            assert sum(row["converted"] for row in reports) == 3
            again = await backfill(connection, cipher, True)
            assert sum(row["converted"] for row in again) == 0
            enabled = await connection.scalar(
                sa.text(
                    "select tgenabled::text from pg_trigger where tgrelid='knowledge_revision_chunks'::regclass and tgname='knowledge_revision_chunks_are_immutable'"
                )
            )
            assert enabled == "O"
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    sa.text("select name from documents where id=:id"), {"id": document_id}
                )
            ).all()
            assert "김민수" not in str(rows)
            with pytest.raises(sa.exc.DBAPIError):
                await connection.execute(
                    sa.text(
                        "update knowledge_revision_chunks set text='changed' where revision_id=:id"
                    ),
                    {"id": revision_id},
                )
    finally:
        owners = SqlAlchemyOwnerErasure(create_session_factory(engine))
        await owners.fence(str(user_id))
        await owners.erase(str(user_id))
        await engine.dispose()


@pytest.mark.skipif(
    not os.getenv("TEST_RAG_DATABASE_URL"), reason="requires disposable migrated pgvector"
)
async def test_failed_backfill_rolls_back_rows_and_restores_the_immutable_trigger():
    engine = create_engine(os.environ["TEST_RAG_DATABASE_URL"])
    owner, document, revision = [uuid.uuid4() for _ in range(3)]
    cipher = PrivateDataCipherFake()
    original = cipher.encrypt

    async def unavailable(value, binding):
        if binding.resource_id == str(revision):
            raise RuntimeError("synthetic OpenBao outage")
        return await original(value, binding)

    cipher.encrypt = unavailable
    try:
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "insert into documents(id,user_id,knowledge_key,name,mime,status) values (:id,:owner,'rollback_fixture','private original','text/plain','ready')"
                ),
                {"id": document, "owner": owner},
            )
            await connection.execute(
                sa.text("insert into knowledge_revisions(id,user_id) values (:id,:owner)"),
                {"id": revision, "owner": owner},
            )
            await connection.execute(
                sa.text(
                    "insert into knowledge_revision_chunks(id,revision_id,source_document_id,document_name,seq,text,metadata,embedding) values (gen_random_uuid(),:revision,:doc,'private original',0,'original body','{}',:embedding)"
                ),
                {
                    "revision": revision,
                    "doc": document,
                    "embedding": "[" + ",".join(["1"] + ["0"] * 1535) + "]",
                },
            )
        with pytest.raises(RuntimeError, match="synthetic OpenBao outage"):
            async with engine.begin() as connection:
                await backfill(connection, cipher, True)
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    sa.text("select name from documents where id=:id"), {"id": document}
                )
                == "private original"
            )
            assert (
                await connection.scalar(
                    sa.text(
                        "select tgenabled::text from pg_trigger where tgrelid='knowledge_revision_chunks'::regclass and tgname='knowledge_revision_chunks_are_immutable'"
                    )
                )
                == "O"
            )
    finally:
        owners = SqlAlchemyOwnerErasure(create_session_factory(engine))
        await owners.fence(str(owner))
        await owners.erase(str(owner))
        await engine.dispose()
