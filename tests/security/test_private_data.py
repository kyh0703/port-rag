from pathlib import Path
import json
import ssl
from unittest.mock import patch
import base64

import httpx
import pytest

from rag.security.private_data import (
    OpenBaoPrivateDataCipher,
    StorageBinding,
    PrivateDataUnavailable,
)


@pytest.fixture
def cipher(tmp_path: Path):
    for name in ["role", "secret"]:
        path = tmp_path / name
        path.write_text(name)
        path.chmod(0o600)
    values = {}

    def handle(request):
        if request.url.path.endswith("login"):
            return httpx.Response(200, json={"auth": {"client_token": "fixture-token"}})
        if request.url.path.endswith("revoke-self"):
            return httpx.Response(204)
        body = json.loads(request.content)
        if "/encrypt/" in request.url.path:
            token = "vault:v1:" + base64.b64encode(f"opaque-{len(values)}".encode()).decode()
            values[token] = (body["plaintext"], body["associated_data"])
            return httpx.Response(200, json={"data": {"ciphertext": token}})
        value = values.get(body["ciphertext"])
        if value is None or value[1] != body["associated_data"]:
            return httpx.Response(400, json={"errors": ["private upstream message"]})
        return httpx.Response(200, json={"data": {"plaintext": value[0]}})

    with patch(
        "rag.security.private_data.ssl.create_default_context",
        return_value=ssl.create_default_context(),
    ):
        provider = OpenBaoPrivateDataCipher(
            address="https://openbao.test",
            ca_file=tmp_path / "ca",
            role_file=tmp_path / "role",
            secret_file=tmp_path / "secret",
            transport=httpx.MockTransport(handle),
        )
    return provider


async def test_large_value_round_trip_and_owner_binding(cipher):
    binding = StorageBinding("owner", "document", "content")
    value = "김민수 010-1234-5678 " * 15000
    encrypted = await cipher.encrypt(value, binding)
    assert "김민수" not in encrypted
    assert await cipher.decrypt(encrypted, binding) == value
    with pytest.raises(PrivateDataUnavailable):
        await cipher.decrypt(encrypted, StorageBinding("other", "document", "content"))
    await cipher.aclose()


async def test_plaintext_and_insecure_endpoint_are_refused(cipher, tmp_path):
    with pytest.raises(PrivateDataUnavailable):
        await cipher.decrypt("private text", StorageBinding("owner", "document", "content"))
    with pytest.raises(ValueError):
        OpenBaoPrivateDataCipher(
            address="http://openbao",
            ca_file=tmp_path / "ca",
            role_file=tmp_path / "role",
            secret_file=tmp_path / "secret",
        )
    await cipher.aclose()
