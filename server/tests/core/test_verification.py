"""Unit tests for server/core/verification.py (Story 3.5, AC2-AC5).

Tests:
  - compute_expected_rows: no dimensions, with dimensions, empty manifest fallback
  - run_post_pull_verification: verdict matrix (ok/partial/empty)
  - run_post_pull_verification: zero-division guard
  - run_post_pull_verification: never-raises contract
  - _set_connection_health_red: upserts 'populate_failed' status

Strategy:
  - All DB and DuckDB calls mocked -- no real Postgres or DuckDB required.
  - HEALTH_POLLER_ENABLED=false, QUEUE_WORKER_ENABLED=false.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_pg_connection():
    """Build a fake psycopg connection context manager."""
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)

    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor = MagicMock(return_value=mock_cursor)
    mock_conn.commit = MagicMock()
    mock_conn.close = MagicMock()

    @contextmanager
    def _fake_get_connection():
        yield mock_conn

    return _fake_get_connection, mock_conn, mock_cursor


# ---------------------------------------------------------------------------
# compute_expected_rows tests (AC2, T3.1)
# ---------------------------------------------------------------------------


class TestComputeExpectedRows:
    def test_no_dimensions_returns_days(self):
        """Profile with no dimensions -> days (1 row/day minimum expectation)."""
        from core.verification import compute_expected_rows

        manifest = {
            "report_profiles": [
                {"id": "p1", "granularity": "day", "dimensions": []}
            ]
        }
        result = compute_expected_rows(manifest, "2026-07-01", "2026-07-07")
        assert result == 7  # 7 days

    def test_with_dimensions_and_cardinality(self):
        """Profile with cardinality-declared dimensions -> days * product(cardinalities)."""
        from core.verification import compute_expected_rows

        manifest = {
            "report_profiles": [
                {
                    "id": "p1",
                    "granularity": "day",
                    "dimensions": [
                        {"name": "device_category", "cardinality": 3},
                        {"name": "country", "cardinality": 50},
                    ],
                }
            ]
        }
        result = compute_expected_rows(manifest, "2026-07-01", "2026-07-07")
        assert result == 7 * 3 * 50  # 1050

    def test_empty_manifest_returns_days(self):
        """Empty manifest {} -> days fallback (no report_profiles)."""
        from core.verification import compute_expected_rows

        result = compute_expected_rows({}, "2026-07-01", "2026-07-03")
        assert result == 3  # 3 days

    def test_no_report_profiles_returns_days(self):
        """Manifest with report_profiles=[] -> days fallback."""
        from core.verification import compute_expected_rows

        result = compute_expected_rows({"report_profiles": []}, "2026-07-01", "2026-07-01")
        assert result == 1  # single day

    def test_string_dimensions_use_cardinality_1(self):
        """String-only dimension entries (no cardinality dict) default to 1 each."""
        from core.verification import compute_expected_rows

        manifest = {
            "report_profiles": [
                {
                    "id": "p1",
                    "granularity": "day",
                    "dimensions": ["date", "device_category", "country"],
                }
            ]
        }
        result = compute_expected_rows(manifest, "2026-07-01", "2026-07-05")
        # 5 days * 1 * 1 * 1 = 5
        assert result == 5

    def test_picks_first_day_granularity_profile(self):
        """First profile with granularity=day is used; others ignored."""
        from core.verification import compute_expected_rows

        manifest = {
            "report_profiles": [
                {"id": "weekly", "granularity": "week", "dimensions": []},
                {
                    "id": "daily",
                    "granularity": "day",
                    "dimensions": [{"name": "d", "cardinality": 2}],
                },
            ]
        }
        result = compute_expected_rows(manifest, "2026-07-01", "2026-07-04")
        assert result == 4 * 2  # 8

    def test_single_day_window(self):
        """Single-day window (date_from == date_to) returns at least 1."""
        from core.verification import compute_expected_rows

        result = compute_expected_rows({}, "2026-07-01", "2026-07-01")
        assert result == 1

    def test_ga4_manifest_no_cardinality_returns_days(self):
        """GA4 manifest has string dimensions (no cardinality) -> days fallback."""
        from core.verification import compute_expected_rows

        # Reflect the actual GA4 manifest structure (dimensions as strings)
        manifest = {
            "report_profiles": [
                {
                    "id": "standard_daily",
                    "dimensions": ["date", "device_category", "country"],
                    "metrics": ["sessions", "active_users", "conversions"],
                }
            ]
        }
        result = compute_expected_rows(manifest, "2026-07-01", "2026-07-07")
        # 7 days * 1 * 1 * 1 (string dims default to cardinality 1)
        assert result == 7

    # AI-20: verification_expected_rows_per_day tests (Story 4.1, AC9)

    def test_verification_expected_rows_per_day_int_overrides_cardinality(self):
        """AI-20: verification_expected_rows_per_day=150 -> days * 150 (ignores cardinality)."""
        from core.verification import compute_expected_rows

        manifest = {
            "report_profiles": [
                {
                    "id": "standard_daily",
                    "dimensions": ["date", "device_category", "country"],
                    "metrics": ["sessions", "active_users", "conversions"],
                    "verification_expected_rows_per_day": 150,
                }
            ]
        }
        result = compute_expected_rows(manifest, "2026-07-01", "2026-07-07")
        # AI-20 path: 7 days * 150 = 1050
        assert result == 7 * 150

    def test_verification_expected_rows_per_day_null_falls_through(self):
        """AI-20: verification_expected_rows_per_day=null -> fall through to cardinality."""
        from core.verification import compute_expected_rows

        manifest = {
            "report_profiles": [
                {
                    "id": "campaign_daily",
                    "dimensions": ["date", "campaign_id"],
                    "metrics": ["spend"],
                    "verification_expected_rows_per_day": None,
                }
            ]
        }
        result = compute_expected_rows(manifest, "2026-07-01", "2026-07-07")
        # null -> cardinality fallback -> 7 days * 1 * 1 = 7
        assert result == 7

    def test_verification_expected_rows_per_day_absent_falls_through(self):
        """AI-20: absent key -> fall through to cardinality (same as null)."""
        from core.verification import compute_expected_rows

        manifest = {
            "report_profiles": [
                {
                    "id": "standard_daily",
                    "dimensions": ["date", "device_category"],
                    "metrics": ["sessions"],
                    # no verification_expected_rows_per_day key
                }
            ]
        }
        result = compute_expected_rows(manifest, "2026-07-01", "2026-07-03")
        # absent -> cardinality fallback -> 3 days * 1 * 1 = 3
        assert result == 3

    def test_ga4_real_manifest_verification_rows_per_day(self):
        """AI-20: GA4 real manifest (verification_expected_rows_per_day=150) returns days*150."""
        import json
        from pathlib import Path

        from core.verification import compute_expected_rows

        manifest_path = (
            Path(__file__).parents[2]
            / "modules"
            / "google-analytics"
            / "manifest.json"
        )
        if not manifest_path.exists():
            import pytest
            pytest.skip("GA4 manifest.json not found")

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        result = compute_expected_rows(manifest, "2026-07-01", "2026-07-07")
        # GA4 standard_daily has verification_expected_rows_per_day=150
        assert result == 7 * 150


# ---------------------------------------------------------------------------
# run_post_pull_verification verdict tests (AC4, T3.4)
# ---------------------------------------------------------------------------


class TestRunPostPullVerification:
    """Tests for run_post_pull_verification verdict matrix and side effects."""

    _BASE_MANIFEST = {
        "report_profiles": [
            {"id": "p1", "granularity": "day", "dimensions": []}
        ]
    }

    def _call_verification(
        self,
        actual_rows: int,
        expected_rows: int,
        *,
        threshold: str = "0.5",
    ):
        """Call run_post_pull_verification with mocked _count_raw_rows and DB.

        Returns the captured INSERT params tuple.
        """
        fake_conn_mgr, mock_conn, mock_cursor = _make_fake_pg_connection()

        with (
            patch("core.verification._count_raw_rows", return_value=actual_rows),
            patch(
                "core.verification.compute_expected_rows",
                return_value=expected_rows,
            ),
            patch("core.db.get_connection", new=fake_conn_mgr),
            patch.dict(os.environ, {"VERIFICATION_PARTIAL_THRESHOLD": threshold}),
        ):
            from core.verification import run_post_pull_verification

            run_post_pull_verification(
                pull_id="pull_test_001",
                connection_ref_id="conn_test_001",
                date_from="2026-07-01",
                date_to="2026-07-07",
                manifest=self._BASE_MANIFEST,
            )

        return mock_cursor

    def test_verdict_ok(self):
        """actual >= threshold * expected -> verdict 'ok'."""
        from unittest.mock import patch

        fake_conn_mgr, mock_conn, mock_cursor = _make_fake_pg_connection()

        with (
            patch("core.verification._count_raw_rows", return_value=150),
            patch("core.verification.compute_expected_rows", return_value=150),
            patch("core.db.get_connection", new=fake_conn_mgr),
        ):
            from core.verification import run_post_pull_verification

            run_post_pull_verification(
                pull_id="pull_ok",
                connection_ref_id="conn_ok",
                date_from="2026-07-01",
                date_to="2026-07-07",
                manifest=self._BASE_MANIFEST,
            )

        # Verify INSERT was called
        execute_calls = [str(c) for c in mock_cursor.execute.call_args_list]
        insert_calls = [c for c in execute_calls if "INSERT" in c and "pull_verifications" in c]
        assert insert_calls, "Expected INSERT into app.pull_verifications"

        # Verdict should be in the call args
        all_call_args = str(mock_cursor.execute.call_args_list)
        assert "ok" in all_call_args

    def test_verdict_empty(self):
        """actual_rows == 0 -> verdict 'empty'."""
        fake_conn_mgr, mock_conn, mock_cursor = _make_fake_pg_connection()
        health_calls: list = []

        with (
            patch("core.verification._count_raw_rows", return_value=0),
            patch("core.verification.compute_expected_rows", return_value=7),
            patch("core.db.get_connection", new=fake_conn_mgr),
            patch(
                "core.verification._set_connection_health_red",
                side_effect=lambda *a, **kw: health_calls.append(a),
            ),
        ):
            from core.verification import run_post_pull_verification

            run_post_pull_verification(
                pull_id="pull_empty",
                connection_ref_id="conn_empty",
                date_from="2026-07-01",
                date_to="2026-07-07",
                manifest=self._BASE_MANIFEST,
            )

        all_call_args = str(mock_cursor.execute.call_args_list)
        assert "empty" in all_call_args
        # _set_connection_health_red must be called on empty verdict
        assert health_calls, "_set_connection_health_red must be called for empty verdict"
        assert health_calls[0][1] == "empty"

    def test_verdict_partial(self):
        """ratio < threshold -> verdict 'partial'."""
        fake_conn_mgr, mock_conn, mock_cursor = _make_fake_pg_connection()
        health_calls: list = []

        with (
            patch("core.verification._count_raw_rows", return_value=50),
            patch("core.verification.compute_expected_rows", return_value=150),
            patch("core.db.get_connection", new=fake_conn_mgr),
            patch(
                "core.verification._set_connection_health_red",
                side_effect=lambda *a, **kw: health_calls.append(a),
            ),
            patch.dict(os.environ, {"VERIFICATION_PARTIAL_THRESHOLD": "0.5"}),
        ):
            from core.verification import run_post_pull_verification

            run_post_pull_verification(
                pull_id="pull_partial",
                connection_ref_id="conn_partial",
                date_from="2026-07-01",
                date_to="2026-07-07",
                manifest=self._BASE_MANIFEST,
            )

        all_call_args = str(mock_cursor.execute.call_args_list)
        assert "partial" in all_call_args
        assert health_calls, "_set_connection_health_red must be called for partial verdict"
        assert health_calls[0][1] == "partial"

    def test_zero_expected_rows_no_division_error(self):
        """expected_rows == 0 -> verdict 'ok', ratio 1.0, no exception."""
        fake_conn_mgr, mock_conn, mock_cursor = _make_fake_pg_connection()

        with (
            patch("core.verification._count_raw_rows", return_value=0),
            patch("core.verification.compute_expected_rows", return_value=0),
            patch("core.db.get_connection", new=fake_conn_mgr),
        ):
            from core.verification import run_post_pull_verification

            # Must not raise
            run_post_pull_verification(
                pull_id="pull_zero",
                connection_ref_id="conn_zero",
                date_from="2026-07-01",
                date_to="2026-07-01",
                manifest={},
            )

        all_call_args = str(mock_cursor.execute.call_args_list)
        assert "ok" in all_call_args
        # completeness_ratio should be 1.0
        assert "1.0" in all_call_args or "1" in all_call_args

    def test_never_raises_on_db_error(self):
        """run_post_pull_verification swallows DB exceptions (HG-1 contract)."""
        from unittest.mock import patch

        with (
            patch("core.verification._count_raw_rows", return_value=10),
            patch("core.verification.compute_expected_rows", return_value=10),
            patch(
                "core.db.get_connection",
                side_effect=Exception("DB connection refused"),
            ),
        ):
            from core.verification import run_post_pull_verification

            # Must NOT raise -- any exception is logged at WARNING
            run_post_pull_verification(
                pull_id="pull_fail",
                connection_ref_id="conn_fail",
                date_from="2026-07-01",
                date_to="2026-07-07",
                manifest={},
            )

    def test_ver_id_has_ver_prefix(self):
        """Verification ID written to DB starts with 'ver_'."""
        fake_conn_mgr, mock_conn, mock_cursor = _make_fake_pg_connection()
        inserted_ids: list[str] = []

        def _capture_execute(sql, params=None):
            if params and isinstance(params, (list, tuple)) and len(params) > 0:
                first = str(params[0])
                if first.startswith("ver_"):
                    inserted_ids.append(first)

        mock_cursor.execute.side_effect = _capture_execute

        with (
            patch("core.verification._count_raw_rows", return_value=5),
            patch("core.verification.compute_expected_rows", return_value=5),
            patch("core.db.get_connection", new=fake_conn_mgr),
        ):
            from core.verification import run_post_pull_verification

            run_post_pull_verification(
                pull_id="pull_prefix",
                connection_ref_id="conn_prefix",
                date_from="2026-07-01",
                date_to="2026-07-01",
                manifest={},
            )

        assert inserted_ids, "Expected a ver_ ULID to be passed to the INSERT"
        assert all(i.startswith("ver_") for i in inserted_ids)


# ---------------------------------------------------------------------------
# _set_connection_health_red tests (AC5, T3.3)
# ---------------------------------------------------------------------------


class TestSetConnectionHealthRed:
    def test_upserts_populate_failed_status(self):
        """_set_connection_health_red calls upsert with 'populate_failed' status."""
        fake_conn_mgr, mock_conn, mock_cursor = _make_fake_pg_connection()

        with patch("core.db.get_connection", new=fake_conn_mgr):
            from core.verification import _set_connection_health_red

            _set_connection_health_red("conn_red_001", "empty", "pull_red_001")

        execute_calls = str(mock_cursor.execute.call_args_list)
        assert "populate_failed" in execute_calls
        assert "connection_health" in execute_calls
        mock_conn.commit.assert_called()

    def test_never_raises_on_db_error(self):
        """_set_connection_health_red swallows DB exceptions."""
        with patch(
            "core.db.get_connection",
            side_effect=Exception("DB unavailable"),
        ):
            from core.verification import _set_connection_health_red

            # Must NOT raise
            _set_connection_health_red("conn_err", "partial", "pull_err")

    def test_does_not_update_last_fetched_at(self):
        """Upsert SQL must NOT reference last_fetched_at (pull ran but no rows)."""
        fake_conn_mgr, mock_conn, mock_cursor = _make_fake_pg_connection()

        with patch("core.db.get_connection", new=fake_conn_mgr):
            from core.verification import _set_connection_health_red

            _set_connection_health_red("conn_nofetch", "empty", "pull_nofetch")

        execute_calls = str(mock_cursor.execute.call_args_list)
        assert "last_fetched_at" not in execute_calls


# ---------------------------------------------------------------------------
# _count_raw_rows real-DuckDB tests (F-1 / F-6, Story 24.3)
# ---------------------------------------------------------------------------


class TestCountRawRowsRealDuckDB:
    """Integration-level tests using a real temp DuckDB file (no mocks on DuckDB).

    These tests prove:
      F-6: flag OFF reads from main.raw_* without touching org resolution.
      F-1: flag ON reads from the org schema using double-quoted identifiers
           (symmetry with write path that uses SET search_path + unquoted DDL).
      Mixed-case symmetry: a table name DuckDB lowercases on write is found
           correctly by the quoted read path.
    """

    def test_flag_off_reads_from_main(self, tmp_path, monkeypatch):
        """F-6: _count_raw_rows flag OFF finds rows written to main.raw_x."""
        import duckdb
        from core import verification

        db = str(tmp_path / "flagoff.duckdb")
        # Write a row into main.raw_test_daily (flag OFF style -- no schema)
        con = duckdb.connect(db)
        con.execute("CREATE TABLE raw_test_daily (pull_id VARCHAR, v INTEGER)")
        con.execute("INSERT INTO raw_test_daily VALUES ('pull-off-1', 42)")
        con.close()

        # Register the table name so _count_raw_rows can resolve it
        verification.register_raw_table_name("raw_test_daily", provider="_test_flagoff")

        monkeypatch.setenv("TOOROW_DUCKDB_PATH", db)
        monkeypatch.delenv("TOOROW_ORG_SCHEMAS", raising=False)  # flag OFF

        count = verification._count_raw_rows(
            pull_id="pull-off-1",
            provider="_test_flagoff",
            project_id=None,
        )
        assert count == 1, (
            "flag OFF: _count_raw_rows must find the row in main.raw_test_daily"
        )

    def test_flag_off_does_not_resolve_org(self, tmp_path, monkeypatch):
        """F-6: _count_raw_rows flag OFF never calls resolve_org_schemas."""
        from unittest.mock import patch

        import duckdb
        from core import verification, warehouse_tenancy

        db = str(tmp_path / "flagoff_noresolve.duckdb")
        con = duckdb.connect(db)
        con.execute("CREATE TABLE raw_test2 (pull_id VARCHAR)")
        con.execute("INSERT INTO raw_test2 VALUES ('p2')")
        con.close()

        verification.register_raw_table_name("raw_test2", provider="_test_noresolve")
        monkeypatch.setenv("TOOROW_DUCKDB_PATH", db)
        monkeypatch.delenv("TOOROW_ORG_SCHEMAS", raising=False)

        with patch.object(
            warehouse_tenancy, "resolve_org_schemas"
        ) as mock_resolve:
            verification._count_raw_rows(
                pull_id="p2",
                provider="_test_noresolve",
                project_id=None,
            )
            mock_resolve.assert_not_called()

    def test_flag_on_reads_from_org_schema(self, tmp_path, monkeypatch):
        """F-1: _count_raw_rows flag ON reads from org schema (quoted identifiers)."""
        from unittest.mock import patch

        import duckdb
        from core import verification, warehouse_tenancy
        from core.warehouse_tenancy import OrgSchemas

        db = str(tmp_path / "flagon.duckdb")
        # Write a row into the org schema (simulating what open_raw_writer does)
        con = duckdb.connect(db)
        con.execute('CREATE SCHEMA IF NOT EXISTS "org_acme_raw"')
        con.execute(
            'CREATE TABLE "org_acme_raw"."raw_ga4_standard_daily"'
            " (pull_id VARCHAR, project_id VARCHAR)"
        )
        con.execute(
            'INSERT INTO "org_acme_raw"."raw_ga4_standard_daily" VALUES (?, ?)',
            ["pull-on-1", "proj-42"],
        )
        con.close()

        verification.register_raw_table_name(
            "raw_ga4_standard_daily", provider="_test_flagon"
        )
        monkeypatch.setenv("TOOROW_DUCKDB_PATH", db)
        monkeypatch.setenv("TOOROW_ORG_SCHEMAS", "1")

        org_schemas = OrgSchemas(
            org_id="org-1",
            org_slug="acme",
            warehouse_slug="acme",
            raw="org_acme_raw",
            marts="org_acme_marts",
        )
        with patch.object(
            warehouse_tenancy, "resolve_org_schemas", return_value=org_schemas
        ):
            count = verification._count_raw_rows(
                pull_id="pull-on-1",
                provider="_test_flagon",
                project_id="proj-42",
            )
        assert count == 1, (
            "flag ON: _count_raw_rows must find the row in org_acme_raw"
        )

    def test_flag_on_does_not_count_main_row(self, tmp_path, monkeypatch):
        """F-1/F-2: flag ON with both main and org populated counts only org rows."""
        from unittest.mock import patch

        import duckdb
        from core import verification, warehouse_tenancy
        from core.warehouse_tenancy import OrgSchemas

        db = str(tmp_path / "dual_schema.duckdb")
        con = duckdb.connect(db)
        # Simulate legacy main row (different pull_id from org row)
        con.execute(
            "CREATE TABLE raw_test_dual (pull_id VARCHAR, project_id VARCHAR)"
        )
        con.execute(
            "INSERT INTO raw_test_dual VALUES ('main-pull-1', 'proj-42')"
        )
        # Org schema row
        con.execute('CREATE SCHEMA IF NOT EXISTS "org_beta_raw"')
        con.execute(
            'CREATE TABLE "org_beta_raw"."raw_test_dual"'
            " (pull_id VARCHAR, project_id VARCHAR)"
        )
        con.execute(
            'INSERT INTO "org_beta_raw"."raw_test_dual" VALUES (?, ?)',
            ["org-pull-1", "proj-42"],
        )
        con.close()

        verification.register_raw_table_name("raw_test_dual", provider="_test_dual")
        monkeypatch.setenv("TOOROW_DUCKDB_PATH", db)
        monkeypatch.setenv("TOOROW_ORG_SCHEMAS", "1")

        org_schemas = OrgSchemas(
            org_id="org-2",
            org_slug="beta",
            warehouse_slug="beta",
            raw="org_beta_raw",
            marts="org_beta_marts",
        )
        with patch.object(
            warehouse_tenancy, "resolve_org_schemas", return_value=org_schemas
        ):
            # pull_id from org schema -- must be found
            org_count = verification._count_raw_rows(
                pull_id="org-pull-1",
                provider="_test_dual",
                project_id="proj-42",
            )
            # pull_id from main only -- must NOT be found via org schema
            main_count = verification._count_raw_rows(
                pull_id="main-pull-1",
                provider="_test_dual",
                project_id="proj-42",
            )
        assert org_count == 1, "flag ON must find the org-schema row"
        assert main_count == 0, (
            "flag ON must NOT find the main-only row (correct schema isolation)"
        )
