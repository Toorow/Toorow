"""Unit tests for Story 44.7 -- target_fields_versions (server/core/datamodel.py).

Coverage (per the story's acceptance criteria):
  - version append per operation (created/updated/approved/deleted)
  - real caller identity persisted (never the literal "system")
  - diff correctness (before/after per actually-changed field)
  - soft-delete visibility rules (list_target_fields / get_target_field exclude
    status='deleted'; list_field_versions does NOT filter -- history outlives
    visibility)
  - a soft-deleted field is not resurrected by PATCH or approve (404, not a
    silent write) -- fix-list findings #2 and #4
  - a mapping cannot bind to a soft-deleted target field -- finding #8
  - delete-then-recreate revives the row (change_kind='created' again), and
    the version_number genuinely comes from the MAX(version_number)+1 path
    (not a hardcoded 1) -- fix-list finding #1
  - idempotent re-approve appends nothing

Uses the same mock-cursor-sequence pattern as test_datamodel.py (MagicMock
context-manager cursors, one per conn.cursor() call in call order).

Also includes a pg-gated end-to-end lifecycle test (skipped when
TEST_POSTGRES_DSN is unset), following the pg_available pattern used by
test_dimension_lineage.py.
"""

from __future__ import annotations

import json
import os
import unittest.mock as um
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_NOW = datetime(2026, 7, 26, 9, 0, 0, tzinfo=timezone.utc)

# Column order used throughout app.target_fields RETURNING / SELECT clauses.
_TF_GOVERNANCE_COLS = [
    "name", "display_name", "data_type", "field_kind", "measure", "description",
    "created_by", "is_default", "created_at", "status", "approved_at",
    "approved_by", "updated_at",
]

# Column order of the app.target_fields_versions INSERT in
# core.datamodel._insert_target_field_version_row (changed_at is now(), not a
# bound param, so it is excluded here).
_TFV_INSERT_COLS = [
    "name", "version_number", "display_name", "data_type", "field_kind",
    "measure", "description", "created_by", "is_default", "created_at",
    "status", "approved_at", "approved_by", "updated_at",
    "change_kind", "diff", "changed_by",
]


def _version_row_dict(params) -> dict:
    """Zip a captured target_fields_versions INSERT params tuple into a dict.

    Fix-list finding #12: `x in params` only proves the tuple contains x
    *somewhere* -- it is satisfied even when x lands in the wrong column, or
    when `1 in params` is trivially true because `True == 1` sits in some
    unrelated boolean column. Zipping against the real column order and
    asserting on named keys is a genuine assertion.
    """
    assert len(params) == len(_TFV_INSERT_COLS), (
        f"expected {len(_TFV_INSERT_COLS)} bound params, got {len(params)}: {params!r}"
    )
    return dict(zip(_TFV_INSERT_COLS, params))


def _make_ctx(cur):
    """Wrap a cursor mock into a context-manager-compatible object."""
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=cur)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def _cur(fetchone_value=None, description=None, execute_side_effect=None):
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = fetchone_value
    if description is not None:
        # psycopg exposes cursor.description as a sequence of column
        # descriptors where d[0] is the column NAME (production code does
        # `[d[0] for d in cur.description]`). Accept plain strings here and
        # wrap them, so a bare list of names cannot silently degrade into
        # one-letter keys ('n', 'd', ...) downstream.
        cur.description = [(c,) if isinstance(c, str) else c for c in description]
    if execute_side_effect is not None:
        cur.execute.side_effect = execute_side_effect
    return cur


def _tf_row(name="clicks", **overrides):
    base = {
        "name": name, "display_name": "Clics", "data_type": "integer",
        "field_kind": "metric", "measure": "sum", "description": "d",
        "created_by": "user@test", "is_default": False, "created_at": _NOW,
        "status": "draft", "approved_at": None, "approved_by": None,
        "updated_at": _NOW,
    }
    base.update(overrides)
    return tuple(base[c] for c in _TF_GOVERNANCE_COLS)


def _noop_transaction_conn() -> MagicMock:
    """A MagicMock conn whose conn.transaction() is a real no-op ctx manager.

    create_target_field wraps the row INSERT-or-revive + version append in
    nested conn.transaction() calls (fix-list finding #3); MagicMock supports
    the context-manager protocol automatically, but we pin it down explicitly
    so nesting depth changes in the implementation can't silently change test
    behaviour.
    """
    conn = MagicMock()
    conn.transaction.return_value.__enter__ = MagicMock(return_value=None)
    conn.transaction.return_value.__exit__ = MagicMock(return_value=False)
    return conn


