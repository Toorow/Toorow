"""The relationship authority's refusals and its facet, without a database.

WHAT THIS FILE HOLDS, AND WHAT IT LEAVES TO POSTGRES. Everything provable by
reading the module: the refusals, their CODES, the fact that a refusal names a
gesture and never an identifier, the identity document the hash is built from,
the owner links, and the two properties of the reverse-link facet that a
person meets first -- both directions, and *a failed read is not an empty list*.

The trigger, the RLS, the privileges and the backfill are proved in
`tests/integration/test_context_relationships_pg.py`, against a real database,
because a fake connection cannot refuse an UPDATE.
"""

from __future__ import annotations

import hashlib

import pytest
from core import context_relationships as relationships
from core.context_relationships import (
    ContextRelationshipRefused,
    Endpoint,
    create_relationship,
    list_for_node,
    owner_link,
    relationship_hash,
)

from tests.support.statement_router import UnknownStatement

ACTOR = "owner@example.com"
PROJECT = "proj_EXAMPLE"


# ---------------------------------------------------------------------------
# A cursor that answers by the SHAPE of the statement, never by its position.
# A side_effect list would make every test below depend on how many reads the
# module happens to do today.
# ---------------------------------------------------------------------------


class _Cursor:
    def __init__(self, world: "_World") -> None:
        self._world = world
        self._rows: list[tuple] = []
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self._world.statements.append((flat, params))
        if self._world.read_raises and flat.startswith("SELECT"):
            raise RuntimeError("the store is unreachable")
        if flat.startswith("SELECT 1 FROM app.context_topics"):
            self._rows = [(1,)] if params[0] in self._world.topics else []
        elif flat.startswith("SELECT 1 FROM app.procedures"):
            self._rows = [(1,)] if params[0] in self._world.procedures else []
        elif flat.startswith("SELECT org_id FROM app.projects"):
            self._rows = [("org_EXAMPLE",)]
        elif flat.startswith("SELECT status FROM app.context_relationships"):
            self._rows = [("active",)] if self._world.relation_exists else []
        elif flat.startswith("SELECT 1 FROM app.context_graph"):
            self._rows = []
        elif "FROM app.context_relationships" in flat and flat.startswith("SELECT id"):
            self._rows = list(self._world.facet_rows)
        elif flat.startswith("INSERT INTO app.context_graph"):
            self.description = [(name,) for name in _EDGE_COLUMNS]
            self._rows = [
                (
                    "edge_EXAMPLE",
                    params[1],
                    params[2],
                    params[3],
                    params[4],
                    params[5],
                    params[6],
                    params[7],
                    "2026-08-28T00:00:00Z",
                )
            ]
        elif flat.startswith((
            "INSERT INTO app.context_relationships",
            "UPDATE app.context_relationships",
            "INSERT INTO app.context_relationship_versions",
            # The audit row every declaration writes (context_relationships.py) --
            # swallowed by the old `else`, so no test knew it was there.
            "INSERT INTO app.audit_log",
        )):
            self._rows = []
        else:
            # AI-317: a statement this fake was never taught is a failing test,
            # not an empty answer -- an empty answer keeps every assertion
            # downstream about a path the product never took.
            raise UnknownStatement(f"_Cursor has no answer for: {flat}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


_EDGE_COLUMNS = [
    "id",
    "from_id",
    "from_type",
    "to_id",
    "to_type",
    "edge_type",
    "project_id",
    "created_by",
    "created_at",
]


class _World:
    def __init__(self, **kwargs) -> None:
        self.topics: set[str] = kwargs.get("topics", {"top_1", "top_2"})
        self.procedures: set[str] = kwargs.get("procedures", set())
        self.relation_exists: bool = kwargs.get("relation_exists", False)
        self.facet_rows: list[tuple] = kwargs.get("facet_rows", [])
        self.read_raises: bool = kwargs.get("read_raises", False)
        self.statements: list[tuple[str, object]] = []

    def cursor(self):
        return _Cursor(self)


def _declare(world: _World, **overrides):
    payload = {
        "project_id": PROJECT,
        "source_type": "topic",
        "source_id": "top_1",
        "target_type": "topic",
        "target_id": "top_2",
        "relationship_kind": "relates_to",
        "actor": ACTOR,
    }
    payload.update(overrides)
    return create_relationship(world, **payload)


# ---------------------------------------------------------------------------
# The refusals, each by its CODE
# ---------------------------------------------------------------------------


def test_a_relation_between_two_master_data_objects_belongs_to_the_mdm():
    """Story 49-6 AC2, the refusal the story names in as many words.

    `app.mdm_business_links` and the accepted 49.2 service are the authority for
    a link whose two ends are governed identities. Accepting one here would mint
    a SECOND authority for one fact -- which is the defect `governance.md`'s
    convergence amendment exists to close, reproduced in a new store.
    """
    world = _World()
    with pytest.raises(ContextRelationshipRefused) as refusal:
        _declare(
            world,
            source_type="master_data_node",
            source_id="mdnode_A",
            target_type="master_data_node",
            target_id="mdnode_B",
        )
    assert refusal.value.code == "master_data_relation_belongs_to_the_mdm"
    assert "Master Data" in refusal.value.message
    assert not [s for s, _ in world.statements if s.startswith("INSERT")]


def test_the_kind_comes_from_the_one_vocabulary_the_store_publishes():
    world = _World()
    with pytest.raises(ContextRelationshipRefused) as refusal:
        _declare(world, relationship_kind="relation_invalide_xyz")
    assert refusal.value.code == "unknown_relationship_kind"
    # The sentence LISTS what to choose instead. A refusal that only says "no"
    # leaves the caller guessing at a vocabulary it cannot see.
    assert "relates_to" in refusal.value.message


def test_an_endpoint_type_outside_the_eight_is_refused_before_any_read():
    world = _World()
    with pytest.raises(ContextRelationshipRefused) as refusal:
        _declare(world, target_type="widget")
    assert refusal.value.code == "unknown_endpoint_type"
    assert world.statements == [], "a malformed request opened a cursor"


def test_a_datastream_cannot_be_related_from_the_platform_scope():
    """The three Project-scoped owners have no meaning above a project.

    A platform relation is visible from every project; pointing it at one
    project's Datastream would make it unreadable from every other, which is a
    dangling link with extra steps.
    """
    world = _World()
    with pytest.raises(ContextRelationshipRefused) as refusal:
        _declare(
            world,
            project_id=None,
            target_type="datastream",
            target_id="ds_EXAMPLE",
        )
    assert refusal.value.code == "endpoint_needs_a_project"


def test_an_absent_endpoint_is_refused_and_names_the_gesture():
    world = _World(topics={"top_1"})
    with pytest.raises(ContextRelationshipRefused) as refusal:
        _declare(world)
    assert refusal.value.code == "endpoint_not_in_scope"
    assert "Pick a target that exists" in refusal.value.message
    # NEVER THE IDENTIFIER. `top_2` is what the caller sent; printing it back
    # tells a person nothing they can act on.
    assert "top_2" not in refusal.value.message


def test_a_relation_that_names_one_object_twice_is_refused():
    world = _World(topics={"top_1"})
    with pytest.raises(ContextRelationshipRefused) as refusal:
        _declare(world, target_id="top_1")
    assert refusal.value.code == "relation_points_at_itself"


def test_a_second_identical_relation_is_refused_rather_than_duplicated():
    world = _World(relation_exists=True)
    with pytest.raises(ContextRelationshipRefused) as refusal:
        _declare(world)
    assert refusal.value.code == "relation_already_exists"
    assert not [s for s, _ in world.statements if s.startswith("INSERT")]


# ---------------------------------------------------------------------------
# What a declaration writes
# ---------------------------------------------------------------------------


def test_declaring_writes_the_authority_its_version_and_ONE_projection():
    world = _World()
    relation = _declare(world)

    written = [s for s, _ in world.statements if s.startswith("INSERT INTO app.")]
    assert sum(1 for s in written if "app.context_relationships" in s) == 1
    assert sum(1 for s in written if "app.context_relationship_versions" in s) == 1
    assert sum(1 for s in written if "app.context_graph" in s) == 1

    assert relation["status"] == "active"
    assert relation["versions"][0]["version_number"] == 1
    assert relation["versions"][0]["lifecycle"] == "created"
    # The FACT carries ids and types, and nothing copied from either owner.
    fact = relation["versions"][0]["fact"]
    assert fact["source"] == {"type": "topic", "id": "top_1"}
    assert set(fact) == {"source", "target", "kind", "provenance"}


def test_an_endpoint_the_legacy_store_cannot_hold_projects_nothing():
    """A Semantic View is an endpoint of the AUTHORITY and of nothing else.

    `app.context_graph`'s CHECK knows five node types (031, 117, 272). Writing a
    projection row for a sixth would mean inventing a value the constraint
    refuses -- so the relation is held, and the projection is absent rather than
    faked.
    """
    world = _World()
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(
            relationships,
            "_endpoint_exists",
            lambda conn, endpoint, *, project_id: True,
        )
        relation = _declare(
            world, target_type="semantic_view", target_id="sv_EXAMPLE"
        )
    assert relation["projection"] is None
    assert relation["projection_edge_id"] is None
    assert not [s for s, _ in world.statements if "INSERT INTO app.context_graph" in s]


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_the_hash_is_the_document_migration_317_builds_in_sql():
    """Seven fields joined by newline -- the ONE document, in two languages.

    Migration 317's backfill computes this in SQL so a carried relation and a
    declared one agree byte for byte. The pg test pins the agreement against the
    database; this pins the document itself, so a change to it is a change
    somebody had to write down.
    """
    digest = relationship_hash(
        project_id=PROJECT,
        source=Endpoint("topic", "top_1"),
        target=Endpoint("target_field", "clicks"),
        relationship_kind="explains",
    )
    expected = hashlib.sha256(
        "\n".join(
            [
                "context-relationship.v1",
                PROJECT,
                "topic",
                "top_1",
                "target_field",
                "clicks",
                "explains",
            ]
        ).encode("utf-8")
    ).hexdigest()
    assert digest == expected


def test_the_hash_ignores_nothing_that_identifies_the_relation():
    base = dict(
        project_id=PROJECT,
        source=Endpoint("topic", "top_1"),
        target=Endpoint("topic", "top_2"),
        relationship_kind="relates_to",
    )
    reference = relationship_hash(**base)
    assert relationship_hash(**{**base, "relationship_kind": "explains"}) != reference
    assert relationship_hash(**{**base, "project_id": None}) != reference
    assert (
        relationship_hash(**{**base, "target": Endpoint("topic", "top_3")}) != reference
    )


# ---------------------------------------------------------------------------
# The reverse-link facet (AC6)
# ---------------------------------------------------------------------------


def _facet_row(source: tuple[str, str], target: tuple[str, str]) -> tuple:
    return (
        "crel_EXAMPLE",
        "org_EXAMPLE",
        PROJECT,
        source[0],
        source[1],
        target[0],
        target[1],
        "explains",
        "active",
        "console",
        "edge_EXAMPLE",
        "crelv_EXAMPLE",
        ACTOR,
        "2026-08-28T00:00:00Z",
        "2026-08-28T00:00:00Z",
    )


def test_the_facet_answers_both_directions_with_the_other_end_and_its_owner():
    world = _World(
        facet_rows=[
            _facet_row(("topic", "top_1"), ("target_field", "clicks")),
            _facet_row(("procedure", "proc_1"), ("topic", "top_1")),
        ]
    )
    facet = list_for_node(world, project_id=PROJECT, node_type="topic", node_id="top_1")

    assert facet["state"] == "ready"
    assert [item["other"]["type"] for item in facet["outgoing"]] == ["target_field"]
    assert [item["other"]["type"] for item in facet["incoming"]] == ["procedure"]
    # EXACT OWNER LINKS, not a search the browser has to fan out.
    assert facet["incoming"][0]["owner"]["href"] == "/api/context/procedures/proc_1"


def test_a_failed_facet_read_says_so_and_never_renders_as_no_relations():
    """The story's *Incomplete if*, in one assertion.

    An empty list and an unreadable store look identical on a screen, and the
    difference is the whole meaning of the panel: "nothing is related to this"
    is a fact a person acts on; "I could not look" is not.
    """
    world = _World(read_raises=True)
    facet = list_for_node(world, project_id=PROJECT, node_type="topic", node_id="top_1")

    assert facet["state"] == "unavailable"
    assert "outgoing" not in facet and "incoming" not in facet
    assert facet["reason"] == "related_items_unreadable"


def test_the_facet_is_bounded_and_says_when_it_cut():
    world = _World(
        facet_rows=[
            _facet_row(("topic", "top_1"), ("topic", f"top_{n}")) for n in range(10)
        ]
    )
    facet = list_for_node(
        world, project_id=PROJECT, node_type="topic", node_id="top_1", limit=3
    )
    assert len(facet["outgoing"]) + len(facet["incoming"]) == 3
    assert facet["truncated"] is True

    facet = list_for_node(
        world, project_id=PROJECT, node_type="topic", node_id="top_1", limit=10_000
    )
    assert facet["limit"] == relationships.MAX_FACET_LIMIT


# ---------------------------------------------------------------------------
# Owner links
# ---------------------------------------------------------------------------


def test_every_endpoint_type_resolves_to_an_address_and_a_surface():
    """No endpoint type falls through to a link nobody can open."""
    for node_type in sorted(relationships.ENDPOINT_TYPES):
        link = owner_link(Endpoint(node_type, "obj_EXAMPLE"), project_id=PROJECT)
        assert link["href"].startswith("/api/"), node_type
        assert link["surface"], node_type
