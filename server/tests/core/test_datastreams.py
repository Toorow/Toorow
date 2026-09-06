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


def _datastream_insert(cur):
    """L'appel qui INSERT le Datastream, pas le dernier appel du curseur.

    `create_datastream` ecrit desormais DEUX lignes : le Datastream, puis son
    lien dans `app.project_flux` -- sans lequel toutes ses routes de detail
    repondaient 404 sur un objet pourtant present. Lire `call_args` prenait donc
    l'insertion du lien.
    """
    for call in reversed(cur.execute.call_args_list):
        sql = call[0][0]
        if "INSERT INTO app.datastreams" in sql:
            return sql, (call[0][1] if len(call[0]) > 1 else ())
    raise AssertionError("aucun INSERT INTO app.datastreams parmi les appels")


def _project_flux_insert(cur):
    """L'insertion du lien projet<->flux, ou None si elle n'a pas eu lieu."""
    for call in cur.execute.call_args_list:
        if "INSERT INTO app.project_flux" in call[0][0]:
            return call[0][1] if len(call[0]) > 1 else ()
    return None


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
        sql, params = _datastream_insert(cur)
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
                {"name": "x", "module_name": "ga", "schedule_mode": "invalid_mode"},
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
        sql, params = _datastream_insert(cur)
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
        sql, params = _datastream_insert(cur)
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
        with pytest.raises(ValueError, match="reviewed Datastream operation"):
            update_datastream("ds_001", "proj_a", {"schedule_mode": "invalid_mode"}, conn)

    def test_the_reference_role_can_be_declared_after_creation(self):
        """Without this, no person and no model can build the join.

        Measured on 2026-08-12: a project whose facts carried 519 video ids had
        exactly one stream able to name them, and `data_role` was writable only at
        creation -- `flows.upsert_flow` passed it here and it was dropped in
        silence.
        """
        from core.datastreams import update_datastream

        row = _ds_row()
        conn, cur = _make_conn([row])
        cur.fetchone.side_effect = [row, row]
        update_datastream(
            "ds_001", "proj_a", {"data_role": "Reference & targets"}, conn
        )
        sql = " ".join(str(call[0][0]) for call in cur.execute.call_args_list)
        assert "data_role" in sql

    def test_a_role_outside_the_seven_names_the_seven(self):
        from core.datastreams import DATA_ROLES, update_datastream

        row = _ds_row()
        conn, cur = _make_conn([row])
        cur.fetchone.return_value = row
        with pytest.raises(ValueError, match="data_role invalide"):
            update_datastream("ds_001", "proj_a", {"data_role": "Catalogue"}, conn)
        # The message carries the allowed set, so the next attempt is right.
        try:
            update_datastream("ds_001", "proj_a", {"data_role": "Catalogue"}, conn)
        except ValueError as exc:
            for role in DATA_ROLES:
                assert role in str(exc)

    def test_update_enabled_requires_governed_operation(self):
        from core.datastreams import update_datastream

        conn, _ = _make_conn([_ds_row()])
        with pytest.raises(ValueError, match="reviewed Datastream operation"):
            update_datastream("ds_001", "proj_a", {"enabled": False}, conn)


# ---------------------------------------------------------------------------
# enable_disable_datastream
# ---------------------------------------------------------------------------


class TestEnableDisable:
    def test_direct_enable_requires_governed_operation(self):
        from core.datastreams import enable_disable_datastream

        conn, _ = _make_conn([_ds_row(enabled=False)])
        with pytest.raises(ValueError, match="reviewed Datastream operation"):
            enable_disable_datastream("ds_001", "proj_a", True, conn)

# ---------------------------------------------------------------------------
# delete_datastream
# ---------------------------------------------------------------------------