# ---------------------------------------------------------------------------
# create_target_field -> version 1 'created', via the MAX(version_number)+1
# helper on BOTH the fresh path and the revival path (finding #1)
# ---------------------------------------------------------------------------


class TestCreateAppendsVersion:
    def test_create_appends_version_1_created(self):
        """Fresh create: INSERT, then MAX(version_number) lookup (0 -> next=1),
        then the version INSERT -- three cursors, not two. If create_target_field
        regressed to a hardcoded version_number=1 INSERT (skipping the MAX
        lookup), this test's cursor sequence would under-consume and fail.
        """
        from core.datamodel import create_target_field

        inserted: list[tuple] = []
        cur1 = _cur(fetchone_value=_tf_row("my_metric"), description=_TF_GOVERNANCE_COLS)
        cur2 = _cur(fetchone_value=(0,))  # MAX(version_number) -- none yet
        cur3 = _cur()

        def capture(sql, params=None):
            inserted.append((sql, params))

        cur3.execute.side_effect = capture

        conn = _noop_transaction_conn()
        conn.cursor.side_effect = [_make_ctx(cur1), _make_ctx(cur2), _make_ctx(cur3)]

        result = create_target_field(
            {
                "name": "my_metric", "display_name": "Ma metrique",
                "data_type": "integer", "field_kind": "metric", "measure": "sum",
            },
            created_by="jean@test",
            conn=conn,
        )

        assert result["name"] == "my_metric"
        assert len(inserted) == 1
        sql, params = inserted[0]
        assert "target_fields_versions" in sql.lower()

        row = _version_row_dict(params)
        assert row["name"] == "my_metric"
        assert row["version_number"] == 1
        assert row["change_kind"] == "created"
        assert row["changed_by"] == "jean@test"
        assert row["diff"] is None
        conn.commit.assert_called_once()


# ---------------------------------------------------------------------------
# update_target_field -> 'updated' version, diff, real identity, no-op skip
# ---------------------------------------------------------------------------


