"""La decouverte dit ce qu'on peut croiser, et refuse le reste (story 69.4).

WHY THIS FILE EXISTS. `tests/core/test_entity_context.py` proves the ASSEMBLY
of the discovery envelope with every store seam stubbed -- the right test for
an assembly. What it cannot prove is that the two new sections actually READ
what they claim, and that the zero-coverage refusal asks a real authority:

  * `crossable_attributes` reads the attributes node versions carry AND the
    classifications a PUBLISHED rule set derives, and NAMES a type that has
    neither (AC1);
  * `import_freshness` reads the ledger and reports `published` imports only --
    an opened-then-failed run is not freshness (AC1);
  * `assert_cross_has_coverage` refuses on the coverage 68.3 measured, never on
    a second count of its own, and `unavailable` is never treated as zero (AC2).
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
    reason="TEST_POSTGRES_DSN not set -- live Postgres discovery test skipped",
)

ACTOR = "alice@example.com"
KIND = "video"
BARE_KIND = "venue"

PROPERTY_SCHEMA = {"type": "object", "properties": {"duration_seconds": {"type": "number"}}}
RULES = [
    {
        "name": "content_type",
        "rules": [
            {"when": {"field": "duration_seconds", "op": "<", "value": 60}, "then": "short"},
        ],
        "otherwise": "long",
    }
]


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _ulid(prefix: str) -> str:
    """Un id que le CHECK du magasin accepte : prefixe + ULID Crockford."""
    from ulid import ULID

    return f"{prefix}_{ULID()}"


@pytest.fixture()
def world(live_postgres):
    """Two declared types: one carrying an attribute, one carrying nothing."""
    from tests.integration.epic66_fixtures import make_project

    conn = live_postgres
    org_id, project_id = make_project(conn, "Epic 69.4")
    for kind, key in ((KIND, "video_id"), (BARE_KIND, "venue_id")):
        okr.declare_entity_type(
            conn,
            org_id=org_id,
            project_id=project_id,
            object_kind=kind,
            canonical_key=key,
            display_name=kind.title(),
            actor=ACTOR,
        )
    registry = master_data.fetch_registry(conn, project_id=project_id, object_kind=KIND)
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
        label="clip-a",
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


def _crossable(world):
    return okr._crossable_attributes_section(
        world["conn"], project_id=world["project_id"], object_kinds=[KIND, BARE_KIND]
    )


# ---------------------------------------------------------------------------
# AC1: what an agent may cross BY.
# ---------------------------------------------------------------------------


@requires_postgres
def test_a_carried_attribute_is_announced_with_how_many_entities_carry_it(world):
    section = _crossable(world)
    by_kind = {row["object_kind"]: row for row in section["by_object_kind"]}
    assert by_kind[KIND]["carried"] == [{"attribute": "duration_seconds", "node_count": 1}]
    # Rien n'est derive tant qu'aucune regle n'est publiee -- et l'absence est
    # une liste vide, pas une section manquante.
    assert by_kind[KIND]["derived"] == []


@requires_postgres
def test_a_type_nobody_can_cross_by_is_named(world):
    section = _crossable(world)
    # << Cette entite ne porte rien par quoi l'analyser >> est ce qu'un modele
    # doit savoir AVANT de promettre une analyse.
    assert section["without_attribute"] == [BARE_KIND]


@requires_postgres
def test_publishing_a_rule_adds_a_crossable_attribute_with_its_version(world):
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

    by_kind = {row["object_kind"]: row for row in _crossable(world)["by_object_kind"]}
    derived = by_kind[KIND]["derived"]
    assert [entry["attribute"] for entry in derived] == ["content_type"]
    # La version voyage : republier une regle change les reponses sans toucher
    # un fait, donc une reponse qui ne la nomme pas n'est pas re-derivable.
    assert derived[0]["rule_set_version_id"] == version["id"]


# ---------------------------------------------------------------------------
# AC1: how stale the answer would be.
# ---------------------------------------------------------------------------


@requires_postgres
def test_freshness_counts_published_imports_only(world):
    conn = world["conn"]
    ds_id, plan_id, mapping_id = _id("ds_"), _id("dsp_"), _id("dmap_")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.datastreams
                (id, project_id, name, module_name, source_kind, enabled, created_by, org_id)
            VALUES (%s, %s, 'Feed', NULL, 'managed_feed', FALSE, %s, %s)
            """,
            (ds_id, world["project_id"], ACTOR, world["org_id"]),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_plan_versions
                (id, datastream_id, project_id, version_number, contract_version,
                 source_kind, writer_kind, destination_policy, normalized_payload,
                 content_hash, idempotency_key_hash, created_by)
            VALUES (%s, %s, %s, 1, '1', 'managed_feed', 'toorow', 'managed_raw',
                    '{}'::jsonb, repeat('a', 64), repeat('b', 64), %s)
            """,
            (plan_id, ds_id, world["project_id"], ACTOR),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_mapping_versions
                (id, datastream_id, project_id, version_number, mapping_contract_version,
                 source_schema_hash, plan_version_id, content_hash, ossie_spec_version,
                 toorow_extension_version, executable, mapping_payload, ossie_projection,
                 idempotency_key_hash, created_by)
            VALUES (%s, %s, %s, 1, '1', repeat('a', 64), %s, repeat('c', 64), '0.1.1',
                    '1', TRUE, '{}'::jsonb, '{}'::jsonb, repeat('d', 64), %s)
            """,
            (mapping_id, ds_id, world["project_id"], plan_id, ACTOR),
        )
        for outcome, observed in (
            ("published", "2026-08-20 04:00:00+00"),
            # Un import OUVERT puis echoue n'est pas de la fraicheur : le
            # compter laisserait un flux casse avoir l'air a jour.
            ("failed", "2026-08-25 04:00:00+00"),
        ):
            cur.execute(
                """
                INSERT INTO app.managed_feed_import_ledger
                    (id, datastream_id, project_id, plan_version_id, mapping_version_id,
                     feed_format, write_mode, idempotency_key_hash, payload_fingerprint,
                     outcome, snapshot_observed_at, created_by)
                VALUES (%s, %s, %s, %s, %s, 'csv', 'replace', %s, %s, %s, %s::timestamptz, %s)
                """,
                (
                    _ulid("mfl"),
                    ds_id,
                    world["project_id"],
                    plan_id,
                    mapping_id,
                    uuid.uuid4().hex + uuid.uuid4().hex,
                    uuid.uuid4().hex + uuid.uuid4().hex,
                    outcome,
                    observed,
                    ACTOR,
                ),
            )

    section = okr._import_freshness_section(conn, project_id=world["project_id"])
    row = {entry["datastream_id"]: entry for entry in section["by_datastream"]}[ds_id]
    assert row["published_import_count"] == 1
    assert row["last_snapshot_at"].startswith("2026-08-20")
    assert section["never_published"] == []