class TestDeleteDatastream:
    def test_returns_false_when_not_found(self):
        from core.datastreams import delete_datastream

        conn, cur = _make_conn([])
        cur.fetchone.return_value = None
        result = delete_datastream("ds_missing", "proj_a", conn)
        assert result is None

    def test_soft_archives_when_pull_jobs_exist(self):
        """A pull job lets go instead of refusing, so it has to be asked for.

        `pull_jobs` references a Datastream ON DELETE SET NULL: a hard delete
        would succeed and leave the pull history orphaned. It is counted last,
        after the tables whose keys restrict.
        """
        from core.datastreams import delete_datastream

        row = _ds_row()
        conn, cur = _make_conn([row])
        cur.fetchall.return_value = []  # nothing in the catalogue holds it
        cur.fetchone.side_effect = [row, (2,)]  # 2 pull_jobs reference it
        result = delete_datastream("ds_001", "proj_a", conn)
        assert result == "archived"
        # UPDATE (not DELETE) should be called for soft-archive
        calls = cur.execute.call_args_list
        update_calls = [c for c in calls if "UPDATE" in str(c)]
        assert len(update_calls) >= 1

    def test_soft_archives_when_a_plan_version_ROW_exists(self):
        """THE ROW, not the pointer -- which is what made a Fleet ungrowable-back.

        A Datastream that never published has a plan VERSION and a NULL
        `current_plan_version_id`. Reading the pointer answered "no history", the
        hard delete ran, and `fk_datastream_plan_datastream_scope` refused it with
        a 500. Measured in production 2026-08-12: 27 of 28 deletions failed, so
        every Datastream born in the setup wizard was undeletable.
        """
        from core.datastreams import delete_datastream

        row = _ds_row()
        conn, cur = _make_conn([row])
        cur.fetchall.return_value = [("datastream_plan_versions", "datastream_id")]
        cur.fetchone.side_effect = [row, (1,), (0,)]
        result = delete_datastream("ds_001", "proj_a", conn)
        assert result == "archived"
        sql_calls = [str(call.args[0]) for call in cur.execute.call_args_list]
        assert any("archived_at" in sql for sql in sql_calls)
        assert not any("DELETE FROM app.datastreams" in sql for sql in sql_calls)

    def test_the_tables_that_hold_it_are_read_from_the_catalogue(self):
        """Nothing here names a table, so a migration that adds one is covered.

        The previous version kept the list by hand and named three things while
        33 tables carry a restricting key -- and nothing made that wrong out loud.
        """
        from core.datastreams import _restricting_references

        conn, cur = _make_conn([])
        cur.fetchall.return_value = [
            ("datastream_executions", "datastream_id"),
            ("datastream_setup_drafts", "materialized_datastream_id"),
        ]
        cur.fetchone.side_effect = [(3,), (0,), (0,)]
        held = _restricting_references(conn, "ds_001")
        assert held == [("datastream_executions", 3)]
        catalogue_query = str(cur.execute.call_args_list[0].args[0])
        assert "pg_constraint" in catalogue_query
        assert "confdeltype" in catalogue_query
        counted = [str(call.args[0]) for call in cur.execute.call_args_list[1:]]
        assert "app.datastream_setup_drafts" in counted[1]
        assert "materialized_datastream_id" in counted[1], (
            "a table that names the column differently must still be counted"
        )

    def test_soft_archives_when_an_inbound_receipt_exists(self):
        """Un recu d'entree est de l'histoire immuable, comme un pull job.

        AI-200 : la suppression dure butait sur la cle etrangere de
        `app.inbound_receipts`, protegee par une garde append-only, et rendait un
        500 `db_error`. Le flux devenait indestructible -- et continuait a
        consommer le quota d'essai de l'organisation. Archiver preserve le recu,
        ce que la garde protege, et rend le flux inactif.
        """
        from core.datastreams import delete_datastream

        row = _ds_row()
        conn, cur = _make_conn([row])
        # get_datastream, 1 recu inbound, 0 pull job.
        cur.fetchall.return_value = [("inbound_receipts", "datastream_id")]
        cur.fetchone.side_effect = [row, (1,), (0,)]
        result = delete_datastream("ds_001", "proj_a", conn)
        assert result == "archived", (
            "the caller is told it ARCHIVED; inferring the disposition from the "
            "pull-job count alone is what made the API answer `deleted` here"
        )
        sql_calls = [str(call.args[0]) for call in cur.execute.call_args_list]
        assert any("archived_at" in sql for sql in sql_calls)
        assert not any("DELETE FROM app.datastreams" in sql for sql in sql_calls)

    def test_hard_deletes_when_no_pull_jobs(self):
        from core.datastreams import delete_datastream

        row = _ds_row()
        conn, cur = _make_conn([row])
        # get_datastream row, then 0 pull jobs (nothing in the catalogue holds
        # it), then to_regclass('app.resource_grants') -- the 21.5-follow-up sweep
        # of dangling per-flux grants (d3ba092); exercise the table-present path
        # so the grants DELETE is covered too.
        cur.fetchall.return_value = []
        cur.fetchone.side_effect = [row, (0,), ("app.resource_grants",)]
        result = delete_datastream("ds_001", "proj_a", conn)
        assert result == "deleted"
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
                    # params[3] is the name
                    # params[2] n'est le nom QUE dans l'INSERT du Datastream ;
                    # l'insertion du lien project_flux passe par ici aussi.
                    if "INSERT INTO app.datastreams" in sql:
                        inserted_names.append(params[3])
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


