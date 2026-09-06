"""Unit tests for core.org_purge -- the org tenant-tree erasure planner.

No database: the FK graph is injected through a fake connection, so the tests
pin the PLANNING rules (order, cycle breaking, preserved ledgers, bounds)
rather than a snapshot of the current schema.
"""

from __future__ import annotations

import pytest
from core import org_purge

from tests.support.statement_router import (
    StatementInventory,
    UnknownStatement,
    describe,
)

#: EVERY statement `core.org_purge` issues, named, in the order an if/elif
#: chain would test them (first match wins). Read off the PRODUCT, not off the
#: chain this replaces: that chain named two of the five -- the two catalog
#: reads of `_load_graph` (org_purge.py:147 and :158) -- and let the other three
#: fall into an `else` that answered "no rows" and recorded the text. Those
#: three are the statements the purge is MADE of: the transaction flag
#: (org_purge.py:355), the tenant DELETE (:287 and :309) and the cycle-breaking
#: UPDATE (:186, emitted at :297).
#:
#: The tenant statements are matched on their SHAPE, not on a table name: the
#: table is whatever the injected FK graph says, so a fragment naming one would
#: only ever match the fixture that invented it.
_ORG_PURGE = StatementInventory(
    "_FakeCursor (core.org_purge)",
    fk_graph="from pg_constraint c",
    nullable_columns="not a.attisdropped",
    flag_the_erasure="set local app.rgpd_erasure",
    detach_back_reference=("update ", "= null where "),
    delete_tenant_rows="delete from ",
)

#: The one projection `describe` refuses to derive: `_FK_GRAPH_SQL` selects two
#: correlated `array_agg` sub-selects, and a router that guessed a column list
#: out of that would be parsing SQL. Named here, once, exactly where the
#: derivation stops -- everywhere else the description comes from the statement.
_NAMED_DESCRIPTION = {
    "fk_graph": [
        ("conname",),
        ("child_table",),
        ("parent_table",),
        ("child_cols",),
        ("parent_cols",),
    ],
}

#: Statements that return NO RESULT SET. `description = None` is what psycopg
#: reserves for exactly these -- a SET, and DML with no RETURNING.
_NO_RESULT_SET = frozenset(
    {"flag_the_erasure", "detach_back_reference", "delete_tenant_rows"}
)


class _FakeCursor:
    """The catalog and the tenant statements -- and a refusal for anything else.

    AI-317: an unrecognized statement is NAMED, never answered. The silence it
    replaces was load-bearing here: `purge_org_tree` reads `cur.rowcount` after
    every planned statement, so a DELETE that stopped matching would still have
    been counted as one erased row and the audit totals would have stayed green.
    """

    def __init__(self, fk_rows, null_rows, executed):
        self._fk_rows = fk_rows
        self._null_rows = null_rows
        self._executed = executed
        self._result: list = []
        self.description: list[tuple[str]] | None = None
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        statement = _ORG_PURGE.match(sql)
        if statement in _NO_RESULT_SET:
            self.description = None
        else:
            self.description = _NAMED_DESCRIPTION.get(statement) or describe(sql)
        match statement:
            case "fk_graph":
                self._result = self._fk_rows
                self.rowcount = len(self._fk_rows)
            case "nullable_columns":
                self._result = self._null_rows
                self.rowcount = len(self._null_rows)
            case "flag_the_erasure":
                # A SET affects no row and psycopg reports -1. The purge does
                # not read it here; what the test reads is the ORDER, so the
                # statement is recorded like the tenant ones.
                self._executed.append(sql)
                self._result = []
                self.rowcount = -1
            case "detach_back_reference" | "delete_tenant_rows":
                # One row per planned statement, which is what lets the tests
                # tell an erased row from a detached reference.
                self._executed.append(sql)
                self._result = []
                self.rowcount = 1
            case _:  # pragma: no cover - a name added to the inventory, unanswered
                raise _ORG_PURGE.unknown(sql)

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


def test_cycle_with_no_nullable_column_is_deleted_not_refused():
    """A back-reference that cannot be detached is DELETED, not refused.

    This test asserted the opposite until 2026-08-03, and the refusal it pinned
    made the whole endpoint unreachable: `fk_master_data_nodes_registry_any_scope`
    is `registry_id NOT NULL ... ON DELETE RESTRICT` (migration 140:194), so
    `plan_purge` raised on EVERY organization -- measured on an org with zero
    `master_data_nodes` rows and on an org_id that does not exist. No
    organization could be erased, which is the RGPD path.

    Deleting is the coherent action and not a widening: a NOT NULL foreign key
    says the child is meaningless without its parent, and the parent is already
    queued for deletion on the same path. The statement stays scoped to the
    tenant by the same predicate every other statement uses.
    """
    conn = _conn(
        [
            ("fk_a_org", "app.a", ORG, ["org_id"], ["id"]),
            ("fk_b_a", "app.b", "app.a", ["a_id"], ["id"]),
            ("fk_a_b", "app.a", "app.b", ["b_id"], ["id"]),
        ],
        {("app.a", "b_id"): False},
    )
    plan = org_purge.plan_purge(conn, "org_x")

    cut = [op for op in plan if op.conname == "fk_a_b"]
    assert len(cut) == 1
    assert cut[0].kind == "delete"
    assert cut[0].sql.startswith("DELETE FROM app.a WHERE")
    # It runs BEFORE the ancestors' deletes -- that ordering is the whole reason
    # the branch exists, and a plan that emitted it last would trip the FK.
    assert plan.index(cut[0]) < min(
        i for i, op in enumerate(plan) if op.kind == "delete" and op.conname != "fk_a_b"
    )


def test_a_detachable_cycle_is_still_detached_not_deleted():
    """The nullable case must not have been swept up by the change above.

    `datastreams <-> datastream_executions` is broken by NULLing
    `current_published_execution_id`; turning that into a DELETE would erase
    executions the org still owns.
    """
    conn = _conn(
        [
            ("fk_a_org", "app.a", ORG, ["org_id"], ["id"]),
            ("fk_b_a", "app.b", "app.a", ["a_id"], ["id"]),
            ("fk_a_b", "app.a", "app.b", ["b_id"], ["id"]),
        ],
        {("app.a", "b_id"): True},
    )
    plan = org_purge.plan_purge(conn, "org_x")
    cut = [op for op in plan if op.conname == "fk_a_b"]
    assert len(cut) == 1
    assert cut[0].kind == "null"


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
    # The fake cursor reports rowcount=1 for every tenant statement: 2 deletes,
    # 1 detach.
    assert result["total_rows"] == 2
    assert result["refs_detached"] == 1
    assert set(result["rows_by_table"]) == {"app.datastreams", "app.datastream_executions"}


def test_the_fake_refuses_a_statement_it_was_never_taught():
    """AI-317: an unknown statement is named, not answered with "no rows".

    The statement below is the plausible one: the endpoint that calls this
    module looks the organization up before it erases anything. Handed to the
    old fake it went to the `else`, which recorded it as a purge statement and
    reported `rowcount = 1` -- one more erased row in the RGPD audit trail, for
    a read.
    """
    cursor = _FakeCursor([], [], [])

    with pytest.raises(UnknownStatement) as raised:
        cursor.execute("SELECT id FROM app.organizations WHERE id = %s", ("org_x",))

    message = str(raised.value)
    assert "select id from app.organizations" in message
    assert "delete_tenant_rows" in message
