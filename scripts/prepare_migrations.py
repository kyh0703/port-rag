#!/usr/bin/env python3
"""Materialize a pinned port-spec bundle without network or database access.

This bootstrap is copied into consuming repositories by bundle_migrations.py.
Edit it in port-spec, not in a consuming repository.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import re
import tarfile


TARGETS = {"api": "src/migrations", "rag": "alembic/versions"}
PATTERNS = {"api": r"Migration[0-9][A-Za-z0-9_]*\.ts", "rag": r"[0-9][a-z0-9_]*\.py"}


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def prepare(root: Path) -> int:
    lock = json.loads((root / "migration-source.lock.json").read_text())
    owner = lock["owner"]
    if lock.get("schemaVersion") != 1 or owner not in TARGETS:
        raise ValueError("Unsupported migration bundle contract")
    if not re.fullmatch(r"[0-9a-f]{40}", lock["revision"]):
        raise ValueError("A complete spec commit is required")
    payload = (root / "vendor/migrations.tar.gz").read_bytes()
    if digest(payload) != lock["bundleSha256"]:
        raise ValueError("Migration bundle checksum mismatch")
    bootstrap = root / "scripts/prepare_migrations.py"
    if digest(bootstrap.read_bytes()) != lock["bootstrapSha256"]:
        raise ValueError("Migration bootstrap checksum mismatch")

    expected = lock["files"]
    contents: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for entry in archive.getmembers():
            if (
                not entry.isfile()
                or not re.fullmatch(PATTERNS[owner], entry.name)
                or entry.name in contents
                or entry.name not in expected
            ):
                raise ValueError("Unsafe or unexpected migration archive member")
            stream = archive.extractfile(entry)
            if stream is None:
                raise ValueError("Missing migration archive content")
            contents[entry.name] = stream.read()
    if set(contents) != set(expected):
        raise ValueError("Migration bundle file list mismatch")
    for name, content in contents.items():
        if digest(content) != expected[name]:
            raise ValueError(f"Migration checksum mismatch: {name}")

    destination = root / TARGETS[owner]
    destination.mkdir(parents=True, exist_ok=True)
    # Do not silently delete a newly generated migration or overwrite local edits.
    for item in destination.iterdir():
        if not re.fullmatch(PATTERNS[owner], item.name):
            continue
        if item.is_symlink() or item.name not in contents:
            raise ValueError(f"Import the local migration into port-spec first: {item.name}")
        if item.read_bytes() != contents[item.name]:
            raise ValueError(f"Local migration differs from the pinned source: {item.name}")
    for name, content in contents.items():
        target = destination / name
        if not target.exists():
            target.write_bytes(content)
    return len(contents)


if __name__ == "__main__":
    service_root = Path(__file__).resolve().parents[1]
    print(f"Prepared {prepare(service_root)} pinned port-spec migrations")