# ---------------------------------------------------------------------------
# data_role -- the write path that migration 093 never had
# ---------------------------------------------------------------------------


class TestDataRole:
    """What a Datastream is FOR, declared instead of guessed.

    Migration 093 added app.datastreams.data_role with a 7-value CHECK and
    backfilled it ONCE by running a regular expression over module_name. After
    that, `data_role` appeared in exactly one SELECT and in no INSERT or UPDATE
    anywhere in the server: the column could never be written again, so every
    Datastream kept whatever the regex guessed and the person who actually knows
    the answer had no way to give it (found 2026-07-27).
    """

    def _conn(self):
        from unittest.mock import MagicMock

        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        cur.description = [("id",), ("project_id",), ("name",), ("data_role",)]
        cur.fetchone.return_value = ("ds_1", "proj_1", "GSC pages", "Performance")
        conn = MagicMock()
        conn.cursor.return_value = cur
        return conn, cur

    def test_declared_role_reaches_the_insert(self):
        from core.datastreams import create_datastream

        conn, cur = self._conn()
        create_datastream(
            {"name": "GSC pages", "module_name": "gsc", "data_role": "Performance"},
            "proj_1",
            "person_1",
            conn,
        )

        sql, params = _datastream_insert(cur)
        assert "data_role" in sql, "the insert must carry the column"
        assert "Performance" in params, "the declared role must be bound, not dropped"

    def test_an_invalid_role_is_refused_before_the_database(self):
        """A 422 naming the allowed set beats a CheckViolation surfacing as a 500."""
        import pytest
        from core.datastreams import create_datastream

        conn, _ = self._conn()
        with pytest.raises(ValueError, match="data_role"):
            create_datastream(
                {"name": "x", "module_name": "gsc", "data_role": "Whatever"},
                "proj_1",
                "person_1",
                conn,
            )

    def test_role_stays_optional(self):
        """Omitting it must not break creation -- most existing callers do."""
        from core.datastreams import create_datastream

        conn, cur = self._conn()
        create_datastream({"name": "x", "module_name": "gsc"}, "proj_1", "person_1", conn)
        _sql, params = _datastream_insert(cur)
        assert params[-1] is None

    def test_the_allowed_set_matches_the_migration(self):
        """DATA_ROLES mirrors migration 093's CHECK; drift here is a 500 in production.

        BOTH DIRECTIONS. Inclusion alone only catches a role added here and missing
        from the database; it never catches a role the database accepts and this
        module refuses, which is a 422 on a perfectly legal value.
        """
        import re

        from core.datastreams import DATA_ROLES

        from tests.conftest import REPO_ROOT

        sql = (REPO_ROOT / "infra/nango/migrations/093_datastream_data_role.sql").read_text(
            encoding="utf-8"
        )
        checked = re.search(r"data_role IN \(([^)]*)\)", sql, re.S)
        assert checked, "the CHECK of migration 093 could not be read"
        in_database = set(re.findall(r"'([^']+)'", checked.group(1)))
        assert in_database == set(DATA_ROLES)

    def test_the_wizard_select_offers_exactly_these_seven(self):
        """The screen is the THIRD copy of this vocabulary, and it had no guard.

        `app.datastreams.data_role` was mirrored in Python and checked against the
        migration, but the `<select>` an operator actually uses was checked against
        nothing -- which is how it came to offer `Finance`, `Reference` and
        `Operations`, three tokens that exist in no constraint and in no server
        module. Choosing one was accepted for six wizard sections and refused at
        materialization.

        Equality in both directions, deliberately: inclusion would let an eighth
        role land in the database and never reach the screen, and the screen would
        stay green while refusing a value the product supports.
        """
        import re

        from core.datastreams import DATA_ROLES

        from tests.conftest import REPO_ROOT

        source = (
            REPO_ROOT
            / "ui/admin/src/datastreams/preconfiguration/DatastreamSetupWizard.tsx"
        ).read_text(encoding="utf-8")
        declared = re.search(r"const DATA_ROLES:[^=]*=\s*\[(.*?)\n\];", source, re.S)
        assert declared, "the wizard no longer declares a DATA_ROLES constant"
        on_screen = re.findall(r'\["([^"]+)",\s*"[^"]*"\]', declared.group(1))

        assert on_screen == list(DATA_ROLES)


