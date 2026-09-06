"""LA CHAINE ENTIERE, sur une base reelle : fichier -> entite -> regle -> croisement.

CE QUE CE FICHIER GARDE (story 69.5, AC3). Chaque story des epics 68 et 69 est
prouvee chez elle. Aucune ne prouve que les maillons TIENNENT ENSEMBLE, et
c'est precisement ce qui casse en silence : une story change une cle, sa voisine
la lit encore sous l'ancien nom, les deux suites restent vertes et la chaine ne
transporte plus rien.

LE PARCOURS, DANS L'ORDRE OU UN UTILISATEUR LE FAIT :

  1. il DECLARE son type d'entite (68.1) ;
  2. il importe son CATALOGUE -- le mapping designe la colonne cle, la route
     reference envoie les lignes en attributs MDM versionnes (68.2, 68.5) ;
  3. il importe son CALENDRIER -- le mapping declare des roles d'evenement, la
     route evenements pose des marqueurs rattaches au Datastream (68.4) ;
  4. la plateforme RATTACHE les cles observees a ses entites, avec un verdict
     par occurrence (68.3) ;
  5. il publie une REGLE qui derive une classification (68.6) ;
  6. la decouverte annonce ce qu'il peut croiser, et avec quelle couverture
     (68.7, 69.4) ;
  7. le croisement est autorise, et sa reponse nomme son chemin et sa version
     (69.3).

CE QUI N'EST PAS ICI, ET POURQUOI. Le maillon 8 -- la REPONSE d'un modele qui
cite tout cela -- demande un deploiement et un appel de modele ; il est declare
`Unverifiable` dans le Dev Agent Record de la story avec sa raison, jamais
maquille en succes. Le maillon 7 cote entrepot (le SQL du croisement) est
prouve par `tests/conformance/test_semantic_fact_by_entity_attribute.py` sur un
build dbt reel : il lui faut DuckDB, pas Postgres.
"""

from __future__ import annotations

import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

from core import context_event_import as cei  # noqa: E402
from core import entity_cross_read as ecr  # noqa: E402
from core import entity_key_matching as ekm  # noqa: E402
from core import entity_reference_import as eri  # noqa: E402
from core import entity_rule_derivation as erd  # noqa: E402
from core import master_data  # noqa: E402
from core import object_kind_registry as okr  # noqa: E402
from core.import_runner import run_import  # noqa: E402

requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- the end-to-end chain needs a live Postgres",
)

ACTOR = "alice@example.com"
KIND = "video"
CONTRACT = {"format": "csv", "write_mode": "replace", "header_row": 1}


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _hash() -> str:
    """Trois Datastreams dans UN projet : les empreintes doivent differer.

    `uq_datastream_plan_idempotency` porte (project_id, idempotency_key_hash) :
    une constante partagee ferait echouer le deuxieme flux, ce qu'un test a UN
    seul flux ne peut pas voir.
    """
    return uuid.uuid4().hex + uuid.uuid4().hex


def _known_event_type() -> str:
    from core.report_dictionary import _load_event_type_dictionary

    types = sorted(_load_event_type_dictionary())
    assert types, "the canonical event dictionary is empty -- this test proves nothing"
    return types[0]


# --- Le catalogue : une colonne cle designee, dans la maille, sans mesure. ----
CATALOGUE_MAPPING = {
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
            "field_id": "duration_seconds",
            "physical_type": "integer",
            "binding": {"status": "confirmed", "canonical_target": "duration_seconds"},
        },
    ],
}
CATALOGUE_PROJECTION = {
    "executable": True,
    "grain": ["video_id"],
    "full_grain_relation": {
        "grain_columns": [{"field_id": "video_id"}],
        "source_fields": ["video_id", "duration_seconds"],
    },
}

