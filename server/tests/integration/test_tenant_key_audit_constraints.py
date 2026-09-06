"""Integration tests for app.tenant_key_audit schema constraints (Story 7.3, AC8).

Runs against the live platform Postgres.
Skipped when TEST_POSTGRES_DSN (or PLATFORM_DB_URL as a fallback) is not set.

Tests:
  - test_tka_fk_requires_valid_project: inserting a tka_ row with a non-existent
    project_id must raise ForeignKeyViolation.
"""

from __future__ import annotations

import os

import pytest

# Allow either TEST_POSTGRES_DSN or the standard PLATFORM_DB_URL
_DSN = os.environ.get("TEST_POSTGRES_DSN") or os.environ.get(
    "PLATFORM_DB_URL",
    "postgresql://connector:connector_dev_only@localhost:5432/connector",
)

_pg_reachable = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN") and not os.environ.get("PLATFORM_DB_URL"),
    reason="TEST_POSTGRES_DSN / PLATFORM_DB_URL not set -- skipping live DB constraint tests",
)


def _check_reachable() -> bool:
    """Probe the DSN without ever being able to block collection.

    This runs at IMPORT time (the skipif below is evaluated at decoration), so a
    connect that hangs hangs `pytest --collect-only` for the WHOLE server suite --
    the project's designated pre-deploy gate. That is exactly what happened on
    2026-07-27: with no DSN configured this fell back to the hardcoded localhost
    default, where something accepts the TCP connection on 5432 but never
    completes the startup handshake, so the connect blocked forever and the suite
    looked merely "slow" for over an hour.

    connect_timeout matches the house convention used by every _pg_reachable()
    probe in server/tests/core (2 seconds). A reachability probe that can wait
    indefinitely does not answer the question it was written to answer.
    """
    try:
        import psycopg  # noqa: PLC0415

        conn = psycopg.connect(_DSN, connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


_pg_available = pytest.mark.skipif(
    not _check_reachable(),
    reason="platform Postgres not reachable -- skipping constraint tests",
)


@_pg_available
def test_tka_fk_requires_valid_project():
    """Inserting a tka_ row with a non-existent project_id must raise ForeignKeyViolation."""
    import psycopg  # noqa: PLC0415
    from psycopg.errors import ForeignKeyViolation  # noqa: PLC0415
    from ulid import ULID  # noqa: PLC0415

    conn = psycopg.connect(_DSN)
    try:
        # Use a deliberately non-existent project_id
        bad_project_id = f"proj_does_not_exist_{ULID()}"
        tka_id = f"tka_{ULID()}"

        with pytest.raises(ForeignKeyViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.tenant_key_audit (id, project_id, action, performed_by)
                    VALUES (%s, %s, 'key_created', 'test')
                    """,
                    (tka_id, bad_project_id),
                )
            conn.commit()
    finally:
        conn.rollback()
        conn.close()


@_pg_available
def test_tka_action_check_constraint():
    """Inserting a tka_ row with an invalid action must raise CheckViolation."""
    import psycopg  # noqa: PLC0415
    from psycopg.errors import CheckViolation  # noqa: PLC0415
    from ulid import ULID  # noqa: PLC0415

    conn = psycopg.connect(_DSN)
    try:
        tka_id = f"tka_{ULID()}"
        with pytest.raises(CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.tenant_key_audit (id, project_id, action, performed_by)
                    VALUES (%s, 'default', 'invalid_action_xyz', 'test')
                    """,
                    (tka_id,),
                )
            conn.commit()
    finally:
        conn.rollback()
        conn.close()