class TestUpdateAppendsVersion:
    def test_update_appends_version_with_diff_and_real_identity(self):
        from core.datamodel import update_target_field

        before = _tf_row("clicks", display_name="Clics", description="old")
        after = _tf_row("clicks", display_name="Clics (MaJ)", description="new")

        cur1 = _cur(fetchone_value=before, description=_TF_GOVERNANCE_COLS)
        cur2 = _cur(fetchone_value=after, description=_TF_GOVERNANCE_COLS)
        cur3 = _cur(fetchone_value=(0,))  # MAX(version_number)

        version_calls: list[tuple] = []
        cur4 = _cur()

        def capture(sql, params=None):
            version_calls.append(params)

        cur4.execute.side_effect = capture

        conn = MagicMock()
        conn.cursor.side_effect = [
            _make_ctx(cur1), _make_ctx(cur2), _make_ctx(cur3), _make_ctx(cur4),
        ]

        with um.patch("core.audit.write_audit_row") as mock_audit:
            result = update_target_field(
                "clicks",
                {"display_name": "Clics (MaJ)", "description": "new"},
                conn,
                identity="jean@test",
            )

        assert result["display_name"] == "Clics (MaJ)"
        assert len(version_calls) == 1
        row = _version_row_dict(version_calls[0])
        assert row["version_number"] == 1  # next version_number (MAX was 0)
        assert row["change_kind"] == "updated"
        assert row["changed_by"] == "jean@test"  # never "system"

        diff = json.loads(row["diff"])
        assert diff["display_name"] == {"before": "Clics", "after": "Clics (MaJ)"}
        assert diff["description"] == {"before": "old", "after": "new"}

        # Audit row also carries the real identity, never "system".
        mock_audit.assert_called_once()
        assert mock_audit.call_args.kwargs["identity"] == "jean@test"

    def test_empty_patch_appends_no_version(self):
        """A no-op patch (empty dict) touches only the BEFORE-fetch cursor."""
        from core.datamodel import update_target_field

        current = _tf_row("clicks")
        cur1 = _cur(fetchone_value=current, description=_TF_GOVERNANCE_COLS)

        conn = MagicMock()
        conn.cursor.side_effect = [_make_ctx(cur1)]

        result = update_target_field("clicks", {}, conn, identity="jean@test")

        assert result is not None
        assert conn.cursor.call_count == 1  # no UPDATE, no version, no MAX lookup
        conn.commit.assert_not_called()

    def test_patch_with_same_values_appends_no_version(self):
        """A patch whose values match the current row yields an empty diff -> no version."""
        from core.datamodel import update_target_field

        row = _tf_row("clicks", display_name="Clics")
        cur1 = _cur(fetchone_value=row, description=_TF_GOVERNANCE_COLS)
        cur2 = _cur(fetchone_value=row, description=_TF_GOVERNANCE_COLS)

        conn = MagicMock()
        conn.cursor.side_effect = [_make_ctx(cur1), _make_ctx(cur2)]

        with um.patch("core.audit.write_audit_row"):
            update_target_field("clicks", {"display_name": "Clics"}, conn, identity="jean@test")

        # Only 2 cursors consumed: BEFORE-select + UPDATE. No MAX/INSERT version cursors.
        assert conn.cursor.call_count == 2

    def test_restored_from_hint_sets_change_kind_restored(self):
        from core.datamodel import update_target_field

        before = _tf_row("clicks", display_name="Clics")
        after = _tf_row("clicks", display_name="Clics (v1)")

        cur1 = _cur(fetchone_value=before, description=_TF_GOVERNANCE_COLS)
        cur2 = _cur(fetchone_value=after, description=_TF_GOVERNANCE_COLS)
        cur3 = _cur(fetchone_value=(3,))

        version_calls: list[tuple] = []
        cur4 = _cur()

        def capture(sql, params=None):
            version_calls.append(params)

        cur4.execute.side_effect = capture

        conn = MagicMock()
        conn.cursor.side_effect = [
            _make_ctx(cur1), _make_ctx(cur2), _make_ctx(cur3), _make_ctx(cur4),
        ]

        with um.patch("core.audit.write_audit_row"):
            update_target_field(
                "clicks",
                {"display_name": "Clics (v1)", "restored_from": 1},
                conn,
                identity="jean@test",
            )

        assert version_calls
        row = _version_row_dict(version_calls[0])
        assert row["version_number"] == 4  # next version_number (MAX was 3)
        assert row["change_kind"] == "restored"
        diff = json.loads(row["diff"])
        assert diff["_restored_from"] == 1

    def test_patch_on_deleted_field_returns_none(self):
        """Fix-list finding #4: PATCHing a soft-deleted field is a 404, not a
        silent resurrection -- returns None before any UPDATE is issued."""
        from core.datamodel import update_target_field

        deleted_row = _tf_row("old_kpi", status="deleted")
        cur1 = _cur(fetchone_value=deleted_row, description=_TF_GOVERNANCE_COLS)

        conn = MagicMock()
        conn.cursor.side_effect = [_make_ctx(cur1)]

        result = update_target_field(
            "old_kpi", {"display_name": "New name"}, conn, identity="jean@test",
        )

        assert result is None
        assert conn.cursor.call_count == 1  # only the BEFORE-select, no UPDATE
        conn.commit.assert_not_called()


# ---------------------------------------------------------------------------
# approve_target_field -> 'approved' version; idempotent re-approve = no-op
# ---------------------------------------------------------------------------


