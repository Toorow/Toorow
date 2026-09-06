"""The feeder-less entity-type declaration, against a real database (Story 68.1).

WHY THIS FILE EXISTS. `tests/core/test_entity_types.py` proves the rules off
base, and it is right about them. What it cannot see is the SCHEMA half of the
story, and the neighbouring epic is the reason to distrust that gap (AI-206:
four of six locks were Postgres triggers, invisible until the previous one
fell). So this file walks declare -> replay -> conflict -> list end to end as
the ordinary `connector` role and asserts what only a live base answers:

  * a type declared through this door carries ``version_scope = 'node'`` and
    its ``canonical_key`` PERSISTED -- the column is migration 293's, and no
    off-base test can see a default;
  * a replay writes NOTHING: same registry id, no second audit row;
  * the named conflict is raised on a DIFFERENT declaration of the same kind,
    and leaves no audit trace of having happened;
  * the declared type shows ``live_source_count = 0`` -- "declared, not fed",
    in the module's own list AND in the console's registries lens (AC3);
  * the declaration is project-scoped: another Project neither sees the type
    nor conflicts with it.

Every write happens inside the caller's transaction; `live_postgres` rolls back.
"""

from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from core import object_kind_registry as okr  # noqa: E402

from tests.integration.epic66_fixtures import make_project  # noqa: E402

DECLARATION = {
    "object_kind": "video",
    "canonical_key": "video_id",
    "display_name": "Videos",
}


@pytest.fixture()
def world(live_postgres):
    """Two projects in one org: isolation is a fact, not a mock."""
    conn = live_postgres
    org_id, project_a = make_project(conn, "Epic 68 A")
    _org_b, project_b = make_project(conn, "Epic 68 B")
    return {"conn": conn, "org_id": org_id, "project_a": project_a, "project_b": project_b}


def _declare(world, project_key="project_a", **overrides):
    declaration = {**DECLARATION, **overrides}
    return okr.declare_entity_type(
        world["conn"],
        org_id=world["org_id"],
        project_id=world[project_key],
        actor="alice@example.com",
        **declaration,
    )


