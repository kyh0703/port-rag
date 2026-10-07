"""External OpenBao boundary fake. Ciphertext tokens contain no user data."""

from contextlib import asynccontextmanager
import base64
import copy
import hashlib
from rag.security.private_data import StorageBinding, PrivateDataUnavailable


class PrivateDataCipherFake:
    def __init__(self):
        self.values = {}

    async def lookup(self, value, binding: StorageBinding):
        return (
            "vault:v1:"
            + base64.b64encode(hashlib.sha256(repr((binding, value)).encode()).digest()).decode()
        )

    async def encrypt(self, value, binding: StorageBinding):
        token = "vault:v1:" + base64.b64encode(f"fixture-{len(self.values)}".encode()).decode()
        self.values[token] = (copy.deepcopy(value), binding)
        return token

    async def decrypt(self, token, binding: StorageBinding):
        value = self.values.get(token)
        if value is None or value[1] != binding:
            raise PrivateDataUnavailable()
        return copy.deepcopy(value[0])


private_data_cipher = PrivateDataCipherFake()


class PlainParserInputFixture:
    @asynccontextmanager
    async def decrypted_path(self, path, *, user_id):
        yield path
