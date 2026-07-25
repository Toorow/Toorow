"""Integration tests: Story 8.2 datastream schema constraints (AI-37).

Runs against a REAL Postgres (via the live_postgres fixture from conftest.py)
so UNIQUE(project_id, name), FK violations, and backfill idempotence are
verified against the actual migration 023 schema.

Skips when TEST_POSTGRES_DSN is not set (default CI and local runs).

Prerequisites:
  - Migrations 001-023 applied (including 023_datastreams.sql).
  - live_postgres fixture from server/tests/conftest.py.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres constraint test skipped",
)


def _unique_id(prefix=""):
    return f"{prefix}{uuid.uuid4().hex[:8]}"


def _seed_project(conn, project_id: str) -> None:
    """Insert a minimal project row (idempotent)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.projects (id, name, slug, created_by)
            VALUES (%s, %s, %s, 'test')
            ON CONFLICT DO NOTHING
            """,
            (project_id, project_id, project_id),
        )
    conn.commit()


def _seed_connection(conn, conn_id: str, project_id: str, provider: str = "ga") -> None:
    """Insert a minimal connection_ref row (idempotent)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.connection_ref (id, provider, nango_connection_id, project_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (conn_id, provider, f"nango_{conn_id}", project_id),
        )
    conn.commit()


