"""Story 62.1 -- the MMM extract against a real store and a real warehouse.

WHAT THIS PROVES THAT THE UNIT SUITE CANNOT. `test_mmm_export.py` scripts the
connection and stubs the read, so it proves the gates and the file shape. Here
the Semantic View, its bindings, its Output version, the canonical fields and the
measurement grain are ROWS in Postgres, the landing is a table in DuckDB, and the
rows in the file were summed by the warehouse. Every line of the file is then
traced back to the exact grain version, Output version and relation the
provenance names -- which is the whole claim of the story and cannot be made
against a stub.

NOTHING HERE NAMES A CONNECTOR. The landing is a plain daily table with a source
currency beside its amount, which is the shape twelve staging models of this
repository carry (`money_provenance_columns`).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb
import pytest
from ulid import ULID

_SERVER_DIR = Path(__file__).resolve().parents[2]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from core import metric_dimensions, mmm_export  # noqa: E402
from core.analytics_alignment_read import (  # noqa: E402
    REFUSAL_DIMENSION_NOT_IN_GRAIN,
)
from core.mmm_export import MmmExportRefused, build_extract, extract_csv  # noqa: E402

pytestmark = pytest.mark.pg

_HASH = "b" * 64
_RELATION = "main_marts.mmm_media_daily"
_AUTHOR = "story-62.1-harness"

#: The window the fixture lands, and the one day it deliberately does NOT land --
#: the silent hole the epic names ("une semaine manquante change le coefficient
#: d'un modele sans que personne ne le voie").
_START, _END = "2026-07-01", "2026-07-04"
_MISSING_DAY = "2026-07-03"


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


@pytest.fixture()
def warehouse(tmp_path, monkeypatch):
    """A daily landing: day x channel, an amount with its source currency, a count."""
    path = tmp_path / "mmm.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
    con.execute(
        "CREATE TABLE main_marts.mmm_media_daily ("
        "project_id VARCHAR, date VARCHAR, channel VARCHAR, "
        "cost DOUBLE, cost_source_currency VARCHAR, clicks BIGINT, "
        "pull_id VARCHAR, loaded_at VARCHAR)"
    )
    con.execute(
        "INSERT INTO main_marts.mmm_media_daily VALUES "
        "('default','2026-07-01','search',100.0,'EUR',10,'p','l'),"
        "('default','2026-07-01','social',50.0,'EUR',5,'p','l'),"
        "('default','2026-07-02','search',120.0,'EUR',12,'p','l'),"
        # 2026-07-03 lands NOTHING, on purpose.
        "('default','2026-07-04','search',90.0,'EUR',9,'p','l')"
    )
    con.close()
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(path))
    return str(path)


@pytest.fixture()
def second_currency(warehouse):
    """The same landing, with one day reported in another currency."""
    con = duckdb.connect(warehouse)
    con.execute(
        "INSERT INTO main_marts.mmm_media_daily VALUES "
        "('default','2026-07-02','social',70.0,'USD',7,'p','l')"
    )
    con.close()
    return warehouse


def _seed(
    conn,
    *,
    cost_value_type="money",
    grain_members=("date", "channel"),
    view_status="published",
) -> dict:
    """Org -> project -> canonical fields -> grain -> published View over the landing."""
    ids = {
        "org": _uid("org"),
        "project": _uid("proj"),
        "ds": _uid("ds"),
        "plan": _uid("dpv"),
        "mapping": _uid("dmv"),
        "view": f"sv_{ULID()}",
        "view_v": f"svv_{ULID()}",
    }
    concepts = {
        "date": ("dimension", "date"),
        "channel": ("dimension", "string"),
        "cost": ("metric", cost_value_type),
        "clicks": ("metric", "integer"),
    }
    mapping_payload = {
        "grain": ["date", "channel"],
        "fields": [
            {"field_id": name, "physical_type": "string" if kind == "dimension" else "number",
             "binding": {"canonical_target": name, "status": "confirmed"}}
            for name, (kind, _vt) in concepts.items()
        ],
    }
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s,%s,%s,%s)",
            (ids["org"], "62.1", ids["org"].lower().replace("_", "-"), _AUTHOR))
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
            "VALUES (%s,%s,%s,%s,%s)",
            (ids["project"], ids["org"], "62.1",
             ids["project"].lower().replace("_", "-"), _AUTHOR))

        # The canonical vocabulary a measurement grain is declared over.
        for name, (kind, value_type) in concepts.items():
            ids[f"cf_{name}"] = _uid("mdm")
            cur.execute(
                "INSERT INTO app.mdm_canonical_fields "
                "(id, project_id, concept_kind, canonical_name, aggregation, "
                " value_type, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (ids[f"cf_{name}"], ids["project"], kind, name,
                 "sum" if kind == "metric" else None,
                 "decimal" if value_type == "money" else value_type, _AUTHOR))

        # The Semantic Concepts and their published versions.
        for name, (kind, value_type) in concepts.items():
            ids[f"sc_{name}"] = f"sc_{ULID()}"
            ids[f"scv_{name}"] = f"scv_{ULID()}"
            cur.execute(
                "INSERT INTO app.semantic_concepts (id, project_id, kind, name, created_by) "
                "VALUES (%s,%s,%s,%s,%s)",
                (ids[f"sc_{name}"], ids["project"], kind, name, _AUTHOR))
            cur.execute(
                """INSERT INTO app.semantic_concept_versions
                     (id, concept_id, project_id, version_number, status, kind, name,
                      label, value_type, expression, aggregation, additivity_class,
                      semantic_type, allowed_grains, content_hash, created_by)
                   VALUES (%s,%s,%s,1,'published',%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,
                           %s,%s,%s,%s)""",
                (ids[f"scv_{name}"], ids[f"sc_{name}"], ids["project"], kind, name,
                 name.title(), value_type,
                 json.dumps({"op": "sum", "field": name}) if kind == "metric" else None,
                 json.dumps({"type": "sum"}) if kind == "metric" else None,
                 "additive" if kind == "metric" else None,
                 None if kind == "metric" else "categorical", [], _HASH, _AUTHOR))
            cur.execute(
                "UPDATE app.semantic_concepts SET current_version_id = %s WHERE id = %s",
                (ids[f"scv_{name}"], ids[f"sc_{name}"]))

        cur.execute("INSERT INTO app.semantic_views (id, project_id, name, created_by) "
                    "VALUES (%s,%s,'media_daily',%s)",
                    (ids["view"], ids["project"], _AUTHOR))
        cur.execute(
            """INSERT INTO app.semantic_view_versions
                 (id, view_id, project_id, version_number, status, name, label,
                  dependency_fingerprint, content_hash, created_by)
               VALUES (%s,%s,%s,1,%s,'media_daily','Media daily',%s,%s,%s)""",
            (ids["view_v"], ids["view"], ids["project"], view_status, _HASH, _HASH,
             _AUTHOR))
        cur.execute("UPDATE app.semantic_views SET current_version_id = %s WHERE id = %s",
                    (ids["view_v"], ids["view"]))
        for ordinal, name in enumerate(concepts):
            cur.execute(
                """INSERT INTO app.semantic_view_version_concepts
                     (view_version_id, ordinal, concept_id, concept_version_id, role)
                   VALUES (%s,%s,%s,%s,%s)""",
                (ids["view_v"], ordinal, ids[f"sc_{name}"], ids[f"scv_{name}"],
                 concepts[name][0]))

        cur.execute(
            "INSERT INTO app.datastreams (id, org_id, project_id, name, created_by, "
            "source_kind) VALUES (%s,%s,%s,'Media source',%s,'managed_feed')",
            (ids["ds"], ids["org"], ids["project"], _AUTHOR))
        cur.execute(
            """INSERT INTO app.datastream_plan_versions
                 (id, datastream_id, project_id, version_number, contract_version,
                  source_kind, writer_kind, destination_policy, normalized_payload,
                  content_hash, idempotency_key_hash, created_by)
               VALUES (%s,%s,%s,1,'plan.v1','connector_pull','toorow','managed_raw',
                       '{}'::jsonb,%s,%s,%s)""",
            (ids["plan"], ids["ds"], ids["project"], _HASH, _HASH, _AUTHOR))
        cur.execute(
            """INSERT INTO app.datastream_mapping_versions
                 (id, datastream_id, project_id, version_number, mapping_contract_version,
                  source_schema_hash, plan_version_id, content_hash, ossie_spec_version,
                  toorow_extension_version, mapping_payload, ossie_projection,
                  idempotency_key_hash, created_by)
               VALUES (%s,%s,%s,1,'mapping.v1',%s,%s,%s,'0.1.1','2',%s::jsonb,'{}'::jsonb,
                       %s,%s)""",
            (ids["mapping"], ids["ds"], ids["project"], _HASH, ids["plan"], _HASH,
             json.dumps(mapping_payload), _HASH, _AUTHOR))
        cur.execute("UPDATE app.datastreams SET current_mapping_version_id = %s WHERE id = %s",
                    (ids["mapping"], ids["ds"]))
        for ordinal, name in enumerate(concepts):
            cur.execute(
                """INSERT INTO app.semantic_view_version_bindings
                     (view_version_id, ordinal, concept_id, datastream_id, project_id,
                      mapping_version_id, binding_state)
                   VALUES (%s,%s,%s,%s,%s,%s,'active')""",
                (ids["view_v"], ordinal, ids[f"sc_{name}"], ids["ds"], ids["project"],
                 ids["mapping"]))

        execution_id, output_id = _uid("dse"), _uid("dso")
        ids["output_version"] = _uid("dsov")
        cur.execute(
            """INSERT INTO app.datastream_executions
                 (id, datastream_id, project_id, plan_version_id, mapping_version_id,
                  projection_plan_ref, state, created_by)
               VALUES (%s,%s,%s,%s,%s,'{}'::jsonb,'published',%s)""",
            (execution_id, ids["ds"], ids["project"], ids["plan"], ids["mapping"], _AUTHOR))
        cur.execute(
            """INSERT INTO app.datastream_outputs
                 (id, org_id, project_id, datastream_id, output_kind, stable_name, created_by)
               VALUES (%s,%s,%s,%s,'full_grain',%s,%s)""",
            (output_id, ids["org"], ids["project"], ids["ds"], _RELATION, _AUTHOR))
        cur.execute(
            """INSERT INTO app.datastream_output_versions
                 (id, output_id, org_id, project_id, datastream_id, execution_id,
                  plan_version_id, mapping_version_id, relation_ref, grain_evidence,
                  evidence, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'{}'::jsonb,'{}'::jsonb,%s)""",
            (ids["output_version"], output_id, ids["org"], ids["project"], ids["ds"],
             execution_id, ids["plan"], ids["mapping"], _RELATION, _AUTHOR))

    # The governed grains. Declared through the module that owns them, never by
    # an INSERT: a fixture that wrote the rows itself would prove the export
    # against a grain the MDM would have refused.
    for metric in ("cost", "clicks"):
        grain = metric_dimensions.create_measurement_grain(
            conn,
            project_id=ids["project"],
            name=f"{metric} by day and channel",
            head_field_id=ids[f"cf_{metric}"],
            member_field_ids=[ids[f"cf_{name}"] for name in grain_members],
            actor=_AUTHOR,
        )
        ids[f"grain_{metric}"] = grain["id"]
        ids[f"grain_version_{metric}"] = (grain.get("current_version") or {}).get("id")
    return ids


def _extract(conn, ids, **kwargs):
    call = {
        "project_id": ids["project"],
        "semantic_view_version_id": ids["view_v"],
        "metrics": ["cost"],
        "dimensions": ["channel"],
        "start": _START,
        "end": _END,
    }
    call.update(kwargs)
    return build_extract(conn, **call)


# ---------------------------------------------------------------------------
# The file, read off a real relation.
# ---------------------------------------------------------------------------


def test_the_extract_is_long_and_every_row_is_traceable_to_its_grain_and_relation(
    live_postgres, warehouse
):
    ids = _seed(live_postgres)
    payload = _extract(live_postgres, ids, metrics=["cost", "clicks"])

    assert payload["columns"] == [
        "date", "channel", "metric", "value", "currency", "measurement_grain_version_id"
    ]
    rows = {
        (row["date"], row["channel"], row["metric"]): row["value"]
        for row in payload["rows"]
    }
    assert rows[("2026-07-01", "search", "cost")] == 100.0
    assert rows[("2026-07-01", "social", "cost")] == 50.0
    assert rows[("2026-07-04", "search", "clicks")] == 9

    provenance = payload["provenance"]
    # EVERY ROW carries the version that sanctioned its cut, and it is the
    # version the MDM actually minted -- not a string this test invented.
    for row in payload["rows"]:
        expected = ids[f"grain_version_{row['metric']}"]
        assert row["measurement_grain_version_id"] == expected
    assert provenance["measurement_grains"]["cost"]["measurement_grain_id"] == (
        ids["grain_cost"]
    )
    # And to the relation, the Output version and the Datastream it was read from.
    assert provenance["relation"] == _RELATION
    assert provenance["output_version_id"] == ids["output_version"]
    assert provenance["datastream_id"] == ids["ds"]
    assert provenance["semantic_view_version_id"] == ids["view_v"]
    assert provenance["writes"].startswith("none")


def test_the_missing_day_is_named_in_the_provenance_and_not_absorbed(
    live_postgres, warehouse
):
    ids = _seed(live_postgres)
    provenance = _extract(live_postgres, ids)["provenance"]
    gaps = provenance["date_gaps"]
    assert gaps["per_metric"]["cost"]["dates"] == [_MISSING_DAY]
    assert gaps["total"] == 1
    assert provenance["window"] == {"start": _START, "end": _END, "days": 4}


def test_the_csv_holds_exactly_the_rows_the_read_returned(live_postgres, warehouse):
    ids = _seed(live_postgres)
    payload = _extract(live_postgres, ids)
    lines = extract_csv(payload).decode("utf-8").strip().splitlines()
    assert lines[0] == ",".join(payload["columns"])
    assert len(lines) == 1 + len(payload["rows"])
    assert all("EUR" in line for line in lines[1:])


def test_the_extract_writes_nothing_anywhere(live_postgres, warehouse):
    """A read is a read. Nothing in `app` moves, and no relation is created."""
    ids = _seed(live_postgres)
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT relname, n_tup_ins + n_tup_upd + n_tup_del FROM pg_stat_xact_user_tables"
        )
        before = dict(cur.fetchall())
    _extract(live_postgres, ids, metrics=["cost", "clicks"])
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT relname, n_tup_ins + n_tup_upd + n_tup_del FROM pg_stat_xact_user_tables"
        )
        after = dict(cur.fetchall())
    moved = {
        name: (before.get(name, 0), count)
        for name, count in after.items()
        if count != before.get(name, 0)
    }
    assert moved == {}, moved

    con = duckdb.connect(warehouse, read_only=True)
    try:
        tables = {row[0] for row in con.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()}
    finally:
        con.close()
    assert tables == {"mmm_media_daily"}, tables


# ---------------------------------------------------------------------------
# The refusals, against the real stores.
# ---------------------------------------------------------------------------


def test_two_currencies_over_the_window_refuse_the_file(live_postgres, second_currency):
    ids = _seed(live_postgres)
    with pytest.raises(MmmExportRefused) as caught:
        _extract(live_postgres, ids)
    assert caught.value.code == "CROSS_CURRENCY_REFUSAL"
    assert caught.value.detail["currencies"] == ["EUR", "USD"]


def test_a_currency_outside_the_window_does_not_refuse_the_file(
    live_postgres, second_currency
):
    """The probe is bounded by the window, so a later switch does not veto today."""
    ids = _seed(live_postgres)
    payload = _extract(live_postgres, ids, start=_START, end="2026-07-01")
    assert payload["provenance"]["currencies"]["cost"]["currency"] == "EUR"


def test_a_dimension_no_grain_relates_to_the_metric_is_refused_before_the_warehouse(
    live_postgres, warehouse
):
    ids = _seed(live_postgres, grain_members=("date",))
    with pytest.raises(MmmExportRefused) as caught:
        _extract(live_postgres, ids)
    # THE GRAIN'S OWN CONSTANT, imported. A string retyped here would let the
    # export answer a code the MDM no longer emits and still read green.
    assert caught.value.code == REFUSAL_DIMENSION_NOT_IN_GRAIN
    assert "cost" in caught.value.message


def test_a_draft_view_version_is_refused_against_the_real_store(live_postgres, warehouse):
    """Seeded as a draft, never demoted: `reject_semantic_version_mutation` is a
    trigger, and a published version cannot be edited into a draft. The state is
    the one a View is actually in before somebody publishes it."""
    ids = _seed(live_postgres, view_status="draft")
    with pytest.raises(MmmExportRefused) as caught:
        _extract(live_postgres, ids)
    assert caught.value.code == mmm_export.REFUSAL_VIEW_NOT_PUBLISHED


def test_a_view_version_of_another_project_does_not_resolve(live_postgres, warehouse):
    ids = _seed(live_postgres)
    other = _seed(live_postgres)
    with pytest.raises(MmmExportRefused) as caught:
        build_extract(
            live_postgres,
            project_id=other["project"],
            semantic_view_version_id=ids["view_v"],
            metrics=["cost"],
            dimensions=["channel"],
            start=_START,
            end=_END,
        )
    assert caught.value.code == mmm_export.REFUSAL_VIEW_NOT_FOUND


def test_a_non_monetary_metric_needs_no_currency_and_is_not_refused(
    live_postgres, second_currency
):
    """Two currencies in the landing veto the amount, never the click count."""
    ids = _seed(live_postgres)
    payload = _extract(live_postgres, ids, metrics=["clicks"])
    assert payload["provenance"]["currencies"]["clicks"]["monetary"] is False
    assert {row["currency"] for row in payload["rows"]} == {""}
