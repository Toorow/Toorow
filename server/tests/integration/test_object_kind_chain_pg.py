"""The object-kind chain, against a real database (AI-232, epic 64).

WHY THIS FILE EXISTS. Every story of epic 64 that touches the client's object was
proven WITHOUT a database -- `test_object_kind_registry.py` says so in its own
header, and it is right about the rules it holds: a grain refusal is generic. But
four of the six things the chain promises are not rules, they are SCHEMA, and the
neighbouring action item is the reason to distrust that gap. AI-206 shipped a
chain proven entirely off-base and it carried SIX locks, four of them Postgres
triggers, each invisible until the previous one fell.

So this file walks declare -> describe -> release end to end as the ordinary
`connector` role, and asserts the four things only a live base answers:

  * a registry declared through this door carries ``version_scope = 'node'``.
    Left to the column default it would be ``registry`` -- ONE payload for every
    video instead of one per video -- and no off-base test can see a default;
  * TWO live bindings coexist on ONE registry, one per namespace (Story 64.13),
    and a second binding in the SAME namespace is refused BY THE PARTIAL UNIQUE
    INDEX of migration 238, not by Python;
  * the mirror views of migration 233 project exactly the live rows: an archived
    node and a retired alias disappear from them. The filter is IN the view, and
    a view is frozen at creation;
  * a released binding is not an absent kind. `describe` still answers, with
    ``no_live_source_binding``, and `object_kind_for_datastream` stops deriving.

WHAT IT FOUND, AND WHAT IT DID NOT. It found ONE lock, and in the fixture rather
than in the product: `app.reject_datastream_mapping_version_mutation` makes a
published mapping version immutable, so a grain cannot be written after the fact
-- `epic66_fixtures.make_datastream` now takes it as a parameter. The chain
itself passed first walk, with no hidden trigger. That is the measurement this
file is here to make repeatable, not a claim it inherits from the epic document.

Every write happens inside the caller's transaction; `live_postgres` rolls back.
"""

from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from core import master_data as md  # noqa: E402
from core import object_kind_registry as okr  # noqa: E402
from core.master_data import MasterDataConflict, MasterDataNotFound  # noqa: E402

from tests.integration.epic66_fixtures import (  # noqa: E402
    make_datastream,
    make_project,
    uid,
)

#: The dossier's own two shapes: one entity of five carries a key (migration 238).
KEYED_NS = "client_workbook"
LABELLED_NS = "pos_export"


def _mapping_version(conn, datastream_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_mapping_version_id FROM app.datastreams WHERE id = %s",
            (datastream_id,),
        )
        return str(cur.fetchone()[0])


@pytest.fixture()
def world(live_postgres):
    """A project, a keyed source and a label-only source, both feeding `video`."""
    conn = live_postgres
    org_id, project_id = make_project(conn, "Epic 64")

    keyed = make_datastream(
        conn,
        org_id,
        project_id,
        "workbook video",
        unbound_fields=("video_id", "title"),
        grain=["video_id"],
    )
    labelled = make_datastream(
        conn,
        org_id,
        project_id,
        "point of sale export",
        unbound_fields=("restaurant_name",),
    )
    return {
        "conn": conn,
        "org_id": org_id,
        "project_id": project_id,
        "keyed": keyed,
        "keyed_mapping": _mapping_version(conn, keyed),
        "labelled": labelled,
        "labelled_mapping": _mapping_version(conn, labelled),
    }


def _declare_keyed(world, namespace: str = KEYED_NS):
    return okr.declare_object_kind(
        world["conn"],
        org_id=world["org_id"],
        project_id=world["project_id"],
        object_kind="video",
        label="Videos",
        datastream_id=world["keyed"],
        mapping_version_id=world["keyed_mapping"],
        namespace=namespace,
        actor="tester",
    )


def _declare_labelled(world, namespace: str = LABELLED_NS):
    return okr.declare_object_kind(
        world["conn"],
        org_id=world["org_id"],
        project_id=world["project_id"],
        object_kind="video",
        label="Videos",
        datastream_id=world["labelled"],
        mapping_version_id=world["labelled_mapping"],
        namespace=namespace,
        actor="tester",
        identity_mode="governed_label",
        label_field="restaurant_name",
    )


