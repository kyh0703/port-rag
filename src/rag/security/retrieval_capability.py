"""Verification for session-bound immutable knowledge retrieval capabilities."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Callable


class InvalidRetrievalCapability(ValueError):
    """Raised when a retrieval capability cannot authorize the requested revision."""


class RetrievalCapabilityVerifier:
    def __init__(self, secret: str, *, now: Callable[[], float] = time.time) -> None:
        self._secret = secret.encode("utf-8")
        self._now = now

    def verify(self, token: str, *, knowledge_revision_id: str) -> str:
        try:
            payload_part, signature_part = token.split(".")
            supplied_signature = _decode_base64url(signature_part)
            expected_signature = hmac.new(
                self._secret,
                payload_part.encode("ascii"),
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(supplied_signature, expected_signature):
                raise InvalidRetrievalCapability("invalid retrieval capability")

            payload = json.loads(_decode_base64url(payload_part))
            if not isinstance(payload, dict) or set(payload) != {
                "userId",
                "knowledgeRevisionId",
                "sessionId",
                "exp",
            }:
                raise InvalidRetrievalCapability("invalid retrieval capability")
            user_id = str(uuid.UUID(payload["userId"]))
            signed_revision_id = str(uuid.UUID(payload["knowledgeRevisionId"]))
            requested_revision_id = str(uuid.UUID(knowledge_revision_id))
            session_id = payload["sessionId"]
            expires_at = payload["exp"]
            if (
                not isinstance(session_id, str)
                or not session_id.strip()
                or type(expires_at) is not int
                or expires_at <= int(self._now())
                or signed_revision_id != requested_revision_id
            ):
                raise InvalidRetrievalCapability("invalid retrieval capability")
            return user_id
        except InvalidRetrievalCapability:
            raise
        except (binascii.Error, KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
            raise InvalidRetrievalCapability("invalid retrieval capability") from exc


def _decode_base64url(value: str) -> bytes:
    if not value:
        raise InvalidRetrievalCapability("invalid retrieval capability")
    return base64.b64decode(
        value + "=" * (-len(value) % 4),
        altchars=b"-_",
        validate=True,
    )
