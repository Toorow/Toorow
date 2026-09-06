"""Story 70.2 against a real database -- the SQL, the constraints, the guard.

``test_observed_entities.py`` proves the model. What it cannot prove is that the
five generic tables actually accept these writes: the attachment is written by
hand against ``app.master_data_aliases`` rather than through
:func:`core.master_data.record_alias` (which writes organization scope and
case-folds), and a column name or a CHECK that disagrees would be invisible to a
scripted connection.

pg-gated: skipped without ``TEST_POSTGRES_DSN``. It runs offline against the
disposable Postgres (``python scripts/disposable_postgres.py up``) and commits
nothing -- ``live_postgres`` rolls the transaction back.
"""

from __future__ import annotations

import pytest
from core.master_data import MasterDataConflict, UsedByReference, register_used_by
from core.observed_entities import (
    LEVEL_L1,
    LEVEL_L2,
    LEVEL_L3,
    PROVENANCE_INHERITED_L1,
    PROVENANCE_INHERITED_PARENT,
    PROVENANCE_SELF,
    PROVENANCE_UNRESOLVED,
    ObservedEntityPath,
    attach_observed_entity,
    ensure_observed_entity_registry,
    fetch_attachment,
    list_attachments,
    load_entity_projection,
    publish_entity_hierarchy,
    reattach_observed_entity,
    resolution_counts,
)

ACTOR = "owner@example.com"
PLATFORM = "example_ads"

LEFT_L1 = ObservedEntityPath(platform=PLATFORM, level=LEVEL_L1, l1_id="c_1")
LEFT_L2 = ObservedEntityPath(platform=PLATFORM, level=LEVEL_L2, l1_id="c_1", l2_id="p_1")
LEFT_L3 = ObservedEntityPath(
    platform=PLATFORM, level=LEVEL_L3, l1_id="c_1", l2_id="p_1", l3_id="cr_9"
)
RIGHT_L1 = ObservedEntityPath(platform=PLATFORM, level=LEVEL_L1, l1_id="c_2")
RIGHT_L2 = ObservedEntityPath(platform=PLATFORM, level=LEVEL_L2, l1_id="c_2", l2_id="p_2")
RIGHT_L3 = ObservedEntityPath(
    platform=PLATFORM, level=LEVEL_L3, l1_id="c_2", l2_id="p_2", l3_id="cr_9"
)
ALL_PATHS = (LEFT_L1, LEFT_L2, LEFT_L3, RIGHT_L1, RIGHT_L2, RIGHT_L3)


@pytest.fixture
def observed_scope(pg_conn, inbound_pg_scope):
    """A registry with the six paths of the measured case attached to it."""

    org = inbound_pg_scope["org_id"]
    project = inbound_pg_scope["project_id"]
    datastream = inbound_pg_scope["datastream_id"]

    registry = ensure_observed_entity_registry(
        pg_conn, org_id=org, project_id=project, actor=ACTOR
    )
    node_of_key: dict[str, str] = {}
    for path in ALL_PATHS:
        result = attach_observed_entity(
            pg_conn,
            org_id=org,
            project_id=project,
            registry_id=registry["id"],
            path=path,
            datastream_id=datastream,
            actor=ACTOR,
        )
        assert result["outcome"] == "attached"
        node_of_key[path.canonical_key] = result["node_id"]

    return {
        "org_id": org,
        "project_id": project,
        "datastream_id": datastream,
        "registry_id": registry["id"],
        "node_of_key": node_of_key,
    }


def test_the_registry_mounts_and_the_attachment_round_trips(pg_conn, observed_scope) -> None:
    project = observed_scope["project_id"]

    stored = fetch_attachment(pg_conn, project_id=project, path=LEFT_L3)

    assert stored is not None
    assert stored["normalized_value"] == LEFT_L3.canonical_key
    assert stored["node_id"] == observed_scope["node_of_key"][LEFT_L3.canonical_key]
    assert len(list_attachments(pg_conn, project_id=project)) == len(ALL_PATHS)


