"""AI-342 -- a published View promises only what its relations can answer.

THE DEFECT, MEASURED 2026-09-01 on the reference Project. The Semantic View
`kardinal_youtube` v4 published ELEVEN dimensions and the relation its bindings
name materialised TWO. « views by country » therefore compiled, resolved, ran and
answered ZERO ROWS. Not a refusal -- an empty answer, which a person reads as
"no views from anywhere" while the truth was "this source publishes no country
split at all".

THE CLASS, NOT THE INSTANCE. Nothing here names YouTube. A breakdown landing
holds every dimension in one pair of columns (`breakdown_dimension`,
`breakdown_value`), so the pivot that reads it answers "present" for any
dimension asked of it, including one nothing ever landed. That is true of every
Connector that lands long and every View that binds one, so both halves of the
repair are asserted over the shape rather than over a source:

  * PUBLICATION refuses the binding by name (`prepare_change_set`), which is
    where the repair belongs -- the promise is made there;
  * the READ refuses it too (`resolve_physical_plan`), because every View
    published before the guard existed is still in force, and a relation's keys
    can change after a publication.

And the third property is the one that keeps a guard from becoming a nuisance: a
relation whose shape cannot be read refuses NOTHING. « We could not look » and
« nothing is there » are opposite answers.
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

from core import query_execution, relation_shape, semantic_model  # noqa: E402

pytestmark = pytest.mark.pg

_HASH = "a" * 64
_RELATION = "main_marts.fact_daily_kpi"

#: What the relation carries. `country` is a breakdown it publishes; `age_group`
#: is one it does not -- the exact pair AI-342 is about, and the reason the
#: fixture lands both an answerable and an unanswerable dimension.
_CARRIED = "country"
_ABSENT = "age_group"


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


@pytest.fixture()
def warehouse(tmp_path, monkeypatch):
    """A real breakdown landing, in the exact long shape the fact publishes.

    Two dimensions of one connector and one of another, so a shape read cannot
    pass by accident: the relation has the slot, it has keys, and `age_group` is
    not one of them.
    """
    path = tmp_path / "ai342.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
    con.execute(
        "CREATE TABLE main_marts.fact_daily_kpi ("
        "project_id VARCHAR, date VARCHAR, connector VARCHAR, metric VARCHAR, "
        "breakdown_dimension VARCHAR, breakdown_value VARCHAR, value DOUBLE, "
        "pull_id VARCHAR, loaded_at VARCHAR)"
    )
    con.execute(
        "INSERT INTO main_marts.fact_daily_kpi VALUES "
        "('default','2026-07-01','youtube-analytics','views','country','FR',700,'p','l'),"
        "('default','2026-07-01','youtube-analytics','views','country','US',350,'p','l'),"
        "('default','2026-07-01','youtube-analytics','views','country','DE',150,'p','l'),"
        "('default','2026-07-01','youtube-analytics','views','channel_id','UC_x',1200,'p','l')"
    )
    con.close()
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(path))
    return str(path)


def _seed(conn, *, dimension: str, with_output: bool = True, measure: str = "views") -> dict:
    """Org -> project -> concepts -> published View bound to the landing above."""
    ids = {
        "org": _uid("org"), "project": _uid("proj"),
        "measure": f"sc_{ULID()}", "measure_v": f"scv_{ULID()}",
        "dimension": f"sc_{ULID()}", "dimension_v": f"scv_{ULID()}",
        "view": f"sv_{ULID()}", "view_v": f"svv_{ULID()}",
        "ds": _uid("ds"), "plan": _uid("dpv"), "mapping": _uid("dmv"),
    }
    mapping_payload = {
        "grain": ["date", dimension],
        "fields": [
            {"field_id": dimension, "physical_type": "string",
             "binding": {"canonical_target": dimension, "status": "confirmed"}},
            {"field_id": measure, "physical_type": "number",
             "binding": {"canonical_target": measure, "status": "confirmed"}},
        ],
    }
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s,%s,%s,%s)",
            (ids["org"], "AI-342", ids["org"].lower().replace("_", "-"), "tester"))
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
            "VALUES (%s,%s,%s,%s,%s)",
            (ids["project"], ids["org"], "AI-342",
             ids["project"].lower().replace("_", "-"), "tester"))
        cur.execute(
            """INSERT INTO app.semantic_concepts (id, project_id, kind, name, created_by)
               VALUES (%s,%s,'metric',%s,'tester'), (%s,%s,'dimension',%s,'tester')""",
            (ids["measure"], ids["project"], measure,
             ids["dimension"], ids["project"], dimension))
        cur.execute(
            """INSERT INTO app.semantic_concept_versions
                 (id, concept_id, project_id, version_number, status, kind, name, label,
                  value_type, expression, aggregation, additivity_class, semantic_type,
                  allowed_grains, content_hash, created_by)
               VALUES
                 (%s,%s,%s,1,'published','metric',%s,%s,'integer',%s::jsonb,
                  %s::jsonb,'additive',NULL,%s,%s,'tester'),
                 (%s,%s,%s,1,'published','dimension',%s,%s,'string',NULL,NULL,NULL,
                  'categorical',%s,%s,'tester')""",
            (ids["measure_v"], ids["measure"], ids["project"],
             measure, measure.title(),
             json.dumps({"op": "sum", "field": measure}), json.dumps({"type": "sum"}),
             [], _HASH,
             ids["dimension_v"], ids["dimension"], ids["project"], dimension,
             dimension.replace("_", " ").title(), [], _HASH))
        for head, version in ((ids["measure"], ids["measure_v"]),
                              (ids["dimension"], ids["dimension_v"])):
            cur.execute("UPDATE app.semantic_concepts SET current_version_id = %s WHERE id = %s",
                        (version, head))
        cur.execute("INSERT INTO app.semantic_views (id, project_id, name, created_by) "
                    "VALUES (%s,%s,'breakdown_view','tester')", (ids["view"], ids["project"]))
        cur.execute(
            """INSERT INTO app.semantic_view_versions
                 (id, view_id, project_id, version_number, status, name, label,
                  dependency_fingerprint, content_hash, created_by)
               VALUES (%s,%s,%s,1,'published','breakdown_view','Breakdown view',%s,%s,
                       'tester')""",
            (ids["view_v"], ids["view"], ids["project"], _HASH, _HASH))
        cur.execute("UPDATE app.semantic_views SET current_version_id = %s WHERE id = %s",
                    (ids["view_v"], ids["view"]))
        cur.execute(
            "INSERT INTO app.datastreams (id, org_id, project_id, name, created_by, "
            "source_kind) VALUES (%s,%s,%s,'Breakdown source','tester','managed_feed')",
            (ids["ds"], ids["org"], ids["project"]))
        cur.execute(
            """INSERT INTO app.datastream_plan_versions
                 (id, datastream_id, project_id, version_number, contract_version,
                  source_kind, writer_kind, destination_policy, normalized_payload,
                  content_hash, idempotency_key_hash, created_by)
               VALUES (%s,%s,%s,1,'plan.v1','connector_pull','toorow','managed_raw',
                       '{}'::jsonb,%s,%s,'tester')""",
            (ids["plan"], ids["ds"], ids["project"], _HASH, _HASH))
        cur.execute(
            """INSERT INTO app.datastream_mapping_versions
                 (id, datastream_id, project_id, version_number, mapping_contract_version,
                  source_schema_hash, plan_version_id, content_hash, ossie_spec_version,
                  toorow_extension_version, mapping_payload, ossie_projection,
                  idempotency_key_hash, created_by)
               VALUES (%s,%s,%s,1,'mapping.v1',%s,%s,%s,'0.1.1','2',%s::jsonb,'{}'::jsonb,
                       %s,'tester')""",
            (ids["mapping"], ids["ds"], ids["project"], _HASH, ids["plan"], _HASH,
             json.dumps(mapping_payload), _HASH))
        cur.execute("UPDATE app.datastreams SET current_mapping_version_id = %s WHERE id = %s",
                    (ids["mapping"], ids["ds"]))
        for ordinal, concept in enumerate((ids["measure"], ids["dimension"])):
            cur.execute(
                """INSERT INTO app.semantic_view_version_bindings
                     (view_version_id, ordinal, concept_id, datastream_id, project_id,
                      mapping_version_id, binding_state)
                   VALUES (%s,%s,%s,%s,%s,%s,'active')""",
                (ids["view_v"], ordinal, concept, ids["ds"], ids["project"], ids["mapping"]))
        if not with_output:
            return ids
        execution_id, output_id = _uid("dse"), _uid("dso")
        ids["output_version"] = _uid("dsov")
        cur.execute(
            """INSERT INTO app.datastream_executions
                 (id, datastream_id, project_id, plan_version_id, mapping_version_id,
                  projection_plan_ref, state, created_by)
               VALUES (%s,%s,%s,%s,%s,'{}'::jsonb,'published','tester')""",
            (execution_id, ids["ds"], ids["project"], ids["plan"], ids["mapping"]))
        cur.execute(
            """INSERT INTO app.datastream_outputs
                 (id, org_id, project_id, datastream_id, output_kind, stable_name, created_by)
               VALUES (%s,%s,%s,%s,'full_grain',%s,'tester')""",
            (output_id, ids["org"], ids["project"], ids["ds"], _RELATION))
        cur.execute(
            """INSERT INTO app.datastream_output_versions
                 (id, output_id, org_id, project_id, datastream_id, execution_id,
                  plan_version_id, mapping_version_id, relation_ref, grain_evidence,
                  evidence, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'{}'::jsonb,'{}'::jsonb,'tester')""",
            (ids["output_version"], output_id, ids["org"], ids["project"], ids["ds"],
             execution_id, ids["plan"], ids["mapping"], _RELATION))
    return ids


def _spec(ids: dict) -> dict:
    return {
        "measures": [{"id": ids["measure"]}],
        "dimensions": [{"id": ids["dimension"]}],
        "time": {},
    }


def test_the_country_question_answers_off_the_breakdown_the_relation_carries(
    live_postgres, warehouse
):
    """The measure AI-342 asks for: a country question that comes back with rows."""
    ids = _seed(live_postgres, dimension=_CARRIED)
    plan = query_execution.resolve_physical_plan(
        live_postgres, project_id=ids["project"],
        semantic_view_version_id=ids["view_v"], spec=_spec(ids))
    assert "unavailable_reason" not in plan, plan

    sql, params = query_execution.build_sql(plan, _spec(ids))
    con = duckdb.connect(warehouse, read_only=True)
    try:
        rows = dict(con.execute(sql, params).fetchall())
    finally:
        con.close()
    assert rows == {"FR": 700.0, "US": 350.0, "DE": 150.0}, rows


def test_a_dimension_the_relation_does_not_carry_is_refused_at_read(live_postgres, warehouse):
    """The empty answer becomes a sentence, and the sentence names the gesture."""
    ids = _seed(live_postgres, dimension=_ABSENT)
    plan = query_execution.resolve_physical_plan(
        live_postgres, project_id=ids["project"],
        semantic_view_version_id=ids["view_v"], spec=_spec(ids))

    assert plan.get("missing_link") == "breakdown_dimension", plan
    reason = plan["unavailable_reason"]
    assert _ABSENT in reason
    # It says what the relation DOES publish -- a refusal that only says no
    # leaves the reader with nowhere to go.
    assert _CARRIED in reason
    assert "collect" in reason.lower()


def test_the_refusal_is_the_relation_and_not_the_row_count(live_postgres, warehouse):
    """A dimension the relation carries but has no rows for is NOT refused.

    « The source does not publish this breakdown » and « it published none today »
    are different facts with different repairs, and conflating them would let a
    quiet day read as a broken binding. The guard asks the KEYS, never the rows,
    so a carried dimension filtered to an empty window still resolves.
    """
    ids = _seed(live_postgres, dimension=_CARRIED)
    spec = _spec(ids) | {"time": {"member_id": None, "start": "2030-01-01"}}
    plan = query_execution.resolve_physical_plan(
        live_postgres, project_id=ids["project"],
        semantic_view_version_id=ids["view_v"], spec=spec)
    assert "unavailable_reason" not in plan, plan


def test_an_unreadable_relation_refuses_nothing(live_postgres, warehouse, monkeypatch):
    """Fail-open: we could not look is not nothing is there."""
    ids = _seed(live_postgres, dimension=_ABSENT)
    monkeypatch.setattr(
        relation_shape, "read",
        lambda *args, **kwargs: relation_shape.unreadable(_RELATION))
    plan = query_execution.resolve_physical_plan(
        live_postgres, project_id=ids["project"],
        semantic_view_version_id=ids["view_v"], spec=_spec(ids))
    assert plan.get("missing_link") != "breakdown_dimension", plan


def _prepare(conn, ids: dict, dimension: str) -> list[dict]:
    """Ask publication to validate a View binding *dimension* to the landing."""
    intent = {
        "action": "edit_view",
        "view": {
            "name": "breakdown_view",
            "label": "Breakdown view",
            "concepts": [
                {"concept_id": ids["measure"], "concept_version_id": ids["measure_v"]},
                {"concept_id": ids["dimension"], "concept_version_id": ids["dimension_v"]},
            ],
            "bindings": [
                {"concept_id": ids["measure"], "datastream_id": ids["ds"],
                 "mapping_version_id": ids["mapping"]},
                {"concept_id": ids["dimension"], "datastream_id": ids["ds"],
                 "mapping_version_id": ids["mapping"]},
            ],
        },
    }
    change_set = semantic_model.create_change_set(
        conn, ids["project"], actor="tester", object_type="semantic-view",
        object_id=ids["view"], base_version_id=ids["view_v"], intent=intent,
        idempotency_key=str(ULID()))
    prepared = semantic_model.prepare_change_set(
        conn, ids["project"], change_set.id, actor="tester")
    return prepared["validation"]["refusals"]


def test_publishing_a_binding_the_relation_cannot_answer_is_refused_by_name(
    live_postgres, warehouse
):
    """The repair belongs at publication: the promise is made there."""
    ids = _seed(live_postgres, dimension=_ABSENT)
    codes = [r["code"] for r in _prepare(live_postgres, ids, _ABSENT)]
    assert "dimension_absent_from_bound_relation" in codes, codes


def test_publishing_a_binding_the_relation_answers_is_not_refused(live_postgres, warehouse):
    """The same act, on a dimension the landing carries, raises nothing."""
    ids = _seed(live_postgres, dimension=_CARRIED)
    codes = [r["code"] for r in _prepare(live_postgres, ids, _CARRIED)]
    assert "dimension_absent_from_bound_relation" not in codes, codes


def test_publishing_a_measure_the_relation_does_not_carry_is_refused_by_name(
    live_postgres, warehouse
):
    """AI-352 (governance.md, 2026-09-02): the promise holds for measures too.

    The landing's `metric` keys are `views` only; a View binding a `clicks`
    measure to it would answer every clicks question with nothing.
    """
    ids = _seed(live_postgres, dimension=_CARRIED, measure="clicks")
    codes = [r["code"] for r in _prepare(live_postgres, ids, _CARRIED)]
    assert "measure_absent_from_bound_relation" in codes, codes


def test_publishing_a_measure_the_relation_carries_is_not_refused(live_postgres, warehouse):
    """`views` is a `metric` key of the landing: the same act raises nothing."""
    ids = _seed(live_postgres, dimension=_CARRIED, measure="views")
    codes = [r["code"] for r in _prepare(live_postgres, ids, _CARRIED)]
    assert "measure_absent_from_bound_relation" not in codes, codes


def test_a_datastream_that_never_ran_is_not_refused(live_postgres, warehouse):
    """Binding before the first run is the ordinary order of the product.

    There is no Output version, so there is no relation to ask -- and refusing
    here would make a View unpublishable until its sources had collected once.
    """
    ids = _seed(live_postgres, dimension=_ABSENT, with_output=False)
    codes = [r["code"] for r in _prepare(live_postgres, ids, _ABSENT)]
    assert "dimension_absent_from_bound_relation" not in codes, codes
