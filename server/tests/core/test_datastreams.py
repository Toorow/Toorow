"""Unit tests for server/core/datastreams.py (Story 8.2, AC10).

Tests CRUD validation and business logic with a mock DB connection.
Does NOT verify schema constraints (that is the job of the live-Postgres
integration tests in test_datastreams_constraints.py).

AD-5: every test that calls a CRUD function also verifies project_id scoping
returns the expected None / empty list for a different project_id.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conn(rows=None, rowcount=1):
    """Build a minimal mock psycopg connection + cursor."""
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = rows[0] if rows else None
    cur.fetchall.return_value = rows if rows else []
    cur.rowcount = rowcount
    cur.description = [
        ("id",),
        ("project_id",),
        ("name",),
        ("module_name",),
        ("connection_ref_id",),
        ("report_profile_id",),
        ("enabled",),
        ("schedule_mode",),
        ("refetch_days",),
        ("date_window_days",),
        ("config",),
        ("created_by",),
        ("created_at",),
        ("updated_at",),
    ]
    return conn, cur


_NOW = datetime(2026, 7, 12, 10, 0, 0, tzinfo=timezone.utc)


def _ds_row(
    ds_id="ds_001",
    project_id="proj_a",
    name="GA Standard",
    module_name="google-analytics",
    conn_ref_id="conn_x",
    profile_id="standard_daily",
    enabled=True,
    schedule_mode="nightly",
    refetch_days=3,
    date_window_days=30,
    config=None,
    created_by="system",
):
    return (
        ds_id,
        project_id,
        name,
        module_name,
        conn_ref_id,
        profile_id,
        enabled,
        schedule_mode,
        refetch_days,
        date_window_days,
        config,
        created_by,
        _NOW,
        _NOW,
    )


# ---------------------------------------------------------------------------
# list_datastreams
# ---------------------------------------------------------------------------


class TestListDatastreams:
    def test_returns_list_for_project(self):
        from core.datastreams import list_datastreams

        rows = [_ds_row()]
        conn, cur = _make_conn(rows)
        result = list_datastreams("proj_a", conn)
        assert len(result) == 1
        assert result[0]["id"] == "ds_001"
        assert result[0]["project_id"] == "proj_a"

    def test_passes_project_id_filter(self):
        """AD-5: list query must include project_id in WHERE clause."""
        from core.datastreams import list_datastreams

        conn, cur = _make_conn([])
        list_datastreams("proj_a", conn)
        sql_called = cur.execute.call_args[0][0]
        assert "project_id" in sql_called
        params = cur.execute.call_args[0][1]
        assert "proj_a" in params

    def test_empty_project_returns_empty_list(self):
        from core.datastreams import list_datastreams

        conn, cur = _make_conn([])
        result = list_datastreams("proj_empty", conn)
        assert result == []


# ---------------------------------------------------------------------------
# get_datastream
# ---------------------------------------------------------------------------


class TestGetDatastream:
    def test_legacy_row_has_normalized_versioned_read_fields(self):
        from core.datastreams import get_datastream

        conn, _ = _make_conn([_ds_row()])
        result = get_datastream("ds_001", "proj_a", conn)
        assert result["source_kind"] == "connector_pull"
        assert result["writer_kind"] == "toorow"
        assert result["destination_policy"] == "managed_raw"
        assert result["cadence_mode"] == "daily"
        assert result["current_plan_version_id"] is None
        assert result["versioned"] is False

    def test_returns_datastream_for_matching_project(self):
        from core.datastreams import get_datastream

        conn, cur = _make_conn([_ds_row()])
        result = get_datastream("ds_001", "proj_a", conn)
        assert result is not None
        assert result["id"] == "ds_001"

    def test_returns_none_when_not_found(self):
        from core.datastreams import get_datastream

        conn, cur = _make_conn([])
        cur.fetchone.return_value = None
        result = get_datastream("ds_missing", "proj_a", conn)
        assert result is None

    def test_passes_both_id_and_project_id(self):
        """AD-5: get query must scope by (id, project_id)."""
        from core.datastreams import get_datastream

        conn, cur = _make_conn([])
        cur.fetchone.return_value = None
        get_datastream("ds_001", "proj_a", conn)
        sql = cur.execute.call_args[0][0]
        params = cur.execute.call_args[0][1]
        assert "id" in sql.lower() or "%s" in sql
        assert "proj_a" in params
        assert "ds_001" in params


# ---------------------------------------------------------------------------
# create_datastream
# ---------------------------------------------------------------------------


class TestCreateDatastream:
    def test_versioned_external_shell_defaults_disabled_without_module(self):
        from core.datastreams import create_datastream

        row = _ds_row(module_name=None, enabled=False)
        conn, cur = _make_conn([row])
        cur.fetchone.return_value = row
        create_datastream(
            {"name": "External BQ", "source_kind": "external_bq"},
            "proj_a",
            "member-1",
            conn,
        )
        sql, params = cur.execute.call_args.args
        assert "source_kind" in sql
        assert "external_bq" in params
        assert False in params
        assert None in params

    def test_creates_successfully(self):
        from core.datastreams import create_datastream

        row = _ds_row(ds_id="ds_new", name="New Stream")
        conn, cur = _make_conn([row])
        cur.fetchone.return_value = row
        result = create_datastream(
            {"name": "New Stream", "module_name": "google-analytics"},
            "proj_a",
            "user1",
            conn,
        )
        assert result["id"] == "ds_new"
        assert result["name"] == "New Stream"

    def test_raises_on_missing_name(self):
        from core.datastreams import create_datastream

        conn, _ = _make_conn([])
        with pytest.raises(ValueError, match="name"):
            create_datastream({"module_name": "ga"}, "proj_a", "user1", conn)

    def test_raises_on_missing_module_name(self):
        from core.datastreams import create_datastream

        conn, _ = _make_conn([])
        with pytest.raises(ValueError, match="module_name"):
            create_datastream({"name": "test"}, "proj_a", "user1", conn)

    def test_raises_on_invalid_schedule_mode(self):
        from core.datastreams import create_datastream

        conn, cur = _make_conn([_ds_row()])
        cur.fetchone.return_value = _ds_row()
        with pytest.raises(ValueError, match="schedule_mode"):
            create_datastream(
                {"name": "x", "module_name": "ga", "schedule_mode": "weekly"},
                "proj_a",
                "user1",
                conn,
            )

    def test_default_schedule_mode_is_nightly(self):
        from core.datastreams import create_datastream

        row = _ds_row(ds_id="ds_new")
        conn, cur = _make_conn([row])
        cur.fetchone.return_value = row
        create_datastream(
            {"name": "Stream", "module_name": "ga"},
            "proj_a",
            "user1",
            conn,
        )
        sql, params = cur.execute.call_args[0]
        # schedule_mode 'nightly' should appear in params
        assert "nightly" in params

    def test_mints_ds_prefix_id(self):
        from core.datastreams import create_datastream

        row = _ds_row(ds_id="ds_MINTED")
        conn, cur = _make_conn([row])
        cur.fetchone.return_value = row
        create_datastream(
            {"name": "S", "module_name": "ga"},
            "proj_a",
            "user1",
            conn,
        )
        # Check that the ID param passed to INSERT starts with 'ds_'
        sql, params = cur.execute.call_args[0]
        assert params[0].startswith("ds_")


# ---------------------------------------------------------------------------
# update_datastream
# ---------------------------------------------------------------------------


class TestUpdateDatastream:
    def _setup(self, conn, cur, existing_row, updated_row=None):
        """Set up cursor to return existing on first fetchone, updated on second."""
        if updated_row is None:
            updated_row = existing_row
        cur.fetchone.side_effect = [existing_row, updated_row]

    def test_returns_none_for_wrong_project(self):
        from core.datastreams import update_datastream

        conn, cur = _make_conn([])
        cur.fetchone.return_value = None
        result = update_datastream("ds_001", "proj_other", {"name": "New"}, conn)
        assert result is None

    def test_returns_existing_when_no_patchable_fields(self):
        from core.datastreams import update_datastream

        row = _ds_row()
        conn, cur = _make_conn([row])
        cur.fetchone.return_value = row
        result = update_datastream("ds_001", "proj_a", {"unknown_field": "x"}, conn)
        # No UPDATE issued; returns the existing row.
        assert result["id"] == "ds_001"

    def test_raises_on_invalid_schedule_mode(self):
        from core.datastreams import update_datastream

        row = _ds_row()
        conn, cur = _make_conn([row])
        cur.fetchone.return_value = row
        with pytest.raises(ValueError, match="schedule_mode"):
            update_datastream("ds_001", "proj_a", {"schedule_mode": "weekly"}, conn)

    def test_update_enabled(self):
        from core.datastreams import update_datastream

        row = _ds_row()
        updated = _ds_row(enabled=False)
        conn, cur = _make_conn([row])
        cur.fetchone.side_effect = [row, updated]
        update_datastream("ds_001", "proj_a", {"enabled": False}, conn)
        # Verify UPDATE was called
        assert cur.execute.call_count >= 2


# ---------------------------------------------------------------------------
# enable_disable_datastream
# ---------------------------------------------------------------------------


class TestEnableDisable:
    def test_enables_datastream(self):
        from core.datastreams import enable_disable_datastream

        row = _ds_row(enabled=False)
        enabled_row = _ds_row(enabled=True)
        conn, cur = _make_conn([row])
        cur.fetchone.side_effect = [row, enabled_row]
        # Story 34.2: enabling re-checks the trial cap; neutralise that governance
        # read here -- this test is about the enable/disable SQL, not the cap.
        with patch("core.trial_enforcement.check_datastream_limit"):
            result = enable_disable_datastream("ds_001", "proj_a", True, conn)
        assert result is not None

    def test_returns_none_when_not_found(self):
        from core.datastreams import enable_disable_datastream

        conn, cur = _make_conn([])
        cur.fetchone.return_value = None
        result = enable_disable_datastream("ds_missing", "proj_a", True, conn)
        assert result is None


# ---------------------------------------------------------------------------
# delete_datastream
# ---------------------------------------------------------------------------


class TestDeleteDatastream:
    def test_returns_false_when_not_found(self):
        from core.datastreams import delete_datastream

        conn, cur = _make_conn([])
        cur.fetchone.return_value = None
        result = delete_datastream("ds_missing", "proj_a", conn)
        assert result is False

    def test_soft_archives_when_pull_jobs_exist(self):
        from core.datastreams import delete_datastream

        row = _ds_row()
        conn, cur = _make_conn([row])
        # First fetchone = get_datastream, second = ref_count > 0
        cur.fetchone.side_effect = [row, (2,)]  # 2 pull_jobs reference it
        result = delete_datastream("ds_001", "proj_a", conn)
        assert result is True
        # UPDATE (not DELETE) should be called for soft-archive
        calls = cur.execute.call_args_list
        update_calls = [c for c in calls if "UPDATE" in str(c)]
        assert len(update_calls) >= 1

    def test_soft_archives_when_plan_version_exists(self):
        from core.datastreams import delete_datastream

        row = _ds_row()
        conn, cur = _make_conn([row])
        cur.fetchone.side_effect = [row, (0,), ("dsp_01",)]
        result = delete_datastream("ds_001", "proj_a", conn)
        assert result is True
        sql_calls = [str(call.args[0]) for call in cur.execute.call_args_list]
        assert any("archived_at" in sql for sql in sql_calls)
        assert not any("DELETE FROM app.datastreams" in sql for sql in sql_calls)

    def test_hard_deletes_when_no_pull_jobs(self):
        from core.datastreams import delete_datastream

        row = _ds_row()
        conn, cur = _make_conn([row])
        # 4 fetchone: get_datastream row, pull_jobs count, plan-version pointer,
        # then to_regclass('app.resource_grants') -- the 21.5-follow-up sweep of
        # dangling per-flux grants (d3ba092) added that 4th read; exercise the
        # table-present path so the grants DELETE is covered too.
        cur.fetchone.side_effect = [row, (0,), (None,), ("app.resource_grants",)]
        result = delete_datastream("ds_001", "proj_a", conn)
        assert result is True
        calls = cur.execute.call_args_list
        delete_calls = [c for c in calls if "DELETE" in str(c)]
        assert len(delete_calls) >= 1
        assert any("resource_grants" in str(c) for c in delete_calls), (
            "the dangling flux resource_grants sweep must run on hard delete"
        )


# ---------------------------------------------------------------------------
# get_datastream_summaries
# ---------------------------------------------------------------------------


class TestGetDatastreamSummaries:
    def test_returns_summaries_with_last_pull(self):
        from core.datastreams import get_datastream_summaries

        summary_cols = [
            ("id",),
            ("project_id",),
            ("name",),
            ("module_name",),
            ("connection_ref_id",),
            ("enabled",),
            ("schedule_mode",),
            ("refetch_days",),
            ("source_kind",),
            ("current_plan_version_id",),
            ("current_plan_version",),
            ("writer_kind",),
            ("destination_policy",),
            ("executable",),
            ("validation_issues",),
            ("intent_payload",),
            ("next_run_at",),
            ("connection_status",),
            ("last_pull_id",),
            ("last_pull_state",),
            ("last_pull_completed_at",),
        ]
        row = (
            "ds_001",
            "proj_a",
            "GA Stream",
            "google-analytics",
            "conn_x",
            True,
            "nightly",
            3,
            "connector_pull",
            "dsp_01",
            1,
            "toorow",
            "managed_raw",
            True,
            [],
            {"schedule": {"mode": "hourly"}},
            _NOW,
            "active",
            "pull_abc",
            "done",
            _NOW,
        )
        conn, cur = _make_conn([])
        cur.description = summary_cols
        cur.fetchall.return_value = [row]
        result = get_datastream_summaries("proj_a", conn)
        assert len(result) == 1
        assert result[0]["last_pull_completed_at"] == _NOW.isoformat()
        assert result[0]["versioned"] is True
        assert result[0]["current_plan_version"] == 1
        assert result[0]["cadence_mode"] == "hourly"
        assert result[0]["next_run_at"] == _NOW.isoformat()
        assert "ds.archived_at IS NULL" in str(cur.execute.call_args.args[0])


# ---------------------------------------------------------------------------
# backfill_datastreams
# ---------------------------------------------------------------------------


class TestBackfillDatastreams:
    def _make_db_mock(self):
        """Build a mock connection context + cursor that returns empty fetchall."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = cur
        cur.fetchall.return_value = []
        return mock_conn, cur

    def test_returns_summary_dict(self):
        """backfill_datastreams returns a dict with created/skipped/mappings_created/errors."""
        from core.datastreams import backfill_datastreams

        mock_conn, cur = self._make_db_mock()

        # backfill imports get_connection from core.db at call time
        with patch("core.db.get_connection", return_value=mock_conn):
            # Patch core.main so _loaded_modules is accessible
            with patch.dict("sys.modules", {"core.main": MagicMock(_loaded_modules=[])}):
                result = backfill_datastreams()

        assert isinstance(result, dict)
        assert "created" in result
        assert "skipped" in result
        assert "mappings_created" in result
        assert "errors" in result

    def test_skips_when_no_connections(self):
        from core.datastreams import backfill_datastreams

        mock_conn, cur = self._make_db_mock()

        with patch("core.db.get_connection", return_value=mock_conn):
            with patch.dict("sys.modules", {"core.main": MagicMock(_loaded_modules=[])}):
                result = backfill_datastreams()

        assert result["created"] == 0
        assert result["errors"] == []

    def test_two_connections_same_provider_create_two_datastreams(self):
        """Fix [MEDIUM #9]: two connections for the same provider create two DISTINCT datastreams.

        Before the fix the names were identical ('<provider> - <profile>') and ON CONFLICT
        DO NOTHING silently seeded connection B's mappings into connection A's datastream.
        After the fix, the name includes the last 4 chars of connection_ref_id so both
        are distinct.
        """
        from core.datastreams import backfill_datastreams

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        inserted_names: list[str] = []

        def make_cursor():
            cur = MagicMock()
            cur.__enter__ = MagicMock(return_value=cur)
            cur.__exit__ = MagicMock(return_value=False)

            def execute(sql, params=None):
                if params and len(params) >= 3 and "INSERT INTO app.datastreams" in sql:
                    # params[2] is the name
                    inserted_names.append(params[2])
                    cur.fetchone.return_value = (params[0],)  # return the ds_id
                elif "SELECT id, provider, project_id" in sql or (params and len(params) == 0):
                    pass
                else:
                    cur.fetchone.return_value = None
                cur.fetchall.return_value = []

            cur.execute = execute
            return cur

        # First cursor call returns two connection_ref rows (same provider, different ids)
        first_cur = MagicMock()
        first_cur.__enter__ = MagicMock(return_value=first_cur)
        first_cur.__exit__ = MagicMock(return_value=False)
        first_cur.fetchall.return_value = [
            ("conn_AAAA1111", "generic", "proj_1"),
            ("conn_BBBB2222", "generic", "proj_1"),
        ]

        cur_sequence = iter([first_cur])

        def cursor_factory():
            try:
                c = next(cur_sequence)
            except StopIteration:
                c = make_cursor()
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=c)
            cm.__exit__ = MagicMock(return_value=False)
            return cm

        mock_conn.cursor = cursor_factory

        fake_module = MagicMock()
        fake_module.name = "generic"
        fake_module.manifest = {
            "report_profiles": [{"id": "daily", "display_name": "Daily"}],
            "canonical_metric_mapping": {},
            "canonical_dimension_mapping": {},
        }

        with patch("core.db.get_connection", return_value=mock_conn):
            with patch.dict("sys.modules", {"core.main": MagicMock(_loaded_modules=[fake_module])}):
                backfill_datastreams()

        # Both names must be distinct (include conn suffix)
        assert len(set(inserted_names)) == len(inserted_names), (
            f"Duplicate names generated: {inserted_names}"
        )
        # Each name must include the last-4 suffix
        for name in inserted_names:
            assert "[" in name and "]" in name, f"Name {name!r} missing conn suffix bracket"