class TestApproveAppendsVersion:
    def test_approve_appends_version_approved(self):
        from core.datamodel import approve_target_field

        draft = _tf_row("clicks", status="draft")
        approved = _tf_row("clicks", status="approved", approved_by="jean@test")

        cur1 = _cur(fetchone_value=draft, description=_TF_GOVERNANCE_COLS)
        cur2 = _cur(fetchone_value=approved, description=_TF_GOVERNANCE_COLS)
        cur3 = _cur()  # INSERT target_field_approvals
        cur4 = _cur(fetchone_value=(0,))  # MAX(version_number)

        version_calls: list[tuple] = []
        cur5 = _cur()

        def capture(sql, params=None):
            version_calls.append(params)

        cur5.execute.side_effect = capture

        conn = MagicMock()
        conn.cursor.side_effect = [
            _make_ctx(cur1), _make_ctx(cur2), _make_ctx(cur3), _make_ctx(cur4), _make_ctx(cur5),
        ]

        approve_target_field("clicks", "jean@test", conn)

        assert len(version_calls) == 1
        row = _version_row_dict(version_calls[0])
        assert row["version_number"] == 1
        assert row["change_kind"] == "approved"
        assert row["changed_by"] == "jean@test"

    def test_idempotent_reapprove_appends_nothing(self):
        """Re-approving an already-approved field appends NO version row."""
        from core.datamodel import approve_target_field

        already_approved = _tf_row("clicks", status="approved", approved_by="original@test")
        cur1 = _cur(fetchone_value=already_approved, description=_TF_GOVERNANCE_COLS)

        conn = MagicMock()
        conn.cursor.side_effect = [_make_ctx(cur1)]

        result = approve_target_field("clicks", "second@test", conn)

        assert result["approved_by"] == "original@test"
        assert conn.cursor.call_count == 1  # only the SELECT -- no UPDATE, no version
        conn.commit.assert_not_called()

    def test_approve_deleted_field_returns_none(self):
        """Fix-list finding #2: approving a soft-deleted field is a 404, not a
        resurrection -- returns None before any UPDATE/approvals/version write."""
        from core.datamodel import approve_target_field

        deleted_row = _tf_row("old_kpi", status="deleted")
        cur1 = _cur(fetchone_value=deleted_row, description=_TF_GOVERNANCE_COLS)

        conn = MagicMock()
        conn.cursor.side_effect = [_make_ctx(cur1)]

        result = approve_target_field("old_kpi", "jean@test", conn)

        assert result is None
        assert conn.cursor.call_count == 1  # only the SELECT
        conn.commit.assert_not_called()


# ---------------------------------------------------------------------------
# delete_target_field -> soft delete + 'deleted' version + real identity
# ---------------------------------------------------------------------------


class TestDeleteAppendsVersion:
    def test_delete_soft_deletes_and_appends_version_deleted(self):
        from core.datamodel import delete_target_field

        after = _tf_row("old_kpi", status="deleted")
        cur1 = _cur(fetchone_value=("old_kpi", False, 0, "approved"))
        cur2 = _cur(fetchone_value=after, description=_TF_GOVERNANCE_COLS)
        cur3 = _cur(fetchone_value=(2,))

        version_calls: list[tuple] = []
        cur4 = _cur()

        def capture(sql, params=None):
            version_calls.append(params)

        cur4.execute.side_effect = capture

        conn = MagicMock()
        conn.cursor.side_effect = [
            _make_ctx(cur1), _make_ctx(cur2), _make_ctx(cur3), _make_ctx(cur4),
        ]

        with um.patch("core.audit.write_audit_row") as mock_audit:
            delete_target_field("old_kpi", "jean@test", conn)

        assert len(version_calls) == 1
        row = _version_row_dict(version_calls[0])
        assert row["version_number"] == 3  # next version (MAX was 2)
        assert row["change_kind"] == "deleted"
        assert row["changed_by"] == "jean@test"
        mock_audit.assert_called_once()
        assert mock_audit.call_args.kwargs["identity"] == "jean@test"
        assert mock_audit.call_args.kwargs["action"] == "target_field.deleted"
        conn.commit.assert_called_once()

        # The existence/is_default/used_by_count/status guard SELECT locks the
        # row (finding #11): assert FOR UPDATE is actually part of that query.
        guard_sql = cur1.execute.call_args[0][0]
        assert "FOR UPDATE" in guard_sql


# ---------------------------------------------------------------------------
# Soft-delete visibility: list/get exclude status='deleted'
# ---------------------------------------------------------------------------


