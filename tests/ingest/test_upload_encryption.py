import io
import uuid
from pathlib import Path

import pytest
from starlette.datastructures import UploadFile
from rag.ingest.uploads import LocalUploadStorage
from rag.security.private_data import PrivateDataUnavailable
from tests.fakes import MemoryOwnerAdmission
from tests.private_data_fixture import PrivateDataCipherFake


async def test_encrypted_upload_round_trip_and_plaintext_working_file_cleanup(tmp_path: Path):
    owner = str(uuid.uuid4())
    storage = LocalUploadStorage(
        owner_access=MemoryOwnerAdmission(),
        cipher=PrivateDataCipherFake(),
        staging_root=tmp_path / "staging",
    )
    private = "김민수 010-1234-5678".encode() * 10000
    try:
        path = await storage.save(
            UploadFile(io.BytesIO(private), filename="patient.txt"), user_id=owner
        )
        assert "김민수".encode() not in path.read_bytes()
        async with storage.decrypted_path(path, user_id=owner) as plaintext:
            assert plaintext.read_bytes() == private
            assert plaintext != path
        assert not plaintext.exists()
        assert path.exists()
        with pytest.raises(PrivateDataUnavailable):
            async with storage.decrypted_path(path, user_id=str(uuid.uuid4())):
                pass
        lines = path.read_bytes().splitlines(keepends=True)
        path.write_bytes(b"".join(lines[:-1]))
        with pytest.raises(PrivateDataUnavailable):
            async with storage.decrypted_path(path, user_id=owner):
                pass
    finally:
        storage.close()


async def test_openbao_outage_removes_partial_upload_and_never_writes_plaintext(tmp_path: Path):
    cipher = PrivateDataCipherFake()

    async def unavailable(*args):
        raise PrivateDataUnavailable()

    cipher.encrypt = unavailable
    storage = LocalUploadStorage(
        owner_access=MemoryOwnerAdmission(), cipher=cipher, staging_root=tmp_path / "staging"
    )
    try:
        with pytest.raises(PrivateDataUnavailable):
            await storage.save(
                UploadFile(io.BytesIO(b"private caller audio"), filename="private.txt"),
                user_id=str(uuid.uuid4()),
            )
        assert not list((tmp_path / "staging" / "owners").rglob("*.txt"))
    finally:
        storage.close()