def _audit_actions(conn, project_id: str) -> list[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT action, identity
            FROM app.audit_log
            WHERE metadata->>'project_id' = %s
              AND action LIKE 'mdm.entity_type.%%'
            ORDER BY created_at, id
            """,
            (project_id,),
        )
        return [(row[0], row[1]) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# What only the schema can prove: the scope and the persisted key.
# ---------------------------------------------------------------------------


def test_a_declared_entity_type_versions_per_node_and_carries_its_key(world):
    """`version_scope` and `canonical_key` are WRITTEN, never defaults.

    Left to the column defaults the type would version per KIND -- one payload
    for every video instead of one per video -- and its canonical key would be
    NULL, which the migration reserves for "declared before this door existed".
    """
    declared = _declare(world)
    assert declared["replayed"] is False
    with world["conn"].cursor() as cur:
        cur.execute(
            "SELECT version_scope, canonical_key, label, created_by "
            "FROM app.master_data_registries WHERE id = %s",
            (declared["registry"]["id"],),
        )
        assert cur.fetchone() == ("node", "video_id", "Videos", "alice@example.com")


def test_the_declaration_writes_no_source_binding(world):
    """Feeder-less means `master_data_source_bindings` stays empty (68.2 binds)."""
    declared = _declare(world)
    with world["conn"].cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.master_data_source_bindings WHERE registry_id = %s",
            (declared["registry"]["id"],),
        )
        assert cur.fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Replay vs duplicate: the two are distinguishable in the database too.
# ---------------------------------------------------------------------------


def test_a_replay_returns_the_same_registry_and_writes_nothing(world):
    first = _declare(world)
    second = _declare(world)
    assert second["replayed"] is True
    assert second["registry"]["id"] == first["registry"]["id"]
    # One declaration, one audit row. A replay is not a mutation, and an audit
    # row asserting one would be a lie.
    assert _audit_actions(world["conn"], world["project_a"]) == [
        ("mdm.entity_type.declared", "alice@example.com")
    ]


def test_a_different_key_for_the_same_kind_is_the_named_conflict(world):
    _declare(world)
    with pytest.raises(okr.EntityTypeExists) as excinfo:
        _declare(world, canonical_key="external_id")
    assert excinfo.value.code == "entity_type_exists"
    message = str(excinfo.value)
    assert "video" in message
    # The holder is named: the registry id and who declared it.
    assert "alice@example.com" in message


def test_a_different_label_for_the_same_kind_is_the_named_conflict(world):
    _declare(world)
    with pytest.raises(okr.EntityTypeExists):
        _declare(world, display_name="Films")


def test_a_refused_duplicate_leaves_no_trace_of_having_happened(world):
    """Le geste et sa preuve commitent ensemble, ou pas du tout."""
    _declare(world)
    with pytest.raises(okr.EntityTypeExists):
        _declare(world, canonical_key="external_id")
    assert _audit_actions(world["conn"], world["project_a"]) == [
        ("mdm.entity_type.declared", "alice@example.com")
    ]


# ---------------------------------------------------------------------------
# "Declared, not fed" is a state, in both lists (AC3).
# ---------------------------------------------------------------------------


def test_the_declared_type_lists_with_zero_live_sources(world):
    assert okr.list_entity_types(world["conn"], project_id=world["project_a"]) == []

    _declare(world)
    types = okr.list_entity_types(world["conn"], project_id=world["project_a"])
    assert len(types) == 1
    listed = types[0]
    assert listed["object_kind"] == "video"
    assert listed["canonical_key"] == "video_id"
    assert listed["display_name"] == "Videos"
    assert listed["version_scope"] == "node"
    assert listed["live_source_count"] == 0
    assert listed["node_count"] == 0


def test_the_declared_type_appears_in_the_registries_lens_unfed(world):
    """The console's existing lens (AC3): declared and never declared differ."""
    from core.governance_read_model import compose_governance_collection  # noqa: PLC0415

    def _lens():
        return compose_governance_collection(
            world["project_a"],
            "master-data",
            world["conn"],
            lens="registries",
            org_id=world["org_id"],
        )

    assert _lens()["items"] == []

    declared = _declare(world)
    items = _lens()["items"]
    assert [item["object_ref"]["id"] for item in items] == [declared["registry"]["id"]]
    assert items[0]["summary"]["live_source_count"] == 0


# ---------------------------------------------------------------------------
# Isolation: a declaration is one Project's fact (AC4).
# ---------------------------------------------------------------------------


def test_another_project_neither_sees_nor_conflicts_with_the_type(world):
    _declare(world)

    # Project B's list is empty: "never declared HERE", not "declared elsewhere".
    assert okr.list_entity_types(world["conn"], project_id=world["project_b"]) == []

    # The SAME kind, key and label in project B is a fresh declaration, not a
    # conflict and not a replay: uniqueness is (project, kind), never global.
    declared_b = _declare(world, project_key="project_b")
    assert declared_b["replayed"] is False
    assert declared_b["registry"]["id"] != _declare(world)["registry"]["id"]


def test_the_audit_row_names_the_actor_and_the_project(world):
    declared = _declare(world)
    rows = _audit_actions(world["conn"], world["project_a"])
    assert rows == [("mdm.entity_type.declared", "alice@example.com")]
    with world["conn"].cursor() as cur:
        cur.execute(
            """
            SELECT metadata->>'registry_id', metadata->>'object_kind',
                   metadata->>'canonical_key'
            FROM app.audit_log
            WHERE metadata->>'project_id' = %s
              AND action = 'mdm.entity_type.declared'
            """,
            (world["project_a"],),
        )
        assert cur.fetchone() == (declared["registry"]["id"], "video", "video_id")
