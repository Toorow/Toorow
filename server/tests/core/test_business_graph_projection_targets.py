"""A business link to a Datastream or a Semantic object stays visible (49.6).

`graph_projection` emits an EDGE for every business link, and the graph bundle
drops edges whose endpoint resolves to no node. It synthesized a node for
`report_view` only -- so when `ee42bb8` made `datastream`, `semantic_view` and
`semantic_concept` linkable, a link to one of them would have been stored,
returned by the links API, and drawn nowhere.

That is the failure this repository names a defect that looks like an empty
state, and it would have been introduced by the same wave that opened the types.
"""

from __future__ import annotations

import pytest
from core.business_taxonomy import _owned_target_nodes

PROJECT = "proj_EXAMPLE"


class _Cursor:
    def __init__(self, rows, *, explode: bool = False):
        self._rows = rows
        self._explode = explode
        self.statements: list[str] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> None:
        if self._explode:
            raise RuntimeError("owner unreadable")
        self.statements.append(" ".join(sql.split()))

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows=(), *, explode: bool = False):
        self.cur = _Cursor(list(rows), explode=explode)

    def cursor(self) -> _Cursor:
        return self.cur


def _link(target_type: str, target_id: str) -> dict:
    return {"target_type": target_type, "target_id": target_id}


@pytest.mark.parametrize(
    "target_type,table",
    [
        ("datastream", "app.datastreams"),
        ("semantic_view", "app.semantic_views"),
        ("semantic_concept", "app.semantic_concepts"),
    ],
)
def test_a_linked_target_becomes_a_node_so_its_edge_is_not_dropped(target_type, table) -> None:
    conn = _Conn([("obj_1", "Paid media daily", PROJECT)])

    nodes = _owned_target_nodes(conn, [_link(target_type, "obj_1")], project_id=PROJECT)

    assert [n["id"] for n in nodes] == ["obj_1"]
    assert nodes[0]["node_type"] == target_type
    assert table in conn.cur.statements[0]
    # Scoped in the lookup, like every other governed read in this module.
    assert "project_id = %s" in conn.cur.statements[0]


def test_the_title_is_read_from_the_owner_not_derived_from_the_id() -> None:
    """Showing `ds_01J...` in a mindmap is not a name."""
    conn = _Conn([("ds_1", "Paid media daily", PROJECT)])

    nodes = _owned_target_nodes(conn, [_link("datastream", "ds_1")], project_id=PROJECT)

    assert nodes[0]["title"] == "Paid media daily"


def test_an_unreadable_owner_still_yields_a_node_and_claims_no_name() -> None:
    """The link must stay visible even when its owner cannot be read.

    Dropping the node would hide a link that exists; inventing a title would
    claim a name nobody supplied. It falls back to the id, which is true.
    """
    conn = _Conn(explode=True)

    nodes = _owned_target_nodes(conn, [_link("datastream", "ds_1")], project_id=PROJECT)

    assert [n["id"] for n in nodes] == ["ds_1"]
    assert nodes[0]["title"] == "ds_1"


def test_types_the_bundle_already_composes_are_not_synthesized_twice() -> None:
    """Topics, procedures and schema docs come from their own stores."""
    conn = _Conn([("x", "y", PROJECT)])

    nodes = _owned_target_nodes(
        conn,
        [
            _link("topic", "top_1"),
            _link("procedure", "proc_1"),
            _link("schema_doc", "sch_1"),
            _link("target_field", "revenue"),
            _link("report_view", "ga4/sessions"),
        ],
        project_id=PROJECT,
    )

    assert nodes == []


def test_a_canonical_field_is_looked_up_at_both_scopes_and_says_which(  # noqa: D103
) -> None:
    """A platform field belongs to every project, and the node must say so.

    The three types above are project-owned, so `scope: "project"` was a fact.
    A canonical field exists at both scopes: writing `project` on a platform row
    would tell the reader a vocabulary the whole platform shares belongs to the
    project in front of them.
    """
    conn = _Conn([("mdmcf_1", "Net revenue", None)])

    nodes = _owned_target_nodes(conn, [_link("canonical_field", "mdmcf_1")], project_id=PROJECT)

    assert [n["id"] for n in nodes] == ["mdmcf_1"]
    assert nodes[0]["title"] == "Net revenue"
    assert nodes[0]["scope"] == "platform"
    statement = conn.cur.statements[0]
    assert "app.mdm_canonical_fields" in statement
    assert "canonical_name" in statement
    assert "(project_id IS NULL OR project_id = %s)" in statement


