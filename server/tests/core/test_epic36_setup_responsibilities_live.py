"""Live PostgreSQL deployment gate for Story 36.6 setup coordination."""

from __future__ import annotations

import os

import pytest

_DSN = os.environ.get("TEST_POSTGRES_DSN")


def test_setup_responsibility_deployment_gate_never_silently_skips_postgres():
    enabled = os.environ.get("TOOROW_EPIC36_PRODUCTION_ENABLED", "false").lower()
    if enabled in {"1", "true", "yes"}:
        assert _DSN, "TEST_POSTGRES_DSN is mandatory when the Epic 36 gate is enabled"
    if not _DSN:
        pytest.skip("Epic 36 production gate is off; live Postgres is unavailable")


@pytest.mark.skipif(not _DSN, reason="Requires TEST_POSTGRES_DSN")
def test_live_setup_schema_enforces_unique_and_immutable_capabilities():
    import psycopg

    with psycopg.connect(_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT to_regclass('app.setup_journeys'),
                   to_regclass('app.setup_tasks'),
                   to_regclass('app.setup_handoffs'),
                   to_regclass('app.setup_handoff_sessions')
            """
        )
        assert all(cur.fetchone()), "migration 065 must be applied"
        cur.execute(
            """
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = 'app.setup_handoffs'::regclass
            """
        )
        constraints = {row[0] for row in cur.fetchall()}
        assert "setup_handoffs_bearer_hash_key" in constraints
        assert "uq_setup_handoff_operation" in constraints
        cur.execute(
            """
            SELECT tgname
            FROM pg_trigger
            WHERE tgrelid = 'app.setup_handoffs'::regclass
              AND NOT tgisinternal
            """
        )
        assert "trg_setup_handoff_binding_immutable" in {row[0] for row in cur.fetchall()}
