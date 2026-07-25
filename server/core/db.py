"""toorow -- Platform Postgres connection helper (Story 2.4, T5.3).

Thin wrapper around psycopg (v3, sync) for the platform-db (app schema).
Reads PLATFORM_DB_URL env var at call time -- no module-level connection pool
(stateless module pattern, same as warehouse.py and nango_client.py).

Design decision (recorded per Dev Notes):
  psycopg[binary] v3 (sync) -- consistent with the existing sync pattern in
  audit.py and nango_client.py's _run_coro wrapper. asyncpg would require
  an async context that conflicts with Starlette's sync route handlers without
  an asyncio.run() wrapper (nested-loop risk). psycopg 3.3.4 is already in venv.

AD-3: no token columns. connection_ref stores only nango_connection_id.
Windows/CI note (L-3): all log output uses ASCII-safe strings only.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger(__name__)

PLATFORM_DB_URL_ENV = "PLATFORM_DB_URL"

# psycopg imported at module level so tests can patch core.db.psycopg.
try:
    import psycopg as _psycopg
except ImportError:  # pragma: no cover
    _psycopg = None  # type: ignore[assignment]


def _db_url() -> str:
    """Return the Postgres DSN, reading env var at call time.

    Priority:
      1. PLATFORM_DB_URL env var (explicit override).
      2. Default: platform-db dev credentials from docker-compose.
    """
    override = os.environ.get(PLATFORM_DB_URL_ENV, "").strip()
    if override:
        return override
    password = os.environ.get("PLATFORM_DB_PASSWORD", "connector_dev_only")
    return f"postgresql://connector:{password}@localhost:5432/connector"


def _connect_timeout_seconds() -> int:
    """Bounded TCP connect timeout (seconds), env-overridable.

    Without it, psycopg falls back to the OS TCP timeout (~21 s+ per attempt on
    Windows). Every best-effort ``get_connection()`` call in a test run against an
    absent local Postgres then serialises those waits -- the root cause of the
    multi-hour full-suite runs at ~0 CPU (AI-60, diagnosed 2026-07-21: pytest stuck
    in SynSent on ::1:5432). Production (Supabase pooler) connects well under this
    bound; failures surface in seconds instead of dozens of them.
    """
    raw = os.environ.get("PLATFORM_DB_CONNECT_TIMEOUT", "5").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 5


@contextmanager
def get_connection() -> Iterator["_psycopg.Connection"]:
    """Context manager: yield an open psycopg connection, auto-commit on exit.

    Usage:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(...)
            conn.commit()

    Raises:
        EnvironmentError: if psycopg is not installed.
        psycopg.Error: on connection failure.
    """
    if _psycopg is None:  # pragma: no cover
        raise EnvironmentError(
            "psycopg (v3) is not installed. Add psycopg[binary] to server/pyproject.toml."
        )
    url = _db_url()
    if "connect_timeout" in url:
        # A DSN-specified timeout wins (kwargs would override it otherwise).
        conn = _psycopg.connect(url)
    else:
        conn = _psycopg.connect(url, connect_timeout=_connect_timeout_seconds())
    try:
        yield conn
    finally:
        conn.close()


def set_local_access_context(
    conn: Any, identity: str, *, enforce_epic36: bool = False
) -> None:
    """Install server-trusted access context for the current transaction only."""
    if not identity or identity == "anonymous":
        raise ValueError("a non-anonymous identity is required")
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('toorow.identity', %s, true)", (identity,))
        cur.execute(
            "SELECT set_config('toorow.enforce_epic36', %s, true)",
            ("on" if enforce_epic36 else "off",),
        )

@contextmanager
def get_warehouse_connection(read_only: bool = False) -> Iterator[Any]:
    """Context manager: yield an open DuckDB warehouse connection.

    Uses TOOROW_DUCKDB_PATH env var if set, or defaults to ':memory:'.

    Args:
        read_only: When True, open the DuckDB database in read-only mode
            (structural AD-8 enforcement for read-only consumers such as the
            schema-context generator). A ':memory:' database cannot be opened
            read-only, so the flag is ignored for the in-memory default.
    """
    import duckdb

    path = os.environ.get("TOOROW_DUCKDB_PATH", "").strip() or ":memory:"
    if read_only and path != ":memory:":
        conn = duckdb.connect(path, read_only=True)
    else:
        conn = duckdb.connect(path)
    try:
        yield conn
    finally:
        conn.close()
