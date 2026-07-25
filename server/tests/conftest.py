"""Shared pytest fixtures for the server test suite.

Provides the ``live_postgres`` fixture (AI-37, Story 6.1, AC14): a real psycopg
connection to the test Postgres, used to verify schema constraints (NOT NULL / FK /
UNIQUE) that mocked cursors cannot catch. Skips when ``TEST_POSTGRES_DSN`` is unset.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def live_postgres():
    """Yield a real psycopg connection to the test Postgres (AI-37).

    Skips the test when ``TEST_POSTGRES_DSN`` is not set — CI without a Postgres
    service, and contributors who have not opted in, run everything else. When set,
    the DSN points at a database where the app schema migrations (incl. 014) have
    been applied.
    """
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN not set — skipping live Postgres integration test")

    import psycopg  # noqa: PLC0415

    conn = psycopg.connect(dsn)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
