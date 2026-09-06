"""Story 72.7 -- the Chart Template door of `render_analyze_result`, against real rows.

WHY THIS CANNOT BE A UNIT TEST. `test_analyze_template_offer.py` proves the shape
of the door -- exclusive parameters, bounded block, the block that disappears with
the render tool -- entirely against values. Three sentences of this story are
about ROWS and only a database can answer them:

  1. AC29 -- a template the deterministic verdict refuses is refused BY THE TOOL,
     with the unsatisfied predicate named, and NOTHING is written. Proved by
     counting rows before and after the refusal, not by trusting the exception;
  2. AC29 again -- no nearest family and no dropped requirement: the refusal names
     the same predicate story 72.3 names, and no Spec version appears under a
     neighbouring family;
  3. AC30 -- the block is scoped to the Project: a Chart Template of another
     Project of the same org is not offered, and reads exactly like one that does
     not exist.

The compatible path is proved here too, because "the model's proposal takes the
same route as a human hand" is a claim about a stored row: `proposed_by`, the
provenance column, and the Query Spec version the Result already carried.

Every test rolls back.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from core import analyze_render_mcp as adapter
from core import analyze_template_offer as offer
from core.query_specs import canonical_hash
from core.semantic_compiler import COMPILER_VERSION
from core.visualization_templates import CHART_TEMPLATE_CONTRACT_VERSION

pytestmark = pytest.mark.usefixtures("live_postgres")


def _id(prefix: str, tag: str) -> str:
    body = (tag + "0" * 26)[:26]
    return f"{prefix}_{body}"


ORG = _id("org", "7270EXAMPTAG")
PROJECT = _id("proj", "7270EXAMPTAG")
OTHER_PROJECT = _id("proj", "7270EXAMPTWO")
VIEW = _id("sv", "7270EXAMPVEW")
VIEW_VERSION = _id("svv", "7270EXAMPVEW")
CLICKS = _id("sc", "7270EXAMPMET")
CLICKS_V = _id("scv", "7270EXAMPMET")
CHANNEL = _id("sc", "7270EXAMPDMN")
CHANNEL_V = _id("scv", "7270EXAMPDMN")
ARTIFACT = _id("sca", "7270EXAMPART")
SPEC = _id("qs", "7270EXAMPSPC")
SPEC_VERSION = _id("qsv", "7270EXAMPSPC")
ATTEMPT = _id("qea", "7270EXAMPATT")
RESULT = _id("qr", "7270EXAMPRES")
FITTING = _id("vtpl", "7270EXAMPTPL")
FITTING_VERSION = _id("vtv", "7270EXAMPTPL")
DEMANDING = _id("vtpl", "7270EXAMPTWO")
DEMANDING_VERSION = _id("vtv", "7270EXAMPTWO")
FOREIGN = _id("vtpl", "7270EXAMPFGN")
FOREIGN_VERSION = _id("vtv", "7270EXAMPFGN")
HASH = "a" * 64
AT = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
ACTOR = "owner@example.com"

SCHEMA = {"fields": [{"id": CLICKS, "name": "clicks"}, {"id": CHANNEL, "name": "channel"}]}

MATRIX = {
    "metrics": [{"concept_id": CLICKS, "version_id": CLICKS_V, "label": "Clicks"}],
    "dimensions": [{"concept_id": CHANNEL, "version_id": CHANNEL_V, "label": "Channel"}],
    "cells": [{"metric_id": CLICKS, "dimension_id": CHANNEL, "queryable": True, "join_path": []}],
}

QUERY_SPEC = {
    "contract_version": "query-spec.v1",
    "semantic_view_id": VIEW,
    "semantic_view_version_id": VIEW_VERSION,
    "measures": [{"id": CLICKS, "version_id": CLICKS_V}],
    "dimensions": [{"id": CHANNEL, "version_id": CHANNEL_V}],
    "filters": [],
    "sort": [],
    "comparison": "none",
    "grain": "day",
    "row_limit": 100,
}

FITTING_DOCUMENT = {
    "spec_contract_version": CHART_TEMPLATE_CONTRACT_VERSION,
    "schema_version": 1,
    "family": "bar",
    "answers_question": "How does one measure compare across a few categories?",
    "requires": {
        "measure": {"min": 1, "max": 1},
        "dimension": {"min": 1, "max": 1, "max_cardinality": 12},
    },
}

#: The same family, asking for one measure more than this Result carries. There
#: is no nearest family and no dropped requirement: it is refused by its name.
DEMANDING_DOCUMENT = {
    "spec_contract_version": CHART_TEMPLATE_CONTRACT_VERSION,
    "schema_version": 1,
    "family": "bar",
    "answers_question": "How do two measures compare across a few categories?",
    "requires": {"measure": {"min": 2}, "dimension": {"min": 1}},
}


def _rows(channels: int = 3) -> list[dict]:
    return [{"channel": f"channel-{index % channels}", "clicks": index} for index in range(6)]


@pytest.fixture
def seeded(live_postgres):
    """One question, one Result, and three Chart Templates -- one in another Project."""
    conn = live_postgres
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s,%s,%s,'test')",
            (ORG, "Story 72.7 fixture", ORG.replace("_", "-")),
        )
        for project in (PROJECT, OTHER_PROJECT):
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
                "VALUES (%s,%s,%s,%s,'test')",
                (project, ORG, "Story 72.7 fixture", project.replace("_", "-")),
            )
        cur.execute(
            """INSERT INTO app.semantic_concepts (id, project_id, kind, name, created_by)
               VALUES (%s,%s,'metric','clicks','test'), (%s,%s,'dimension','channel','test')""",
            (CLICKS, PROJECT, CHANNEL, PROJECT),
        )
        cur.execute(
            """INSERT INTO app.semantic_concept_versions
                 (id, concept_id, project_id, version_number, status, kind, name, label,
                  definition, value_type, unit, expression, aggregation, additivity_class,
                  semantic_type, allowed_grains, content_hash, created_by)
               VALUES
                 (%s,%s,%s,1,'published','metric','clicks','Clicks','Confirmed ad clicks',
                  'integer','count',%s::jsonb,%s::jsonb,'additive',NULL,%s,%s,'test'),
                 (%s,%s,%s,1,'published','dimension','channel','Channel','Acquisition channel',
                  'string',NULL,NULL,NULL,NULL,'category',%s,%s,'test')""",
            (
                CLICKS_V, CLICKS, PROJECT, json.dumps({"op": "sum", "field": "clicks"}),
                json.dumps({"type": "sum"}), [], HASH,
                CHANNEL_V, CHANNEL, PROJECT, [], HASH,
            ),
        )
        cur.execute(
            """INSERT INTO app.semantic_views (id, project_id, name, lifecycle_status, created_by)
               VALUES (%s,%s,'channel_performance','published','test')""",
            (VIEW, PROJECT),
        )
        cur.execute(
            """INSERT INTO app.semantic_view_versions
                 (id, view_id, project_id, version_number, status, name, label, query_policy,
                  dependency_fingerprint, content_hash, created_by)
               VALUES (%s,%s,%s,1,'published','channel_performance','Channel performance',
                       %s::jsonb,%s,%s,'test')""",
            (VIEW_VERSION, VIEW, PROJECT, json.dumps({"max_row_limit": 5000}), HASH, HASH),
        )
        cur.execute(
            """INSERT INTO app.semantic_compiled_artifacts
                 (id, project_id, view_version_id, compiler_version, content_hash,
                  queryability_matrix, ossie_projection, ossie_spec_version,
                  toorow_extension_version)
               VALUES (%s,%s,%s,%s,%s,%s::jsonb,'{}'::jsonb,'1','1')""",
            (ARTIFACT, PROJECT, VIEW_VERSION, COMPILER_VERSION, HASH, json.dumps(MATRIX)),
        )
        cur.execute(
            """INSERT INTO app.query_specs
                 (id, org_id, project_id, semantic_view_id, name, created_by)
               VALUES (%s,%s,%s,%s,'Clicks by channel','test')""",
            (SPEC, ORG, PROJECT, VIEW),
        )
        cur.execute(
            """INSERT INTO app.query_spec_versions
                 (id, query_spec_id, org_id, project_id, version_number, semantic_view_id,
                  semantic_view_version_id, spec, content_hash, created_by)
               VALUES (%s,%s,%s,%s,1,%s,%s,%s::jsonb,%s,'test')""",
            (SPEC_VERSION, SPEC, ORG, PROJECT, VIEW, VIEW_VERSION, json.dumps(QUERY_SPEC), HASH),
        )
        cur.execute(
            "UPDATE app.query_specs SET current_version_id = %s WHERE id = %s",
            (SPEC_VERSION, SPEC),
        )
        for head, version, project, label, document in (
            (FITTING, FITTING_VERSION, PROJECT, "Measure by category", FITTING_DOCUMENT),
            (DEMANDING, DEMANDING_VERSION, PROJECT, "Two measures", DEMANDING_DOCUMENT),
            (FOREIGN, FOREIGN_VERSION, OTHER_PROJECT, "Another Project", FITTING_DOCUMENT),
        ):
            cur.execute(
                """INSERT INTO app.visualization_templates
                     (id, org_id, project_id, label, seed_origin, created_by)
                   VALUES (%s,%s,%s,%s,'project','test')""",
                (head, ORG, project, label),
            )
            cur.execute(
                """INSERT INTO app.visualization_template_versions
                     (id, template_id, org_id, project_id, version_number, family,
                      spec_contract_version, schema_version, document, content_hash, created_by)
                   VALUES (%s,%s,%s,%s,1,%s,%s,1,%s::jsonb,%s,'test')""",
                (
                    version, head, ORG, project, document["family"],
                    document["spec_contract_version"], json.dumps(document),
                    canonical_hash(document),
                ),
            )
            cur.execute(
                "UPDATE app.visualization_templates SET current_version_id = %s WHERE id = %s",
                (version, head),
            )
        cur.execute(
            """INSERT INTO app.query_execution_attempts
                 (id, org_id, project_id, query_spec_version_id, result_id, requested_by)
               VALUES (%s,%s,%s,%s,%s,'test')""",
            (ATTEMPT, ORG, PROJECT, SPEC_VERSION, RESULT),
        )
        cur.execute(
            """INSERT INTO app.query_results
                 (id, org_id, project_id, attempt_id, query_spec_version_id, outcome,
                  ai_path_absent_literal, content_hash, row_count, cell_count, byte_count,
                  truncated, started_at, ended_at)
               VALUES (%s,%s,%s,%s,%s,'success','No AI path',%s,6,12,240,false,%s,%s)""",
            (RESULT, ORG, PROJECT, ATTEMPT, SPEC_VERSION, HASH, AT, AT),
        )
        cur.execute(
            """INSERT INTO app.query_result_payloads
                 (result_id, org_id, project_id, content_hash, result_schema, manifest, rows_chunk)
               VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)""",
            (
                RESULT, ORG, PROJECT, HASH, json.dumps(SCHEMA),
                json.dumps({"grain": "day"}), json.dumps(_rows()),
            ),
        )
    yield conn
    conn.rollback()


def _counts(conn) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT (SELECT count(*) FROM app.visualizations),
                   (SELECT count(*) FROM app.visualization_spec_versions),
                   (SELECT count(*) FROM app.query_results),
                   (SELECT count(*) FROM app.query_execution_attempts)
            """
        )
        row = cur.fetchone()
    return {
        "visualizations": row[0],
        "spec_versions": row[1],
        "results": row[2],
        "attempts": row[3],
    }


