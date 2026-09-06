"""An anonymous API request is refused before a database connection is opened.

2026-08-30: eleven route tests had been red since 67-17 (0fe57979) because the
canonical resolver opened `get_connection()` FIRST and looked for a credential
second -- a request with no Bearer token and no browser session cost one
connection per probe, and in a suite it was a real connection attempt on
127.0.0.1:5432 for a test that only asserts the 401.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from starlette.requests import Request


def _request(headers: list[tuple[bytes, bytes]]) -> Request:
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(
        {"type": "http", "method": "GET", "path": "/api/jobs/job_01", "headers": headers,
         "query_string": b""},
        receive,
    )


def _refuse_connection(*args, **kwargs):
    raise AssertionError("get_connection was opened for a request carrying no credential")


def test_no_bearer_and_no_session_is_refused_without_a_connection(monkeypatch):
    from core import api_auth

    monkeypatch.setenv("TOOROW_AUTH_MODE", "static")
    monkeypatch.setenv("TOOROW_STATIC_TOKEN", "test-token-abc")
    with patch("core.db.get_connection", _refuse_connection):
        ok, identity = asyncio.run(api_auth._authenticate_canonical_api_request(_request([])))
    assert (ok, identity) == (False, "")


def test_an_empty_bearer_is_no_credential():
    from core.api_auth import request_carries_a_credential as carries

    assert carries(_request([(b"authorization", b"Bearer ")])) is False
    assert carries(_request([(b"authorization", b"Basic abc")])) is False
    assert carries(_request([(b"authorization", b"Bearer t")])) is True
