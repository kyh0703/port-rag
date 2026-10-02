"""Owner-addressable disposable staging, shared safely by local RAG processes."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import re
import tempfile
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import BinaryIO

from starlette.datastructures import UploadFile

from rag.security.owner_erasure import OwnerAdmission

_INSTANCE_ID = re.compile(r"^[0-9a-f]{32}$")
_OWNER_HASH = re.compile(r"^[0-9a-f]{64}$")


class LegacyUploadCleanupRequired(Exception):
    def __init__(self) -> None:
        super().__init__("RAG_LEGACY_UPLOAD_CLEANUP_REQUIRED")


class LocalUploadStorage:
    def __init__(
        self,
        *,
        owner_access: OwnerAdmission,
        staging_root: Path | None = None,
        legacy_root: Path | None = None,
        clean_legacy_uploads_on_start: bool = False,
    ) -> None:
        self._owner_access = owner_access
        self._root = staging_root or Path(tempfile.gettempdir()) / "rag-owner-uploads-v1"
        self._legacy_root = legacy_root or Path(tempfile.gettempdir())
        self._instance_id = uuid.uuid4().hex
        self._instance_lock: BinaryIO | None = None
        if self._root.is_symlink():
            raise RuntimeError("RAG upload staging root must not be a symlink")
        missing_directories: list[Path] = []
        ancestor = self._root
        while not ancestor.exists():
            missing_directories.append(ancestor)
            ancestor = ancestor.parent
        # Publish each new ancestor before creating descendants. Sync the
        # existing boundary too, in case an earlier startup failed its fsync.
        self._sync_directory(ancestor.resolve())
        self._sync_directory(ancestor.parent.resolve())
        for directory in reversed(missing_directories):
            directory.mkdir(mode=0o700, exist_ok=True)
            if directory.is_symlink():
                raise RuntimeError("RAG upload staging directory must not be a symlink")
            self._sync_directory(directory)
            self._sync_directory(directory.parent.resolve())
        for name in ("owners", "locks", "instances"):
            directory = self._root / name
            directory.mkdir(mode=0o700, exist_ok=True)
            if directory.is_symlink():
                raise RuntimeError("RAG upload staging directory must not be a symlink")
        self._sync_directory(self._root)
        with self._open_lock(self._root / ".startup.lock") as startup:
            fcntl.flock(startup, fcntl.LOCK_EX)
            self._cleanup_abandoned_instances()
            # Old rag-uploads-* directories have no live-process marker. This
            # explicit cutover flag is valid only after every old RAG process stops.
            if clean_legacy_uploads_on_start:
                for directory in self._legacy_root.glob("rag-uploads-*"):
                    self._remove_upload_directory(directory)
                self.require_legacy_cleanup()
            self._instance_lock = self._open_lock(self._root / "instances" / self._instance_id)
            try:
                fcntl.flock(self._instance_lock, fcntl.LOCK_EX)
                os.fsync(self._instance_lock.fileno())
                self._sync_directory(self._root / "instances")
            except BaseException:
                self.close()
                raise

    @asynccontextmanager
    async def hold_owner(self, user_id: str, *, exclusive: bool = False) -> AsyncIterator[str]:
        owner_hash = hashlib.sha256(str(uuid.UUID(user_id)).encode()).hexdigest()
        async with self._hold_lock(self._root / "locks" / owner_hash, exclusive=exclusive):
            yield owner_hash

    @asynccontextmanager
    async def _hold_lock(self, path: Path, *, exclusive: bool) -> AsyncIterator[None]:
        lock = self._open_lock(path)
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            # Blocking flock must not occupy the executor used by parsing
            # threads: a waiting erasure could otherwise starve its own drain.
            while True:
                try:
                    fcntl.flock(lock, mode | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.01)
            yield
        finally:
            lock.close()

    async def save(self, upload: UploadFile, *, user_id: str) -> Path:
        await self._owner_access.assert_active(user_id)
        async with self.hold_owner(user_id) as owner_hash:
            await self._owner_access.assert_active(user_id)
            directory = self._root / "owners" / owner_hash / self._instance_id
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = directory / f"{uuid.uuid4().hex}{Path(upload.filename or '').suffix}"
            try:
                with path.open("xb") as output:
                    while chunk := await upload.read(1024 * 1024):
                        output.write(chunk)
                await self._owner_access.assert_active(user_id)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            return path

    async def erase_owner(self, user_id: str) -> None:
        # Active upload/ingest holds a shared lock. The durable SQL fence stops
        # the next stage; this exclusive lock drains readers, including threads
        # in another local process, before unlinking only this owner's originals.
        async with (
            self.hold_owner(user_id, exclusive=True) as owner_hash,
            self._hold_lock(self._root / ".startup.lock", exclusive=True),
        ):
            directory = self._root / "owners" / owner_hash
            if directory.is_symlink():
                raise RuntimeError("RAG owner staging directory must not be a symlink")
            if directory.exists():
                for instance in directory.iterdir():
                    if _INSTANCE_ID.fullmatch(instance.name) is None:
                        raise RuntimeError("Unexpected RAG upload instance directory")
                    self._remove_upload_directory(instance)
                self._sync_directory(directory)
                directory.rmdir()
            # Retry this barrier even when an earlier attempt already removed
            # the owner directory but failed to make that removal durable.
            self._sync_directory(directory.parent)

    def require_legacy_cleanup(self) -> None:
        if any(self._legacy_root.glob("rag-uploads-*")):
            raise LegacyUploadCleanupRequired()
        if self._legacy_root.exists():
            self._sync_directory(self._legacy_root.resolve())

    def close(self) -> None:
        if self._instance_lock is not None:
            self._instance_lock.close()
            self._instance_lock = None

    def _cleanup_abandoned_instances(self) -> None:
        instances = self._root / "instances"
        owners = tuple((self._root / "owners").iterdir())
        instance_ids: set[str] = set()
        for marker in instances.iterdir():
            if _INSTANCE_ID.fullmatch(marker.name) is None:
                raise RuntimeError("Unexpected RAG upload process marker")
            instance_ids.add(marker.name)
        for owner in owners:
            if _OWNER_HASH.fullmatch(owner.name) is None or owner.is_symlink():
                raise RuntimeError("Unexpected RAG owner staging directory")
            # Older or interrupted startups may leave originals without a
            # marker. They still need recovery under the same owner lock.
            for directory in owner.iterdir():
                if _INSTANCE_ID.fullmatch(directory.name) is None:
                    raise RuntimeError("Unexpected RAG upload instance directory")
                instance_ids.add(directory.name)
        for instance_id in sorted(instance_ids):
            marker = instances / instance_id
            with self._open_lock(marker) as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                complete = True
                for owner in owners:
                    with self._open_lock(self._root / "locks" / owner.name) as owner_lock:
                        try:
                            fcntl.flock(owner_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            # Startup holds the global lock, so waiting for an
                            # owner lock would invert erasure's lock order.
                            complete = False
                            continue
                        self._remove_upload_directory(owner / instance_id)
                if complete:
                    marker.unlink(missing_ok=True)
                else:
                    os.fsync(lock.fileno())
                self._sync_directory(instances)
        # An earlier attempt may have unlinked its last marker before a failed
        # sync; an empty-marker retry must still acknowledge that boundary.
        self._sync_directory(instances)

    def _remove_upload_directory(self, directory: Path) -> None:
        if directory.is_symlink():
            raise RuntimeError("Unexpected RAG staging path")
        if not directory.exists():
            self._sync_directory(directory.parent.resolve())
            return
        if not directory.is_dir():
            raise RuntimeError("Unexpected RAG staging path")
        if directory.stat().st_uid != os.getuid():
            raise RuntimeError("RAG staging directory belongs to another OS user")
        for path in directory.iterdir():
            if path.is_dir() and not path.is_symlink():
                raise RuntimeError("Unexpected nested RAG staging directory")
            path.unlink(missing_ok=True)
        self._sync_directory(directory)
        directory.rmdir()
        self._sync_directory(directory.parent.resolve())

    def _sync_directory(self, directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _open_lock(self, path: Path) -> BinaryIO:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        return os.fdopen(descriptor, "a+b")
