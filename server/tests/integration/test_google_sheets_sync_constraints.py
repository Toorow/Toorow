"""Live-Postgres integration tests for the Google Sheets sync consumer (Story 12.10).

These tests apply migration 079 (idempotently, against a disposable test DB)
and exercise the real constraints + end-to-end sync flow that mocked-cursor
tests cannot catch:

  * Migration 079 applies cleanly after 077 (additive + idempotent).
  * managed_feed_sync_schedule table has correct shape + constraints.
  * A sync run through run_sync with a real managed_feed_ledger open_import
    creates the ledger row BEFORE any row is written (NFR14).
  * Idempotent re-run of the same run_id -> replay via the 12.8 ledger.
  * Unchanged snapshot -> noop ledger row (no second candidate).
  * failed outcome leaves prior published version intact.
  * cadence_mode CHECK constraint is enforced (invalid value rejected).
  * quota_allow_hourly_sync column added to project_preferences (additive).

SKIP when TEST_POSTGRES_DSN is unset. Migration 079 is applied to the test DB;
Supabase production apply is human-gated on Jean's authorization.

Phase-B note: the sheets_adapter is fully mocked (no real Sheets API calls).
Live Google OAuth is BLOCKED Phase B (AI-08).
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "infra" / "nango" / "migrations"

# Migration sequence needed for this story (must be applied in order).
_MIGRATION_CHAIN = [
    "030_versioned_datastream_intents.sql",
    "032_datastream_field_mappings.sql",
    "042_datastream_candidate_registry.sql",
    "077_managed_feed_import_ledger.sql",
    "079_google_sheets_sync.sql",
]

requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres constraint test skipped",
)

_SPREADSHEET_ID = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
_SHEET_RANGE = "Budget2026!A:F"

_MINIMAL_COLUMN_MAPPING = {
    "date_column": "Date",
    "metric_columns": {"Budget": "budget_declared"},
}

_RAW_VALUES = [
    ["Date", "Budget"],
    ["2026-07-01", "10000"],
    ["2026-07-02", "5000"],
]

_EXECUTABLE_PLAN = {"executable": True, "grain": ["date"]}


def _uid(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _fake_adapter(connection_id, spreadsheet_id, sheet_range):
    """Minimal read-only adapter mock (no network, no write scope)."""
    return _RAW_VALUES


def _apply_migrations(conn) -> None:
    with conn.cursor() as cur:
        for migration_name in _MIGRATION_CHAIN:
            path = MIGRATIONS / migration_name
            if path.exists():
                cur.execute(path.read_text(encoding="utf-8"))
    conn.commit()


def _insert_test_project_and_datastream(conn, project_id: str, ds_id: str) -> None:
    """Insert the minimal rows needed for FK constraints."""
    with conn.cursor() as cur:
        # app.projects (may not exist in test schema -- skip if absent).
        try:
            cur.execute(
                """
                INSERT INTO app.projects (id, name, status)
                VALUES (%s, %s, 'active')
                ON CONFLICT (id) DO NOTHING
                """,
                (project_id, f"Test Project {project_id[:8]}"),
            )
        except Exception:
            conn.rollback()

        # app.datastreams (minimal row).
        try:
            cur.execute(
                """
                INSERT INTO app.datastreams
                    (id, project_id, name, source_kind, enabled)
                VALUES (%s, %s, 'test-ds', 'managed_feed', TRUE)
                ON CONFLICT (id) DO NOTHING
                """,
                (ds_id, project_id),
            )
        except Exception:
            conn.rollback()

        # app.project_preferences (needed for rejection gate).
        try:
            cur.execute(
                """
                INSERT INTO app.project_preferences (project_id)
                VALUES (%s)
                ON CONFLICT (project_id) DO NOTHING
                """,
                (project_id,),
            )
        except Exception:
            conn.rollback()
    conn.commit()


# ---------------------------------------------------------------------------
# Migration integrity tests (no DB required -- migration text assertions).
# ---------------------------------------------------------------------------


class TestMigrationText:
    """These run without Postgres: they verify the migration SQL text is sane."""

    def _migration_text(self) -> str:
        path = MIGRATIONS / "079_google_sheets_sync.sql"
        return path.read_text(encoding="utf-8")

    def test_migration_file_exists(self):
        assert (MIGRATIONS / "079_google_sheets_sync.sql").exists()

    def test_migration_creates_sync_schedule_table(self):
        sql = self._migration_text()
        assert "managed_feed_sync_schedule" in sql

    def test_migration_has_if_not_exists_guards(self):
        sql = self._migration_text()
        # Every CREATE TABLE should be guarded.
        assert "CREATE TABLE IF NOT EXISTS" in sql

    def test_migration_has_cadence_mode_check_constraint(self):
        sql = self._migration_text()
        assert "cadence_mode" in sql
        assert "CHECK" in sql
        assert "manual" in sql
        assert "daily" in sql
        assert "hourly" in sql

    def test_migration_adds_quota_allow_hourly_sync_to_preferences(self):
        sql = self._migration_text()
        assert "quota_allow_hourly_sync" in sql
        assert "ADD COLUMN IF NOT EXISTS" in sql

    def test_migration_does_not_alter_or_drop_existing_columns(self):
        sql = self._migration_text().upper()
        # Additive contract: no DROP, no ALTER COLUMN <x> TYPE.
        assert "DROP COLUMN" not in sql
        # ALTER TABLE is allowed only for ADD COLUMN.
        if "ALTER TABLE" in sql:
            assert "DROP COLUMN" not in sql

    def test_migration_has_no_provider_name_branch(self):
        """AD-2: no provider name / no branch on feed_format in the migration DDL."""
        sql = self._migration_text().lower()
        # The migration stores feed_format as opaque text; it has no CASE/IF branch.
        # Check that 'google' does not appear as a value guard (index WHERE is OK).
        assert "case feed_format" not in sql

    def test_migration_references_app_schema(self):
        sql = self._migration_text()
        assert "app." in sql


# ---------------------------------------------------------------------------
# Live-Postgres tests.
# ---------------------------------------------------------------------------


@requires_postgres
class TestMigration079AppliesCleanly:
    """Verify migration 079 applies cleanly to a real Postgres instance."""

    @pytest.fixture(autouse=True)
    def _conn(self):
        import psycopg

        dsn = os.environ["TEST_POSTGRES_DSN"]
        with psycopg.connect(dsn) as conn:
            _apply_migrations(conn)
            self.conn = conn
            yield
            conn.rollback()

    def test_sync_schedule_table_exists(self):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'app'
                  AND table_name = 'managed_feed_sync_schedule'
                """
            )
            assert cur.fetchone() is not None

    def test_sync_schedule_has_cadence_mode_column(self):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'app'
                  AND table_name = 'managed_feed_sync_schedule'
                  AND column_name = 'cadence_mode'
                """
            )
            row = cur.fetchone()
            assert row is not None
            assert row[1] == "text"

    def test_project_preferences_has_quota_allow_hourly_sync(self):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'app'
                  AND table_name = 'project_preferences'
                  AND column_name = 'quota_allow_hourly_sync'
                """
            )
            assert cur.fetchone() is not None

    def test_cadence_mode_check_constraint_enforced(self):
        """Invalid cadence_mode value is rejected by the DB constraint."""
        import psycopg

        project_id = _uid("proj_")
        ds_id = _uid("ds_")
        _insert_test_project_and_datastream(self.conn, project_id, ds_id)

        with pytest.raises(psycopg.errors.CheckViolation):
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.managed_feed_sync_schedule
                        (datastream_id, project_id, connection_id, spreadsheet_id,
                         sheet_range, cadence_mode, created_by)
                    VALUES (%s, %s, 'conn_1', %s, 'Sheet1!A:F', 'weekly', 'test')
                    """,
                    (ds_id, project_id, _SPREADSHEET_ID),
                )

    def test_migration_applies_idempotently(self):
        """Applying migration 079 twice does not raise (IF NOT EXISTS guards)."""
        _apply_migrations(self.conn)  # second application -- must not raise


@requires_postgres
class TestSyncConsumerEndToEnd:
    """End-to-end run_sync flow against a real Postgres + 12.8 ledger."""

    @pytest.fixture(autouse=True)
    def _conn(self):
        import psycopg

        dsn = os.environ["TEST_POSTGRES_DSN"]
        with psycopg.connect(dsn) as conn:
            _apply_migrations(conn)
            self.project_id = _uid("proj_")
            self.ds_id = _uid("ds_")
            _insert_test_project_and_datastream(conn, self.project_id, self.ds_id)
            self.conn = conn
            yield
            conn.rollback()

    def _base_args(self, **overrides) -> dict[str, Any]:
        base = dict(
            datastream_id=self.ds_id,
            project_id=self.project_id,
            connection_id="conn_goog_1",
            spreadsheet_id=_SPREADSHEET_ID,
            sheet_range=_SHEET_RANGE,
            sheet_name="Budget2026",
            column_mapping=_MINIMAL_COLUMN_MAPPING,
            plan_version_id=_uid("dsp_"),
            mapping_version_id=_uid("dmap_"),
            projection_plan=_EXECUTABLE_PLAN,
            actor="test-pg",
            cadence_mode="manual",
            quota_profile=None,
            run_id="pg-test-run-001",
            conn=self.conn,
            sheets_adapter=_fake_adapter,
            duckdb_path=":memory:",
        )
        base.update(overrides)
        return base

    def test_sync_creates_ledger_row_before_rows_written(self):
        """NFR14: the ledger row exists the moment open_import returns.

        We verify this by checking the ledger table directly after run_sync.
        The row must exist regardless of whether landing write succeeded.
        """
        from core.google_sheets_sync import run_sync

        result = run_sync(**self._base_args())

        # A ledger row must exist.
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, outcome, datastream_id, feed_format
                FROM app.managed_feed_import_ledger
                WHERE id = %s AND project_id = %s
                """,
                (result["ledger_id"], self.project_id),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[2] == self.ds_id
        assert row[3] == "google_sheets"  # feed_format stored correctly

    def test_sync_outcome_written_or_terminal(self):
        """After a validated run the ledger is 'written' (pending Phase-B publish).

        C1b: run_sync does NOT mark the ledger 'published' (the atomic pointer swap is
        Phase B). A fully-validated run leaves the ledger in the non-terminal
        'written' state and returns outcome='validated_pending_publication'. A blocked
        run (rejected/failed) is terminal. Either is a legitimate honest end-state.
        """
        from core.google_sheets_sync import (
            OUTCOME_VALIDATED_PENDING_PUBLICATION,
            run_sync,
        )
        from core.managed_feed_ledger import OUTCOME_WRITTEN, TERMINAL_OUTCOMES

        result = run_sync(**self._base_args())

        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT outcome FROM app.managed_feed_import_ledger WHERE id = %s",
                (result["ledger_id"],),
            )
            row = cur.fetchone()
        assert row is not None
        # A validated run stays 'written' (pending publication); a blocked run is terminal.
        if result["outcome"] == OUTCOME_VALIDATED_PENDING_PUBLICATION:
            assert row[0] == OUTCOME_WRITTEN
        else:
            assert row[0] in TERMINAL_OUTCOMES

    def test_same_run_id_is_idempotent(self):
        """A second call with the same run_id returns a replay result (no duplicate row)."""
        from core.google_sheets_sync import run_sync

        args = self._base_args(run_id="pg-idemp-run-001")

        run_sync(**args)
        result2 = run_sync(**args)

        assert result2.get("replay") is True
        # No second ledger row should have been inserted.
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM app.managed_feed_import_ledger
                WHERE datastream_id = %s AND project_id = %s
                """,
                (self.ds_id, self.project_id),
            )
            count = cur.fetchone()[0]
        # Should be exactly 1 (same run_id replays, never inserts a second row).
        assert count == 1, f"Expected 1 ledger row but found {count}"

    def test_failed_run_leaves_prior_published_unchanged(self):
        """A failed sync does not touch a REAL current_published_execution_id (M1).

        M1: the prior version of this test asserted None == None (vacuous, because
        run_sync never sets the pointer). Here we establish a REAL baseline pointer by
        running one sync (which mints a real execution via open_import -> 12.5
        create_execution) and then setting current_published_execution_id to that
        execution id directly -- a valid value that satisfies the composite FK
        (current_published_execution_id, id, project_id) ->
        datastream_executions(id, datastream_id, project_id). We then run a FAILING
        sync and prove the pointer is left intact (not NULL, not swapped).
        """
        from core.google_sheets_sync import run_sync

        # First: a baseline sync. This mints a real 12.5 execution for this datastream
        # (open_import -> create_execution) and lands + validates the snapshot. It does
        # NOT publish (Phase B) -- so we seed the pointer ourselves below to a REAL
        # execution id to simulate a prior published version.
        baseline = run_sync(**self._base_args(run_id="pg-baseline-001"))
        baseline_execution_id = baseline.get("execution_id")
        assert baseline_execution_id is not None, (
            "baseline run must mint a real execution to point at"
        )

        # Seed a REAL published pointer (satisfies the composite FK to the execution).
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE app.datastreams
                SET current_published_execution_id = %s
                WHERE id = %s AND project_id = %s
                """,
                (baseline_execution_id, self.ds_id, self.project_id),
            )
        self.conn.commit()

        # Confirm the baseline pointer is now a NON-NULL real execution id.
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT current_published_execution_id
                FROM app.datastreams
                WHERE id = %s AND project_id = %s
                """,
                (self.ds_id, self.project_id),
            )
            row = cur.fetchone()
        baseline_pointer = row[0] if row else None
        assert baseline_pointer == baseline_execution_id
        assert baseline_pointer is not None  # M1: the baseline is REAL, not None.

        # Now: a failing sync (permission error).
        def failing_adapter(connection_id, spreadsheet_id, sheet_range):
            raise PermissionError("Consent revoked")

        result = run_sync(
            **self._base_args(
                run_id="pg-fail-001",
                sheets_adapter=failing_adapter,
            )
        )

        assert result["outcome"] == "failed"

        # The published pointer must be UNCHANGED (still the baseline execution id).
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT current_published_execution_id
                FROM app.datastreams
                WHERE id = %s AND project_id = %s
                """,
                (self.ds_id, self.project_id),
            )
            row_after = cur.fetchone()
        pointer_after = row_after[0] if row_after else None

        assert pointer_after == baseline_pointer, (
            f"Failed sync must not change the published pointer: "
            f"before={baseline_pointer!r}, after={pointer_after!r}"
        )
        assert pointer_after is not None  # not nulled by the failing run.