class TestSoftDeleteVisibility:
    def test_list_target_fields_excludes_deleted(self):
        from core.datamodel import list_target_fields

        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []
        cur.description = [(c,) for c in [*_TF_GOVERNANCE_COLS[:9], "used_by_count"]]

        executed_sqls: list[str] = []

        def capture(sql, params=None):
            executed_sqls.append(sql)

        cur.execute.side_effect = capture

        list_target_fields(conn)

        assert executed_sqls
        assert "status != 'deleted'" in executed_sqls[0]

    def test_get_target_field_excludes_deleted(self):
        from core.datamodel import get_target_field

        cur1 = MagicMock()
        cur1.__enter__ = MagicMock(return_value=cur1)
        cur1.__exit__ = MagicMock(return_value=False)
        cur1.fetchone.return_value = None  # deleted -> excluded by WHERE clause -> no row

        executed_sqls: list[str] = []

        def capture(sql, params=None):
            executed_sqls.append(sql)

        cur1.execute.side_effect = capture

        conn = MagicMock()
        conn.cursor.side_effect = [_make_ctx(cur1)]

        result = get_target_field("deleted_field", conn)

        assert result is None
        assert "status != 'deleted'" in executed_sqls[0]


# ---------------------------------------------------------------------------
# Delete-then-recreate revives the row (change_kind='created' again), through
# the SAME MAX(version_number)+1 helper as the fresh path (finding #1) --
# the MAX below is deliberately non-zero so a regression to a hardcoded
# version_number=1 on the revival path fails this test.
# ---------------------------------------------------------------------------


class TestDeleteThenRecreate:
    def test_recreate_revives_deleted_row(self):
        """create_target_field on a name that is status='deleted' revives it."""
        from core.datamodel import create_target_field

        class FakeUniqueViolation(Exception):
            pass

        FakeUniqueViolation.__name__ = "UniqueViolation"

        # cur1: INSERT attempt -- raises unique violation
        cur1 = _cur()
        cur1.execute.side_effect = FakeUniqueViolation("duplicate key value")

        # cur2: SELECT status ... FOR UPDATE -> the name is soft-deleted
        cur2 = _cur(fetchone_value=("deleted",))

        # cur3: UPDATE ... RETURNING (revival)
        revived = _tf_row("old_name", status="draft", display_name="New content")
        cur3 = _cur(fetchone_value=revived, description=_TF_GOVERNANCE_COLS)

        # cur4: SELECT COALESCE(MAX(version_number), 0) -- this name already
        # carries 2 versions from before its soft-delete.
        cur4 = _cur(fetchone_value=(2,))

        # cur5: INSERT INTO target_fields_versions (change_kind='created')
        version_calls: list[tuple] = []
        cur5 = _cur()

        def capture(sql, params=None):
            version_calls.append(params)

        cur5.execute.side_effect = capture

        conn = _noop_transaction_conn()
        conn.cursor.side_effect = [
            _make_ctx(cur1), _make_ctx(cur2), _make_ctx(cur3), _make_ctx(cur4), _make_ctx(cur5),
        ]

        result = create_target_field(
            {
                "name": "old_name", "display_name": "New content",
                "data_type": "integer", "field_kind": "metric", "measure": "sum",
            },
            created_by="jean@test",
            conn=conn,
        )

        assert result["display_name"] == "New content"
        assert result["status"] == "draft"
        assert len(version_calls) == 1
        row = _version_row_dict(version_calls[0])
        # This is the assertion that would have caught finding #1: a hardcoded
        # version_number=1 on the revival path would produce 1 here, not the
        # real MAX(2)+1 = 3.
        assert row["version_number"] == 3
        assert row["change_kind"] == "created"
        assert row["changed_by"] == "jean@test"
        conn.commit.assert_called_once()

    def test_recreate_over_live_field_raises_duplicate_field_error(self):
        """create_target_field on a LIVE (non-deleted) name raises DuplicateFieldError.

        Pinned to the DuplicateFieldError subclass (not bare ValueError): the
        API maps it to HTTP 409 (route contract) while plain ValueError maps
        to 422 -- a bare-ValueError raise here would silently regress the
        documented 409 on duplicate names (44.7 re-review).
        """
        from core.datamodel import DuplicateFieldError, create_target_field

        class FakeUniqueViolation(Exception):
            pass

        FakeUniqueViolation.__name__ = "UniqueViolation"

        cur1 = _cur()
        cur1.execute.side_effect = FakeUniqueViolation("duplicate key value")
        cur2 = _cur(fetchone_value=("approved",))  # NOT deleted -> genuine duplicate

        conn = _noop_transaction_conn()
        conn.cursor.side_effect = [_make_ctx(cur1), _make_ctx(cur2)]

        with pytest.raises(DuplicateFieldError, match="existe deja"):
            create_target_field(
                {
                    "name": "clicks", "display_name": "X",
                    "data_type": "integer", "field_kind": "metric", "measure": "sum",
                },
                created_by="jean@test",
                conn=conn,
            )


