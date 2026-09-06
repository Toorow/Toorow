"""The reference route, end to end, against a real database (Story 68.5).

WHY THIS FILE EXISTS. `tests/core/test_entity_reference_route.py` proves the
ROUTING decision and the row rules off base, and it is right about them. What
it cannot see is the half the acceptance criteria are actually about:

  * a reference file lands as versioned MDM node attributes -- real nodes,
    real `master_data_object_versions` rows, published (AC1);
  * an UNCHANGED snapshot is an honest no-op: freshness moves, no duplicate
    version is minted (AC2);
  * prior versions stay queryable as-of: a changed snapshot APPENDS, and the
    version that was current yesterday is still readable (AC3);
  * a row that cannot name its entity is rejected with per-row evidence IN
    THE LEDGER, never skipped in silence;
  * and -- the defect this story's wiring closed -- the route is actually
    reached: no warehouse relation is allocated for a reference file.

Every write happens on the caller's transaction; `live_postgres` rolls back.
"""

from __future__ import annotations

import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

from core import entity_reference_import as eri  # noqa: E402
from core import object_kind_registry as okr  # noqa: E402
from core.import_runner import run_import  # noqa: E402
from core.tabular_types import CsvExcelImportError  # noqa: E402

requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres route test skipped",
)

ACTOR = "alice@example.com"
KIND = "video"
CONTRACT = {"format": "csv", "write_mode": "replace", "header_row": 1}

#: One designated column, IN the grain, no measures -- the three declarations
#: `reference_designation` reads. Anything else is the warehouse route.
MAPPING_PAYLOAD = {
    "grain": ["video_id"],
    "fields": [
        {
            "field_id": "video_id",
            "physical_type": "string",
            "binding": {
                "status": "confirmed",
                "canonical_target": "video_id",
                "designates_object_kind": KIND,
            },
        },
        {
            "field_id": "title",
            "physical_type": "string",
            "binding": {"status": "confirmed", "canonical_target": "title"},
        },
        {
            "field_id": "lang",
            "physical_type": "string",
            "binding": {"status": "confirmed", "canonical_target": "lang"},
        },
    ],
}
#: The pinned projection the mapping above compiles to. No `additive_measures`:
#: a file carrying measures is a facts file and takes the warehouse route.
PROJECTION = {
    "executable": True,
    "grain": ["video_id"],
    "full_grain_relation": {
        "grain_columns": [{"field_id": "video_id"}],
        "source_fields": ["video_id", "title", "lang"],
    },
}


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _csv(rows) -> bytes:
    lines = ["video_id,title,lang"] + [",".join(row) for row in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


@pytest.fixture()
def world(live_postgres):
    """A managed_feed datastream, its pinned bundle, and a declared type."""
    from tests.integration.epic66_fixtures import make_project

    conn = live_postgres
    org_id, project_id = make_project(conn, "Epic 68.5")
    ds_id, plan_id, mapping_id = _id("ds_"), _id("dsp_"), _id("dmap_")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.datastreams
                (id, project_id, name, module_name, source_kind, enabled, created_by, org_id)
            VALUES (%s, %s, 'Video catalogue', NULL, 'managed_feed', FALSE, %s, %s)
            """,
            (ds_id, project_id, ACTOR, org_id),
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
            (plan_id, ds_id, project_id, ACTOR),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_mapping_versions
                (id, datastream_id, project_id, version_number, mapping_contract_version,
                 source_schema_hash, plan_version_id, content_hash, ossie_spec_version,
                 toorow_extension_version, executable, mapping_payload, ossie_projection,
                 idempotency_key_hash, created_by)
            VALUES (%s, %s, %s, 1, '1', repeat('a', 64), %s, repeat('c', 64), '0.1.1',
                    '1', TRUE, %s::jsonb, '{}'::jsonb, repeat('d', 64), %s)
            """,
            (
                mapping_id,
                ds_id,
                project_id,
                plan_id,
                psycopg.types.json.Json(MAPPING_PAYLOAD),
                ACTOR,
            ),
        )
    declared = okr.declare_entity_type(
        conn,
        org_id=org_id,
        project_id=project_id,
        object_kind=KIND,
        canonical_key="video_id",
        display_name="Videos",
        actor=ACTOR,
    )
    return {
        "conn": conn,
        "org_id": org_id,
        "project_id": project_id,
        "datastream_id": ds_id,
        "plan_version_id": plan_id,
        "mapping_version_id": mapping_id,
        "registry": declared["registry"],
    }


