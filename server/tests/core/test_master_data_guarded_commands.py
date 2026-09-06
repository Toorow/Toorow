"""Story 49.2 -- archiving a Master Data node is impact-guarded.

`register_used_by` has written `app.master_data_used_by` since the subsystem
landed. Nothing read it. `archive_node` therefore retired identities that live
Datastream mappings, Analyze filters and Context links still pointed at, and the
only symptom was that those references quietly stopped resolving.

The property these tests hold is the fail-closed one: an unreadable used-by store
must raise, never return "no dependents". `MasterDataUnavailable` exists in this
module precisely so an outage cannot be mistaken for a clear path -- it was
declared long before anything raised it.
"""

from __future__ import annotations

import pytest
from core.master_data import (
    MasterDataConflict,
    MasterDataNotFound,
    MasterDataUnavailable,
    archive_node,
    archive_org_node_guarded,
    assess_node_impact,
    assess_org_node_impact,
    restore_node,
)

PROJECT = "proj_EXAMPLE"
NODE = "mdn_1"

_NODE_ROW = (
    NODE, "org_EXAMPLE", PROJECT, "reg_1", "market", "France",
    None, None, "person_1", None, None,
)


class _Cursor:
    def __init__(self, results: list, *, explode: bool = False):
        self._results = list(results)
        self._current: list = []
        self.statements: list[str] = []
        self._explode = explode

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> None:
        if self._explode:
            raise RuntimeError("used-by store offline")
        self.statements.append(" ".join(sql.split()))
        self._current = self._results.pop(0) if self._results else []

    def fetchall(self) -> list:
        return self._current

    def fetchone(self):
        return self._current[0] if self._current else None


class _Conn:
    def __init__(self, results: list, *, explode: bool = False):
        self.cur = _Cursor(results, explode=explode)

    def cursor(self) -> _Cursor:
        return self.cur


# --- The guard -------------------------------------------------------------


def test_archiving_a_node_with_live_consumers_is_refused_and_names_them() -> None:
    conn = _Conn([[("datastream_mapping", "ds_1", "Meta Ads", None)]])

    with pytest.raises(MasterDataConflict) as exc:
        archive_node(conn, project_id=PROJECT, node_id=NODE)

    assert "still used by" in str(exc.value)
    assert "datastream_mapping (1)" in str(exc.value)
    # And nothing was written: the refusal came before the UPDATE.
    assert not any("UPDATE" in s for s in conn.cur.statements)


def test_an_unreadable_used_by_store_fails_closed_instead_of_archiving() -> None:
    """The property this file exists for: an outage is not "no dependents"."""
    conn = _Conn([], explode=True)

    with pytest.raises(MasterDataUnavailable) as exc:
        archive_node(conn, project_id=PROJECT, node_id=NODE)

    assert "could not be read" in str(exc.value)
    assert not conn.cur.statements


def test_a_node_nobody_uses_archives_without_ceremony() -> None:
    conn = _Conn([[], [_NODE_ROW]])

    node = archive_node(conn, project_id=PROJECT, node_id=NODE)

    assert node["id"] == NODE
    assert any("UPDATE" in s for s in conn.cur.statements)


def test_an_acknowledged_impact_proceeds_and_stays_an_explicit_decision() -> None:
    conn = _Conn([[("analyze_filter", "flt_1", "Markets", None)], [_NODE_ROW]])

    node = archive_node(conn, project_id=PROJECT, node_id=NODE, acknowledge_impact=True)

    assert node["id"] == NODE
    assert any("UPDATE" in s for s in conn.cur.statements)


def test_the_impact_report_groups_consumers_by_kind() -> None:
    conn = _Conn(
        [
            [
                ("datastream_mapping", "ds_1", "Meta", None),
                ("datastream_mapping", "ds_2", "TikTok", None),
                ("context_link", "ctx_1", "Retail", None),
            ]
        ]
    )

    impact = assess_node_impact(conn, project_id=PROJECT, node_id=NODE)

    assert impact.is_clear is False
    assert impact.describe() == "context_link (1), datastream_mapping (2)"


def test_a_released_consumer_does_not_block_because_the_query_excludes_it() -> None:
    """Released rows are excluded in SQL, so the guard reads only live use."""
    conn = _Conn([[]])

    impact = assess_node_impact(conn, project_id=PROJECT, node_id=NODE)

    assert impact.is_clear is True
    assert "released_at IS NULL" in conn.cur.statements[0]


# --- Restore ---------------------------------------------------------------


def test_restore_brings_the_node_back_under_its_original_id() -> None:
    conn = _Conn([[_NODE_ROW]])

    node = restore_node(conn, project_id=PROJECT, node_id=NODE)

    assert node["id"] == NODE  # restored, not recreated under a new ID
    assert "archived_at = NULL" in conn.cur.statements[0]
    assert "archived_at IS NOT NULL" in conn.cur.statements[0]


def test_restoring_a_node_that_was_never_archived_is_reported_not_silently_ok() -> None:
    conn = _Conn([[]])

    with pytest.raises(MasterDataNotFound) as exc:
        restore_node(conn, project_id=PROJECT, node_id=NODE)

    assert "archived node" in str(exc.value)


# --- The organization twin (2026-08-25) ------------------------------------
#
# A converged Business Domain is an ORGANIZATION node: `project_id IS NULL`.
# `assess_node_impact`'s `WHERE project_id = %s` therefore matches nothing and
# reports an identity nobody depends on -- the silent zero this whole file
# exists to refuse, reintroduced by scope rather than by an outage. The two
# tests below hold the property that makes the organization read a guard and not
# a second empty answer.


def test_the_organization_read_scopes_by_the_node_not_by_a_project() -> None:
    conn = _Conn([[("semantic-view", "sv_1", "Revenue", "svv_1")]])

    impact = assess_org_node_impact(conn, org_id="org_EXAMPLE", node_id="bd_1")

    assert impact.describe() == "semantic-view (1)"
    statement = conn.cur.statements[0]
    # The organization is proved through the NODE, never trusted from the
    # caller, and a Project-less node is what it looks for.
    assert "JOIN app.master_data_nodes" in statement
    assert "n.project_id IS NULL" in statement
    assert "u.released_at IS NULL" in statement


def test_an_unreadable_store_blocks_the_organization_archive_rather_than_clearing_it(
) -> None:
    conn = _Conn([], explode=True)

    with pytest.raises(MasterDataUnavailable):
        archive_org_node_guarded(conn, org_id="org_EXAMPLE", node_id="bd_1")


def test_an_acknowledged_organization_archive_proceeds_and_an_unacknowledged_one_does_not(
) -> None:
    consumers = [("semantic-view", "sv_1", "Revenue", "svv_1")]

    with pytest.raises(MasterDataConflict) as exc:
        archive_org_node_guarded(conn := _Conn([consumers]), org_id="org_E", node_id="bd_1")
    assert "semantic-view (1)" in str(exc.value)
    # The refusal came BEFORE the UPDATE: one statement was run, the read.
    assert len(conn.cur.statements) == 1

    conn = _Conn([consumers, [_NODE_ROW]])
    node = archive_org_node_guarded(
        conn, org_id="org_E", node_id="bd_1", acknowledge_impact=True
    )
    assert node["id"] == NODE
