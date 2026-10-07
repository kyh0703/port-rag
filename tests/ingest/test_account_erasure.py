from __future__ import annotations
from tests.private_data_fixture import private_data_cipher

import asyncio
import errno
import hashlib
import io
import os
import uuid
from pathlib import Path

import pytest
from starlette.datastructures import UploadFile

from rag.ingest.pipeline import IngestPipeline
from rag.ingest.types import IngestJob
from rag.ingest.uploads import LegacyUploadCleanupRequired, LocalUploadStorage
from rag.ingest.worker import IngestWorker
from rag.security.owner_erasure import OwnerDataErased
from tests.fakes import StaticFakeEmbedder
from tests.fakes import MemoryOwnerAdmission
from tests.ingest.test_pipeline import MemoryStore, SplitChunker, TextParser


class BlockingEmbedder(StaticFakeEmbedder):
    def __init__(self) -> None:
        super().__init__(dimensions=3)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.started.set()
        await self.release.wait()
        return await super().embed_texts(texts)


@pytest.mark.asyncio
async def test_erasure_drains_only_owned_ingestion_and_rejects_late_queue(tmp_path: Path) -> None:
    owners = MemoryOwnerAdmission()
    owner, other = str(uuid.uuid4()), str(uuid.uuid4())
    storage = LocalUploadStorage(
        staging_root=tmp_path / "staging", legacy_root=tmp_path / "legacy", owner_access=owners,
        cipher=private_data_cipher,
    )
    store = MemoryStore()
    embedder = BlockingEmbedder()
    pipeline = IngestPipeline(
        parser=TextParser(), chunker=SplitChunker(), embedder=embedder,
        store=store, owner_access=owners,
        storage=storage,
    )
    worker = IngestWorker(pipeline, owner_access=owners, storage=storage)
    owned_id, queued_id, other_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    owned = await storage.save(UploadFile(io.BytesIO(b"owned active"), filename="a.txt"), user_id=owner)
    queued = await storage.save(UploadFile(io.BytesIO(b"owned queued"), filename="q.txt"), user_id=owner)
    preserved = await storage.save(UploadFile(io.BytesIO(b"other account"), filename="b.txt"), user_id=other)
    worker.start()
    try:
        await worker.enqueue(IngestJob(document_id=owned_id, path=owned, user_id=owner))
        await embedder.started.wait()
        await worker.enqueue(IngestJob(document_id=queued_id, path=queued, user_id=owner))
        await worker.enqueue(IngestJob(document_id=other_id, path=preserved, user_id=other))
        owners.erased.add(owner)
        erasure = asyncio.create_task(worker.erase_user(owner))
        await asyncio.sleep(0)
        assert not erasure.done()
        assert b"other account" not in preserved.read_bytes()
        async with storage.decrypted_path(preserved, user_id=other) as readable:
            assert readable.read_bytes() == b"other account"
        embedder.release.set()
        await erasure
        await storage.erase_owner(owner)
        await worker.join()

        assert owned_id not in store.chunks
        assert queued_id not in store.chunks
        assert owned_id not in store.statuses
        assert [chunk.text for chunk in store.chunks[other_id]] == ["other account"]
        assert not owned.exists()
        assert not queued.exists()
        late = tmp_path / "late.txt"
        late.write_text("must never ingest")
        with pytest.raises(OwnerDataErased):
            await worker.enqueue(IngestJob(document_id=uuid.uuid4(), path=late, user_id=owner))
        assert not late.exists()
    finally:
        embedder.release.set()
        await worker.stop()
        storage.close()