def test_the_same_path_attaches_once(pg_conn, observed_scope) -> None:
    """Re-observing the same entity is not a second identity."""

    before = list_attachments(pg_conn, project_id=observed_scope["project_id"])

    again = attach_observed_entity(
        pg_conn,
        org_id=observed_scope["org_id"],
        project_id=observed_scope["project_id"],
        registry_id=observed_scope["registry_id"],
        path=LEFT_L3,
        datastream_id=observed_scope["datastream_id"],
        actor=ACTOR,
    )

    assert again["outcome"] == "unchanged"
    assert again["node_id"] == observed_scope["node_of_key"][LEFT_L3.canonical_key]
    assert len(list_attachments(pg_conn, project_id=observed_scope["project_id"])) == len(before)


def test_one_creative_id_under_two_parents_is_two_nodes(pg_conn, observed_scope) -> None:
    nodes = observed_scope["node_of_key"]

    assert LEFT_L3.entity_id == RIGHT_L3.entity_id == "cr_9"
    assert nodes[LEFT_L3.canonical_key] != nodes[RIGHT_L3.canonical_key]


def test_the_published_hierarchy_resolves_the_four_provenances(
    pg_conn, observed_scope
) -> None:
    nodes = observed_scope["node_of_key"]
    node_paths = {nodes[path.canonical_key]: path for path in ALL_PATHS}

    published = publish_entity_hierarchy(
        pg_conn,
        org_id=observed_scope["org_id"],
        project_id=observed_scope["project_id"],
        registry_id=observed_scope["registry_id"],
        actor=ACTOR,
        node_paths=node_paths,
        declared_dimensions={
            nodes[LEFT_L1.canonical_key]: {"brand": "Example Brand One", "channel": "search"},
            nodes[LEFT_L2.canonical_key]: {"channel": "display"},
            nodes[LEFT_L3.canonical_key]: {"format": "video"},
            nodes[RIGHT_L1.canonical_key]: {"brand": "Example Brand Two"},
        },
    )
    assert published["status"] == "current"

    projection = load_entity_projection(pg_conn, project_id=observed_scope["project_id"])
    assert projection is not None
    assert projection.hierarchy_version_id == published["id"]

    left = projection.resolve(
        nodes[LEFT_L3.canonical_key], ("format", "channel", "brand", "audience")
    )
    assert left["format"].provenance == PROVENANCE_SELF
    assert left["channel"].provenance == PROVENANCE_INHERITED_PARENT
    assert left["channel"].value == "display"
    assert left["brand"].provenance == PROVENANCE_INHERITED_L1
    assert left["brand"].value == "Example Brand One"
    assert left["audience"].provenance == PROVENANCE_UNRESOLVED
    assert left["audience"].value is None

    counts = resolution_counts(left)
    assert counts == {
        PROVENANCE_SELF: 1,
        PROVENANCE_INHERITED_PARENT: 1,
        PROVENANCE_INHERITED_L1: 1,
        PROVENANCE_UNRESOLVED: 1,
    }

    # The other sighting of the same creative id inherits its OWN branch.
    right = projection.resolve(nodes[RIGHT_L3.canonical_key], ("brand",))
    assert right["brand"].value == "Example Brand Two"
    assert right["brand"].source_node_id == nodes[RIGHT_L1.canonical_key]


