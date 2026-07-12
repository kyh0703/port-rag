"""Tests for optional Sentry initialization."""

import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest
from rag.config import Settings
from rag.main import initialize_sentry
from rag.main import scrub_sentry_event
from sentry_sdk.integrations.fastapi import FastApiIntegration

INTEGRATION_SCRIPT = """\
import json
import secrets

import sentry_sdk
from fastapi.testclient import TestClient
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

import rag.main
from rag.config import Settings


class MemoryTransport(Transport):
    def __init__(self):
        super().__init__()
        self.events = []

    def capture_envelope(self, envelope: Envelope) -> None:
        self.events.extend(
            event for item in envelope.items if (event := item.get_event()) is not None
        )


transport = MemoryTransport()
real_init = sentry_sdk.init


def init_with_memory_transport(**kwargs):
    return real_init(transport=transport, **kwargs)


rag.main.sentry_sdk.init = init_with_memory_transport
settings = Settings(
    _env_file=None,
    DATABASE_URL="postgresql+asyncpg://port:port@localhost:5432/port",
    EMBEDDER="fake",
    SENTRY_DSN="https://public@example.ingest.sentry.io/1",
)
rag.main.initialize_sentry(settings)
app = rag.main.create_app()


@app.post("/crash")
async def crash():
    raise RuntimeError("crash")


sentinel = secrets.token_urlsafe()
body = f"body-{sentinel}"
query = f"token={sentinel}"
header = f"header-{sentinel}"
response = TestClient(app, raise_server_exceptions=False).post(
    f"/crash?{query}",
    content=body,
    headers={"x-secret": header},
)
sentry_sdk.flush()

assert response.status_code == 500
assert len(transport.events) == 1
event = transport.events[0]
request = event["request"]
assert request["method"] == "POST"
assert request["url"].endswith("/crash")
assert "data" not in request
assert "query_string" not in request
assert "headers" not in request
frames = event["exception"]["values"][0]["stacktrace"]["frames"]
assert all("vars" not in frame for frame in frames)
event_repr = repr(event)
assert body not in event_repr
assert query not in event_repr
assert header not in event_repr

print(
    json.dumps(
        {
            "event_count": len(transport.events),
            "request_method": request["method"],
            "request_url": request["url"],
            "has_frame_vars": any("vars" in frame for frame in frames),
            "contains_sentinel": any(value in event_repr for value in (body, query, header)),
        }
    )
)
"""


def make_settings(*, sentry_dsn: str | None = None) -> Settings:
    return Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://port:port@localhost:5432/port",
        EMBEDDER="fake",
        SENTRY_DSN=sentry_dsn,
    )


def test_initialize_sentry_with_configured_dsn():
    with patch("rag.main.sentry_sdk.init") as init:
        initialize_sentry(make_settings(sentry_dsn="https://public@example.ingest.sentry.io/1"))

    init.assert_called_once()
    assert init.call_args.kwargs["dsn"] == "https://public@example.ingest.sentry.io/1"
    assert isinstance(init.call_args.kwargs["integrations"][0], FastApiIntegration)
    assert init.call_args.kwargs["send_default_pii"] is False
    assert init.call_args.kwargs["max_request_body_size"] == "never"
    assert init.call_args.kwargs["include_local_variables"] is False
    assert init.call_args.kwargs["before_send"] is scrub_sentry_event


@pytest.mark.parametrize("sentry_dsn", [None, ""])
def test_initialize_sentry_skips_unconfigured_dsn(sentry_dsn: str | None):
    with patch("rag.main.sentry_sdk.init") as init:
        initialize_sentry(make_settings(sentry_dsn=sentry_dsn))

    init.assert_not_called()


def test_scrub_sentry_event_removes_request_pii():
    event = {
        "message": "ingest failed",
        "extra": {"document_id": "doc-123"},
        "request": {
            "url": "https://reg.example/documents",
            "data": {"content": "secret document"},
            "query_string": "api_key=secret",
            "headers": {"authorization": "Bearer secret"},
        },
    }

    result = scrub_sentry_event(event, hint={})

    assert result == {
        "message": "ingest failed",
        "extra": {"document_id": "doc-123"},
        "request": {"url": "https://reg.example/documents"},
    }


def test_unhandled_fastapi_exception_is_captured_without_request_pii():
    result = subprocess.run(
        [sys.executable, "-c", INTEGRATION_SCRIPT],
        cwd=Path(__file__).resolve().parent.parent,
        env={key: value for key, value in os.environ.items() if not key.startswith("SENTRY_")},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["event_count"] == 1
    assert payload["request_method"] == "POST"
    assert payload["request_url"].endswith("/crash")
    assert payload["has_frame_vars"] is False
    assert payload["contains_sentinel"] is False
