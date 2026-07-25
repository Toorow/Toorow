"""Integration test: app.project_reports UNIQUE constraint (AI-37, Story 6.1, AC14).

Runs against a REAL Postgres (via the live_postgres fixture) so the
UNIQUE (project_id, module_name, report_id) constraint from migration 014 is
verified for real — a mocked cursor cannot catch a schema constraint violation
(the Epic 5 F-1 lesson). Skips when TEST_POSTGRES_DSN is not set.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set — live Postgres constraint test skipped",
)


def test_unique_constraint_enforced(live_postgres):
    """Two rows with the same (project_id, module_name, report_id) -> UniqueViolation."""
    import psycopg

    project_id = f"proj_it_{uuid.uuid4().hex[:8]}"
    conn = live_postgres

    try:
        # Story 7.1: project_reports now has an FK to app.projects. Seed the
        # parent project row before inserting report rows.
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.projects (id, name, slug, created_by)
                VALUES (%s, %s, %s, 'test') ON CONFLICT DO NOTHING
                """,
                (project_id, project_id, project_id),
            )
            cur.execute(
                """
                INSERT INTO app.project_reports
                    (id, project_id, module_name, report_id, enabled, display_order)
                VALUES (%s, %s, 'google-analytics', 'overview_daily', TRUE, 0)
                """,
                (f"rpt_{uuid.uuid4().hex}", project_id),
            )
        conn.commit()

        with pytest.raises(psycopg.errors.UniqueViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.project_reports
                        (id, project_id, module_name, report_id, enabled, display_order)
                    VALUES (%s, %s, 'google-analytics', 'overview_daily', FALSE, 1)
                    """,
                    (f"rpt_{uuid.uuid4().hex}", project_id),
                )
            conn.commit()
        conn.rollback()
    finally:
        # Cleanup the row we inserted.
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.project_reports WHERE project_id = %s", (project_id,)
            )
            cur.execute("DELETE FROM app.projects WHERE id = %s", (project_id,))
        conn.commit()