def test_a_project_declared_canonical_field_is_scoped_to_the_project() -> None:
    conn = _Conn([("mdmcf_2", "Net revenue", PROJECT)])

    nodes = _owned_target_nodes(conn, [_link("canonical_field", "mdmcf_2")], project_id=PROJECT)

    assert nodes[0]["scope"] == "project"


def test_an_unread_canonical_field_falls_back_to_the_narrower_scope() -> None:
    """Guessing `platform` would widen a link nobody widened."""
    conn = _Conn(explode=True)

    nodes = _owned_target_nodes(conn, [_link("canonical_field", "mdmcf_3")], project_id=PROJECT)

    assert [n["id"] for n in nodes] == ["mdmcf_3"]
    assert nodes[0]["title"] == "mdmcf_3"
    assert nodes[0]["scope"] == "project"


def test_each_target_is_emitted_once_however_many_links_point_at_it() -> None:
    conn = _Conn([("ds_1", "Paid media daily", PROJECT)])

    nodes = _owned_target_nodes(
        conn,
        [_link("datastream", "ds_1"), _link("datastream", "ds_1")],
        project_id=PROJECT,
    )

    assert len(nodes) == 1


def test_the_browser_can_draw_every_node_type_the_projection_emits() -> None:
    """The same asymmetry as the target types, one layer further out.

    A node type the server emits and `NodeTypeKey` does not carry is a node React
    never renders -- the link would be invisible again, for a different reason.
    """
    from pathlib import Path

    page = (
        Path(__file__).resolve().parents[3]
        / "ui" / "admin" / "src" / "KnowledgeGraphPage.tsx"
    ).read_text(encoding="utf-8")

    # Derived from the map, not retyped beside it: the hardcoded triple stayed
    # green when `canonical_field` was added to the synthesizer (AI-298), which
    # is the exact drift this test exists to catch.
    from core.business_taxonomy import _SYNTHESIZED_TARGETS

    for target_type in _SYNTHESIZED_TARGETS:
        assert f'"{target_type}"' in page, f"KnowledgeGraphPage cannot draw {target_type}"
        assert f"{target_type}:" in page, f"{target_type} has no label or default filter"


def test_graph_projection_actually_calls_the_synthesizer(monkeypatch) -> None:
    """Drives `graph_projection`, not just the helper underneath it.

    The first version of this file tested `_owned_target_nodes` alone. Deleting
    the single line that CALLS it from `graph_projection` left those tests green
    -- a harness covering one link of the chain, which is the failure this
    repository has shipped twice. This drives the projection end to end.
    """
    import core.business_taxonomy as bt

    monkeypatch.setattr(
        bt, "list_taxonomy", lambda conn, **kw: {"domains": [], "classifications": []}
    )
    monkeypatch.setattr(
        bt,
        "list_links",
        lambda conn, **kw: [
            {
                "id": "lnk_1",
                "taxonomy_type": "business_domain",
                "taxonomy_id": "bdm_1",
                "target_type": "datastream",
                "target_id": "ds_1",
                "relation_type": "applies_to",
                "created_by": "person_1",
                "created_at": "2026-07-31T00:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(bt, "taxonomy_hierarchy_edges", lambda *a, **kw: [])

    bundle = bt.graph_projection(
        _Conn([("ds_1", "Paid media daily", PROJECT)]), org_id="org_1", project_id=PROJECT
    )

    node_ids = {node["id"] for node in bundle["nodes"]}
    edge_targets = {edge["to_id"] for edge in bundle["edges"]}

    # The edge exists AND its endpoint resolves: this is the pair whose absence
    # made the link invisible.
    assert "ds_1" in edge_targets
    assert "ds_1" in node_ids, "the edge points at a node the bundle would drop as dangling"