# ---------------------------------------------------------------------------
# AC2: the zero-coverage refusal.
# ---------------------------------------------------------------------------


@requires_postgres
def test_a_cross_with_no_attached_occurrence_is_refused_naming_the_gesture(world):
    with pytest.raises(ecr.CrossReadRefused) as excinfo:
        ecr.assert_cross_has_coverage(
            world["conn"], project_id=world["project_id"], object_kind=KIND
        )
    assert excinfo.value.code == "cross_no_coverage"
    # Le refus nomme le geste : declarer la liaison, ou enregistrer les alias.
    assert "Declare the key binding" in excinfo.value.message


@requires_postgres
def test_one_resolved_occurrence_is_enough_to_answer(world, monkeypatch):
    conn = world["conn"]
    ds_id = _id("ds_")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.datastreams
                (id, project_id, name, module_name, source_kind, enabled, created_by, org_id)
            VALUES (%s, %s, 'Feed', NULL, 'managed_feed', FALSE, %s, %s)
            """,
            (ds_id, world["project_id"], ACTOR, world["org_id"]),
        )
        cur.execute(
            """
            INSERT INTO app.entity_key_match_verdicts
                (id, org_id, project_id, datastream_id, object_kind, field_id,
                 raw_value_hash, normalized_value, occurrence_count,
                 observed_from, observed_to, verdict, relation, node_id,
                 reason_code, state, created_by)
            VALUES (%s, %s, %s, %s, %s, 'video_id', %s, 'v-1', 3,
                    DATE '2026-08-01', DATE '2026-08-07', 'resolved', 'exact', %s,
                    'exact_lookup', 'current', %s)
            """,
            (
                _ulid("ekmv"),
                world["org_id"],
                world["project_id"],
                ds_id,
                KIND,
                "a" * 64,
                world["node_id"],
                ACTOR,
            ),
        )
    found = ecr.assert_cross_has_coverage(
        conn, project_id=world["project_id"], object_kind=KIND
    )
    assert found["bound"] == 1
    assert found["eligible"] == 1


@requires_postgres
def test_an_unreadable_verdict_store_is_not_a_coverage_of_zero(world, monkeypatch):
    monkeypatch.setattr(
        "core.entity_key_matching.matching_coverage",
        lambda conn, *, project_id: {
            "state": "unavailable",
            "coverage": {"bound": None, "eligible": None, "by_state": {}},
            "rows": [],
            "unavailable_reason": {"code": "entity_key_verdicts_unreadable"},
        },
    )
    with pytest.raises(ecr.CrossReadRefused) as excinfo:
        ecr.assert_cross_has_coverage(
            world["conn"], project_id=world["project_id"], object_kind=KIND
        )
    # Le refus est DIFFERENT : accuser le Projet d'un defaut qui est le notre
    # enverrait quelqu'un reparer ce qui n'est pas casse.
    assert excinfo.value.code == "cross_coverage_unavailable"