# ---------------------------------------------------------------------------
# The default nobody can see off-base.
# ---------------------------------------------------------------------------


def test_a_client_object_versions_per_node_not_per_kind(world):
    """`version_scope` is written, never inherited from the column default.

    This is the whole point of a client object: each video carries its own
    properties. `registry` -- the default -- would give one payload to the kind,
    and every per-object attribute of Story 64.2 would land on it.
    """
    declared = _declare_keyed(world)
    with world["conn"].cursor() as cur:
        cur.execute(
            "SELECT version_scope FROM app.master_data_registries WHERE id = %s",
            (declared["registry"]["id"],),
        )
        assert cur.fetchone()[0] == "node"


def test_the_identity_fields_are_read_from_the_pinned_mapping(world):
    """The grain is READ at declaration time, never copied into the binding."""
    assert _declare_keyed(world)["identity_fields"] == ["video_id"]
    with world["conn"].cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='app' AND table_name='master_data_source_bindings'"
        )
        columns = {row[0] for row in cur.fetchall()}
    # A copied grain is a second truth that drifts from the mapping it came from.
    assert "identity_fields" not in columns and "grain" not in columns


# ---------------------------------------------------------------------------
# Two sources, two modes, ONE identity (Story 64.13).
# ---------------------------------------------------------------------------


def test_two_live_sources_feed_one_registry_one_per_namespace(world):
    """The dossier's `video` arrives keyed AND spelled. Both bindings stay live."""
    first = _declare_keyed(world)
    second = _declare_labelled(world)
    assert second["registry"]["id"] == first["registry"]["id"]

    described = okr.describe_object_kind(
        world["conn"], project_id=world["project_id"], object_kind="video"
    )
    modes = {s["namespace"]: s["identity_mode"] for s in described["sources"]}
    assert modes == {KEYED_NS: "source_key", LABELLED_NS: "governed_label"}
    assert described["unavailable_reason"] is None

    # Each source answers "what identifies this object" in its own words.
    by_ns = {s["namespace"]: s for s in described["sources"]}
    assert by_ns[KEYED_NS]["identity_fields"] == ["video_id"]
    assert by_ns[LABELLED_NS]["identity_fields"] == []
    assert by_ns[LABELLED_NS]["label_field"] == "restaurant_name"


def test_a_second_binding_in_the_same_namespace_is_refused_by_the_index(world):
    """The refusal comes from the partial unique index, and names the holder.

    Proven here and not off-base: Python never checks this. Migration 238 moved
    the index to (project, registry, namespace), and a test that mocked the
    INSERT would have passed against the 236 index it replaced.
    """
    _declare_keyed(world)
    with pytest.raises(MasterDataConflict) as excinfo:
        _declare_labelled(world, namespace=KEYED_NS)
    message = str(excinfo.value)
    assert KEYED_NS in message and world["keyed"] in message


# ---------------------------------------------------------------------------
# The mirror (Story 64.9) shows the live rows and only those.
# ---------------------------------------------------------------------------


def test_the_mirror_views_carry_the_live_node_and_drop_the_archived_one(world):
    """`archived_at IS NULL` lives in the view, so nothing downstream can forget it."""
    conn = world["conn"]
    declared = _declare_keyed(world)
    registry_id = declared["registry"]["id"]

    live = md.create_node(
        conn,
        org_id=world["org_id"],
        project_id=world["project_id"],
        registry_id=registry_id,
        node_kind="video",
        label="Burger King review",
        actor="tester",
    )
    archived = md.create_node(
        conn,
        org_id=world["org_id"],
        project_id=world["project_id"],
        registry_id=registry_id,
        node_kind="video",
        label="Retired cut",
        actor="tester",
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.master_data_nodes SET archived_at = NOW() WHERE id = %s",
            (archived["id"],),
        )
        cur.execute(
            "SELECT node_id FROM app.master_data_nodes_dim_v WHERE project_id = %s",
            (world["project_id"],),
        )
        mirrored = {row[0] for row in cur.fetchall()}
    assert mirrored == {live["id"]}