# ---------------------------------------------------------------------------
# list_field_versions -- reads history regardless of the parent row's status
# ---------------------------------------------------------------------------


class TestListFieldVersions:
    def test_returns_all_versions_newest_first(self):
        from core.datamodel import list_field_versions

        cols = [
            "name", "version_number", "display_name", "data_type", "field_kind",
            "measure", "description", "created_by", "is_default", "created_at",
            "status", "approved_at", "approved_by", "updated_at",
            "change_kind", "diff", "changed_by", "changed_at",
        ]
        rows = [
            (
                "clicks", 4, "Clics", "integer", "metric", "sum", None, "u",
                False, _NOW, "deleted", None, None, _NOW, "deleted", None,
                "jean@test", _NOW,
            ),
            (
                "clicks", 3, "Clics", "integer", "metric", "sum", None, "u",
                False, _NOW, "approved", _NOW, "jean@test", _NOW, "approved", None,
                "jean@test", _NOW,
            ),
        ]

        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = rows
        cur.description = [(c,) for c in cols]

        conn = MagicMock()
        conn.cursor.side_effect = [_make_ctx(cur)]

        result = list_field_versions("clicks", conn)

        assert len(result) == 2
        assert result[0]["change_kind"] == "deleted"
        assert result[0]["version_number"] == 4
        assert result[1]["change_kind"] == "approved"


# ---------------------------------------------------------------------------
# upsert_mapping -- fix-list finding #8: cannot bind to a soft-deleted target
# ---------------------------------------------------------------------------


class TestUpsertMappingRejectsDeletedTarget:
    def test_upsert_mapping_rejects_deleted_target_field(self):
        from core.datamodel import upsert_mapping

        cur1 = _cur(fetchone_value=("deleted",))  # SELECT status FROM target_fields

        conn = MagicMock()
        conn.cursor.side_effect = [_make_ctx(cur1)]

        with pytest.raises(ValueError, match="supprime"):
            upsert_mapping("ds_1", "clicks_raw", "old_kpi", "jean@test", conn)

        # No mapping INSERT/UPSERT was attempted, no commit.
        assert conn.cursor.call_count == 1
        conn.commit.assert_not_called()

    def test_upsert_mapping_allows_live_target_field(self):
        from core.datamodel import upsert_mapping

        cur1 = _cur(fetchone_value=("approved",))  # SELECT status -- live field
        cur2 = _cur(fetchone_value=None)  # SELECT current target_field -- none yet (map)
        cur3 = _cur(
            fetchone_value=("ds_1", "clicks_raw", "clicks", False, _NOW),
            description=[
                (c,) for c in
                ["datastream_id", "source_field", "target_field", "is_key_column", "created_at"]
            ],
        )

        conn = MagicMock()
        conn.cursor.side_effect = [_make_ctx(cur1), _make_ctx(cur2), _make_ctx(cur3)]

        with um.patch("core.audit.write_audit_row"):
            result = upsert_mapping("ds_1", "clicks_raw", "clicks", "jean@test", conn)

        assert result["target_field"] == "clicks"
        conn.commit.assert_called_once()

    def test_upsert_mapping_unmap_skips_deleted_check(self):
        """target_field_name=None (unmap) never queries target_fields status at all."""
        from core.datamodel import upsert_mapping

        cur1 = _cur(fetchone_value=("clicks",))  # before: currently "clicks"
        cur2 = _cur(
            fetchone_value=("ds_1", "clicks_raw", None, False, _NOW),
            description=[
                (c,) for c in
                ["datastream_id", "source_field", "target_field", "is_key_column", "created_at"]
            ],
        )
        cur3 = _cur(fetchone_value=("proj_1",))  # audit project lookup (44.9)

        conn = MagicMock()
        conn.cursor.side_effect = [_make_ctx(cur1), _make_ctx(cur2), _make_ctx(cur3)]

        with um.patch("core.audit.write_audit_row"):
            result = upsert_mapping("ds_1", "clicks_raw", None, "jean@test", conn)

        assert result["target_field"] is None
        # before-select + upsert + audit project lookup -- but NO
        # target_fields status lookup (that guard only runs when mapping TO a
        # field, never on unmap).
        assert conn.cursor.call_count == 3


