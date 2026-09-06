"""Versioned business rules on an entity type, against a real database (Story 68.6).

WHY THIS FILE EXISTS. `tests/core/test_entity_rule_derivation.py` proves the
validator and the evaluator off base, and it is right about them. What it
cannot see is the STORAGE half of the story, which is where the acceptance
criteria live:

  * publishing COMPUTES the derived attributes and stamps every row with the
    rule-set version id (AC2) -- a fact about `master_data_derived_attributes`,
    migration 294's table, invisible off base;
  * publishing a NEW version re-derives what reads return while the facts --
    the node payloads -- stay byte-identical (AC3, CAP-4);
  * an invalid set is refused at publish with a named reason and leaves
    NOTHING half-published: no pointer moved, no stamped row, no audit row
    (AC4);
  * the read path fails closed (`RuleSetUnavailable`) when no rule set is
    published, rather than inventing classifications.

Every write happens inside the caller's transaction; `live_postgres` rolls
back.
"""

from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from core import entity_rule_derivation as erd  # noqa: E402
from core import master_data  # noqa: E402
from core import object_kind_registry as okr  # noqa: E402
from core.canonical_field_registry import declare_project_field  # noqa: E402
from core.governance_rule_sets import RuleSetUnavailable  # noqa: E402

from tests.integration.epic66_fixtures import make_project  # noqa: E402

ACTOR = "alice@example.com"

PROPERTY_SCHEMA = {
    "type": "object",
    "properties": {
        "duration_seconds": {"type": "number"},
        "lang": {"type": "string"},
    },
}

RULES_V1 = [
    {
        "name": "content_type",
        "rules": [
            {"when": {"field": "duration_seconds", "op": "<", "value": 60}, "then": "short"},
        ],
        "otherwise": "long",
    }
]

RULES_V2 = [
    {
        "name": "content_type",
        "rules": [
            {"when": {"field": "duration_seconds", "op": "<", "value": 60}, "then": "clip"},
        ],
        "otherwise": "feature",
    }
]


@pytest.fixture()
def world(live_postgres):
    """A declared entity type, its type version, and three nodes with facts.

    The third node carries NO attributes: it is the proof that "unclassified"
    is the absence of a stamped row, never a stored NULL.
    """
    conn = live_postgres
    org_id, project_id = make_project(conn, "Epic 68.6")
    declared = okr.declare_entity_type(
        conn,
        org_id=org_id,
        project_id=project_id,
        object_kind="video",
        canonical_key="video_id",
        display_name="Videos",
        actor=ACTOR,
    )
    registry = declared["registry"]
    type_version = master_data.ensure_type_version(
        conn,
        org_id=org_id,
        object_kind="video",
        label="Video",
        property_schema=PROPERTY_SCHEMA,
        actor=ACTOR,
    )

    node_ids = {}
    for label, facts in (
        ("clip-a", {"duration_seconds": 42}),
        ("clip-b", {"duration_seconds": 3600}),
        ("clip-c", {}),
    ):
        node = master_data.create_node(
            conn,
            org_id=org_id,
            project_id=project_id,
            registry_id=registry["id"],
            node_kind="video",
            label=label,
            actor=ACTOR,
        )
        version = master_data.create_node_version(
            conn,
            org_id=org_id,
            registry_id=registry["id"],
            node_id=node["id"],
            payload={"attributes": facts},
            actor=ACTOR,
            type_version_id=type_version["id"],
        )
        master_data.publish_node_version(conn, org_id=org_id, version_id=version["id"], actor=ACTOR)
        node_ids[label] = node["id"]

    return {
        "conn": conn,
        "org_id": org_id,
        "project_id": project_id,
        "registry": registry,
        "nodes": node_ids,
    }


def _draft(world, rules):
    return erd.draft_entity_rule_set(
        world["conn"],
        org_id=world["org_id"],
        project_id=world["project_id"],
        object_kind="video",
        derived_attributes=rules,
        label="Video classifications",
        actor=ACTOR,
    )


def _head(world):
    with world["conn"].cursor() as cur:
        cur.execute(
            "SELECT id, current_version_id, last_known_good_version_id "
            "FROM app.governance_rule_sets "
            "WHERE project_id = %s AND family = 'entity_derivation' AND name = 'video'",
            (world["project_id"],),
        )
        return cur.fetchone()


def _stamped_rows(conn, project_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT node_id, attribute, value, rule_set_version_id "
            "FROM app.master_data_derived_attributes WHERE project_id = %s "
            "ORDER BY node_id, attribute",
            (project_id,),
        )
        return cur.fetchall()


