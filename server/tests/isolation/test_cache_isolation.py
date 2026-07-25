"""Cache-key isolation (Story 7.4, AC3, AC8).

Proves the two project-scoped caches never bleed across projects:
  - Morning briefing cache (app.morning_briefings) is keyed by (project_id,
    briefing_date): beta's briefing must NOT appear in alpha's daily report.
  - Module enablement is per-project (app.project_modules), not shared in-memory:
    alpha disabling a module must not affect beta's view and vice-versa.
"""

from __future__ import annotations

import os

import psycopg
import pytest

pytestmark = pytest.mark.isolation


def _dsn() -> str:
    return os.environ["TEST_POSTGRES_DSN"]


def test_morning_briefing_cache_key_is_project_scoped(two_projects):
    """Beta briefing does NOT appear when querying alpha's (project,date) key."""
    alpha, beta = two_projects

    conn = psycopg.connect(_dsn())
    try:
        with conn.cursor() as cur:
            # Alpha's cache key returns ONLY alpha's briefing.
            cur.execute(
                """
                SELECT project_id, insights FROM app.morning_briefings
                WHERE project_id = %s AND briefing_date = CURRENT_DATE
                """,
                (alpha["id"],),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    assert len(rows) == 1, "exactly one briefing on alpha's cache key"
    assert rows[0][0] == alpha["id"]
    assert "average_position" not in str(rows[0][1]), "beta briefing leaked via cache key!"


def test_module_enablement_cache_scoped(two_projects):
    """Alpha disabling a module does not flip beta's enablement (no shared state)."""
    from core import db as core_db
    from core.module_enablement import is_module_enabled

    alpha, beta = two_projects

    # Flip alpha's google-analytics OFF; beta must keep its own state.
    conn = psycopg.connect(_dsn())
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE app.project_modules SET enabled = FALSE
                WHERE project_id = %s AND module_name = 'google-analytics'
                """,
                (alpha["id"],),
            )
        conn.commit()
    finally:
        conn.close()

    with core_db.get_connection() as c:
        alpha_ga = is_module_enabled("google-analytics", alpha["id"], c)
        # Beta never had a google-analytics row -> default-enabled, unaffected.
        beta_ga = is_module_enabled("google-analytics", beta["id"], c)

    assert alpha_ga is False, "alpha google-analytics now disabled"
    assert beta_ga is True, "beta google-analytics must be unaffected by alpha's change"

    # Restore alpha for idempotency across reruns within the module scope.
    conn = psycopg.connect(_dsn())
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE app.project_modules SET enabled = TRUE
                WHERE project_id = %s AND module_name = 'google-analytics'
                """,
                (alpha["id"],),
            )
        conn.commit()
    finally:
        conn.close()