# ---------------------------------------------------------------------------
# Story 44.9 -- mapping mutation audit: before/after captured, no-op skipped.
# ---------------------------------------------------------------------------


class TestUpsertMappingAuditBeforeAfter:
    def test_map_from_null_writes_before_null_after_value(self):
        """A -> null was the prior state; a fresh map (null -> A) audits before=null."""
        from core.datamodel import upsert_mapping

        # target_field_name is not None -> the 44.7 deleted-target guard's
        # SELECT status runs FIRST (cur0), then the before-SELECT, then the upsert.
        cur0 = _cur(fetchone_value=("approved",))  # deleted-guard: live field
        cur1 = _cur(fetchone_value=None)  # no existing row: before = null
        cur2 = _cur(
            fetchone_value=("ds_1", "clicks_raw", "clicks", False, _NOW),
            description=[
                (c,) for c in
                ["datastream_id", "source_field", "target_field", "is_key_column", "created_at"]
            ],
        )
        conn = MagicMock()
        conn.cursor.side_effect = [_make_ctx(cur0), _make_ctx(cur1), _make_ctx(cur2)]

        with um.patch("core.audit.write_audit_row") as mock_write:
            upsert_mapping("ds_1", "clicks_raw", "clicks", "jean@test", conn)

        mock_write.assert_called_once()
        metadata = mock_write.call_args.kwargs["metadata"]
        assert metadata["datastream_id"] == "ds_1"
        assert metadata["source_field"] == "clicks_raw"
        assert metadata["before"] is None
        assert metadata["after"] == "clicks"
        assert metadata["changed"] is True

    def test_remap_writes_before_a_after_b(self):
        """A -> B: before/after both captured, real prior value not just the new one."""
        from core.datamodel import upsert_mapping

        cur0 = _cur(fetchone_value=("approved",))  # deleted-guard: live field
        cur1 = _cur(fetchone_value=("clicks_old",))  # before = clicks_old
        cur2 = _cur(
            fetchone_value=("ds_1", "clicks_raw", "clicks_new", False, _NOW),
            description=[
                (c,) for c in
                ["datastream_id", "source_field", "target_field", "is_key_column", "created_at"]
            ],
        )
        cur3 = _cur(fetchone_value=("proj_1",))  # project lookup for audit metadata
        conn = MagicMock()
        conn.cursor.side_effect = [
            _make_ctx(cur0), _make_ctx(cur1), _make_ctx(cur2), _make_ctx(cur3),
        ]

        with um.patch("core.audit.write_audit_row") as mock_write:
            upsert_mapping("ds_1", "clicks_raw", "clicks_new", "jean@test", conn)

        metadata = mock_write.call_args.kwargs["metadata"]
        assert metadata["before"] == "clicks_old"
        assert metadata["after"] == "clicks_new"
        assert metadata["changed"] is True
        # 44.9 re-review: the owning project id is carried so a future scoped
        # "last mapping change" surface has a filter key.
        assert metadata["project_id"] == "proj_1"
        assert mock_write.call_args.kwargs["identity"] == "jean@test"

    def test_unmap_writes_before_b_after_null(self):
        """B -> null: unmap audits the real prior target, not just null -> null."""
        from core.datamodel import upsert_mapping

        cur1 = _cur(fetchone_value=("clicks",))  # before = clicks
        cur2 = _cur(
            fetchone_value=("ds_1", "clicks_raw", None, False, _NOW),
            description=[
                (c,) for c in
                ["datastream_id", "source_field", "target_field", "is_key_column", "created_at"]
            ],
        )
        conn = MagicMock()
        conn.cursor.side_effect = [_make_ctx(cur1), _make_ctx(cur2)]

        with um.patch("core.audit.write_audit_row") as mock_write:
            upsert_mapping("ds_1", "clicks_raw", None, "jean@test", conn)

        metadata = mock_write.call_args.kwargs["metadata"]
        assert metadata["before"] == "clicks"
        assert metadata["after"] is None
        assert metadata["changed"] is True

    def test_noop_upsert_writes_no_audit_row(self):
        """A -> A: before == after, so no audit row is written at all."""
        from core.datamodel import upsert_mapping

        cur0 = _cur(fetchone_value=("approved",))  # deleted-guard: live field
        cur1 = _cur(fetchone_value=("clicks",))  # before = clicks (same)
        cur2 = _cur(
            fetchone_value=("ds_1", "clicks_raw", "clicks", False, _NOW),
            description=[
                (c,) for c in
                ["datastream_id", "source_field", "target_field", "is_key_column", "created_at"]
            ],
        )
        conn = MagicMock()
        conn.cursor.side_effect = [_make_ctx(cur0), _make_ctx(cur1), _make_ctx(cur2)]

        with um.patch("core.audit.write_audit_row") as mock_write:
            result = upsert_mapping("ds_1", "clicks_raw", "clicks", "jean@test", conn)

        assert result["target_field"] == "clicks"
        mock_write.assert_not_called()
        conn.commit.assert_called_once()  # the upsert itself still happens


