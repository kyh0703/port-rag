import os
from pathlib import Path

import pytest
from rag.security.private_data import (
    OpenBaoPrivateDataCipher,
    StorageBinding,
    PrivateDataUnavailable,
)


@pytest.mark.skipif(
    not os.getenv("OPENBAO_INTEGRATION_ADDR"), reason="requires disposable TLS OpenBao"
)
async def test_real_openbao_large_text_round_trip_and_scoped_hmac():
    cipher = OpenBaoPrivateDataCipher(
        address=os.environ["OPENBAO_INTEGRATION_ADDR"],
        ca_file=Path(os.environ["OPENBAO_INTEGRATION_CA_CERT_FILE"]),
        role_file=Path(os.environ["OPENBAO_INTEGRATION_ROLE_ID_FILE"]),
        secret_file=Path(os.environ["OPENBAO_INTEGRATION_SECRET_ID_FILE"]),
        key="port-private-data",
        lookup_key="port-private-lookup",
    )
    binding = StorageBinding("fixture-owner", "fixture-document", "text")
    value = "김민수 010-1234-5678 " * 15000
    try:
        encrypted = await cipher.encrypt(value, binding)
        assert "김민수" not in encrypted
        assert await cipher.decrypt(encrypted, binding) == value
        with pytest.raises(PrivateDataUnavailable):
            await cipher.decrypt(encrypted, StorageBinding("other", "fixture-document", "text"))
        lookup = await cipher.lookup("synthetic-hash", binding)
        assert await cipher.lookup("synthetic-hash", binding) == lookup
        assert (
            await cipher.lookup(
                "synthetic-hash", StorageBinding("other", "fixture-document", "text")
            )
            != lookup
        )
    finally:
        await cipher.aclose()