def _pin(conn, template_version_id: str, project_id: str = PROJECT) -> str:
    return adapter._materialize_pinned_spec(
        conn,
        identity=ACTOR,
        org_id=ORG,
        project_id=project_id,
        result_id=RESULT,
        template_version_id=template_version_id,
    )


# ---------------------------------------------------------------------------
# AC29 -- the model cannot make compatible what is not.
# ---------------------------------------------------------------------------


def test_an_incompatible_template_is_refused_by_name_and_writes_nothing(seeded):
    before = _counts(seeded)
    with pytest.raises(Exception) as excinfo:
        _pin(seeded, DEMANDING_VERSION)

    payload = json.loads(str(excinfo.value))
    assert payload["code"] == "chart_template_incompatible"
    #  The unsatisfied predicate itself, anchored on the control it is about in
    #  the TEMPLATE document -- story 72.3's own sentence, carried through the
    #  tool rather than restated by it.
    assert payload["refusals"], "the refusal named no predicate"
    subjects = {refusal["subject"] for refusal in payload["refusals"]}
    assert any(subject and subject.startswith("/requires/measure") for subject in subjects), (
        f"the refusal does not name the well it is about: {subjects}"
    )
    for refusal in payload["refusals"]:
        assert refusal["remedy"], "a refusal without a gesture makes the reader guess"
    assert _counts(seeded) == before, "an incompatible template left rows behind"