@pytest.mark.asyncio
async def test_owned_staging_cleanup_preserves_live_other_instance_and_cleans_crash_remnants(
    tmp_path: Path,
) -> None:
    owners = MemoryOwnerAdmission()
    root, legacy = tmp_path / "staging", tmp_path / "legacy"
    first = LocalUploadStorage(staging_root=root, legacy_root=legacy, owner_access=owners, cipher=private_data_cipher
    )
    owner, other = str(uuid.uuid4()), str(uuid.uuid4())
    target = await first.save(UploadFile(io.BytesIO(b"private"), filename="a.txt"), user_id=owner)
    preserved = await first.save(UploadFile(io.BytesIO(b"preserve"), filename="b.txt"), user_id=other)
    second = LocalUploadStorage(staging_root=root, legacy_root=legacy, owner_access=owners, cipher=private_data_cipher
    )
    try:
        assert target.read_bytes().startswith(b"port-openbao-upload-v1\n")
        assert b"private" not in target.read_bytes()
        assert preserved.read_bytes().startswith(b"port-openbao-upload-v1\n")
        assert b"preserve" not in preserved.read_bytes()
        owners.erased.add(owner)
        await second.erase_owner(owner)
        assert not target.exists()
        assert preserved.read_bytes().startswith(b"port-openbao-upload-v1\n")
        assert b"preserve" not in preserved.read_bytes()
    finally:
        second.close()
        first.close()
    restarted = LocalUploadStorage(staging_root=root, legacy_root=legacy, owner_access=owners, cipher=private_data_cipher
    )
    try:
        assert not preserved.exists()
    finally:
        restarted.close()


@pytest.mark.asyncio
async def test_startup_recovers_markerless_originals_without_touching_live_other_owner(
    tmp_path: Path,
) -> None:
    owners = MemoryOwnerAdmission()
    root, legacy = tmp_path / "staging", tmp_path / "legacy"
    live = LocalUploadStorage(staging_root=root, legacy_root=legacy, owner_access=owners, cipher=private_data_cipher
    )
    owner, other = str(uuid.uuid4()), str(uuid.uuid4())
    preserved = await live.save(
        UploadFile(io.BytesIO(b"live other original"), filename="live.txt"), user_id=other,
    )
    owner_hash = hashlib.sha256(owner.encode()).hexdigest()
    abandoned = root / "owners" / owner_hash / uuid.uuid4().hex
    abandoned.mkdir(parents=True)
    original = abandoned / "private.txt"
    original.write_bytes(b"markerless private original")
    recovered = None
    try:
        recovered = LocalUploadStorage(staging_root=root, legacy_root=legacy, owner_access=owners, cipher=private_data_cipher
        )
        assert not original.exists()
        assert not abandoned.exists()
        assert preserved.read_bytes().startswith(b"port-openbao-upload-v1\n")
        assert b"live other original" not in preserved.read_bytes()
    finally:
        if recovered is not None:
            recovered.close()
        live.close()


@pytest.mark.asyncio
async def test_markerless_recovery_respects_active_owner_reader(tmp_path: Path) -> None:
    owners = MemoryOwnerAdmission()
    root, legacy = tmp_path / "staging", tmp_path / "legacy"
    live = LocalUploadStorage(staging_root=root, legacy_root=legacy, owner_access=owners, cipher=private_data_cipher
    )
    owner = str(uuid.uuid4())
    original = await live.save(
        UploadFile(io.BytesIO(b"active private original"), filename="active.txt"), user_id=owner,
    )
    marker = root / "instances" / original.parent.name
    marker.unlink()
    recovered = None
    try:
        async with live.hold_owner(owner):
            recovered = LocalUploadStorage(
                staging_root=root, legacy_root=legacy, owner_access=owners,
                cipher=private_data_cipher,
            )
            assert original.read_bytes().startswith(b"port-openbao-upload-v1\n")
            assert b"active private original" not in original.read_bytes()
        recovered.close()
        recovered = LocalUploadStorage(staging_root=root, legacy_root=legacy, owner_access=owners, cipher=private_data_cipher
        )
        assert not original.exists()
    finally:
        if recovered is not None:
            recovered.close()
        live.close()