def test_the_alias_mirror_keeps_the_relation_unresolved_and_drops_the_retired(world):
    """A `close` stays `close` in the mirror -- the matching policy is 64.10's, at build time."""
    conn = world["conn"]
    registry_id = _declare_keyed(world)["registry"]["id"]
    node = md.create_node(
        conn,
        org_id=world["org_id"],
        project_id=world["project_id"],
        registry_id=registry_id,
        node_kind="video",
        label="Burger King review",
        actor="tester",
    )

    def _alias(raw: str, relation: str, *, retired: bool) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.master_data_aliases
                    (id, org_id, project_id, node_id, namespace, locale, raw_value,
                     normalized_value, relation, confidence, provenance,
                     retired_at, created_by)
                VALUES (%s,%s,%s,%s,%s,'fr',%s,%s,%s,0.9500,'import',
                        CASE WHEN %s THEN NOW() END,'tester')
                """,
                (
                    uid("mdali"),
                    world["org_id"],
                    world["project_id"],
                    node["id"],
                    KEYED_NS,
                    raw,
                    raw.lower(),
                    relation,
                    retired,
                ),
            )

    _alias("Burger King", "exact", retired=False)
    _alias("BK Drive", "close", retired=False)
    _alias("Quick", "exact", retired=True)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT raw_value, relation FROM app.master_data_aliases_dim_v "
            "WHERE project_id = %s ORDER BY raw_value",
            (world["project_id"],),
        )
        rows = cur.fetchall()
    assert rows == [("BK Drive", "close"), ("Burger King", "exact")]


# ---------------------------------------------------------------------------
# Released is not absent.
# ---------------------------------------------------------------------------


def test_a_released_source_leaves_a_kind_that_still_answers(world):
    """Never declared and no longer fed are two different sentences.

    A screen that showed both as empty would invite an operator to create a
    registry that already exists.
    """
    conn = world["conn"]
    registry_id = _declare_keyed(world)["registry"]["id"]
    _declare_labelled(world)

    okr.release_source_binding(
        conn,
        project_id=world["project_id"],
        registry_id=registry_id,
        namespace=LABELLED_NS,
        actor="tester",
    )
    described = okr.describe_object_kind(
        conn, project_id=world["project_id"], object_kind="video"
    )
    assert [s["namespace"] for s in described["sources"]] == [KEYED_NS]
    assert described["unavailable_reason"] is None
    # The released row stays readable, with who released it and when.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT released_by, released_at IS NOT NULL "
            "FROM app.master_data_source_bindings "
            "WHERE registry_id = %s AND namespace = %s",
            (registry_id, LABELLED_NS),
        )
        assert cur.fetchone() == ("tester", True)

    okr.release_source_binding(
        conn,
        project_id=world["project_id"],
        registry_id=registry_id,
        namespace=KEYED_NS,
        actor="tester",
    )
    starved = okr.describe_object_kind(
        conn, project_id=world["project_id"], object_kind="video"
    )
    assert starved["sources"] == [] and starved["unavailable_reason"] == "no_live_source_binding"

    with pytest.raises(MasterDataNotFound):
        okr.release_source_binding(
            conn,
            project_id=world["project_id"],
            registry_id=registry_id,
            namespace=KEYED_NS,
            actor="tester",
        )


def test_the_kind_a_datastream_feeds_is_derived_and_stops_at_release(world):
    """Story 64.15's seam: the declaration route resolves the kind, never trusts it."""
    conn = world["conn"]
    registry_id = _declare_keyed(world)["registry"]["id"]
    assert (
        okr.object_kind_for_datastream(
            conn, project_id=world["project_id"], datastream_id=world["keyed"]
        )
        == "video"
    )
    # A Datastream that feeds nothing qualifies nothing.
    assert (
        okr.object_kind_for_datastream(
            conn, project_id=world["project_id"], datastream_id=world["labelled"]
        )
        is None
    )

    okr.release_source_binding(
        conn,
        project_id=world["project_id"],
        registry_id=registry_id,
        namespace=KEYED_NS,
        actor="tester",
    )
    assert (
        okr.object_kind_for_datastream(
            conn, project_id=world["project_id"], datastream_id=world["keyed"]
        )
        is None
    )


