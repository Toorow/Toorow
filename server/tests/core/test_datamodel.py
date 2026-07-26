"""Unit tests for server/core/datamodel.py (Story 8.5, AC9).

Tests CRUD validation and business logic with mock DB connections.
Does NOT verify schema constraints (live-Postgres tests are deferred to orchestrator).

Coverage:
  - list_target_fields: project scoping, kind/usage filters
  - get_target_field: returns None for missing; used_by list; conflict detection
  - create_target_field: snake_case validation, enum checks, immutable after create
  - update_target_field: immutability guard (name/data_type/field_kind), patchable fields
  - upsert_mapping: inserts and updates correctly
  - _detect_conflicts: CURRENCY_CONFLICT and MEASURE_NULL detection
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conn(fetchone_rows=None, fetchall_rows=None):
    """Build a minimal mock psycopg connection + cursor (context-manager compatible)."""
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    # Support multiple sequential fetchone calls via side_effect
    if fetchone_rows is not None:
        cur.fetchone.side_effect = iter(fetchone_rows)
    else:
        cur.fetchone.return_value = None

    cur.fetchall.return_value = fetchall_rows or []
    cur.rowcount = 1
    return conn, cur


_NOW = datetime(2026, 7, 12, 10, 0, 0, tzinfo=timezone.utc)

_TF_COLS = [
    ("name",), ("display_name",), ("data_type",), ("field_kind",),
    ("measure",), ("description",), ("created_by",), ("is_default",), ("created_at",),
]

_TF_WITH_COUNT_COLS = _TF_COLS + [("used_by_count",)]

_MAPPING_COLS = [
    ("datastream_id",), ("source_field",), ("target_field",),
    ("is_key_column",), ("created_at",),
]


def _tf_row(
    name="clicks",
    display_name="Clics",
    data_type="integer",
    field_kind="metric",
    measure="sum",
    description="Nombre de clics",
    created_by="system",
    is_default=True,
    created_at=_NOW,
    used_by_count=2,
):
    row = (name, display_name, data_type, field_kind, measure, description,
           created_by, is_default, created_at)
    return row, row + (used_by_count,)


# ---------------------------------------------------------------------------
# list_target_fields
# ---------------------------------------------------------------------------


class TestListTargetFields:
    def test_returns_all_fields_no_filter(self):
        from core.datamodel import list_target_fields

        _, tf_with_count = _tf_row(used_by_count=3)
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = [tf_with_count]
        cur.description = _TF_WITH_COUNT_COLS

        result = list_target_fields(conn)

        assert len(result) == 1
        assert result[0]["name"] == "clicks"
        assert result[0]["used_by_count"] == 3

    def test_project_id_added_to_params(self):
        """Verify project_id is included as a SQL parameter (AD-5 scoping)."""
        from core.datamodel import list_target_fields

        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []
        cur.description = _TF_WITH_COUNT_COLS

        list_target_fields(conn, project_id="proj_x")

        # Verify project_id is passed as first param
        call_args = cur.execute.call_args
        params = call_args[0][1]  # positional args: (sql, params)
        assert "proj_x" in params

    def test_kind_filter_added_to_params(self):
        from core.datamodel import list_target_fields

        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []
        cur.description = _TF_WITH_COUNT_COLS

        list_target_fields(conn, kind="metric")

        call_args = cur.execute.call_args
        params = call_args[0][1]
        assert "metric" in params

    def test_used_by_count_coerced_to_int(self):
        """COUNT() may return Decimal; must be coerced to int."""
        from decimal import Decimal

        from core.datamodel import list_target_fields

        _, tf_with_count = _tf_row(used_by_count=Decimal("5"))
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = [tf_with_count]
        cur.description = _TF_WITH_COUNT_COLS

        result = list_target_fields(conn)
        assert isinstance(result[0]["used_by_count"], int)
        assert result[0]["used_by_count"] == 5

    # AI-49: module filter tests
    def test_module_filter_added_to_params(self):
        """AI-49: module= is included as a SQL parameter for the IN subquery filter."""
        from core.datamodel import list_target_fields

        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []
        cur.description = _TF_WITH_COUNT_COLS

        list_target_fields(conn, module="google-analytics")

        call_args = cur.execute.call_args
        params = call_args[0][1]
        assert "google-analytics" in params

    def test_module_filter_sql_contains_module_name_subquery(self):
        """AI-49: module= causes the SQL to include a module_name subquery JOIN."""
        from core.datamodel import list_target_fields

        executed_sqls: list[str] = []
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []
        cur.description = _TF_WITH_COUNT_COLS

        def capture_sql(sql, params=None):
            executed_sqls.append(sql)
        cur.execute.side_effect = capture_sql

        list_target_fields(conn, module="meta-ads")

        assert executed_sqls, "execute not called"
        sql = executed_sqls[0]
        assert "module_name" in sql.lower(), "expected module_name filter in SQL"
        assert "datastream_mappings" in sql.lower()

    def test_module_filter_unknown_returns_empty(self):
        """AI-49: unknown module -> empty list (200, not an error).

        list_target_fields returns whatever the DB yields -- an empty result for an
        unknown module name is correct behaviour (the IN subquery returns no matches).
        """
        from core.datamodel import list_target_fields

        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []
        cur.description = _TF_WITH_COUNT_COLS

        result = list_target_fields(conn, module="no-such-module")
        assert result == []

    def test_module_none_does_not_add_subquery(self):
        """AI-49: module=None -> no module_name condition in SQL (unfiltered behavior)."""
        from core.datamodel import list_target_fields

        executed_sqls: list[str] = []
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []
        cur.description = _TF_WITH_COUNT_COLS

        def capture_sql(sql, params=None):
            executed_sqls.append(sql)
        cur.execute.side_effect = capture_sql

        list_target_fields(conn)  # no module kwarg

        assert executed_sqls
        sql = executed_sqls[0]
        # The IN subquery for module filtering must NOT appear when module is None.
        assert "ds2.module_name" not in sql


# ---------------------------------------------------------------------------
# get_target_field
# ---------------------------------------------------------------------------


class TestGetTargetField:
    def test_returns_none_when_field_not_found(self):
        from core.datamodel import get_target_field

        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchone.return_value = None
        cur.fetchall.return_value = []
        cur.description = _TF_COLS

        result = get_target_field("nonexistent", conn)
        assert result is None

    def test_returns_field_with_used_by_list(self):
        from core.datamodel import get_target_field

        tf_row, _ = _tf_row(name="clicks", data_type="integer", field_kind="metric", measure="sum")
        ub_row = (
            "ds_001", "Google Analytics", "google-analytics", "proj_a",
            True, "clicks", _NOW, "ok",
        )
        ub_cols = [
            ("datastream_id",), ("datastream_name",), ("module_name",), ("project_id",),
            ("enabled",), ("source_field",), ("last_loaded_at",), ("last_verdict",),
        ]

        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        # First call: fetchone for the field; second call: fetchall for used_by
        cur.fetchone.return_value = tf_row
        cur.fetchall.return_value = [ub_row]
        # description switches between calls via side_effect
        cur.description = _TF_COLS
        # We need description to differ per query; simulate with side_effect
        cur.description = MagicMock()
        cur.description.__iter__ = MagicMock(side_effect=[
            iter(_TF_COLS), iter(ub_cols),
        ])

        # Simpler approach: use separate cursors via context manager stack
        # Actually, mock each cursor call sequentially:
        cur1 = MagicMock()
        cur1.__enter__ = MagicMock(return_value=cur1)
        cur1.__exit__ = MagicMock(return_value=False)
        cur1.fetchone.return_value = tf_row
        cur1.description = _TF_COLS

        cur2 = MagicMock()
        cur2.__enter__ = MagicMock(return_value=cur2)
        cur2.__exit__ = MagicMock(return_value=False)
        cur2.fetchall.return_value = [ub_row]
        cur2.description = ub_cols

        conn2 = MagicMock()
        conn2.cursor.return_value.__enter__ = MagicMock(side_effect=[cur1, cur2])
        conn2.cursor.return_value.__exit__ = MagicMock(return_value=False)
        # Use simpler mock that returns different cursors each time
        conn2.cursor.side_effect = [
            _make_ctx(cur1),
            _make_ctx(cur2),
        ]

        result = get_target_field("clicks", conn2)
        assert result is not None
        assert result["name"] == "clicks"
        assert isinstance(result["used_by"], list)
        assert isinstance(result["conflicts"], list)
        assert result["used_by_count"] == 1

    def test_used_by_count_matches_list_length(self):
        """used_by_count must equal len(used_by)."""
        from core.datamodel import get_target_field

        tf_row, _ = _tf_row(name="impressions", data_type="integer")
        # No used_by rows
        cur1 = MagicMock()
        cur1.__enter__ = MagicMock(return_value=cur1)
        cur1.__exit__ = MagicMock(return_value=False)
        cur1.fetchone.return_value = tf_row
        cur1.description = _TF_COLS

        cur2 = MagicMock()
        cur2.__enter__ = MagicMock(return_value=cur2)
        cur2.__exit__ = MagicMock(return_value=False)
        cur2.fetchall.return_value = []
        cur2.description = []

        conn = MagicMock()
        conn.cursor.side_effect = [_make_ctx(cur1), _make_ctx(cur2)]

        result = get_target_field("impressions", conn)
        assert result["used_by_count"] == 0
        assert len(result["used_by"]) == 0


def _make_ctx(cur):
    """Wrap a cursor mock into a context-manager-compatible object."""
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=cur)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


# ---------------------------------------------------------------------------
# _detect_conflicts
# ---------------------------------------------------------------------------


class TestDetectConflicts:
    def test_no_conflict_single_module(self):
        from core.datamodel import _detect_conflicts

        field = {"data_type": "currency", "measure": "sum", "field_kind": "metric"}
        used_by = [{"module_name": "meta-ads", "datastream_name": "Meta Ads"}]
        result = _detect_conflicts(field, used_by)
        assert result == []

    def test_currency_conflict_two_modules(self):
        """THE known case: cost from meta-ads (USD) + google-ads (EUR)."""
        from core.datamodel import _detect_conflicts

        field = {"data_type": "currency", "measure": "sum", "field_kind": "metric"}
        used_by = [
            {"module_name": "meta-ads", "datastream_name": "Meta Ads"},
            {"module_name": "google-ads", "datastream_name": "Google Ads"},
        ]
        result = _detect_conflicts(field, used_by)
        assert len(result) == 1
        assert result[0]["code"] == "CURRENCY_CONFLICT"
        assert "Meta Ads" in result[0]["affected_streams"]
        assert "Google Ads" in result[0]["affected_streams"]

    def test_decimal_conflict_two_modules(self):
        """Decimal fields with >1 module also trigger currency conflict."""
        from core.datamodel import _detect_conflicts

        field = {"data_type": "decimal", "measure": "sum", "field_kind": "metric"}
        used_by = [
            {"module_name": "mod-a", "datastream_name": "Stream A"},
            {"module_name": "mod-b", "datastream_name": "Stream B"},
        ]
        result = _detect_conflicts(field, used_by)
        assert any(c["code"] == "CURRENCY_CONFLICT" for c in result)

    def test_measure_null_on_numeric_metric(self):
        from core.datamodel import _detect_conflicts

        field = {"data_type": "integer", "measure": None, "field_kind": "metric"}
        used_by = [{"module_name": "ga", "datastream_name": "GA"}]
        result = _detect_conflicts(field, used_by)
        assert any(c["code"] == "MEASURE_NULL" for c in result)

    def test_measure_null_on_dimension_no_conflict(self):
        """Dimensions are not expected to have a measure — no MEASURE_NULL flag."""
        from core.datamodel import _detect_conflicts

        field = {"data_type": "string", "measure": None, "field_kind": "dimension"}
        used_by = [{"module_name": "ga", "datastream_name": "GA"}]
        result = _detect_conflicts(field, used_by)
        assert result == []

    def test_no_conflict_integer_with_measure(self):
        from core.datamodel import _detect_conflicts

        field = {"data_type": "integer", "measure": "sum", "field_kind": "metric"}
        used_by = [{"module_name": "ga", "datastream_name": "GA"}]
        result = _detect_conflicts(field, used_by)
        assert result == []

    def test_empty_used_by_no_conflict(self):
        from core.datamodel import _detect_conflicts

        field = {"data_type": "currency", "measure": "sum", "field_kind": "metric"}
        result = _detect_conflicts(field, [])
        assert result == []


# ---------------------------------------------------------------------------
# create_target_field
# ---------------------------------------------------------------------------


class TestCreateTargetField:
    def test_creates_valid_field(self):
        from core.datamodel import create_target_field

        created_row = (
            "my_metric", "Ma metrique", "integer", "metric", "sum", None,
            "user@test", False, _NOW,
        )
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        # Story 44.7: create_target_field also does a MAX(version_number)
        # lookup (finding #1) -- fetchone() is called twice: INSERT...RETURNING,
        # then SELECT COALESCE(MAX(version_number), 0).
        cur.fetchone.side_effect = [created_row, (0,)]
        cur.description = _TF_COLS

        result = create_target_field(
            {
                "name": "my_metric",
                "display_name": "Ma metrique",
                "data_type": "integer",
                "field_kind": "metric",
                "measure": "sum",
            },
            created_by="user@test",
            conn=conn,
        )
        assert result["name"] == "my_metric"
        assert result["is_default"] is False

    def test_raises_on_missing_name(self):
        from core.datamodel import create_target_field

        conn = MagicMock()
        with pytest.raises(ValueError, match="name est requis"):
            create_target_field(
                {"display_name": "X", "data_type": "integer", "field_kind": "metric"},
                created_by="u",
                conn=conn,
            )

    def test_raises_on_invalid_snake_case(self):
        from core.datamodel import create_target_field

        conn = MagicMock()
        with pytest.raises(ValueError, match="snake_case"):
            create_target_field(
                {
                    "name": "My Field",  # spaces not allowed
                    "display_name": "X",
                    "data_type": "integer",
                    "field_kind": "metric",
                },
                created_by="u",
                conn=conn,
            )

    def test_raises_on_invalid_data_type(self):
        from core.datamodel import create_target_field

        conn = MagicMock()
        with pytest.raises(ValueError, match="data_type invalide"):
            create_target_field(
                {
                    "name": "x_field",
                    "display_name": "X",
                    "data_type": "float",  # not in enum
                    "field_kind": "metric",
                },
                created_by="u",
                conn=conn,
            )

    def test_raises_on_invalid_field_kind(self):
        from core.datamodel import create_target_field

        conn = MagicMock()
        with pytest.raises(ValueError, match="field_kind invalide"):
            create_target_field(
                {
                    "name": "x_field",
                    "display_name": "X",
                    "data_type": "integer",
                    "field_kind": "kpi",  # not in enum
                },
                created_by="u",
                conn=conn,
            )

    def test_raises_on_invalid_measure(self):
        from core.datamodel import create_target_field

        conn = MagicMock()
        with pytest.raises(ValueError, match="measure invalide"):
            create_target_field(
                {
                    "name": "x_field",
                    "display_name": "X",
                    "data_type": "integer",
                    "field_kind": "metric",
                    "measure": "total",  # not in enum
                },
                created_by="u",
                conn=conn,
            )

    def test_snake_case_with_numbers_accepted(self):
        """e.g. 'ctr_7d' is valid snake_case."""
        from core.datamodel import create_target_field

        created_row = ("ctr_7d", "CTR 7j", "decimal", "metric", "average", None, "u", False, _NOW)
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        # Story 44.7: create_target_field also does a MAX(version_number)
        # lookup (finding #1) -- fetchone() is called twice: INSERT...RETURNING,
        # then SELECT COALESCE(MAX(version_number), 0).
        cur.fetchone.side_effect = [created_row, (0,)]
        cur.description = _TF_COLS

        result = create_target_field(
            {
                "name": "ctr_7d", "display_name": "CTR 7j", "data_type": "decimal",
                "field_kind": "metric", "measure": "average",
            },
            created_by="u",
            conn=conn,
        )
        assert result["name"] == "ctr_7d"


# ---------------------------------------------------------------------------
# update_target_field (immutability enforcement)
# ---------------------------------------------------------------------------


class TestUpdateTargetField:
    def test_immutable_name_raises(self):
        from core.datamodel import update_target_field

        conn = MagicMock()
        with pytest.raises(ValueError, match="immuable"):
            update_target_field("clicks", {"name": "new_name"}, conn)

    def test_immutable_data_type_raises(self):
        from core.datamodel import update_target_field

        conn = MagicMock()
        with pytest.raises(ValueError, match="immuable"):
            update_target_field("clicks", {"data_type": "string"}, conn)

    def test_immutable_field_kind_raises(self):
        from core.datamodel import update_target_field

        conn = MagicMock()
        with pytest.raises(ValueError, match="immuable"):
            update_target_field("clicks", {"field_kind": "dimension"}, conn)

    def test_update_display_name(self):
        from core.datamodel import update_target_field

        updated_row = (
            "clicks", "Clics (MaJ)", "integer", "metric", "sum", None, "system", True, _NOW,
        )
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchone.return_value = updated_row
        cur.description = _TF_COLS

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("core.datamodel.write_audit_row", lambda **kw: None, raising=False)
            # Patch the import inside the function
            import unittest.mock as um
            with um.patch("core.audit.write_audit_row"):
                result = update_target_field("clicks", {"display_name": "Clics (MaJ)"}, conn)

        assert result is not None
        assert result["display_name"] == "Clics (MaJ)"

    def test_returns_none_when_not_found(self):
        from core.datamodel import update_target_field

        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchone.return_value = None
        cur.description = _TF_COLS

        import unittest.mock as um
        with um.patch("core.audit.write_audit_row"):
            result = update_target_field("nonexistent", {"description": "x"}, conn)

        assert result is None

    def test_empty_patch_returns_current(self):
        """Empty patch dict returns the current state without executing UPDATE."""
        from core.datamodel import update_target_field

        current_row = ("clicks", "Clics", "integer", "metric", "sum", None, "system", True, _NOW)
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchone.return_value = current_row
        cur.description = _TF_COLS

        result = update_target_field("clicks", {}, conn)
        assert result is not None
        # UPDATE should NOT have been called (empty patch)
        sql_calls = [str(c) for c in cur.execute.call_args_list]
        assert not any("UPDATE" in s for s in sql_calls)

    def test_restore_of_identical_snapshot_appends_exactly_one_restored_version(self):
        """Story 44.8 finding #3: restoring a snapshot whose values already match
        the current row must NOT be a silent no-op -- it still appends exactly
        one change_kind='restored' version row (diff falls back to
        {'_restored_from': <version>} alone, since there is no real field diff).
        """
        from core.datamodel import update_target_field

        # Before and after are IDENTICAL -- the restore target equals the
        # current row's values (e.g. restoring the current version).
        row = ("clicks", "Clics", "integer", "metric", "sum", None, "system",
               True, _NOW, "approved", None, None, _NOW)

        cur1 = MagicMock()  # SELECT before state
        cur1.__enter__ = MagicMock(return_value=cur1)
        cur1.__exit__ = MagicMock(return_value=False)
        cur1.fetchone.return_value = row
        cur1.description = _TF_GOVERNANCE_COLS

        cur2 = MagicMock()  # UPDATE ... RETURNING (same values)
        cur2.__enter__ = MagicMock(return_value=cur2)
        cur2.__exit__ = MagicMock(return_value=False)
        cur2.fetchone.return_value = row
        cur2.description = _TF_GOVERNANCE_COLS

        cur3 = MagicMock()  # SELECT MAX(version_number)
        cur3.__enter__ = MagicMock(return_value=cur3)
        cur3.__exit__ = MagicMock(return_value=False)
        cur3.fetchone.return_value = (2,)

        insert_calls: list[tuple] = []

        cur4 = MagicMock()  # INSERT into target_fields_versions

        def _capture_insert(sql, params=None):
            insert_calls.append((sql, params))

        cur4.__enter__ = MagicMock(return_value=cur4)
        cur4.__exit__ = MagicMock(return_value=False)
        cur4.execute.side_effect = _capture_insert

        conn = MagicMock()
        conn.cursor.side_effect = [
            _make_ctx(cur1), _make_ctx(cur2), _make_ctx(cur3), _make_ctx(cur4),
        ]

        import unittest.mock as um
        with um.patch("core.audit.write_audit_row"):
            result = update_target_field(
                "clicks",
                {
                    "display_name": "Clics",
                    "measure": "sum",
                    "description": None,
                    "restored_from": 1,
                },
                conn,
                identity="alice@toorow.io",
            )

        assert result is not None
        # Exactly one version row appended, change_kind='restored'.
        assert len(insert_calls) == 1
        insert_sql, insert_params = insert_calls[0]
        assert "target_fields_versions" in insert_sql
        assert "restored" in insert_params
        diff_json = insert_params[insert_params.index("restored") + 1]
        assert json.loads(diff_json) == {"_restored_from": 1}


# ---------------------------------------------------------------------------
# upsert_mapping
# ---------------------------------------------------------------------------


class TestUpsertMapping:
    def test_upserts_mapping(self):
        import unittest.mock as um

        from core.datamodel import upsert_mapping

        mapping_row = ("ds_001", "clicks", "clicks", False, _NOW)
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchone.return_value = mapping_row
        cur.description = _MAPPING_COLS

        with um.patch("core.audit.write_audit_row"):
            result = upsert_mapping("ds_001", "clicks", "clicks", "test@test", conn)

        assert result["datastream_id"] == "ds_001"
        assert result["source_field"] == "clicks"
        assert result["target_field"] == "clicks"

    def test_raises_on_empty_datastream_id(self):
        from core.datamodel import upsert_mapping

        conn = MagicMock()
        with pytest.raises(ValueError, match="datastream_id"):
            upsert_mapping("", "clicks", "clicks", "user", conn)

    def test_raises_on_empty_source_field(self):
        from core.datamodel import upsert_mapping

        conn = MagicMock()
        with pytest.raises(ValueError, match="source_field"):
            upsert_mapping("ds_001", "", "clicks", "user", conn)

    def test_unmaps_when_target_none(self):
        import unittest.mock as um

        from core.datamodel import upsert_mapping

        mapping_row = ("ds_001", "some_col", None, False, _NOW)
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchone.return_value = mapping_row
        cur.description = _MAPPING_COLS

        with um.patch("core.audit.write_audit_row"):
            result = upsert_mapping("ds_001", "some_col", None, "user", conn)

        assert result["target_field"] is None


# ---------------------------------------------------------------------------
# Story 13.1: approve_target_field
# ---------------------------------------------------------------------------

# Columns returned by the SELECT and the RETURNING clause in approve_target_field
_TF_GOVERNANCE_COLS = _TF_COLS + [
    ("status",), ("approved_at",), ("approved_by",), ("updated_at",),
]

# Helper: build a draft field row
def _draft_row(name="my_field", approver=None):
    return (
        name, "Mon champ", "integer", "metric", "sum", None,
        "user@test", False, _NOW, "draft", None, approver, _NOW,
    )

# Helper: build an already-approved field row
def _approved_row(name="my_field", approver="approver@test"):
    return (
        name, "Mon champ", "integer", "metric", "sum", None,
        "user@test", False, _NOW, "approved", _NOW, approver, _NOW,
    )


class TestApproveTargetField:
    def test_approve_sets_status_approved(self):
        """approve_target_field transitions draft -> approved and returns approved dict."""
        from core.datamodel import approve_target_field

        draft = _draft_row("my_field")
        approved = _approved_row("my_field", "approver@test")

        # cur1: SELECT (returns draft row -> triggers real transition)
        cur1 = MagicMock()
        cur1.__enter__ = MagicMock(return_value=cur1)
        cur1.__exit__ = MagicMock(return_value=False)
        cur1.fetchone.return_value = draft
        cur1.description = _TF_GOVERNANCE_COLS

        # cur2: UPDATE RETURNING (returns approved row)
        cur2 = MagicMock()
        cur2.__enter__ = MagicMock(return_value=cur2)
        cur2.__exit__ = MagicMock(return_value=False)
        cur2.fetchone.return_value = approved
        cur2.description = _TF_GOVERNANCE_COLS

        # cur3: INSERT into target_field_approvals
        cur3 = MagicMock()
        cur3.__enter__ = MagicMock(return_value=cur3)
        cur3.__exit__ = MagicMock(return_value=False)

        # cur4: Story 44.7 -- SELECT MAX(version_number) for target_fields_versions
        cur4 = MagicMock()
        cur4.__enter__ = MagicMock(return_value=cur4)
        cur4.__exit__ = MagicMock(return_value=False)
        cur4.fetchone.return_value = (0,)

        # cur5: Story 44.7 -- INSERT into target_fields_versions
        cur5 = MagicMock()
        cur5.__enter__ = MagicMock(return_value=cur5)
        cur5.__exit__ = MagicMock(return_value=False)

        conn = MagicMock()
        conn.cursor.side_effect = [
            _make_ctx(cur1), _make_ctx(cur2), _make_ctx(cur3), _make_ctx(cur4), _make_ctx(cur5),
        ]

        result = approve_target_field("my_field", "approver@test", conn)

        assert result is not None
        assert result["status"] == "approved"
        assert result["approved_by"] == "approver@test"
        assert result["name"] == "my_field"
        conn.commit.assert_called_once()

    def test_approve_returns_none_when_field_not_found(self):
        """Returns None when the field does not exist (SELECT returns no row)."""
        from core.datamodel import approve_target_field

        cur1 = MagicMock()
        cur1.__enter__ = MagicMock(return_value=cur1)
        cur1.__exit__ = MagicMock(return_value=False)
        cur1.fetchone.return_value = None
        cur1.description = _TF_GOVERNANCE_COLS

        conn = MagicMock()
        conn.cursor.side_effect = [_make_ctx(cur1)]

        result = approve_target_field("nonexistent", "approver@test", conn)

        assert result is None
        conn.commit.assert_not_called()

    def test_approve_writes_audit_row_on_transition(self):
        """approve_target_field inserts into app.target_field_approvals on draft->approved."""
        from core.datamodel import approve_target_field

        inserted_sqls: list[str] = []

        cur1 = MagicMock()
        cur1.__enter__ = MagicMock(return_value=cur1)
        cur1.__exit__ = MagicMock(return_value=False)
        cur1.fetchone.return_value = _draft_row("clicks")
        cur1.description = _TF_GOVERNANCE_COLS

        cur2 = MagicMock()
        cur2.__enter__ = MagicMock(return_value=cur2)
        cur2.__exit__ = MagicMock(return_value=False)
        cur2.fetchone.return_value = _approved_row("clicks", "admin@test")
        cur2.description = _TF_GOVERNANCE_COLS

        cur3 = MagicMock()
        cur3.__enter__ = MagicMock(return_value=cur3)
        cur3.__exit__ = MagicMock(return_value=False)

        def capture_insert(sql, params=None):
            inserted_sqls.append(sql)

        cur3.execute.side_effect = capture_insert

        cur4 = MagicMock()
        cur4.__enter__ = MagicMock(return_value=cur4)
        cur4.__exit__ = MagicMock(return_value=False)
        cur4.fetchone.return_value = (0,)

        cur5 = MagicMock()
        cur5.__enter__ = MagicMock(return_value=cur5)
        cur5.__exit__ = MagicMock(return_value=False)

        conn = MagicMock()
        conn.cursor.side_effect = [
            _make_ctx(cur1), _make_ctx(cur2), _make_ctx(cur3), _make_ctx(cur4), _make_ctx(cur5),
        ]

        approve_target_field("clicks", "admin@test", conn)

        assert any("target_field_approvals" in s for s in inserted_sqls), (
            "Expected INSERT into target_field_approvals on draft->approved transition"
        )

    def test_approve_already_approved_is_noop(self):
        """Re-approving an already-approved field must be a no-op.

        - Returns the current field dict (preserving original approved_by).
        - Does NOT insert a second audit row.
        - Does NOT call conn.commit (no mutation occurred).
        """
        from core.datamodel import approve_target_field

        # Field is already approved by original_approver@test
        already_approved = _approved_row("clicks", "original_approver@test")

        cur1 = MagicMock()
        cur1.__enter__ = MagicMock(return_value=cur1)
        cur1.__exit__ = MagicMock(return_value=False)
        cur1.fetchone.return_value = already_approved
        cur1.description = _TF_GOVERNANCE_COLS

        conn = MagicMock()
        # Only one cursor should be used (the SELECT); no UPDATE or INSERT.
        conn.cursor.side_effect = [_make_ctx(cur1)]

        result = approve_target_field("clicks", "second_approver@test", conn)

        assert result is not None
        assert result["status"] == "approved"
        # Original approver is preserved (not overwritten by second_approver)
        assert result["approved_by"] == "original_approver@test"
        # No commit: no mutation
        conn.commit.assert_not_called()
        # Only one cursor was consumed (no UPDATE, no INSERT)
        assert conn.cursor.call_count == 1

    def test_approve_immutable_fields_not_touched(self):
        """approve_target_field must not modify name/data_type/field_kind."""
        from core.datamodel import approve_target_field

        executed_sqls: list[str] = []

        # cur1: SELECT returning draft row (triggers real UPDATE)
        cur1 = MagicMock()
        cur1.__enter__ = MagicMock(return_value=cur1)
        cur1.__exit__ = MagicMock(return_value=False)
        cur1.fetchone.return_value = _draft_row("cost")
        cur1.description = _TF_GOVERNANCE_COLS

        # cur2: UPDATE
        cur2 = MagicMock()
        cur2.__enter__ = MagicMock(return_value=cur2)
        cur2.__exit__ = MagicMock(return_value=False)
        cur2.fetchone.return_value = _approved_row("cost", "cfo@test")
        cur2.description = _TF_GOVERNANCE_COLS

        def capture(sql, params=None):
            executed_sqls.append(sql)

        cur2.execute.side_effect = capture

        # cur3: INSERT
        cur3 = MagicMock()
        cur3.__enter__ = MagicMock(return_value=cur3)
        cur3.__exit__ = MagicMock(return_value=False)

        cur4 = MagicMock()
        cur4.__enter__ = MagicMock(return_value=cur4)
        cur4.__exit__ = MagicMock(return_value=False)
        cur4.fetchone.return_value = (0,)

        cur5 = MagicMock()
        cur5.__enter__ = MagicMock(return_value=cur5)
        cur5.__exit__ = MagicMock(return_value=False)

        conn = MagicMock()
        conn.cursor.side_effect = [
            _make_ctx(cur1), _make_ctx(cur2), _make_ctx(cur3), _make_ctx(cur4), _make_ctx(cur5),
        ]

        approve_target_field("cost", "cfo@test", conn)

        # The UPDATE SET clause must NOT assign name/data_type/field_kind.
        update_sqls = [s for s in executed_sqls if "update app.target_fields" in s.lower()]
        assert update_sqls, "no UPDATE on target_fields captured"
        for sql in update_sqls:
            lowered = sql.lower()
            set_clause = lowered.split(" set ", 1)[1].split(" where ", 1)[0]
            assert "name" not in set_clause, "name must not be in UPDATE SET"
            assert "data_type" not in set_clause, "data_type must not be in UPDATE SET"
            assert "field_kind" not in set_clause, "field_kind must not be in UPDATE SET"


# ---------------------------------------------------------------------------
# Story 13.1: delete_target_field
# ---------------------------------------------------------------------------


class TestDeleteTargetField:
    """Story 44.7: delete_target_field(name, identity, conn) -- soft delete.

    Cursor sequence on a real soft-delete: cur1 (guard SELECT), cur2 (UPDATE
    status='deleted' RETURNING), cur3 (MAX(version_number)), cur4 (INSERT
    version row). Guard-failure paths (not found / default / in-use) only
    ever touch cur1.
    """

    _AFTER_ROW = (
        "orphan_field", "Orphan", "integer", "metric", "sum", None,
        "user@test", False, _NOW, "deleted", None, None, _NOW,
    )

    def test_delete_orphan_field_succeeds(self):
        """DELETE a field with zero mappings (and is_default=False) completes without error."""
        from core.datamodel import delete_target_field

        # SELECT row: (name, is_default=False, used_by_count=0, status='draft')
        cur1 = MagicMock()
        cur1.__enter__ = MagicMock(return_value=cur1)
        cur1.__exit__ = MagicMock(return_value=False)
        cur1.fetchone.return_value = ("orphan_field", False, 0, "draft")

        # cur2: UPDATE ... SET status='deleted' RETURNING (soft delete)
        cur2 = MagicMock()
        cur2.__enter__ = MagicMock(return_value=cur2)
        cur2.__exit__ = MagicMock(return_value=False)
        cur2.fetchone.return_value = self._AFTER_ROW
        cur2.description = _TF_GOVERNANCE_COLS

        # cur3: Story 44.7 -- SELECT MAX(version_number)
        cur3 = MagicMock()
        cur3.__enter__ = MagicMock(return_value=cur3)
        cur3.__exit__ = MagicMock(return_value=False)
        cur3.fetchone.return_value = (0,)

        # cur4: Story 44.7 -- INSERT into target_fields_versions
        cur4 = MagicMock()
        cur4.__enter__ = MagicMock(return_value=cur4)
        cur4.__exit__ = MagicMock(return_value=False)

        conn = MagicMock()
        conn.cursor.side_effect = [
            _make_ctx(cur1), _make_ctx(cur2), _make_ctx(cur3), _make_ctx(cur4),
        ]

        import unittest.mock as um
        with um.patch("core.audit.write_audit_row"):
            delete_target_field("orphan_field", "jean@test", conn)  # must not raise

        conn.commit.assert_called_once()

    def test_delete_approved_field_with_no_mappings_succeeds(self):
        """DELETE a previously approved field (0 mappings) succeeds.

        Audit rows in target_field_approvals are NOT deleted -- they remain as
        historical trace. The UPDATE (soft delete) must NOT touch
        target_field_approvals.
        """
        from core.datamodel import delete_target_field

        cur1 = MagicMock()
        cur1.__enter__ = MagicMock(return_value=cur1)
        cur1.__exit__ = MagicMock(return_value=False)
        cur1.fetchone.return_value = ("old_kpi", False, 0, "approved")

        executed_sqls: list[str] = []

        cur2 = MagicMock()
        cur2.__enter__ = MagicMock(return_value=cur2)
        cur2.__exit__ = MagicMock(return_value=False)
        cur2.fetchone.return_value = self._AFTER_ROW
        cur2.description = _TF_GOVERNANCE_COLS

        def capture(sql, params=None):
            executed_sqls.append(sql)

        cur2.execute.side_effect = capture

        cur3 = MagicMock()
        cur3.__enter__ = MagicMock(return_value=cur3)
        cur3.__exit__ = MagicMock(return_value=False)
        cur3.fetchone.return_value = (0,)

        cur4 = MagicMock()
        cur4.__enter__ = MagicMock(return_value=cur4)
        cur4.__exit__ = MagicMock(return_value=False)

        conn = MagicMock()
        conn.cursor.side_effect = [
            _make_ctx(cur1), _make_ctx(cur2), _make_ctx(cur3), _make_ctx(cur4),
        ]

        import unittest.mock as um
        with um.patch("core.audit.write_audit_row"):
            delete_target_field("old_kpi", "jean@test", conn)  # must not raise

        conn.commit.assert_called_once()
        # Audit rows must NOT be deleted (historical preservation); and this
        # must be a soft delete (UPDATE), never a hard DELETE.
        assert not any(
            "target_field_approvals" in s for s in executed_sqls
        ), "delete_target_field must NOT delete rows from target_field_approvals"
        assert any("update" in s.lower() for s in executed_sqls), (
            "delete_target_field must UPDATE (soft delete), not DELETE"
        )
        assert not any(
            s.strip().lower().startswith("delete") for s in executed_sqls
        ), "delete_target_field must never issue a hard DELETE on app.target_fields"

    def test_delete_default_field_raises(self):
        """DELETE a field with is_default=TRUE raises FieldInUseError (H2 guard)."""
        from core.datamodel import FieldInUseError, delete_target_field

        cur1 = MagicMock()
        cur1.__enter__ = MagicMock(return_value=cur1)
        cur1.__exit__ = MagicMock(return_value=False)
        # is_default=True, used_by_count=0 (orphan but still default)
        cur1.fetchone.return_value = ("clicks", True, 0, "approved")

        conn = MagicMock()
        conn.cursor.side_effect = [_make_ctx(cur1)]

        with pytest.raises(FieldInUseError) as exc_info:
            delete_target_field("clicks", "jean@test", conn)

        msg = str(exc_info.value)
        # Message must mention the is_default restriction
        assert "defaut" in msg.lower() or "socle" in msg.lower() or "default" in msg.lower()
        conn.commit.assert_not_called()

    def test_delete_field_in_use_raises_409_error(self):
        """DELETE a field with used_by_count > 0 raises FieldInUseError."""
        from core.datamodel import FieldInUseError, delete_target_field

        cur1 = MagicMock()
        cur1.__enter__ = MagicMock(return_value=cur1)
        cur1.__exit__ = MagicMock(return_value=False)
        # (name, is_default=False, used_by_count=3, status)
        cur1.fetchone.return_value = ("clicks", False, 3, "approved")

        conn = MagicMock()
        conn.cursor.side_effect = [_make_ctx(cur1)]

        with pytest.raises(FieldInUseError) as exc_info:
            delete_target_field("clicks", "jean@test", conn)

        msg = str(exc_info.value)
        assert "clicks" in msg
        assert "3" in msg  # count mentioned
        # FR message with accent
        assert "supprim" in msg.lower() or "mapping" in msg.lower()

        conn.commit.assert_not_called()

    def test_delete_nonexistent_field_raises_value_error(self):
        """DELETE a field that does not exist raises ValueError."""
        from core.datamodel import delete_target_field

        cur1 = MagicMock()
        cur1.__enter__ = MagicMock(return_value=cur1)
        cur1.__exit__ = MagicMock(return_value=False)
        cur1.fetchone.return_value = None  # field not found

        conn = MagicMock()
        conn.cursor.side_effect = [_make_ctx(cur1)]

        with pytest.raises(ValueError, match="introuvable"):
            delete_target_field("no_such_field", "jean@test", conn)

    def test_delete_already_deleted_field_raises_value_error(self):
        """A second DELETE on an already status='deleted' field is 'not found' (404).

        History outlives visibility: the row still exists, but delete_target_field
        must treat it as gone, exactly like list/get do.
        """
        from core.datamodel import delete_target_field

        cur1 = MagicMock()
        cur1.__enter__ = MagicMock(return_value=cur1)
        cur1.__exit__ = MagicMock(return_value=False)
        cur1.fetchone.return_value = ("gone", False, 0, "deleted")

        conn = MagicMock()
        conn.cursor.side_effect = [_make_ctx(cur1)]

        with pytest.raises(ValueError, match="introuvable"):
            delete_target_field("gone", "jean@test", conn)

        conn.commit.assert_not_called()

    def test_delete_field_in_use_fr_message(self):
        """The FieldInUseError message is in French and mentions the used-by count."""
        from core.datamodel import FieldInUseError, delete_target_field

        cur1 = MagicMock()
        cur1.__enter__ = MagicMock(return_value=cur1)
        cur1.__exit__ = MagicMock(return_value=False)
        # (name, is_default=False, used_by_count=7, status)
        cur1.fetchone.return_value = ("cost", False, 7, "approved")

        conn = MagicMock()
        conn.cursor.side_effect = [_make_ctx(cur1)]

        with pytest.raises(FieldInUseError) as exc_info:
            delete_target_field("cost", "jean@test", conn)

        msg = str(exc_info.value)
        assert "7" in msg  # count clearly visible
        # Message is in French (at least one accented word expected)
        assert any(ch in msg for ch in "éèàùîôêâûçÉÈÀÙÎÔÊÂÛÇ")

    def test_delete_identity_is_persisted_in_version_and_audit(self):
        """The real caller identity lands on the version row AND the audit call."""
        from core.datamodel import delete_target_field

        cur1 = MagicMock()
        cur1.__enter__ = MagicMock(return_value=cur1)
        cur1.__exit__ = MagicMock(return_value=False)
        cur1.fetchone.return_value = ("orphan_field", False, 0, "draft")

        cur2 = MagicMock()
        cur2.__enter__ = MagicMock(return_value=cur2)
        cur2.__exit__ = MagicMock(return_value=False)
        cur2.fetchone.return_value = self._AFTER_ROW
        cur2.description = _TF_GOVERNANCE_COLS

        cur3 = MagicMock()
        cur3.__enter__ = MagicMock(return_value=cur3)
        cur3.__exit__ = MagicMock(return_value=False)
        cur3.fetchone.return_value = (0,)

        version_inserts: list[tuple] = []

        cur4 = MagicMock()
        cur4.__enter__ = MagicMock(return_value=cur4)
        cur4.__exit__ = MagicMock(return_value=False)

        def capture_version_insert(sql, params=None):
            version_inserts.append(params)

        cur4.execute.side_effect = capture_version_insert

        conn = MagicMock()
        conn.cursor.side_effect = [
            _make_ctx(cur1), _make_ctx(cur2), _make_ctx(cur3), _make_ctx(cur4),
        ]

        import unittest.mock as um
        with um.patch("core.audit.write_audit_row") as mock_audit:
            delete_target_field("orphan_field", "jean@test", conn)

        assert version_inserts, "no version row inserted"
        # changed_by is the second-to-last positional param (before changed_at's now())
        assert "jean@test" in version_inserts[0]
        mock_audit.assert_called_once()
        assert mock_audit.call_args.kwargs["identity"] == "jean@test"


# ---------------------------------------------------------------------------
# Story 44.8: list_field_versions (LIMIT) + target_field_exists
# ---------------------------------------------------------------------------


class TestListFieldVersions:
    def test_query_is_capped_at_200(self):
        """Story 44.8 finding #7: unbounded timeline read must be capped."""
        from core.datamodel import list_field_versions

        conn, cur = _make_conn(fetchall_rows=[])
        cur.description = [
            ("name",), ("version_number",), ("display_name",), ("data_type",),
            ("field_kind",), ("measure",), ("description",), ("created_by",),
            ("is_default",), ("created_at",), ("status",), ("approved_at",),
            ("approved_by",), ("updated_at",), ("change_kind",), ("diff",),
            ("changed_by",), ("changed_at",),
        ]

        list_field_versions("clicks", conn)

        sql, params = cur.execute.call_args[0]
        assert "LIMIT" in sql
        assert 200 in params