def test_instance_marker_sync_failure_aborts_startup_and_allows_recovery(
    tmp_path: Path, monkeypatch,
) -> None:
    root, legacy = tmp_path / "staging", tmp_path / "legacy"
    root.mkdir()
    (root / "instances").mkdir()
    instances_stat = (root / "instances").stat()
    original_fsync = os.fsync
    failed_markers: list[Path] = []

    def fail_marker_sync(descriptor: int) -> None:
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) == (
            instances_stat.st_dev, instances_stat.st_ino,
        ) and any((root / "instances").iterdir()):
            failed_markers.extend((root / "instances").iterdir())
            raise OSError(errno.EIO, "synthetic marker durability failure")
        original_fsync(descriptor)

    with monkeypatch.context() as fault:
        fault.setattr(os, "fsync", fail_marker_sync)
        with pytest.raises(OSError, match="synthetic marker durability failure"):
            LocalUploadStorage(
                staging_root=root, legacy_root=legacy, owner_access=MemoryOwnerAdmission(),
                cipher=private_data_cipher,
            )
    recovered = LocalUploadStorage(
        staging_root=root, legacy_root=legacy, owner_access=MemoryOwnerAdmission(),
        cipher=private_data_cipher,
    )
    try:
        for marker in failed_markers:
            assert not marker.exists()
    finally:
        recovered.close()


@pytest.mark.asyncio
async def test_marker_removal_sync_failure_is_not_bypassed_by_empty_marker_retry(
    tmp_path: Path, monkeypatch,
) -> None:
    owners = MemoryOwnerAdmission()
    root, legacy = tmp_path / "staging", tmp_path / "legacy"
    live = LocalUploadStorage(staging_root=root, legacy_root=legacy, owner_access=owners, cipher=private_data_cipher
    )
    abandoned = LocalUploadStorage(staging_root=root, legacy_root=legacy, owner_access=owners, cipher=private_data_cipher
    )
    owner, other = str(uuid.uuid4()), str(uuid.uuid4())
    target = await abandoned.save(
        UploadFile(io.BytesIO(b"crashed private original"), filename="private.txt"), user_id=owner,
    )
    preserved = await live.save(
        UploadFile(io.BytesIO(b"live other original"), filename="live.txt"), user_id=other,
    )
    abandoned.close()
    removed_marker = root / "instances" / target.parent.name
    instances_stat = (root / "instances").stat()
    original_fsync = os.fsync

    def fail_marker_removal_sync(descriptor: int) -> None:
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) == (
            instances_stat.st_dev, instances_stat.st_ino,
        ):
            raise OSError(errno.EIO, "synthetic marker removal durability failure")
        original_fsync(descriptor)

    recovered = None
    try:
        with monkeypatch.context() as fault:
            fault.setattr(os, "fsync", fail_marker_removal_sync)
            for _ in range(2):
                with pytest.raises(OSError, match="synthetic marker removal durability failure"):
                    LocalUploadStorage(staging_root=root, legacy_root=legacy, owner_access=owners,
                        cipher=private_data_cipher,
                    )
                assert not target.exists()
                assert not removed_marker.exists()
                assert preserved.read_bytes().startswith(b"port-openbao-upload-v1\n")
                assert b"live other original" not in preserved.read_bytes()
        recovered = LocalUploadStorage(staging_root=root, legacy_root=legacy, owner_access=owners, cipher=private_data_cipher
        )
        assert preserved.read_bytes().startswith(b"port-openbao-upload-v1\n")
        assert b"live other original" not in preserved.read_bytes()
    finally:
        if recovered is not None:
            recovered.close()
        live.close()


def test_new_staging_ancestor_sync_failure_is_not_bypassed_on_retry(
    tmp_path: Path, monkeypatch,
) -> None:
    root = tmp_path / "new-parent" / "staging"
    parent_stat = tmp_path.stat()
    original_fsync = os.fsync

    def fail_ancestor_sync(descriptor: int) -> None:
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) == (
            parent_stat.st_dev, parent_stat.st_ino,
        ) and root.parent.exists():
            raise OSError(errno.EIO, "synthetic ancestor durability failure")
        original_fsync(descriptor)

    with monkeypatch.context() as fault:
        fault.setattr(os, "fsync", fail_ancestor_sync)
        for _ in range(2):
            with pytest.raises(OSError, match="synthetic ancestor durability failure"):
                LocalUploadStorage(
                    staging_root=root, legacy_root=tmp_path / "legacy",
                    owner_access=MemoryOwnerAdmission(),
                    cipher=private_data_cipher,
                )
    recovered = LocalUploadStorage(
        staging_root=root, legacy_root=tmp_path / "legacy", owner_access=MemoryOwnerAdmission(),
        cipher=private_data_cipher,
    )
    recovered.close()