# --- Le calendrier : des roles d'evenement. ----------------------------------
CALENDAR_MAPPING = {
    "grain": ["day", "headline"],
    "fields": [
        {
            "field_id": "day",
            "physical_type": "string",
            "binding": {
                "status": "confirmed",
                "canonical_target": "day",
                "event_role": "date",
            },
        },
        {
            "field_id": "kind",
            "physical_type": "string",
            "binding": {
                "status": "confirmed",
                "canonical_target": "kind",
                "event_role": "type",
            },
        },
        {
            "field_id": "headline",
            "physical_type": "string",
            "binding": {
                "status": "confirmed",
                "canonical_target": "headline",
                "event_role": "label",
            },
        },
    ],
}
CALENDAR_PROJECTION = {
    "executable": True,
    "grain": ["day", "headline"],
    "full_grain_relation": {
        "grain_columns": [{"field_id": "day"}, {"field_id": "headline"}],
        "source_fields": ["day", "kind", "headline"],
    },
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


def _datastream(conn, *, org_id, project_id, name, payload):
    """A managed_feed Datastream pinning one mapping version."""
    ds_id, plan_id, mapping_id = _id("ds_"), _id("dsp_"), _id("dmap_")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.datastreams
                (id, project_id, name, module_name, source_kind, enabled, created_by, org_id)
            VALUES (%s, %s, %s, NULL, 'managed_feed', FALSE, %s, %s)
            """,
            (ds_id, project_id, name, ACTOR, org_id),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_plan_versions
                (id, datastream_id, project_id, version_number, contract_version,
                 source_kind, writer_kind, destination_policy, normalized_payload,
                 content_hash, idempotency_key_hash, created_by)
            VALUES (%s, %s, %s, 1, '1', 'managed_feed', 'toorow', 'managed_raw',
                    '{}'::jsonb, %s, %s, %s)
            """,
            (plan_id, ds_id, project_id, _hash(), _hash(), ACTOR),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_mapping_versions
                (id, datastream_id, project_id, version_number, mapping_contract_version,
                 source_schema_hash, plan_version_id, content_hash, ossie_spec_version,
                 toorow_extension_version, executable, mapping_payload, ossie_projection,
                 idempotency_key_hash, created_by)
            VALUES (%s, %s, %s, 1, '1', %s, %s, %s, '0.1.1',
                    '1', TRUE, %s::jsonb, '{}'::jsonb, %s, %s)
            """,
            (
                mapping_id,
                ds_id,
                project_id,
                _hash(),
                plan_id,
                _hash(),
                psycopg.types.json.Json(payload),
                _hash(),
                ACTOR,
            ),
        )
        cur.execute(
            "UPDATE app.datastreams SET current_mapping_version_id = %s WHERE id = %s",
            (mapping_id, ds_id),
        )
    return {"id": ds_id, "plan_version_id": plan_id, "mapping_version_id": mapping_id}


def _import(conn, stream, *, project_id, csv_bytes, payload, projection):
    return run_import(
        csv_bytes,
        datastream_id=stream["id"],
        project_id=project_id,
        plan_version_id=stream["plan_version_id"],
        mapping_version_id=stream["mapping_version_id"],
        projection_plan=projection,
        actor=ACTOR,
        idempotency_key=f"69-5-{uuid.uuid4().hex[:10]}",
        source_metadata={"filename": "file.csv"},
        contract=CONTRACT,
        conn=conn,
        mapping_payload=payload,
    )


@requires_postgres
def test_the_whole_chain_carries_a_file_to_a_crossable_classification(live_postgres):
    from tests.integration.epic66_fixtures import make_project

    conn = live_postgres
    org_id, project_id = make_project(conn, "Epic 69.5")

    # --- 1. L'utilisateur DECLARE son type d'entite (68.1). ------------------
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

    # --- 2. Il importe son CATALOGUE (68.2 + 68.5). --------------------------
    catalogue = _datastream(
        conn, org_id=org_id, project_id=project_id, name="Catalogue", payload=CATALOGUE_MAPPING
    )
    landed = _import(
        conn,
        catalogue,
        project_id=project_id,
        csv_bytes=b"video_id,duration_seconds\nv-1,42\nv-2,3600\n",
        payload=CATALOGUE_MAPPING,
        projection=CATALOGUE_PROJECTION,
    )
    assert landed["route"] == eri.ROUTE_ENTITY_REFERENCE
    assert landed["landing"]["nodes_created"] == 2

    # --- 3. Il importe son CALENDRIER (68.4). --------------------------------
    calendar = _datastream(
        conn, org_id=org_id, project_id=project_id, name="Calendrier", payload=CALENDAR_MAPPING
    )
    marked = _import(
        conn,
        calendar,
        project_id=project_id,
        csv_bytes=f"day,kind,headline\n2026-08-01,{_known_event_type()},Spring push\n".encode(),
        payload=CALENDAR_MAPPING,
        projection=CALENDAR_PROJECTION,
    )
    assert marked["route"] == cei.ROUTE_CONTEXT_EVENTS
    assert marked["landing"]["events_written"] == 1

    # --- 4. La plateforme RATTACHE les cles observees (68.3). ----------------
    # Les noeuds que le catalogue vient de creer portent leur cle comme LABEL ;
    # un alias exact est ce sur quoi le rattachement se prononce.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, label FROM app.master_data_nodes WHERE registry_id = %s",
            (registry["id"],),
        )
        nodes = cur.fetchall()
    for node_id, label in nodes:
        master_data.record_alias(
            conn,
            org_id=org_id,
            node_id=str(node_id),
            namespace="catalogue",
            raw_value=str(label),
            relation="exact",
            actor=ACTOR,
        )

    facts = _datastream(
        conn, org_id=org_id, project_id=project_id, name="Vues", payload=CATALOGUE_MAPPING
    )
    written = _record_verdicts(conn, facts, project_id, org_id)
    assert written == 2

    coverage = ekm.matching_coverage(conn, project_id=project_id)
    assert coverage["coverage"]["bound"] == 2

    # --- 5. Il publie une REGLE qui derive une classification (68.6). --------
    version = erd.draft_entity_rule_set(
        conn,
        org_id=org_id,
        project_id=project_id,
        object_kind=KIND,
        derived_attributes=RULES,
        label="Video classifications",
        actor=ACTOR,
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.governance_rule_sets "
            "WHERE project_id = %s AND family = 'entity_derivation' AND name = %s",
            (project_id, KIND),
        )
        head_id = cur.fetchone()[0]
    erd.publish_entity_rule_set(
        conn,
        project_id=project_id,
        rule_set_id=head_id,
        version_id=version["id"],
        actor=ACTOR,
    )

    resolved = erd.resolve_derived_attributes(conn, project_id=project_id, object_kind=KIND)
    # Le fichier disait 42 s et 3600 s ; la regle de l'utilisateur en fait
    # `short` et `long`. C'est le moment ou la donnee devient SES mots.
    assert sorted(
        entry["content_type"] for entry in resolved["nodes"].values()
    ) == ["long", "short"]

    # --- 6. La decouverte ANNONCE ce qu'il peut croiser (68.7 + 69.4). -------
    context = okr.describe_entity_reconciliation_context(conn, project_id=project_id)["data"]
    assert context["entity_types"]["count"] == 1
    assert context["matching_coverage"]["status"] == "ok"
    crossable = {
        row["object_kind"]: row for row in context["crossable_attributes"]["by_object_kind"]
    }
    assert [entry["attribute"] for entry in crossable[KIND]["derived"]] == ["content_type"]
    assert crossable[KIND]["derived"][0]["rule_set_version_id"] == version["id"]
    freshness = {
        row["datastream_id"]: row for row in context["import_freshness"]["by_datastream"]
    }
    assert freshness[catalogue["id"]]["published_import_count"] == 1

    # --- 7. Le croisement est AUTORISE, et sa reponse se cite (69.3). --------
    authority = ecr.assert_attribute_is_published(
        conn, project_id=project_id, object_kind=KIND, attribute="content_type"
    )
    assert authority["origin"] == ecr.ORIGIN_DERIVED
    assert ecr.assert_cross_has_coverage(
        conn, project_id=project_id, object_kind=KIND
    )["bound"] == 2

    answer = ecr.compose_cross(
        [
            {
                "attribute_origin": ecr.ORIGIN_DERIVED,
                "attribute": "content_type",
                "attribute_value": "short",
                "resolution_state": "resolved",
                "rule_set_version_id": version["id"],
                "value": 100.0,
            }
        ],
        attribute="content_type",
    )
    meta = ecr.cross_meta(answer)
    # La reponse nomme SA relation et SA version de regle. Sans les deux, elle
    # n'est pas re-derivable, donc pas verifiable.
    assert meta["analytical_path"]["relation"] == ecr.CROSS_RELATION
    assert meta["rule_set_version_ids"] == [version["id"]]


def _record_verdicts(conn, facts, project_id, org_id):
    """Le pilote de 68.3, avec la SEULE doublure de la chaine : la lecture brute.

    `verification.distinct_raw_values` lit une relation de landing d'entrepot ;
    la batir ici ferait de ce test un build dbt deguise, et cette moitie-la est
    deja prouvee par la conformance. Ce qui est mesure ici est le maillon : les
    valeurs observees deviennent des verdicts persistes contre les entites que
    le CATALOGUE vient de creer.
    """
    import core.verification as verification

    original = verification.distinct_raw_values
    verification.distinct_raw_values = lambda pull_id, fields, **kwargs: (
        {"video_id": {"v-1": 5, "v-2": 3}} if "video_id" in fields else {}
    )
    try:
        return ekm.record_verdicts_for_pull(
            conn,
            datastream={"id": facts["id"], "module_name": None, "report_profile_id": None},
            project_id=project_id,
            pull_id="dse_CHAIN",
            date_from="2026-08-01",
            date_to="2026-08-07",
            actor=ACTOR,
            org_id=org_id,
        )
    finally:
        verification.distinct_raw_values = original
