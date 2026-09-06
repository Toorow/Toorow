"""Story 49.2 -- the two Master Data workbench facets answer for themselves.

The six `PENDING_TAB_OWNER` entries naming Story 49.2 were the honest record that
Hierarchy and Mappings & Aliases had no owner. These tests pin what replaced
them, and the property that matters most is the negative one: an object type
with no alias store reports `unavailable` with a reason, never an empty table.
An empty table reads as "this object has no aliases", which is a claim nobody
can make.
"""

from __future__ import annotations

from typing import Any

import pytest
from core.governance_read_model import (
    _enrich_master_data_object,
    _master_data_aliases,
    _master_data_hierarchy,
)

ORG = "org_EXAMPLE"


class _Cursor:
    """Replays a scripted result per executed statement, in order."""

    def __init__(self, results: list[list[tuple]]):
        self._results = list(results)
        self._current: list[tuple] = []
        self.statements: list[str] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.statements.append(" ".join(sql.split()))
        self._current = self._results.pop(0) if self._results else []

    def fetchall(self) -> list[tuple]:
        return self._current

    def fetchone(self) -> tuple | None:
        return self._current[0] if self._current else None


class _Conn:
    def __init__(self, results: list[list[tuple]]):
        self._cursor = _Cursor(results)

    def cursor(self) -> _Cursor:
        return self._cursor


class _ExplodingConn:
    def cursor(self) -> Any:
        raise RuntimeError("store offline")


# --- Hierarchy -------------------------------------------------------------


def test_a_business_domain_is_a_root_and_says_so() -> None:
    """"No ancestors" is an answer here, not an absence: domains ARE the roots."""
    conn = _Conn([[("cls_1", "Retail", "product", "active")]])

    facet = _master_data_hierarchy(conn, ORG, "business-domain", "dom_1")

    assert facet["state"] == "available"
    assert facet["is_root"] is True
    assert facet["ancestors"] == []
    assert [c["id"] for c in facet["children"]] == ["cls_1"]


def test_a_classification_walks_to_its_domain_and_the_domain_closes_the_chain() -> None:
    conn = _Conn(
        [
            [("cls_parent", "dom_1", "Apparel", "Retail")],  # cls_child -> parent
            [(None, "dom_1", None, "Retail")],  # cls_parent -> root of the tree
            [("cls_leaf", "Shoes", "product", "active")],  # children of cls_child
        ]
    )

    facet = _master_data_hierarchy(conn, ORG, "master-data-object", "cls_child")

    assert facet["state"] == "available"
    assert [a["id"] for a in facet["ancestors"]] == ["cls_parent", "dom_1"]
    assert facet["ancestors"][-1]["kind"] == "business-domain"
    assert facet["is_root"] is False


def test_a_classification_cycle_cannot_hang_the_walk() -> None:
    """A parent chain that points back at itself terminates instead of looping."""
    conn = _Conn([[("cls_self", "dom_1", "Self", "Retail")], []])

    facet = _master_data_hierarchy(conn, ORG, "master-data-object", "cls_self")

    assert facet["state"] == "available"
    assert len(facet["ancestors"]) <= 2


def test_a_registry_lists_its_nodes_and_names_the_command_each_one_can_take() -> None:
    """Archived nodes stay listed: a node the screen cannot see cannot be restored.

    The command is decided HERE and not in the browser -- a screen that guessed
    from a label would offer Restore on a live node.
    """
    conn = _Conn(
        [
            [
                ("node_1", "France", "market", None),
                ("node_2", "DACH", "market", "2026-07-30T10:00:00Z"),
            ]
        ]
    )

    facet = _master_data_hierarchy(conn, ORG, "registry", "reg_1")

    assert [c["id"] for c in facet["children"]] == ["node_1", "node_2"]
    assert facet["children"][0]["node_kind"] == "market"
    assert (facet["children"][0]["archived"], facet["children"][0]["command"]) == (False, "archive")
    assert (facet["children"][1]["archived"], facet["children"][1]["command"]) == (True, "restore")


def test_the_registry_query_does_not_filter_archived_nodes_out() -> None:
    """Hiding them would make an archive look like a deletion."""
    conn = _Conn([[]])

    _master_data_hierarchy(conn, ORG, "registry", "reg_1")

    assert "archived_at IS NULL" not in conn._cursor.statements[0]


# --- Aliases ---------------------------------------------------------------


@pytest.mark.parametrize("object_type", ["business-domain", "master-data-object"])
def test_an_object_type_with_no_alias_store_is_unavailable_not_empty(object_type: str) -> None:
    """The property this whole file exists for."""
    facet = _master_data_aliases(_Conn([]), object_type, "obj_1")

    assert facet["state"] == "unavailable"
    assert facet["rows"] == []
    assert facet["reason"]["code"] == "master_data_alias_store_absent"
    assert "missing store" in facet["reason"]["message"]


def test_registry_aliases_carry_their_provenance_and_conflict_state() -> None:
    conn = _Conn(
        [
            [
                (
                    "alias_1", "iso", "fr-FR", "Allemagne", "DE", "exact", 0.98,
                    "operator", "none", None, None, "DACH",
                )
            ]
        ]
    )

    facet = _master_data_aliases(conn, "registry", "reg_1")

    assert facet["state"] == "available"
    row = facet["rows"][0]
    assert (row["raw_value"], row["normalized_value"]) == ("Allemagne", "DE")
    assert (row["relation"], row["confidence"]) == ("exact", 0.98)
    assert (row["provenance"], row["conflict_state"]) == ("operator", "none")
    assert row["node_label"] == "DACH"


def test_a_registry_with_no_alias_row_is_empty_not_unavailable() -> None:
    """The other half of the distinction: the store answered, and it is none."""
    facet = _master_data_aliases(_Conn([[]]), "registry", "reg_1")

    assert facet["state"] == "available"
    assert facet["rows"] == []


# --- Enrichment ------------------------------------------------------------


def test_one_unreadable_store_never_silences_the_other() -> None:
    detail: dict[str, Any] = {"summary": {}}

    _enrich_master_data_object(_ExplodingConn(), ORG, "registry", "reg_1", detail)

    assert detail["summary"]["hierarchy"]["state"] == "unavailable"
    assert detail["summary"]["aliases"]["state"] == "unavailable"
    # Each names its own failure rather than inheriting a single global message.
    assert detail["summary"]["hierarchy"]["reason"]["code"] == "master_data_hierarchy_unreadable"
    assert detail["summary"]["aliases"]["reason"]["code"] == "master_data_aliases_unreadable"


def test_enrichment_adds_both_facets_to_the_summary() -> None:
    detail: dict[str, Any] = {"summary": {"slug": "retail"}}
    conn = _Conn([[("cls_1", "Retail", "product", "active")]])

    _enrich_master_data_object(conn, ORG, "business-domain", "dom_1", detail)

    assert detail["summary"]["slug"] == "retail"  # existing keys survive
    assert detail["summary"]["hierarchy"]["state"] == "available"
    assert detail["summary"]["aliases"]["state"] == "unavailable"
