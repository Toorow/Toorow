"""Un croisement sans autorite est REFUSE en la nommant (story 69.3, AC4).

WHY THIS FILE EXISTS. `tests/core/test_entity_cross_read.py` proves the answer
contract off base. The refusal cannot be proven there: it is a question asked of
two real authorities -- the governed attributes a registry's node versions
carry, and the classifications a PUBLISHED rule set derives (68.6) -- and
"nothing publishes this" is only true if both were actually asked.

Rendre un cadre vide dirait << il n'y a rien >> la ou la verite est
<< personne n'a encore declare cela >>. Ces deux phrases n'appellent pas le
meme geste, et c'est exactement ce que ce test empeche de confondre.
"""

from __future__ import annotations

import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

from core import entity_cross_read as ecr  # noqa: E402
from core import entity_rule_derivation as erd  # noqa: E402
from core import master_data  # noqa: E402
from core import object_kind_registry as okr  # noqa: E402

requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres cross-read test skipped",
)

ACTOR = "alice@example.com"
KIND = "video"

PROPERTY_SCHEMA = {
    "type": "object",
    "properties": {"duration_seconds": {"type": "number"}},
}
RULES = [
    {
        "name": "content_type",
        "rules": [
            {"when": {"field": "duration_seconds", "op": "<", "value": 60}, "then": "short"},
        ],
        "otherwise": "long",
    }
]


@pytest.fixture()
def world(live_postgres):
    """A declared entity type with one node carrying one governed attribute."""
    from tests.integration.epic66_fixtures import make_project

    conn = live_postgres
    org_id, project_id = make_project(conn, "Epic 69.3")
    declared = okr.declare_entity_type(
        conn,
        org_id=org_id,
        project_id=project_id,
        object_kind=KIND,
        canonical_key="video_id",
        display_name="Videos",
        actor=ACTOR,
    )
    registry = declared["registry"]
    type_version = master_data.ensure_type_version(
        conn,
        org_id=org_id,
        object_kind=KIND,
        label="Video",
        property_schema=PROPERTY_SCHEMA,
        actor=ACTOR,
    )
    node = master_data.create_node(
        conn,
        org_id=org_id,
        project_id=project_id,
        registry_id=registry["id"],
        node_kind=KIND,
        label=f"v-{uuid.uuid4().hex[:6]}",
        actor=ACTOR,
    )
    version = master_data.create_node_version(
        conn,
        org_id=org_id,
        registry_id=registry["id"],
        node_id=node["id"],
        payload={"attributes": {"duration_seconds": 42}},
        actor=ACTOR,
        type_version_id=type_version["id"],
    )
    master_data.publish_node_version(conn, org_id=org_id, version_id=version["id"], actor=ACTOR)
    return {
        "conn": conn,
        "org_id": org_id,
        "project_id": project_id,
        "registry": registry,
        "node_id": str(node["id"]),
    }


@requires_postgres
def test_a_carried_attribute_answers_and_names_its_origin(world):
    found = ecr.assert_attribute_is_published(
        world["conn"],
        project_id=world["project_id"],
        object_kind=KIND,
        attribute="duration_seconds",
    )
    assert found["origin"] == ecr.ORIGIN_CARRIED
    assert found["rows"] >= 1


@requires_postgres
def test_an_attribute_nobody_publishes_is_refused_naming_both_gestures(world):
    with pytest.raises(ecr.CrossReadRefused) as excinfo:
        ecr.assert_attribute_is_published(
            world["conn"],
            project_id=world["project_id"],
            object_kind=KIND,
            attribute="content_type",
        )
    assert excinfo.value.code == "cross_attribute_not_published"
    # Le refus nomme LES DEUX gestes qui reparent, parce que deux autorites
    # peuvent porter un attribut et l'appelant ne sait pas laquelle il voulait.
    assert "Declare it on the entity type" in excinfo.value.message
    assert "publish the rule" in excinfo.value.message


@requires_postgres
def test_publishing_the_rule_turns_the_refusal_into_an_answer(world):
    conn = world["conn"]
    version = erd.draft_entity_rule_set(
        conn,
        org_id=world["org_id"],
        project_id=world["project_id"],
        object_kind=KIND,
        derived_attributes=RULES,
        label="Video classifications",
        actor=ACTOR,
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.governance_rule_sets "
            "WHERE project_id = %s AND family = 'entity_derivation' AND name = %s",
            (world["project_id"], KIND),
        )
        head_id = cur.fetchone()[0]
    erd.publish_entity_rule_set(
        conn,
        project_id=world["project_id"],
        rule_set_id=head_id,
        version_id=version["id"],
        actor=ACTOR,
    )

    found = ecr.assert_attribute_is_published(
        conn,
        project_id=world["project_id"],
        object_kind=KIND,
        attribute="content_type",
    )
    # Le refus n'etait pas une propriete de l'attribut : c'etait l'etat du
    # Projet, et publier la regle le change.
    assert found["origin"] == ecr.ORIGIN_DERIVED


@requires_postgres
def test_another_projects_attribute_does_not_answer_for_this_one(world):
    from tests.integration.epic66_fixtures import make_project

    conn = world["conn"]
    _other_org, other_project = make_project(conn, "Epic 69.3 voisin")
    with pytest.raises(ecr.CrossReadRefused) as excinfo:
        ecr.assert_attribute_is_published(
            conn,
            project_id=other_project,
            object_kind=KIND,
            attribute="duration_seconds",
        )
    assert excinfo.value.code == "cross_attribute_not_published"