@pytest.mark.asyncio
async def test_legacy_staging_blocks_erasure_without_explicit_offline_cutover(tmp_path: Path,
) -> None:
    legacy = tmp_path / "legacy"
    abandoned = legacy / "rag-uploads-old-process"
    abandoned.mkdir(parents=True)
    original = abandoned / "sensitive.pdf"
    original.write_bytes(b"legacy original")
    storage = LocalUploadStorage(
        staging_root=tmp_path / "staging", legacy_root=legacy, owner_access=MemoryOwnerAdmission(),
        cipher=private_data_cipher,
    )
    try:
        with pytest.raises(LegacyUploadCleanupRequired, match="RAG_LEGACY_UPLOAD_CLEANUP_REQUIRED"):
            storage.require_legacy_cleanup()
        assert original.read_bytes() == b"legacy original"
    finally:
        storage.close()
    cutover = LocalUploadStorage(
        staging_root=tmp_path / "staging", legacy_root=legacy, owner_access=MemoryOwnerAdmission(),
        clean_legacy_uploads_on_start=True,
        cipher=private_data_cipher,
    )
    try:
        cutover.require_legacy_cleanup()
        assert not abandoned.exists()
    finally:
        cutover.close()


def test_legacy_cleanup_retry_still_requires_parent_durability(
    tmp_path: Path, monkeypatch,
) -> None:
    root, legacy = tmp_path / "staging", tmp_path / "legacy"
    abandoned = legacy / "rag-uploads-crashed"
    abandoned.mkdir(parents=True)
    (abandoned / "private.txt").write_bytes(b"legacy private original")
    parent_stat = legacy.stat()
    original_fsync = os.fsync

    def fail_legacy_parent_sync(descriptor: int) -> None:
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) == (parent_stat.st_dev, parent_stat.st_ino):
            raise OSError(errno.EIO, "synthetic legacy durability failure")
        original_fsync(descriptor)

    with monkeypatch.context() as fault:
        fault.setattr(os, "fsync", fail_legacy_parent_sync)
        for _ in range(2):
            with pytest.raises(OSError, match="synthetic legacy durability failure"):
                LocalUploadStorage(
                    staging_root=root, legacy_root=legacy,
                    owner_access=MemoryOwnerAdmission(), clean_legacy_uploads_on_start=True,
                    cipher=private_data_cipher,
                )
        assert not abandoned.exists()
    recovered = LocalUploadStorage(
        staging_root=root, legacy_root=legacy,
        owner_access=MemoryOwnerAdmission(), clean_legacy_uploads_on_start=True,
        cipher=private_data_cipher,
    )
    try:
        recovered.require_legacy_cleanup()
        assert not abandoned.exists()
    finally:
        recovered.close()


@pytest.mark.asyncio
async def test_staging_cleanup_failure_is_not_reported_as_erased(tmp_path: Path, monkeypatch) -> None:
    owners = MemoryOwnerAdmission()
    owner = str(uuid.uuid4())
    storage = LocalUploadStorage(
        staging_root=tmp_path / "staging", legacy_root=tmp_path / "legacy", owner_access=owners,
        cipher=private_data_cipher,
    )
    path = await storage.save(UploadFile(io.BytesIO(b"private"), filename="a.txt"), user_id=owner)
    original_unlink = Path.unlink

    def fail_owned_unlink(candidate: Path, *args, **kwargs):
        if candidate == path:
            raise PermissionError("synthetic cleanup failure")
        return original_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_owned_unlink)
    try:
        with pytest.raises(PermissionError, match="synthetic cleanup failure"):
            await storage.erase_owner(owner)
        assert path.read_bytes().startswith(b"port-openbao-upload-v1\n")
        assert b"private" not in path.read_bytes()
    finally:
        storage.close()