# ---------------------------------------------------------------------------
# The console can see it (AI-232, the gap this chain still had on 2026-08-17).
# ---------------------------------------------------------------------------


def _registries(world):
    from core.governance_read_model import compose_governance_collection  # noqa: PLC0415

    return compose_governance_collection(
        world["project_id"],
        "master-data",
        world["conn"],
        lens="registries",
        org_id=world["org_id"],
    )


def test_a_declared_object_kind_appears_in_the_registries_lens(world):
    """Before this, the lens read capability-pinned registries and nothing else.

    A client object kind is pinned by no capability, so a whole governed object
    could be declared over MCP and be listed by no screen.
    """
    assert _registries(world)["items"] == []

    declared = _declare_keyed(world)
    items = _registries(world)["items"]
    assert [item["object_ref"]["id"] for item in items] == [declared["registry"]["id"]]

    listed = items[0]
    assert listed["object_ref"]["label"] == "Videos"
    assert listed["summary"]["object_kind"] == "video"
    # `node` and not `registry`: the difference between one payload per video and
    # one payload for every video. A reader cannot tell what a version means here
    # without it.
    assert listed["summary"]["version_scope"] == "node"
    assert listed["summary"]["instance_count"] == 0
    assert listed["summary"]["live_source_count"] == 1


def test_the_workbench_of_a_declared_kind_opens_and_names_what_feeds_it(world):
    """Level 3 resolves by scanning the lens, so absent there was unopenable here."""
    from core.governance_read_model import compose_governance_object  # noqa: PLC0415

    registry_id = _declare_keyed(world)["registry"]["id"]
    _declare_labelled(world)
    md.create_node(
        world["conn"],
        org_id=world["org_id"],
        project_id=world["project_id"],
        registry_id=registry_id,
        node_kind="video",
        label="Burger King review",
        actor="tester",
    )

    detail = compose_governance_object(
        world["project_id"],
        "master-data",
        "registry",
        registry_id,
        world["conn"],
        org_id=world["org_id"],
    )
    assert detail["object"]["summary"]["instance_count"] == 1

    sources = detail["object"]["summary"]["sources"]
    assert sources["state"] == "available"
    by_ns = {row["namespace"]: row for row in sources["rows"]}
    assert set(by_ns) == {KEYED_NS, LABELLED_NS}
    # The Datastream is NAMED, not just referenced: nobody should have to resolve
    # an id in Data to learn what feeds a governed object.
    assert by_ns[KEYED_NS]["datastream_label"] == "workbook video"
    assert by_ns[KEYED_NS]["identity_fields"] == ["video_id"]
    assert by_ns[LABELLED_NS]["identity_fields"] == []
    assert by_ns[LABELLED_NS]["label_field"] == "restaurant_name"


def test_a_kind_no_longer_fed_says_empty_and_a_country_registry_says_unavailable(world):
    """Two different sentences, and collapsing them is the defect this facet avoids.

    `empty` -- this kind is declared and nothing feeds it any more.
    `unavailable` -- this object type has no source binding store at all.
    """
    from core.governance_read_model import (  # noqa: PLC0415
        _client_object_sources,
        compose_governance_object,
    )

    registry_id = _declare_keyed(world)["registry"]["id"]
    okr.release_source_binding(
        world["conn"],
        project_id=world["project_id"],
        registry_id=registry_id,
        namespace=KEYED_NS,
        actor="tester",
    )
    detail = compose_governance_object(
        world["project_id"],
        "master-data",
        "registry",
        registry_id,
        world["conn"],
        org_id=world["org_id"],
    )
    assert detail["object"]["summary"]["sources"]["state"] == "empty"
    assert detail["object"]["summary"]["live_source_count"] == 0

    # A capability registry carries no `object_kind` in its summary and must not
    # be told it is fed by nothing.
    absent = _client_object_sources(world["conn"], "registry", registry_id, {})
    assert absent["state"] == "unavailable"
    assert absent["reason"]["code"] == "master_data_source_bindings_absent"