def test_no_nearest_family_is_substituted_for_the_one_that_does_not_fit(seeded):
    """Refused by its name, never redrawn as a neighbour that would fit."""
    with pytest.raises(Exception):
        _pin(seeded, DEMANDING_VERSION)
    with seeded.cursor() as cur:
        cur.execute(
            "SELECT family FROM app.visualization_spec_versions WHERE project_id = %s",
            (PROJECT,),
        )
        assert cur.fetchall() == []


def test_a_template_of_another_project_reads_exactly_like_one_that_does_not_exist(seeded):
    """Foreign, denied and absent converge on ONE answer, as everywhere else here."""
    with pytest.raises(Exception) as foreign:
        _pin(seeded, FOREIGN_VERSION)
    with pytest.raises(Exception) as absent:
        _pin(seeded, _id("vtv", "7270EXAMPNIL"))
    assert json.loads(str(foreign.value)) == json.loads(str(absent.value))
    assert json.loads(str(foreign.value))["code"] == "chart_template_unavailable"


# ---------------------------------------------------------------------------
# The compatible path -- the same route a human hand takes.
# ---------------------------------------------------------------------------


def test_a_compatible_template_writes_one_ordinary_spec_version_proposed_by_the_model(
    seeded,
):
    before = _counts(seeded)
    version_id = _pin(seeded, FITTING_VERSION)

    after = _counts(seeded)
    assert after["spec_versions"] == before["spec_versions"] + 1
    assert after["visualizations"] == before["visualizations"] + 1
    #  AC26 rides along: no question re-executed, no Result created.
    assert after["results"] == before["results"]
    assert after["attempts"] == before["attempts"]

    with seeded.cursor() as cur:
        cur.execute(
            """SELECT proposed_by, created_by, query_spec_version_id,
                      materialized_from_template_version_id, family
               FROM app.visualization_spec_versions WHERE id = %s""",
            (version_id,),
        )
        row = cur.fetchone()
    assert row[0] == "model", "a model's proposal must be recorded as one"
    assert row[1] == ACTOR
    #  The pin the Result already carried -- never a second execution.
    assert row[2] == SPEC_VERSION
    #  Provenance, beside the document and never inside it.
    assert row[3] == FITTING_VERSION
    assert row[4] == FITTING_DOCUMENT["family"]