# ---------------------------------------------------------------------------
# Pg-gated end-to-end lifecycle test (skipped when TEST_POSTGRES_DSN unset).
# Pattern copied from server/tests/core/test_dimension_lineage.py's
# pg_available fixture.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MIGRATION_050 = _REPO_ROOT / "infra" / "nango" / "migrations" / "050_target_fields_governance.sql"
_MIGRATION_107 = _REPO_ROOT / "infra" / "nango" / "migrations" / "107_target_fields_versions.sql"


def _pg_reachable() -> bool:
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False
    try:
        import psycopg

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pg_available = pytest.mark.skipif(not _pg_reachable(), reason="platform Postgres not reachable")


def _apply_migration(conn, path: Path) -> None:
    with conn.cursor() as cur:
        cur.execute(path.read_text(encoding="utf-8"))
    conn.commit()


@pg_available
def test_live_lifecycle_create_patch_approve_delete_recreate():
    """AC (Story 44.7): create -> patch -> approve -> delete -> recreate by
    identity 'jean' yields exactly 5 version rows (created/updated/approved/
    deleted/created), all changed_by='jean', newest first, with the update
    row's diff holding exact before/after values.
    """
    from core.datamodel import (
        approve_target_field,
        create_target_field,
        delete_target_field,
        list_field_versions,
        update_target_field,
    )
    from core.db import get_connection

    name = f"tf44_{uuid.uuid4().hex[:8]}"

    with get_connection() as conn:
        _apply_migration(conn, _MIGRATION_050)
        _apply_migration(conn, _MIGRATION_107)

        try:
            create_target_field(
                {
                    "name": name, "display_name": "Test Field", "data_type": "integer",
                    "field_kind": "metric", "measure": "sum",
                },
                created_by="jean",
                conn=conn,
            )
            update_target_field(
                name, {"display_name": "Test Field v2"}, conn, identity="jean",
            )
            approve_target_field(name, "jean", conn)
            delete_target_field(name, "jean", conn)
            create_target_field(
                {
                    "name": name, "display_name": "Revived Field", "data_type": "integer",
                    "field_kind": "metric", "measure": "sum",
                },
                created_by="jean",
                conn=conn,
            )

            versions = list_field_versions(name, conn)

            assert len(versions) == 5
            assert all(v["changed_by"] == "jean" for v in versions)
            # newest first
            assert [v["version_number"] for v in versions] == [5, 4, 3, 2, 1]
            assert [v["change_kind"] for v in versions] == [
                "created", "deleted", "approved", "updated", "created",
            ]

            update_version = next(v for v in versions if v["change_kind"] == "updated")
            diff = update_version["diff"]
            assert diff["display_name"] == {
                "before": "Test Field", "after": "Test Field v2",
            }
        finally:
            # Cleanup: the append-only trigger (finding #10) blocks the
            # ON DELETE CASCADE from app.target_fields too, since a cascade
            # delete is still a DELETE on target_fields_versions -- disable
            # user triggers for this test-only teardown.
            with conn.cursor() as cur:
                cur.execute(
                    "ALTER TABLE app.target_fields_versions DISABLE TRIGGER USER"
                )
                cur.execute(
                    "DELETE FROM app.target_fields_versions WHERE name = %s", (name,)
                )
                cur.execute("DELETE FROM app.target_fields WHERE name = %s", (name,))
                cur.execute(
                    "ALTER TABLE app.target_fields_versions ENABLE TRIGGER USER"
                )
            conn.commit()