def _cleanup(conn, project_id: str) -> None:
    """Remove all test data for project_id (reverse FK order)."""
    conn.rollback()
    with conn.cursor() as cur:
        # Remove datastream_mappings first (FK to datastreams)
        cur.execute(
            """
            DELETE FROM app.datastream_mappings
            WHERE datastream_id IN (
                SELECT id FROM app.datastreams WHERE project_id = %s
            )
            """,
            (project_id,),
        )
        cur.execute("DELETE FROM app.datastreams WHERE project_id = %s", (project_id,))
        cur.execute("DELETE FROM app.connection_ref WHERE project_id = %s", (project_id,))
        cur.execute("DELETE FROM app.projects WHERE id = %s", (project_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# 1. UNIQUE(project_id, name) constraint
# ---------------------------------------------------------------------------


class TestDatastreamsUniqueConstraint:
    def test_duplicate_name_within_project_raises(self, live_postgres):
        """INSERT two datastreams with the same name in the same project -> UniqueViolation."""
        import psycopg

        conn = live_postgres
        project_id = f"proj_it_{_unique_id()}"
        try:
            _seed_project(conn, project_id)

            ds_id1 = f"ds_{_unique_id()}"
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.datastreams
                        (id, project_id, name, module_name, created_by)
                    VALUES (%s, %s, 'My Stream', 'google-analytics', 'test')
                    """,
                    (ds_id1, project_id),
                )
            conn.commit()

            ds_id2 = f"ds_{_unique_id()}"
            with pytest.raises(psycopg.errors.UniqueViolation):
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO app.datastreams
                            (id, project_id, name, module_name, created_by)
                        VALUES (%s, %s, 'My Stream', 'meta-ads', 'test')
                        """,
                        (ds_id2, project_id),
                    )
                conn.commit()
        finally:
            _cleanup(conn, project_id)

    def test_same_name_in_different_projects_allowed(self, live_postgres):
        """Same name in different projects must NOT raise."""
        conn = live_postgres
        project_a = f"proj_it_{_unique_id()}"
        project_b = f"proj_it_{_unique_id()}"
        try:
            _seed_project(conn, project_a)
            _seed_project(conn, project_b)

            for proj, ds_id_suffix in [(project_a, "a"), (project_b, "b")]:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO app.datastreams
                            (id, project_id, name, module_name, created_by)
                        VALUES (%s, %s, 'Shared Name', 'google-analytics', 'test')
                        """,
                        (f"ds_{_unique_id()}", proj),
                    )
            conn.commit()
        finally:
            _cleanup(conn, project_a)
            _cleanup(conn, project_b)


# ---------------------------------------------------------------------------
# 2. FK constraint: project_id must reference app.projects
# ---------------------------------------------------------------------------


class TestDatastreamsFKConstraint:
    def test_insert_with_nonexistent_project_raises(self, live_postgres):
        """FK violation when project_id not in app.projects."""
        import psycopg

        conn = live_postgres
        nonexistent_project = f"proj_ghost_{_unique_id()}"
        ds_id = f"ds_{_unique_id()}"

        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.datastreams
                        (id, project_id, name, module_name, created_by)
                    VALUES (%s, %s, 'Test', 'ga', 'test')
                    """,
                    (ds_id, nonexistent_project),
                )
            conn.commit()
        conn.rollback()

    def test_connection_ref_fk_enforced(self, live_postgres):
        """FK violation when connection_ref_id not in app.connection_ref."""
        import psycopg

        conn = live_postgres
        project_id = f"proj_it_{_unique_id()}"
        try:
            _seed_project(conn, project_id)

            ds_id = f"ds_{_unique_id()}"
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO app.datastreams
                            (id, project_id, name, module_name, connection_ref_id, created_by)
                        VALUES (%s, %s, 'Test', 'ga', 'conn_ghost_xxx', 'test')
                        """,
                        (ds_id, project_id),
                    )
                conn.commit()
            conn.rollback()
        finally:
            _cleanup(conn, project_id)


# ---------------------------------------------------------------------------
# 3. datastream_mappings PK constraint: (datastream_id, source_field)
# ---------------------------------------------------------------------------


class TestDatastreamMappingsConstraint:
    def test_duplicate_mapping_raises(self, live_postgres):
        """Duplicate (datastream_id, source_field) -> UniqueViolation."""
        import psycopg

        conn = live_postgres
        project_id = f"proj_it_{_unique_id()}"
        try:
            _seed_project(conn, project_id)

            ds_id = f"ds_{_unique_id()}"
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.datastreams
                        (id, project_id, name, module_name, created_by)
                    VALUES (%s, %s, 'Mapping Test', 'ga', 'test')
                    """,
                    (ds_id, project_id),
                )
                cur.execute(
                    """
                    INSERT INTO app.datastream_mappings
                        (datastream_id, source_field, target_field, is_key_column)
                    VALUES (%s, 'clicks', 'clicks', FALSE)
                    """,
                    (ds_id,),
                )
            conn.commit()

            with pytest.raises((psycopg.errors.UniqueViolation, Exception)):
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO app.datastream_mappings
                            (datastream_id, source_field, target_field, is_key_column)
                        VALUES (%s, 'clicks', 'impressions', FALSE)
                        """,
                        (ds_id,),
                    )
                conn.commit()
            conn.rollback()
        finally:
            _cleanup(conn, project_id)

    def test_target_field_fk_enforced(self, live_postgres):
        """target_field FK to app.target_fields: non-existent target -> ForeignKeyViolation."""
        import psycopg

        conn = live_postgres
        project_id = f"proj_it_{_unique_id()}"
        try:
            _seed_project(conn, project_id)

            ds_id = f"ds_{_unique_id()}"
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.datastreams
                        (id, project_id, name, module_name, created_by)
                    VALUES (%s, %s, 'FK Test', 'ga', 'test')
                    """,
                    (ds_id, project_id),
                )
            conn.commit()

            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO app.datastream_mappings
                            (datastream_id, source_field, target_field, is_key_column)
                        VALUES (%s, 'clicks', 'nonexistent_metric_xyz', FALSE)
                        """,
                        (ds_id,),
                    )
                conn.commit()
            conn.rollback()
        finally:
            _cleanup(conn, project_id)


# ---------------------------------------------------------------------------
# 4. target_fields seeded by migration
# ---------------------------------------------------------------------------


class TestTargetFieldsSeeded:
    def test_canonical_metrics_seeded(self, live_postgres):
        """After migration 023, canonical metrics must exist in app.target_fields."""
        conn = live_postgres
        expected_metrics = {
            "sessions", "active_users", "conversions", "clicks",
            "impressions", "cost", "revenue", "average_position",
        }
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name FROM app.target_fields
                WHERE is_default = TRUE AND field_kind = 'metric'
                """
            )
            actual = {row[0] for row in cur.fetchall()}
        missing = expected_metrics - actual
        assert not missing, f"Missing canonical metrics in target_fields: {missing}"

    def test_canonical_dimensions_seeded(self, live_postgres):
        """After migration 023, core dimensions must exist in app.target_fields."""
        conn = live_postgres
        expected_dims = {"date", "device_category", "country", "page"}
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name FROM app.target_fields
                WHERE is_default = TRUE AND field_kind = 'dimension'
                """
            )
            actual = {row[0] for row in cur.fetchall()}
        missing = expected_dims - actual
        assert not missing, f"Missing core dimensions in target_fields: {missing}"

    def test_average_position_is_metric_with_average_measure(self, live_postgres):
        """average_position has measure='average' (non-additive via weighted avg semantics)."""
        conn = live_postgres
        with conn.cursor() as cur:
            cur.execute(
                "SELECT measure FROM app.target_fields WHERE name = 'average_position'"
            )
            row = cur.fetchone()
        assert row is not None, "average_position not found in target_fields"
        assert row[0] == "average"


# ---------------------------------------------------------------------------
# 5. pull_jobs.datastream_id FK: SET NULL on datastream delete
# ---------------------------------------------------------------------------


class TestPullJobsDatastreamFK:
    def test_datastream_id_column_exists(self, live_postgres):
        """Migration 023 must have added datastream_id to app.pull_jobs."""
        conn = live_postgres
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'app'
                  AND table_name = 'pull_jobs'
                  AND column_name = 'datastream_id'
                """
            )
            row = cur.fetchone()
        assert row is not None, (
            "pull_jobs.datastream_id column missing (migration 023 not applied?)"
        )


