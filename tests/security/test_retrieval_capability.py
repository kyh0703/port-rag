from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from rag.security.retrieval_capability import InvalidRetrievalCapability
from rag.security.retrieval_capability import RetrievalCapabilityVerifier

SECRET = "a-32-byte-minimum-retrieval-secret"
USER_ID = "0197e50a-1234-7abc-8def-0123456789ab"
REVISION_ID = "0197e50a-1234-7abc-8def-0123456789ac"


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def issue(*, revision_id: str = REVISION_ID, expires_at: int = 2_000) -> str:
    payload = _base64url(
        json.dumps(
            {
                "userId": USER_ID,
                "knowledgeRevisionId": revision_id,
                "sessionId": "session-1",
                "exp": expires_at,
            },
            separators=(",", ":"),
        ).encode()
    )
    signature = _base64url(
        hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).digest()
    )
    return f"{payload}.{signature}"


def test_verifies_user_for_exact_revision() -> None:
    verifier = RetrievalCapabilityVerifier(SECRET, now=lambda: 1_000)

    assert verifier.verify(issue(), knowledge_revision_id=REVISION_ID) == USER_ID


@pytest.mark.parametrize(
    "token,revision_id",
    [
        (issue(expires_at=999), REVISION_ID),
        (issue(), "0197e50a-1234-7abc-8def-0123456789ad"),
        (issue() + "tampered", REVISION_ID),
        ("not-a-capability", REVISION_ID),
        ("abc.%%%", REVISION_ID),
    ],
)
def test_rejects_expired_mismatched_or_tampered_capability(
    token: str,
    revision_id: str,
) -> None:
    verifier = RetrievalCapabilityVerifier(SECRET, now=lambda: 1_000)

    with pytest.raises(InvalidRetrievalCapability, match="invalid retrieval capability"):
        verifier.verify(token, knowledge_revision_id=revision_id)