def _import(world, rows, *, key=None, **kwargs):
    return run_import(
        _csv(rows),
        datastream_id=world["datastream_id"],
        project_id=world["project_id"],
        plan_version_id=world["plan_version_id"],
        mapping_version_id=world["mapping_version_id"],
        projection_plan=PROJECTION,
        actor=ACTOR,
        idempotency_key=key or f"68-5-{uuid.uuid4().hex[:10]}",
        source_metadata={"filename": "catalogue.csv"},
        contract=CONTRACT,
        conn=world["conn"],
        mapping_payload=MAPPING_PAYLOAD,
        **kwargs,
    )


def _nodes(world):
    """key -> (node_id, attributes) for every CURRENT version of the registry."""
    with world["conn"].cursor() as cur:
        cur.execute(
            """
            SELECT v.node_id, v.payload, v.import_provenance
            FROM app.master_data_object_versions v
            WHERE v.org_id = %s AND v.registry_id = %s AND v.status = 'current'
            """,
            (world["org_id"], world["registry"]["id"]),
        )
        rows = cur.fetchall()
    out = {}
    for node_id, payload, provenance in rows:
        attributes = (payload or {}).get("attributes") or {}
        out[str(attributes.get("video_id"))] = (str(node_id), attributes, provenance)
    return out


def _version_count(world):
    with world["conn"].cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.master_data_object_versions "
            "WHERE org_id = %s AND registry_id = %s",
            (world["org_id"], world["registry"]["id"]),
        )
        return cur.fetchone()[0]