def test_creation_links_the_datastream_to_its_project():
    """Sans cette ligne, un Datastream neuf est invisible a ses propres ecrans.

    Toutes les routes de detail le resolvent par une jointure sur
    app.project_flux ; la creation ne l'ecrivait pas, et l'Overview, Data,
    Mapping, Runs et /sample repondaient 404 sur un objet bien present dans
    app.datastreams.
    """
    from unittest.mock import MagicMock

    from core.datastreams import create_datastream

    cur = MagicMock()
    cur.fetchone.return_value = ("ds_001", "proj_1", "n", "gsc", None, None, True,
                                "nightly", 3, 30, None, "person_1", None, None)
    cur.description = [("id",), ("project_id",), ("name",), ("module_name",),
                       ("connection_ref_id",), ("report_profile_id",), ("enabled",),
                       ("schedule_mode",), ("refetch_days",), ("date_window_days",),
                       ("config",), ("created_by",), ("created_at",), ("updated_at",)]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    create_datastream({"name": "n", "module_name": "gsc"}, "proj_1", "person_1", conn)

    params = _project_flux_insert(cur)
    assert params is not None, "la creation n'a pas indexe le flux dans son projet"
    assert params[0] == "proj_1"


# ---------------------------------------------------------------------------
# AI-217 -- a weekly row must not be REPORTED as manual.
# ---------------------------------------------------------------------------


class TestTheCadenceAScreenIsTold:
    def test_a_weekly_row_reads_as_weekly(self):
        """It read `manual`, which is the opposite of what the row does.

        `cadence_mode` is derived from `schedule_mode` for every row that carries
        no intent payload, and the derivation knew two cadences: `nightly` and
        `hourly`. Everything else fell through to `manual` -- so a Datastream the
        dispatcher pulls once a week told every screen it never runs at all.
        """
        from core.datastreams import get_datastream

        conn, _ = _make_conn([_ds_row(schedule_mode="weekly")])
        assert get_datastream("ds_001", "proj_a", conn)["cadence_mode"] == "weekly"

    def test_a_manual_row_still_reads_as_manual(self):
        from core.datastreams import get_datastream

        conn, _ = _make_conn([_ds_row(schedule_mode="manual")])
        assert get_datastream("ds_001", "proj_a", conn)["cadence_mode"] == "manual"


class TestTheCadenceVocabularyIsOneTable:
    """AI-217. Two names for one setting, and they must not drift apart.

    The plan intent says `daily`, the stable row says `nightly`, and the
    translation between them existed in four private dictionaries -- three
    forward, one backward. `weekly` was missing from all four, so a legal cadence
    was silently turned into `manual` on the way in and reported as `manual` on
    the way out.
    """

    def test_a_weekly_cadence_maps_to_the_weekly_schedule_mode(self):
        from core.datastreams import schedule_mode_for_cadence

        assert schedule_mode_for_cadence("weekly") == "weekly"

    def test_the_two_names_of_a_daily_cadence_still_meet(self):
        from core.datastreams import cadence_for_schedule_mode, schedule_mode_for_cadence

        assert schedule_mode_for_cadence("daily") == "nightly"
        assert cadence_for_schedule_mode("nightly") == "daily"

    def test_every_legal_cadence_survives_the_round_trip(self):
        """Nothing the CHECK constraint allows may fall through to `manual`.

        The `.get(mode, "manual")` default is what made an unknown cadence look
        like a decision to run on demand. A round trip that loses a value is the
        same defect, one table further on.
        """
        from core.datastreams import cadence_for_schedule_mode, schedule_mode_for_cadence

        for mode in ("nightly", "weekly", "hourly", "manual"):
            assert schedule_mode_for_cadence(cadence_for_schedule_mode(mode)) == mode

    def test_an_unknown_cadence_is_still_read_as_manual(self):
        """Defensive, and deliberately unchanged: an unknown word promises nothing."""
        from core.datastreams import schedule_mode_for_cadence

        assert schedule_mode_for_cadence("fortnightly") == "manual"
