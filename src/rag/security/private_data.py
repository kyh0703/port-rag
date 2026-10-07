"""OpenBao Transit storage boundary. No application encryption keys or plaintext fallback."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import ssl
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

_MAX_JSON = 262_144
_MAX_VALUE = 32 * 1024 * 1024
_CIPHERTEXT = re.compile(r"^vault:v[1-9][0-9]*:[A-Za-z0-9+/]+={0,2}$")
_LARGE_PREFIX = "port-openbao:v1:"


@dataclass(frozen=True)
class StorageBinding:
    owner_id: str
    resource_id: str
    field: str

    def associated_data(self) -> str:
        for value in (self.owner_id, self.resource_id, self.field):
            if (
                not value
                or len(value) > 256
                or value.strip() != value
                or any(ord(c) < 32 or ord(c) == 127 for c in value)
            ):
                raise ValueError("Private-data storage binding is invalid")
        data = json.dumps(
            ["port/private-data/v1", self.owner_id, self.resource_id, self.field],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return base64.b64encode(data.encode()).decode()

    def part(self, name: str) -> StorageBinding:
        return StorageBinding(self.owner_id, self.resource_id, f"{self.field}:{name}")


class PrivateDataCipher(Protocol):
    async def encrypt(self, value: Any, binding: StorageBinding) -> str: ...
    async def decrypt(self, value: str, binding: StorageBinding) -> Any: ...
    async def lookup(self, value: str, binding: StorageBinding) -> str: ...


class PrivateDataUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Private-data encryption service is unavailable")


class OpenBaoPrivateDataCipher:
    def __init__(
        self,
        *,
        address: str,
        ca_file: Path,
        role_file: Path,
        secret_file: Path,
        key: str = "port-rag-private-data",
        lookup_key: str = "port-rag-private-lookup",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlparse(address)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("OpenBao address must be an HTTPS origin")
        if any(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", name) is None for name in (key, lookup_key)):
            raise ValueError("OpenBao data key is invalid")
        self._address = address.rstrip("/")
        self._role_file, self._secret_file, self._key = role_file, secret_file, key
        self._lookup_key = lookup_key
        context = ssl.create_default_context(cafile=str(ca_file))
        self._client = httpx.AsyncClient(
            verify=context, transport=transport, timeout=5.0, follow_redirects=False
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def lookup(self, value: str, binding: StorageBinding) -> str:
        if len(value.encode()) > 4096:
            raise ValueError("Private lookup input exceeds the size limit")
        raw = json.dumps([binding.associated_data(), value], separators=(",", ":")).encode()
        response = await self._authenticated(
            f"transit/hmac/{self._lookup_key}/sha2-256",
            {"input": base64.b64encode(raw).decode(), "key_version": 1},
        )
        digest = response.get("data", {}).get("hmac")
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"vault:v1:[A-Za-z0-9+/]{43}=", digest) is None
        ):
            raise PrivateDataUnavailable()
        return digest

    async def encrypt(self, value: Any, binding: StorageBinding) -> str:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        if len(raw) > _MAX_VALUE:
            raise ValueError("Private-data value exceeds the size limit")
        if len(raw) <= _MAX_JSON:
            return await self._transit("encrypt", raw, binding)
        chunks = [
            await self._transit(
                "encrypt", raw[index : index + 65536], binding.part(f"chunk:{index // 65536}")
            )
            for index in range(0, len(raw), 65536)
        ]
        manifest = {
            "bytes": len(raw),
            "count": len(chunks),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        encrypted_manifest = await self.encrypt(manifest, binding.part("manifest"))
        return _LARGE_PREFIX + json.dumps(
            {"manifest": encrypted_manifest, "chunks": chunks}, separators=(",", ":")
        )

    async def decrypt(self, value: str, binding: StorageBinding) -> Any:
        if value.startswith(_LARGE_PREFIX):
            envelope = json.loads(value[len(_LARGE_PREFIX) :])
            manifest = await self.decrypt(envelope["manifest"], binding.part("manifest"))
            chunks = envelope["chunks"]
            if (
                not isinstance(chunks, list)
                or not isinstance(manifest, dict)
                or not isinstance(manifest.get("bytes"), int)
                or not 0 <= manifest["bytes"] <= _MAX_VALUE
                or len(chunks) != manifest.get("count")
                or len(chunks) != (manifest["bytes"] + 65535) // 65536
            ):
                raise PrivateDataUnavailable()
            raw = b"".join(
                [
                    await self._transit("decrypt", chunk, binding.part(f"chunk:{index}"))
                    for index, chunk in enumerate(chunks)
                ]
            )
            if len(raw) != manifest["bytes"] or hashlib.sha256(raw).hexdigest() != manifest.get(
                "sha256"
            ):
                raise PrivateDataUnavailable()
        else:
            raw = await self._transit("decrypt", value, binding)
        return json.loads(raw.decode("utf-8"))

    async def _transit(
        self, operation: str, value: bytes | str, binding: StorageBinding
    ) -> str | bytes:
        if operation == "decrypt" and (
            not isinstance(value, str) or _CIPHERTEXT.fullmatch(value) is None
        ):
            raise PrivateDataUnavailable()
        body = {
            "associated_data": binding.associated_data(),
            "plaintext" if operation == "encrypt" else "ciphertext": base64.b64encode(
                value
            ).decode()
            if isinstance(value, bytes)
            else value,
        }
        response = await self._authenticated(f"transit/{operation}/{self._key}", body)
        try:
            result = response["data"]["ciphertext" if operation == "encrypt" else "plaintext"]
            if operation == "encrypt":
                if not isinstance(result, str) or _CIPHERTEXT.fullmatch(result) is None:
                    raise PrivateDataUnavailable()
                return result
            raw = base64.b64decode(result, validate=True)
            if len(raw) > _MAX_JSON:
                raise PrivateDataUnavailable()
            return raw
        except Exception:
            raise PrivateDataUnavailable() from None

    async def _authenticated(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        token: str | None = None
        try:
            login = await self._request(
                "auth/approle/login",
                {
                    "role_id": _credential(self._role_file),
                    "secret_id": _credential(self._secret_file),
                },
            )
            token = login["auth"]["client_token"]
            if not isinstance(token, str) or not token:
                raise PrivateDataUnavailable()
            return await self._request(path, body, token)
        except Exception:
            raise PrivateDataUnavailable() from None
        finally:
            if token:
                try:
                    await self._request("auth/token/revoke-self", {}, token)
                except Exception:
                    pass

    async def _request(
        self, path: str, body: dict[str, Any], token: str | None = None
    ) -> dict[str, Any]:
        headers = {"X-Vault-Token": token} if token else {}
        async with self._client.stream(
            "POST", f"{self._address}/v1/{path}", json=body, headers=headers
        ) as response:
            response.raise_for_status()
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > 1024 * 1024:
                    raise PrivateDataUnavailable()
            return json.loads(content) if content else {}


def _credential(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise PrivateDataUnavailable()
    value = path.read_text().strip()
    if not value or len(value) > 8192 or any(c.isspace() for c in value):
        raise PrivateDataUnavailable()
    return value


async def read_text(
    cipher: PrivateDataCipher, value: str | None, binding: StorageBinding
) -> str | None:
    if value is None:
        return None
    if not value.startswith(("vault:", _LARGE_PREFIX)):
        return value  # transitional legacy reads; new writes never take this path
    result = await cipher.decrypt(value, binding)
    if not isinstance(result, str):
        raise PrivateDataUnavailable()
    return result


async def encrypt_json(
    cipher: PrivateDataCipher, value: Any, binding: StorageBinding
) -> dict[str, str]:
    return {
        "storageEncryption": "openbao-transit/v1",
        "ciphertext": await cipher.encrypt(value, binding),
    }


async def read_json(cipher: PrivateDataCipher, value: Any, binding: StorageBinding) -> Any:
    if isinstance(value, dict) and value.get("storageEncryption") == "openbao-transit/v1":
        return await cipher.decrypt(value["ciphertext"], binding)
    return value
