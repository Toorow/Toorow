"""Integration test: app.projects constraints (Story 7.1, AC8).

Runs against a REAL Postgres (via the live_postgres fixture) so the migration 018
schema constraints are verified for real — a mocked cursor cannot catch a schema
constraint violation (the Epic 5 F-1 lesson). Skips when TEST_POSTGRES_DSN is
unset.

Covers:
  - test_slug_unique_constraint: two projects with the same slug -> UniqueViolation.
  - test_fk_connection_ref_requires_valid_project: connection_ref with a
    non-existent project_id -> ForeignKeyViolation.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set — live Postgres constraint test skipped",
)


def test_slug_unique_constraint(live_postgres):
    """Two projects with the same slug -> UniqueViolation."""
    import psycopg

    conn = live_postgres
    slug = f"it-slug-{uuid.uuid4().hex[:8]}"
    pid1 = f"proj_it_{uuid.uuid4().hex[:8]}"
    pid2 = f"proj_it_{uuid.uuid4().hex[:8]}"
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.projects (id, name, slug, created_by, org_id) "
                "VALUES (%s, %s, %s, 'test', 'org_test_fixture')",
                (pid1, "IT One", slug),
            )
        conn.commit()

        with pytest.raises(psycopg.errors.UniqueViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.projects (id, name, slug, created_by, org_id) "
                    "VALUES (%s, %s, %s, 'test', 'org_test_fixture')",
                    (pid2, "IT Two", slug),
                )
            conn.commit()
        conn.rollback()
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.projects WHERE slug = %s", (slug,))
        conn.commit()


def test_fk_connection_ref_requires_valid_project(live_postgres):
    """Inserting a connection_ref with a non-existent project_id -> ForeignKeyViolation."""
    import psycopg

    conn = live_postgres
    conn_id = f"conn_it_{uuid.uuid4().hex[:12]}"
    missing_project = f"proj_missing_{uuid.uuid4().hex[:8]}"
    try:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.connection_ref "
                    "(id, provider, nango_connection_id, project_id, owner_org_id, owner_identity) "
                    "VALUES (%s, 'google-analytics', %s, %s, 'org_test_fixture', "
                    "'tester@example.com')",
                    (conn_id, conn_id, missing_project),
                )
            conn.commit()
        conn.rollback()
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.connection_ref WHERE id = %s", (conn_id,))
        conn.commit()