def test_the_database_refuses_a_second_live_binding_for_one_path(
    pg_conn, observed_scope
) -> None:
    """`uq_master_data_aliases_live_exact` is what makes one path one node.

    Not a service-level convention that a second writer could bypass: the index
    refuses, so "this path names two identities" cannot exist even briefly.
    """

    import psycopg
    from core.observed_entities import attachment_namespace
    from ulid import ULID

    project = observed_scope["project_id"]
    rival = observed_scope["node_of_key"][RIGHT_L3.canonical_key]

    with pytest.raises(psycopg.errors.UniqueViolation), pg_conn.transaction():
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.master_data_aliases
                    (id, org_id, project_id, node_id, namespace, raw_value,
                     normalized_value, relation, provenance, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'exact', 'connector', 'system')
                """,
                (
                    f"mdali_{ULID()}",
                    observed_scope["org_id"],
                    project,
                    rival,
                    attachment_namespace(project),
                    LEFT_L3.canonical_key,
                    LEFT_L3.canonical_key,
                ),
            )


def test_a_destructive_regroup_is_refused_by_the_live_used_by(
    pg_conn, observed_scope
) -> None:
    nodes = observed_scope["node_of_key"]
    leaving = nodes[LEFT_L3.canonical_key]

    register_used_by(
        pg_conn,
        project_id=observed_scope["project_id"],
        registry_id=observed_scope["registry_id"],
        reference=UsedByReference(
            node_id=leaving,
            consumer_kind="datastream_mapping",
            consumer_id="dsm_EXAMPLE",
            consumer_label="Example mapping",
        ),
        actor=ACTOR,
    )

    with pytest.raises(MasterDataConflict) as exc:
        reattach_observed_entity(
            pg_conn,
            org_id=observed_scope["org_id"],
            project_id=observed_scope["project_id"],
            registry_id=observed_scope["registry_id"],
            path=LEFT_L3,
            node_id=nodes[RIGHT_L3.canonical_key],
            actor=ACTOR,
        )

    assert "datastream_mapping" in str(exc.value)
    # And the binding did not move.
    still = fetch_attachment(pg_conn, project_id=observed_scope["project_id"], path=LEFT_L3)
    assert still is not None and still["node_id"] == leaving


def test_an_acknowledged_regroup_moves_the_binding(pg_conn, observed_scope) -> None:
    nodes = observed_scope["node_of_key"]
    target = nodes[RIGHT_L3.canonical_key]

    register_used_by(
        pg_conn,
        project_id=observed_scope["project_id"],
        registry_id=observed_scope["registry_id"],
        reference=UsedByReference(
            node_id=nodes[LEFT_L3.canonical_key],
            consumer_kind="datastream_mapping",
            consumer_id="dsm_EXAMPLE",
            consumer_label="Example mapping",
        ),
        actor=ACTOR,
    )

    result = reattach_observed_entity(
        pg_conn,
        org_id=observed_scope["org_id"],
        project_id=observed_scope["project_id"],
        registry_id=observed_scope["registry_id"],
        path=LEFT_L3,
        node_id=target,
        actor=ACTOR,
        acknowledge_impact=True,
    )

    assert result["outcome"] == "reattached"
    moved = fetch_attachment(pg_conn, project_id=observed_scope["project_id"], path=LEFT_L3)
    assert moved is not None and moved["node_id"] == target
    # Exactly one live binding for this path: the old one was retired, not left
    # beside its successor.
    assert (
        len(
            [
                row
                for row in list_attachments(pg_conn, project_id=observed_scope["project_id"])
                if row["normalized_value"] == LEFT_L3.canonical_key
            ]
        )
        == 1
    )


def test_an_attachment_consumer_is_named_by_its_datastream_not_by_its_id(
    pg_conn, observed_scope
) -> None:
    """The label a Master Data refusal prints is a WORD, minted here.

    An attachment has no workbench of its own -- `master_data_consumers` files it
    under Data with no href -- so `consumer_label` is the only thing naming it in
    the three console lists that enumerate consumers before an archive. It was
    written as the Datastream's id, which put `ds_<ULID>` in a name's position on
    every one of them, and no browser fallback could repair it: the server had
    already answered, and its answer was the identifier.
    `docs/product-architecture/visualization-and-rendering.md` -- *"A member's
    label is a name, never its identifier"* -- applies where the name is minted,
    not only where it is shown.

    `app.datastreams.name` is `NOT NULL` and unique inside a Project
    (`uq_datastreams_project_name`, migration 023), so the word always exists
    while the Datastream does.
    """

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT consumer_label, consumer_kind FROM app.master_data_used_by "
            "WHERE registry_id = %s AND consumer_kind = %s",
            (observed_scope["registry_id"], "observed_entity_attachment"),
        )
        rows = cur.fetchall()

    assert rows, "the attachments registered no consumer at all"
    labels = {row[0] for row in rows}
    assert labels == {"Inbound pg fixture datastream"}
    # And never the address it used to carry.
    assert observed_scope["datastream_id"] not in labels


def test_a_consumer_label_is_absent_rather_than_an_id_when_the_datastream_is_not_there(
    pg_conn, observed_scope
) -> None:
    """`None` is the answer, and it is what lets a screen say "Unnamed consumer".

    The tempting shape is `name or datastream_id`, which is the defect one layer
    down: the store would then hold an identifier in a column called
    `consumer_label`, and no reader could tell it from a word.
    """
    from core.observed_entities import datastream_consumer_label

    assert (
        datastream_consumer_label(
            pg_conn,
            project_id=observed_scope["project_id"],
            datastream_id="ds_not_in_this_project",
        )
        is None
    )
    assert (
        datastream_consumer_label(
            pg_conn, project_id=observed_scope["project_id"], datastream_id=""
        )
        is None
    )