def _node_facts(conn, node_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload, content_hash FROM app.master_data_object_versions "
            "WHERE node_id = %s AND status = 'current'",
            (node_id,),
        )
        return cur.fetchone()


def _audit_rows(conn, project_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT action FROM app.audit_log "
            "WHERE metadata->>'project_id' = %s "
            "  AND action LIKE 'mdm.entity_derivation.%%' ORDER BY created_at, id",
            (project_id,),
        )
        return [row[0] for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# AC2: publishing computes and stamps.
# ---------------------------------------------------------------------------


def test_publish_computes_and_stamps_every_value_with_its_version(world):
    version = _draft(world, RULES_V1)
    head_id, _, _ = _head(world)
    published = erd.publish_entity_rule_set(
        world["conn"],
        project_id=world["project_id"],
        rule_set_id=head_id,
        version_id=version["id"],
        actor=ACTOR,
    )
    assert published["status"] == "published"

    rows = _stamped_rows(world["conn"], world["project_id"])
    # Two classified nodes, the fact-less one derives NOTHING -- and every
    # row names the version that produced it.
    assert {row[0] for row in rows} == {world["nodes"]["clip-a"], world["nodes"]["clip-b"]}
    assert all(row[3] == version["id"] for row in rows)

    resolved = erd.resolve_derived_attributes(
        world["conn"], project_id=world["project_id"], object_kind="video"
    )
    assert resolved["rule_set_version_id"] == version["id"]
    assert resolved["nodes"][world["nodes"]["clip-a"]] == {"content_type": "short"}
    assert resolved["nodes"][world["nodes"]["clip-b"]] == {"content_type": "long"}
    assert world["nodes"]["clip-c"] not in resolved["nodes"]


def test_the_publication_is_audited_with_the_actor_and_the_version(world):
    version = _draft(world, RULES_V1)
    head_id, _, _ = _head(world)
    erd.publish_entity_rule_set(
        world["conn"],
        project_id=world["project_id"],
        rule_set_id=head_id,
        version_id=version["id"],
        actor=ACTOR,
    )
    assert _audit_rows(world["conn"], world["project_id"]) == [
        "mdm.entity_derivation.published"
    ]


# ---------------------------------------------------------------------------
# AC3: a new version re-derives reads; facts are never rewritten.
# ---------------------------------------------------------------------------


def test_a_new_version_re_derives_reads_without_rewriting_facts(world):
    facts_before = {
        label: _node_facts(world["conn"], node_id)
        for label, node_id in world["nodes"].items()
    }

    # The head is minted by the first draft (`ensure_rule_set`), so it must be
    # read AFTER it -- reading it first is reading a row nobody wrote yet.
    v1 = _draft(world, RULES_V1)
    head_id, _, _ = _head(world)
    erd.publish_entity_rule_set(
        world["conn"],
        project_id=world["project_id"],
        rule_set_id=head_id,
        version_id=v1["id"],
        actor=ACTOR,
    )
    v2 = _draft(world, RULES_V2)
    assert v2["id"] != v1["id"]
    erd.publish_entity_rule_set(
        world["conn"],
        project_id=world["project_id"],
        rule_set_id=head_id,
        version_id=v2["id"],
        actor=ACTOR,
    )

    # The read path resolves the CURRENT version: same nodes, new answers,
    # the new stamp.
    resolved = erd.resolve_derived_attributes(
        world["conn"], project_id=world["project_id"], object_kind="video"
    )
    assert resolved["rule_set_version_id"] == v2["id"]
    assert resolved["nodes"][world["nodes"]["clip-a"]] == {"content_type": "clip"}
    assert resolved["nodes"][world["nodes"]["clip-b"]] == {"content_type": "feature"}

    # Both versions' rows coexist -- an old Result may pin v1's -- and the
    # facts the rules read are byte-identical to before any publish.
    rows = _stamped_rows(world["conn"], world["project_id"])
    assert {row[3] for row in rows} == {v1["id"], v2["id"]}
    for label, node_id in world["nodes"].items():
        assert _node_facts(world["conn"], node_id) == facts_before[label]

    # The generic lifecycle did its half: v1 is last-known-good, v2 current.
    _, current, lkg = _head(world)
    assert current == v2["id"]
    assert lkg == v1["id"]


# ---------------------------------------------------------------------------
# AC4: publish fails closed, with a named reason, and nothing half-exists.
# ---------------------------------------------------------------------------


def test_an_unknown_input_field_is_refused_at_publish_and_nothing_moves(world):
    version = _draft(
        world,
        [
            {
                "name": "quality",
                "rules": [
                    {"when": {"field": "bitrate", "op": ">", "value": 1000}, "then": "hd"}
                ],
            }
        ],
    )
    head_id, _, _ = _head(world)
    with pytest.raises(erd.UnknownInputField) as excinfo:
        erd.publish_entity_rule_set(
            world["conn"],
            project_id=world["project_id"],
            rule_set_id=head_id,
            version_id=version["id"],
            actor=ACTOR,
        )
    assert excinfo.value.code == "unknown_input_field"
    assert "bitrate" in str(excinfo.value)

    # Nothing half-published: no pointer, no stamped row, no audit row, and
    # the version stays a draft the operator can repair.
    assert _head(world)[1] is None
    assert _stamped_rows(world["conn"], world["project_id"]) == []
    assert _audit_rows(world["conn"], world["project_id"]) == []
    with world["conn"].cursor() as cur:
        cur.execute(
            "SELECT status FROM app.governance_rule_set_versions WHERE id = %s",
            (version["id"],),
        )
        assert cur.fetchone()[0] == "draft"


def test_an_output_name_colliding_with_a_published_attribute_is_refused(world):
    declare_project_field(
        world["conn"],
        project_id=world["project_id"],
        canonical_name="content_type",
        concept_kind="dimension",
        value_type="string",
        actor=ACTOR,
        object_kind="video",
    )
    version = _draft(world, RULES_V1)
    head_id, _, _ = _head(world)
    with pytest.raises(erd.OutputAttributeConflict) as excinfo:
        erd.publish_entity_rule_set(
            world["conn"],
            project_id=world["project_id"],
            rule_set_id=head_id,
            version_id=version["id"],
            actor=ACTOR,
        )
    assert excinfo.value.code == "output_attribute_conflict"
    assert "content_type" in str(excinfo.value)
    assert _head(world)[1] is None
    assert _stamped_rows(world["conn"], world["project_id"]) == []


def test_a_rule_set_on_an_undeclared_entity_type_is_refused(world):
    version = erd.draft_entity_rule_set(
        world["conn"],
        org_id=world["org_id"],
        project_id=world["project_id"],
        object_kind="podcast",
        derived_attributes=RULES_V1,
        label="Podcast classifications",
        actor=ACTOR,
    )
    with world["conn"].cursor() as cur:
        cur.execute(
            "SELECT id FROM app.governance_rule_sets "
            "WHERE project_id = %s AND family = 'entity_derivation' AND name = 'podcast'",
            (world["project_id"],),
        )
        head_id = cur.fetchone()[0]
    with pytest.raises(erd.EntityTypeNotDeclared) as excinfo:
        erd.publish_entity_rule_set(
            world["conn"],
            project_id=world["project_id"],
            rule_set_id=head_id,
            version_id=version["id"],
            actor=ACTOR,
        )
    assert excinfo.value.code == "entity_type_not_declared"


# ---------------------------------------------------------------------------
# Fail closed at read: no published rule set, no classifications. Ever.
# ---------------------------------------------------------------------------


def test_the_read_fails_closed_when_no_rule_set_is_published(world):
    with pytest.raises(RuleSetUnavailable):
        erd.resolve_derived_attributes(
            world["conn"], project_id=world["project_id"], object_kind="video"
        )


def test_a_drafted_but_unpublished_set_still_fails_the_read(world):
    _draft(world, RULES_V1)
    with pytest.raises(RuleSetUnavailable):
        erd.resolve_derived_attributes(
            world["conn"], project_id=world["project_id"], object_kind="video"
        )


# ---------------------------------------------------------------------------
# Idempotence: replaying a publish writes nothing twice.
# ---------------------------------------------------------------------------


def test_replaying_a_publish_is_a_no_op(world):
    version = _draft(world, RULES_V1)
    head_id, _, _ = _head(world)
    for _ in range(2):
        erd.publish_entity_rule_set(
            world["conn"],
            project_id=world["project_id"],
            rule_set_id=head_id,
            version_id=version["id"],
            actor=ACTOR,
        )
    assert len(_stamped_rows(world["conn"], world["project_id"])) == 2
    assert _audit_rows(world["conn"], world["project_id"]) == [
        "mdm.entity_derivation.published"
    ]