class TestTargetFieldExists:
    def test_true_when_row_present(self):
        from core.datamodel import target_field_exists

        conn, cur = _make_conn(fetchone_rows=[(1,)])
        assert target_field_exists("clicks", conn) is True

    def test_false_when_no_row(self):
        from core.datamodel import target_field_exists

        conn, cur = _make_conn(fetchone_rows=[None])
        assert target_field_exists("unknown", conn) is False

    def test_no_status_filter_in_query(self):
        """Finding #5: history (and its existence check) must be servable for
        soft-deleted fields -- no `status` filter in the existence check."""
        from core.datamodel import target_field_exists

        conn, cur = _make_conn(fetchone_rows=[(1,)])
        target_field_exists("clicks", conn)

        sql = cur.execute.call_args[0][0]
        assert "status" not in sql.lower()


# ---------------------------------------------------------------------------
# Story 13.1: create_target_field -> status='draft' for user fields
# ---------------------------------------------------------------------------


class TestCreateTargetFieldStatus:
    def test_new_user_field_has_status_draft(self):
        """create_target_field inserts status='draft' for user-created fields."""
        from core.datamodel import create_target_field

        # RETURNING now includes status, approved_at, approved_by, updated_at
        created_row = (
            "new_kpi", "Nouveau KPI", "integer", "metric", "sum", None,
            "user@test", False, _NOW, "draft", None, None, _NOW,
        )
        gov_cols = _TF_COLS + [
            ("status",), ("approved_at",), ("approved_by",), ("updated_at",),
        ]

        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        # Story 44.7: create_target_field now goes through the
        # MAX(version_number)+1 helper too (finding #1) -- fetchone() is
        # called twice: once for the target_fields INSERT...RETURNING, once
        # for the SELECT COALESCE(MAX(version_number), 0) lookup.
        cur.fetchone.side_effect = [created_row, (0,)]
        cur.description = gov_cols

        result = create_target_field(
            {
                "name": "new_kpi",
                "display_name": "Nouveau KPI",
                "data_type": "integer",
                "field_kind": "metric",
                "measure": "sum",
            },
            created_by="user@test",
            conn=conn,
        )

        assert result["status"] == "draft"
        assert result["is_default"] is False

        # The INSERT SQL (into app.target_fields, not the version row inserted
        # right after by Story 44.7) must explicitly set status='draft'.
        insert_sqls = [
            c[0][0] for c in cur.execute.call_args_list
            if "insert into app.target_fields" in c[0][0].lower()
            and "target_fields_versions" not in c[0][0].lower()
        ]
        assert insert_sqls, "no INSERT INTO app.target_fields captured"
        assert "draft" in insert_sqls[0].lower()


# ---------------------------------------------------------------------------
# Story 13.1: approved-only filter in report_chain
# ---------------------------------------------------------------------------


class TestReportChainApprovedFilter:
    def test_fetch_target_fields_filters_approved_only(self):
        """_fetch_target_fields must include AND status='approved' in the SQL."""
        from core.report_chain import _fetch_target_fields

        executed_sqls: list[str] = []

        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []
        cur.description = []

        def capture(sql, params=None):
            executed_sqls.append(sql)

        cur.execute.side_effect = capture

        _fetch_target_fields(["clicks", "cost"], conn)

        assert executed_sqls, "execute() was not called"
        sql = executed_sqls[0]
        assert "approved" in sql.lower(), (
            "Expected AND status='approved' filter in report_chain._fetch_target_fields SQL"
        )
