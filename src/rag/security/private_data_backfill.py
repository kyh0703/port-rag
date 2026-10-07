"""Convert legacy RAG payloads using OpenBao. Default mode is read-only inventory."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
from rag.db.database_url import normalize_asyncpg_url
from rag.security.private_data import (
    OpenBaoPrivateDataCipher,
    PrivateDataCipher,
    StorageBinding,
    encrypt_json,
    read_json,
    read_text,
)


@dataclass(frozen=True)
class Target:
    table: str
    query: str
    columns: tuple[str, ...]


TARGETS = (
    Target(
        "documents",
        "select t.id,t.name,t.error,t.user_id as owner_id,t.id as resource_id from documents t",
        ("name", "error"),
    ),
    Target(
        "chunks",
        "select t.id,t.seq,t.text,t.metadata,d.user_id as owner_id,t.document_id as resource_id from chunks t join documents d on d.id=t.document_id",
        ("text", "metadata"),
    ),
    Target(
        "knowledge_revision_chunks",
        "select t.id,t.source_document_id,t.seq,t.document_name,t.text,t.metadata,r.user_id as owner_id,t.revision_id as resource_id from knowledge_revision_chunks t join knowledge_revisions r on r.id=t.revision_id",
        ("document_name", "text", "metadata"),
    ),
    Target(
        "document_webpages",
        "select t.document_id,t.urls,t.content,t.sync_error,t.content_hash,d.user_id as owner_id,t.document_id as resource_id from document_webpages t join documents d on d.id=t.document_id",
        ("urls", "content", "sync_error", "content_hash"),
    ),
)


def encrypted(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("vault:", "port-openbao:v1:"))


def legacy(target: Target, row: dict[str, Any]) -> bool:
    for column in target.columns:
        value = row[column]
        if value is None:
            continue
        if column == "urls":
            if any(not encrypted(url) for url in value):
                return True
        elif column == "metadata":
            if (
                not isinstance(value, dict)
                or value.get("storageEncryption") != "openbao-transit/v1"
            ):
                return True
        elif not encrypted(value):
            return True
    return False


async def convert(cipher: PrivateDataCipher, target: Target, row: dict[str, Any]) -> dict[str, Any]:
    owner, resource = str(row["owner_id"]), str(row["resource_id"])
    output = {column: row[column] for column in target.columns}

    async def text(column: str, field: str):
        value = row[column]
        if value is None:
            return
        binding = StorageBinding(owner, resource, field)
        if encrypted(value):
            await read_text(cipher, value, binding)
        else:
            output[column] = await cipher.encrypt(value, binding)

    async def structured(column: str, field: str):
        value = row[column]
        binding = StorageBinding(owner, resource, field)
        if isinstance(value, dict) and value.get("storageEncryption") == "openbao-transit/v1":
            await read_json(cipher, value, binding)
        else:
            output[column] = await encrypt_json(cipher, value, binding)

    if target.table == "documents":
        await text("name", "document:name")
        await text("error", "document:error")
    elif target.table == "chunks":
        await text("text", f"chunk:{row['seq']}:text")
        await structured("metadata", f"chunk:{row['seq']}:metadata")
    elif target.table == "knowledge_revision_chunks":
        prefix = f"document:{row['source_document_id']}"
        await text("document_name", f"{prefix}:name")
        await text("text", f"{prefix}:chunk:{row['seq']}:text")
        await structured("metadata", f"{prefix}:chunk:{row['seq']}:metadata")
    elif target.table == "document_webpages":
        output["urls"] = []
        for index, url in enumerate(row["urls"]):
            binding = StorageBinding(owner, resource, f"webpage:url:{index}")
            if encrypted(url):
                await read_text(cipher, url, binding)
            output["urls"].append(url if encrypted(url) else await cipher.encrypt(url, binding))
        await text("content", "webpage:content")
        await text("sync_error", "webpage:error")
        if row["content_hash"] and not encrypted(row["content_hash"]):
            output["content_hash"] = await cipher.lookup(
                row["content_hash"], StorageBinding(owner, resource, "webpage:content-fingerprint")
            )
    return output


async def backfill(connection, cipher: PrivateDataCipher, apply: bool) -> list[dict[str, Any]]:
    report = []
    for target in TARGETS:
        converted = before = 0
        immutable = apply and target.table == "knowledge_revision_chunks"
        if immutable:
            # Requires the maintenance table owner. ACCESS EXCLUSIVE prevents any
            # concurrent writer from observing the temporary representation change.
            enabled = await connection.scalar(
                sa.text(
                    "select tgenabled::text from pg_trigger where tgrelid='knowledge_revision_chunks'::regclass and tgname='knowledge_revision_chunks_are_immutable'"
                )
            )
            if enabled != "O":
                raise RuntimeError("Immutable revision trigger must be enabled before backfill")
            await connection.execute(
                sa.text(
                    "alter table knowledge_revision_chunks disable trigger knowledge_revision_chunks_are_immutable"
                )
            )
        key = "document_id" if target.table == "document_webpages" else "id"
        after = None
        while True:
            query = target.query + (f" where t.{key}>:after" if after is not None else "")
            query += f" order by t.{key} limit 100" + (" for update of t" if apply else "")
            rows = (await connection.execute(sa.text(query), {"after": after} if after is not None else {})).mappings().all()
            if not rows:
                break
            for row in rows:
                is_legacy = legacy(target, row)
                if is_legacy:
                    before += 1
                if not apply:
                    continue
                # Validate existing ciphertext too; only changed columns are written.
                values = await convert(cipher, target, row)
                if not is_legacy:
                    continue
                key = "document_id" if target.table == "document_webpages" else "id"
                assignments = ",".join(
                    f"{column}=cast(:{column} as jsonb)"
                    if column in ("metadata", "urls")
                    else f"{column}=:{column}"
                    for column in target.columns
                )
                params = {
                    column: json.dumps(value, ensure_ascii=False)
                    if column in ("metadata", "urls")
                    else value
                    for column, value in values.items()
                }
                await connection.execute(
                    sa.text(f"update {target.table} set {assignments} where {key}=:record_id"),
                    {**params, "record_id": row[key]},
                )
                converted += 1
            after = rows[-1][key]
        if immutable:
            await connection.execute(
                sa.text(
                    "alter table knowledge_revision_chunks enable trigger knowledge_revision_chunks_are_immutable"
                )
            )
        report.append(
            {
                "table": target.table,
                "before": before,
                "converted": converted,
                "remaining": before - converted,
            }
        )
    return report


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL is required")
    cipher = OpenBaoPrivateDataCipher(
        address=os.environ.get("OPENBAO_ADDR", "https://openbao:8200"),
        ca_file=Path(os.environ.get("OPENBAO_CA_CERT_FILE", "/run/openbao-ca/ca.crt")),
        role_file=Path(os.environ.get("OPENBAO_ROLE_ID_FILE", "/run/openbao/rag-role-id")),
        secret_file=Path(os.environ.get("OPENBAO_SECRET_ID_FILE", "/run/openbao/rag-secret-id")),
        key=os.environ.get("OPENBAO_DATA_TRANSIT_KEY", "port-rag-private-data"),
        lookup_key=os.environ.get("OPENBAO_LOOKUP_TRANSIT_KEY", "port-rag-private-lookup"),
    )
    engine = create_async_engine(normalize_asyncpg_url(os.environ["DATABASE_URL"]), echo=False)
    try:
        async with engine.begin() as connection:
            await connection.execute(sa.text("set local lock_timeout='5s'"))
            for result in await backfill(connection, cipher, args.apply):
                print(json.dumps(result))
    finally:
        await cipher.aclose()
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        import sys

        print(
            "RAG private-data backfill failed; row contents and upstream errors are not logged",
            file=sys.stderr,
        )
        sys.exit(1)
