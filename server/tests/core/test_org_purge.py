"""Unit tests for core.org_purge -- the org tenant-tree erasure planner.

No database: the FK graph is injected through a fake connection, so the tests
pin the PLANNING rules (order, cycle breaking, preserved ledgers, bounds)
rather than a snapshot of the current schema.
"""

from __future__ import annotations

import pytest
from core import org_purge


class _FakeCursor:
    def __init__(self, fk_rows, null_rows, executed):
        self._fk_rows = fk_rows
        self._null_rows = null_rows
        self._executed = executed
        self._result: list = []
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if "pg_constraint" in sql:
            self._result = self._fk_rows
        elif "attisdropped" in sql:
            self._result = self._null_rows
        else:
            self._executed.append(sql)
            self._result = []

    def fetchall(self):
        return self._result


class _FakeConn:
    def __init__(self, fk_rows, null_rows):
        self._fk_rows = fk_rows
        self._null_rows = null_rows
        self.executed: list[str] = []

    def cursor(self):
        return _FakeCursor(self._fk_rows, self._null_rows, self.executed)


def _conn(edges, columns):
    """edges: (conname, child, parent, child_cols, parent_cols).

    columns: {(table, col): nullable}
    """
    null_rows = [
        (table.removeprefix("app."), col, not nullable)
        for (table, col), nullable in columns.items()
    ]
    return _FakeConn(list(edges), null_rows)


ORG = org_purge.ROOT_TABLE


def test_children_are_deleted_before_their_parent():
    conn = _conn(
        [
            ("fk_projects_org", "app.projects", ORG, ["org_id"], ["id"]),
            (
                "fk_ds_project",
                "app.datastreams",
                "app.projects",
                ["project_id"],
                ["id"],
            ),
        ],
        {},
    )
    plan = org_purge.plan_purge(conn, "org_x")
    tables = [op.table for op in plan]
    assert tables.index("app.datastreams") < tables.index("app.projects")
    assert all(op.kind == "delete" for op in plan)


def test_grandchild_predicate_is_scoped_to_the_org():
    conn = _conn(
        [
            ("fk_projects_org", "app.projects", ORG, ["org_id"], ["id"]),
            (
                "fk_ds_project",
                "app.datastreams",
                "app.projects",
                ["project_id"],
                ["id"],
            ),
        ],
        {},
    )
    ds = next(op for op in org_purge.plan_purge(conn, "org_x") if op.table == "app.datastreams")
    # The org id is never interpolated -- it stays a bound parameter at the root.
    assert "org_x" not in ds.sql
    assert ds.sql.count("%s") == 1
    assert "app.organizations WHERE id = %s" in ds.sql


def test_cycle_is_broken_by_nulling_only_the_nullable_column():
    """datastreams <-> datastream_executions, as in the real schema.

    The back-reference is composite and only its first column is nullable;
    under MATCH SIMPLE that single NULL is enough to disarm the FK.
    """
    conn = _conn(
        [
            ("fk_ds_org", "app.datastreams", ORG, ["org_id"], ["id"]),
            (
                "fk_exec_ds",
                "app.datastream_executions",
                "app.datastreams",
                ["datastream_id"],
                ["id"],
            ),
            (
                "fk_ds_current_exec",
                "app.datastreams",
                "app.datastream_executions",
                ["current_execution_id", "id", "project_id"],
                ["id", "datastream_id", "project_id"],
            ),
        ],
        {
            ("app.datastreams", "current_execution_id"): True,
            ("app.datastreams", "id"): False,
            ("app.datastreams", "project_id"): False,
        },
    )
    plan = org_purge.plan_purge(conn, "org_x")
    breaks = [op for op in plan if op.kind == "null"]
    assert len(breaks) == 1
    assert breaks[0].sql.startswith("UPDATE app.datastreams SET current_execution_id = NULL")
    assert "id = NULL" not in breaks[0].sql.replace("current_execution_id = NULL", "")
    # Every detach runs before any delete, or a delete could trip the FK.
    assert [op.kind for op in plan][: len(breaks)] == ["null"] * len(breaks)


def test_cycle_with_no_nullable_column_raises():
    conn = _conn(
        [
            ("fk_a_org", "app.a", ORG, ["org_id"], ["id"]),
            ("fk_b_a", "app.b", "app.a", ["a_id"], ["id"]),
            ("fk_a_b", "app.a", "app.b", ["b_id"], ["id"]),
        ],
        {("app.a", "b_id"): False},
    )
    with pytest.raises(RuntimeError, match="cannot break the cycle"):
        org_purge.plan_purge(conn, "org_x")


def test_blocking_fk_from_a_preserved_ledger_raises():
    """An append-only ledger can be neither deleted nor detached row by row."""
    conn = _conn(
        [
            ("fk_conn_org", "app.connection_ref", ORG, ["owner_org_id"], ["id"]),
            (
                "audit_log_connection_ref_fkey",
                "app.audit_log",
                "app.connection_ref",
                ["connection_ref"],
                ["id"],
            ),
        ],
        {("app.audit_log", "connection_ref"): True},
    )
    with pytest.raises(RuntimeError, match="app.audit_log is preserved"):
        org_purge.plan_purge(conn, "org_x")


def test_duplicate_paths_yield_one_statement_each():
    """host_preflights hangs off both the org and its projects."""
    conn = _conn(
        [
            ("fk_projects_org", "app.projects", ORG, ["org_id"], ["id"]),
            ("fk_hp_org", "app.host_preflights", ORG, ["org_id"], ["id"]),
            (
                "fk_hp_org_again",
                "app.host_preflights",
                ORG,
                ["org_id"],
                ["id"],
            ),
        ],
        {},
    )
    plan = org_purge.plan_purge(conn, "org_x")
    hp = [op for op in plan if op.table == "app.host_preflights"]
    assert len(hp) == 1


def test_runaway_graph_is_refused(monkeypatch):
    monkeypatch.setattr(org_purge, "MAX_DEPTH", 3)
    edges = [("fk_0", "app.t0", ORG, ["org_id"], ["id"])]
    edges += [
        (f"fk_{i}", f"app.t{i}", f"app.t{i - 1}", ["parent_id"], ["id"])
        for i in range(1, 8)
    ]
    conn = _conn(edges, {})
    with pytest.raises(RuntimeError, match="exceeded depth"):
        org_purge.plan_purge(conn, "org_x")


def test_purge_flags_the_transaction_before_touching_ledgers():
    conn = _conn([("fk_projects_org", "app.projects", ORG, ["org_id"], ["id"])], {})
    org_purge.purge_org_tree(conn, "org_x")
    assert conn.executed[0] == "SET LOCAL app.rgpd_erasure = 'on'"


def test_detached_references_are_not_counted_as_erased_rows():
    conn = _conn(
        [
            ("fk_ds_org", "app.datastreams", ORG, ["org_id"], ["id"]),
            (
                "fk_exec_ds",
                "app.datastream_executions",
                "app.datastreams",
                ["datastream_id"],
                ["id"],
            ),
            (
                "fk_ds_current_exec",
                "app.datastreams",
                "app.datastream_executions",
                ["current_execution_id"],
                ["id"],
            ),
        ],
        {("app.datastreams", "current_execution_id"): True},
    )
    result = org_purge.purge_org_tree(conn, "org_x")
    # The fake cursor reports rowcount=1 for every statement: 2 deletes, 1 detach.
    assert result["total_rows"] == 2
    assert result["refs_detached"] == 1
    assert set(result["rows_by_table"]) == {"app.datastreams", "app.datastream_executions"}
