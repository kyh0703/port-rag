"""Authentication for trusted internal HTTP clients, before request body handling."""

import hashlib
import hmac
import re

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

HEADER_NAME = b"x-internal-server"
PUBLIC_PATHS = frozenset({"/healthz", "/metrics", "/metrics/"})


def validate_internal_server_key(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[\x21-\x7e]{32,256}", value) is None:
        raise ValueError("INTERNAL_SERVER_KEY must contain 32..256 non-whitespace ASCII characters")
    return value


class InternalServerAuthMiddleware:
    def __init__(self, app: ASGIApp, key: str | None = None) -> None:
        self.app = app
        self.expected_digest = (
            hashlib.sha256(validate_internal_server_key(key).encode("ascii")).digest()
            if key is not None
            else None
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["method"] in {"GET", "HEAD"} and scope["path"] in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        values = [value for name, value in scope["headers"] if name.lower() == HEADER_NAME]
        authenticated = (
            self.expected_digest is not None
            and len(values) == 1
            and 32 <= len(values[0]) <= 256
            and hmac.compare_digest(hashlib.sha256(values[0]).digest(), self.expected_digest)
        )
        if not authenticated:
            response = JSONResponse(
                status_code=401,
                content={
                    "statusCode": 401,
                    "message": "Internal service authentication required",
                    "error": "Unauthorized",
                    "data": None,
                },
                headers={"WWW-Authenticate": "InternalServerKey"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def scrub_internal_key_fields(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: "[Filtered]"
            if isinstance(key, str)
            and key.lower().replace("-", "").replace("_", "")
            in {"xinternalserver", "internalserverkey"}
            else scrub_internal_key_fields(nested)
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [scrub_internal_key_fields(item) for item in value]
    return value