def test_the_template_version_is_left_exactly_as_it_was(seeded):
    """A seed is read, never rewritten -- including by a model."""
    with seeded.cursor() as cur:
        cur.execute(
            "SELECT content_hash, document FROM app.visualization_template_versions "
            "WHERE id = %s",
            (FITTING_VERSION,),
        )
        before = cur.fetchone()
    _pin(seeded, FITTING_VERSION)
    with seeded.cursor() as cur:
        cur.execute(
            "SELECT content_hash, document FROM app.visualization_template_versions "
            "WHERE id = %s",
            (FITTING_VERSION,),
        )
        assert cur.fetchone() == before


# ---------------------------------------------------------------------------
# AC30 -- bounded, scoped to the Project, and no row of the Result.
# ---------------------------------------------------------------------------


def test_the_offered_block_is_scoped_to_the_project_and_holds_only_what_fits(seeded):
    block = offer.compatible_templates(
        seeded, org_id=ORG, project_id=PROJECT, result_id=RESULT
    )
    offered = [entry["visualization_template_version_id"] for entry in block["templates"]]
    assert offered == [FITTING_VERSION], (
        "the block must carry the compatible template of THIS Project and nothing "
        f"else -- it carried {offered}"
    )
    entry = block["templates"][0]
    assert entry["answers_question"] == FITTING_DOCUMENT["answers_question"]
    assert entry["family"] == FITTING_DOCUMENT["family"]
    assert entry["rank"] == 1
    assert block["withheld"] == 0
    #  Not one value of the Result reaches the model through this block.
    serialized = json.dumps(block)
    for value in ("channel-0", "clicks", "channel"):
        assert value not in serialized, f"`{value}` is Result data and reached the block"


def test_the_other_project_sees_its_own_template_and_not_this_one(seeded):
    """The same call, one Project over: the Result does not resolve there either."""
    block = offer.compatible_templates(
        seeded, org_id=ORG, project_id=OTHER_PROJECT, result_id=RESULT
    )
    assert block == {"templates": [], "withheld": 0}