@pytest.mark.asyncio
async def test_owner_staging_erasure_drains_another_instances_active_reader(tmp_path: Path) -> None:
    owners = MemoryOwnerAdmission()
    root, legacy = tmp_path / "staging", tmp_path / "legacy"
    first = LocalUploadStorage(staging_root=root, legacy_root=legacy, owner_access=owners, cipher=private_data_cipher
    )
    second = LocalUploadStorage(staging_root=root, legacy_root=legacy, owner_access=owners, cipher=private_data_cipher
    )
    owner, other = str(uuid.uuid4()), str(uuid.uuid4())
    target = await first.save(UploadFile(io.BytesIO(b"reading original"), filename="a.txt"), user_id=owner)
    preserved = await first.save(UploadFile(io.BytesIO(b"other original"), filename="b.txt"), user_id=other)
    started, release = asyncio.Event(), asyncio.Event()

    async def read_original() -> None:
        async with first.hold_owner(owner):
            started.set()
            await release.wait()
            assert target.read_bytes().startswith(b"port-openbao-upload-v1\n")
            assert b"reading original" not in target.read_bytes()

    reader = asyncio.create_task(read_original())
    erasure = None
    try:
        await started.wait()
        owners.erased.add(owner)
        erasure = asyncio.create_task(second.erase_owner(owner))
        await asyncio.sleep(0)
        assert not erasure.done()
        assert target.read_bytes().startswith(b"port-openbao-upload-v1\n")
        assert b"reading original" not in target.read_bytes()
        release.set()
        await reader
        await erasure
        assert not target.exists()
        assert preserved.read_bytes().startswith(b"port-openbao-upload-v1\n")
        assert b"other original" not in preserved.read_bytes()
    finally:
        release.set()
        try:
            await reader
            if erasure is not None:
                await erasure
        finally:
            second.close()
            first.close()


@pytest.mark.asyncio
async def test_erasing_queued_owner_does_not_complete_another_owners_queue_join(tmp_path: Path,
) -> None:
    owners = MemoryOwnerAdmission()
    owner, other = str(uuid.uuid4()), str(uuid.uuid4())
    storage = LocalUploadStorage(
        staging_root=tmp_path / "staging", legacy_root=tmp_path / "legacy", owner_access=owners,
        cipher=private_data_cipher,
    )
    store = MemoryStore()
    pipeline = IngestPipeline(
        parser=TextParser(), chunker=SplitChunker(), embedder=StaticFakeEmbedder(dimensions=3),
        store=store, owner_access=owners,
        storage=storage,
    )
    worker = IngestWorker(pipeline, owner_access=owners, storage=storage)
    target = await storage.save(UploadFile(io.BytesIO(b"cancel"), filename="a.txt"), user_id=owner)
    preserved = await storage.save(UploadFile(io.BytesIO(b"process"), filename="b.txt"), user_id=other)
    other_id = uuid.uuid4()
    await worker.enqueue(IngestJob(document_id=uuid.uuid4(), path=target, user_id=owner))
    await worker.enqueue(IngestJob(document_id=other_id, path=preserved, user_id=other))
    join_started = asyncio.Event()

    async def wait_for_jobs() -> None:
        join_started.set()
        await worker.join()

    joined = asyncio.create_task(wait_for_jobs())
    try:
        await join_started.wait()
        owners.erased.add(owner)
        await worker.erase_user(owner)
        await asyncio.sleep(0)
        assert not joined.done()
        assert preserved.read_bytes().startswith(b"port-openbao-upload-v1\n")
        assert b"process" not in preserved.read_bytes()
        worker.start()
        await joined
        assert [chunk.text for chunk in store.chunks[other_id]] == ["process"]
        assert not target.exists()
    finally:
        try:
            worker.start()
            await worker.stop()
            await joined
        finally:
            storage.close()