def _rejected(world, ledger_id):
    with world["conn"].cursor() as cur:
        cur.execute(
            "SELECT row_number, field_name, rule FROM app.managed_feed_rejected_rows "
            "WHERE ledger_id = %s ORDER BY row_number",
            (ledger_id,),
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# AC1: the file lands as versioned MDM attributes -- and NOT as facts.
# ---------------------------------------------------------------------------


@requires_postgres
def test_a_reference_file_lands_as_published_node_versions(world):
    result = _import(world, [["v-1", "Intro", "fr"], ["v-2", "Outro", "en"]])

    assert result["route"] == eri.ROUTE_ENTITY_REFERENCE
    assert result["published"] is True
    assert result["blocked"] is False
    assert result["landing"]["nodes_created"] == 2
    assert result["landing"]["versions_minted"] == 2
    # The ledger's evidence names the registry, never a schema.table: no raw
    # relation was allocated for a file that describes entities.
    assert result["landing"]["table"].startswith(eri.LANDING_RELATION_PREFIX)

    landed = _nodes(world)
    assert set(landed) == {"v-1", "v-2"}
    assert landed["v-1"][1] == {"video_id": "v-1", "title": "Intro", "lang": "fr"}
    # AD-7: every minted version names the execution, the pinned mapping and
    # the ledger row that landed it (migration 295).
    provenance = landed["v-1"][2]
    assert provenance["execution_id"] == result["execution"]["id"]
    assert provenance["mapping_version_id"] == world["mapping_version_id"]
    assert provenance["ledger_id"] == result["ledger"]["id"]


@requires_postgres
def test_the_reference_route_allocates_no_warehouse_relation(world):
    result = _import(world, [["v-1", "Intro", "fr"]])
    with world["conn"].cursor() as cur:
        cur.execute(
            "SELECT landing_relation FROM app.managed_feed_import_ledger WHERE id = %s",
            (result["ledger"]["id"],),
        )
        relation = cur.fetchone()[0]
    # A `managed_feed_*` relation here would mean the rows landed as facts --
    # the silent fact-landing this story exists to refuse.
    assert relation.startswith(eri.LANDING_RELATION_PREFIX)
    assert "managed_feed_" not in relation


# ---------------------------------------------------------------------------
# AC2: an unchanged snapshot is an honest no-op.
# ---------------------------------------------------------------------------


@requires_postgres
def test_a_byte_identical_re_import_mints_nothing(world):
    rows = [["v-1", "Intro", "fr"], ["v-2", "Outro", "en"]]
    _import(world, rows)
    before = _version_count(world)

    again = _import(world, rows)

    # The ledger's unchanged-snapshot oracle answers first -- and it can only
    # answer because the first landing marked its row `published`.
    assert again["outcome"] == "noop"
    assert _version_count(world) == before


@requires_postgres
def test_a_changed_snapshot_mints_only_the_entities_that_moved(world):
    _import(world, [["v-1", "Intro", "fr"], ["v-2", "Outro", "en"]])
    before = _version_count(world)

    result = _import(world, [["v-1", "Intro", "fr"], ["v-2", "Outro RE-CUT", "en"]])

    assert result["landing"]["nodes_created"] == 0
    assert result["landing"]["versions_minted"] == 1
    assert result["landing"]["entities_unchanged"] == 1
    assert _version_count(world) == before + 1
    assert _nodes(world)["v-2"][1]["title"] == "Outro RE-CUT"


# ---------------------------------------------------------------------------
# AC3: prior versions stay queryable as-of.
# ---------------------------------------------------------------------------


@requires_postgres
def test_the_prior_version_stays_readable_after_a_change(world):
    _import(world, [["v-1", "Intro", "fr"]])
    node_id = _nodes(world)["v-1"][0]
    _import(world, [["v-1", "Introduction", "fr"]])

    with world["conn"].cursor() as cur:
        cur.execute(
            "SELECT status, payload->'attributes'->>'title' "
            "FROM app.master_data_object_versions "
            "WHERE node_id = %s ORDER BY version_number",
            (node_id,),
        )
        chain = cur.fetchall()

    # Two versions, one node: the change APPENDED. A reclassification never
    # rewrites what the catalogue said yesterday.
    assert [row[1] for row in chain] == ["Intro", "Introduction"]
    assert chain[-1][0] == "current"
    assert chain[0][0] != "current"


# ---------------------------------------------------------------------------
# Per-row evidence: a row that names no entity is rejected, never dropped.
# ---------------------------------------------------------------------------


@requires_postgres
def test_a_key_less_row_is_rejected_with_its_line_in_the_ledger(world):
    # Five named entities and ONE orphan: below the rejection threshold, so the
    # import lands -- and the orphan is evidence, not a silence.
    rows = [[f"v-{n}", f"Clip {n}", "fr"] for n in range(1, 6)]
    rows.insert(2, ["", "Orphan", "fr"])
    result = _import(world, rows)

    assert result["blocked"] is False
    assert result["rejected_count"] == 1
    assert result["landing"]["versions_minted"] == 5
    rejected = _rejected(world, result["ledger"]["id"])
    assert len(rejected) == 1
    assert rejected[0][1] == "video_id"
    assert rejected[0][2] == eri.RULE_KEY_MISSING
    assert set(_nodes(world)) == {f"v-{n}" for n in range(1, 6)}


@requires_postgres
def test_too_many_key_less_rows_refuse_the_import_and_write_no_node(world):
    rows = [["v-1", "Intro", "fr"]] + [["", f"Orphan {n}", "fr"] for n in range(8)]
    result = _import(world, rows)

    assert result["blocked"] is True
    assert result["reason"] == "rejection_threshold_exceeded"
    assert result["outcome"] == "rejected"
    # The whole landing went back with the savepoint: a refused import leaves
    # NOTHING behind, exactly like a discarded warehouse candidate.
    assert _nodes(world) == {}


# ---------------------------------------------------------------------------
# The route refuses what it cannot honour, before minting anything.
# ---------------------------------------------------------------------------


@requires_postgres
def test_a_governed_dispatch_is_refused_naming_the_repair(world):
    with pytest.raises(CsvExcelImportError) as excinfo:
        _import(world, [["v-1", "Intro", "fr"]], publish_candidate=True)
    assert excinfo.value.code == "entity_reference_publication_is_the_import"
    # Refused BEFORE a ledger row exists: nothing to reconcile afterwards.
    with world["conn"].cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.managed_feed_import_ledger WHERE datastream_id = %s",
            (world["datastream_id"],),
        )
        assert cur.fetchone()[0] == 0


@requires_postgres
def test_an_archived_type_refuses_the_import_rather_than_writing_to_it(world):
    from core import master_data

    master_data.set_registry_state(
        world["conn"],
        project_id=world["project_id"],
        registry_id=world["registry"]["id"],
        lifecycle_state="disabled",
    )
    with pytest.raises(CsvExcelImportError) as excinfo:
        _import(world, [["v-1", "Intro", "fr"]])
    assert excinfo.value.code == "entity_reference_designation_invalid"