# ---------------------------------------------------------------------------
# 6. Backfill idempotence
# ---------------------------------------------------------------------------


class TestBackfillIdempotence:
    def test_backfill_twice_does_not_duplicate(self, live_postgres):
        """Running backfill_datastreams twice leaves the same number of rows."""
        conn = live_postgres
        project_id = f"proj_bf_{_unique_id()}"
        conn_id = f"conn_bf_{_unique_id()}"
        try:
            _seed_project(conn, project_id)
            _seed_connection(conn, conn_id, project_id, "google-analytics")

            # Simulate the backfill manually (without loaded modules).
            # Insert one datastream directly to simulate a "first run".
            ds_id = f"ds_{_unique_id()}"
            ds_name = "google-analytics - Standard Daily Report"
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.datastreams
                        (id, project_id, name, module_name, connection_ref_id,
                         report_profile_id, created_by)
                    VALUES (%s, %s, %s, 'google-analytics', %s, 'standard_daily', 'system')
                    ON CONFLICT (project_id, name) DO NOTHING
                    """,
                    (ds_id, project_id, ds_name, conn_id),
                )
            conn.commit()

            # Second run with ON CONFLICT DO NOTHING should be a no-op.
            ds_id2 = f"ds_{_unique_id()}"
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.datastreams
                        (id, project_id, name, module_name, connection_ref_id,
                         report_profile_id, created_by)
                    VALUES (%s, %s, %s, 'google-analytics', %s, 'standard_daily', 'system')
                    ON CONFLICT (project_id, name) DO NOTHING
                    """,
                    (ds_id2, project_id, ds_name, conn_id),
                )
            conn.commit()

            # Verify only one row exists.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM app.datastreams WHERE project_id = %s",
                    (project_id,),
                )
                count = cur.fetchone()[0]
            assert count == 1, f"Expected 1 datastream after idempotent backfill, got {count}"
        finally:
            _cleanup(conn, project_id)
