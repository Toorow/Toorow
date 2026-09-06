"""Story 50.2 -- the composition SQL, run against a real PostgreSQL.

WHY THIS EXISTS BESIDE THE MOCKED SUITE. `test_analyze_workbench.py` proves the
SHAPE of every lens against scripted cursors: fast, exhaustive, and completely
blind to whether a column exists. A misspelled column, a join on a key that is
not there, a `= ANY(%s)` against the wrong type -- none of those can fail a
mocked test, and all of them fail the first real request.

So this file seeds a real published Semantic View, a real Query Spec version and
a real immutable Result through the ordinary `connector` role -- not a superuser,
so RLS actually applies -- and asks for all six lenses. Nothing is committed: the
fixture rolls back, and the seeded rows never outlive the test.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from core.analyze_workbench import (
    LENSES,
    WorkbenchNotFound,
    compose_query_facets,
    compose_result_lens,
    load_query_spec_version,
)
from core.semantic_compiler import COMPILER_VERSION


#: Crockford base32 -- the alphabet the `^[0-9A-HJKMNP-TV-Z]{26}$` id checks
#: enforce. `I`, `L`, `O` and `U` are excluded on purpose (they read as 1, 1, 0
#: and V), so a readable word like "EXAMPLE" is NOT a legal identifier body. The
#: first version of this file learned that from a CheckViolation.
def _id(prefix: str, tag: str) -> str:
    body = (tag + "0" * 26)[:26]
    return f"{prefix}_{body}"


ORG = _id("org", "5029EXAMPTAG")
PROJECT = _id("proj", "5029EXAMPTAG")
VIEW = _id("sv", "5029EXAMPVEW")
VIEW_VERSION = _id("svv", "5029EXAMPVEW")
CLICKS = _id("sc", "5029EXAMPMET")
CLICKS_V = _id("scv", "5029EXAMPMET")
DATE = _id("sc", "5029EXAMPDMN")
DATE_V = _id("scv", "5029EXAMPDMN")
ARTIFACT = _id("sca", "5029EXAMPART")
SPEC = _id("qs", "5029EXAMPSPC")
SPEC_VERSION = _id("qsv", "5029EXAMPSPC")
ATTEMPT = _id("qea", "5029EXAMPATT")
RESULT = _id("qr", "5029EXAMPRES")
FOREIGN_ORG = _id("org", "5029EXAMPFRN")
FOREIGN_PROJECT = _id("proj", "5029EXAMPFRN")
HASH = "a" * 64
AT = datetime(2026, 7, 31, 9, 0, tzinfo=timezone.utc)

MATRIX = {
    "metrics": [{"concept_id": CLICKS, "version_id": CLICKS_V, "label": "Clicks"}],
    "dimensions": [{"concept_id": DATE, "version_id": DATE_V, "label": "Date"}],
    "cells": [{"metric_id": CLICKS, "dimension_id": DATE, "queryable": True, "join_path": []}],
}

QUERY_SPEC = {
    "contract_version": "query-spec.v1",
    "semantic_view_id": VIEW,
    "semantic_view_version_id": VIEW_VERSION,
    "measures": [{"id": CLICKS, "version_id": CLICKS_V}],
    "dimensions": [{"id": DATE, "version_id": DATE_V}],
    "filters": [],
    "sort": [],
    "comparison": "none",
    "grain": "day",
    "row_limit": 100,
    "time": {"member_id": DATE, "start": "2026-07-01", "end": "2026-07-31"},
}

MANIFEST = {
    "semantic_view_version_id": VIEW_VERSION,
    "query_spec_version_id": SPEC_VERSION,
    "grain": "day",
    "row_limit": 100,
    "unavailable_reason": "this Datastream has no published output to query yet",
    "missing_link": "datastream_output_versions",
}


@pytest.fixture
def seeded(live_postgres):
    """Seed one complete analytical chain. Never committed."""
    import json

    conn = live_postgres
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s,%s,%s,%s)",
            (ORG, "Example org", "example-org-50-2", "person-1"),
        )
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, created_by, org_id) VALUES (%s,%s,%s,%s,%s)",
            (PROJECT, "Example project", "example-project-50-2", "person-1", ORG),
        )
        cur.execute(
            """INSERT INTO app.semantic_concepts (id, project_id, kind, name, created_by)
               VALUES (%s,%s,'metric','clicks',%s), (%s,%s,'dimension','reporting_date',%s)""",
            (CLICKS, PROJECT, "person-1", DATE, PROJECT, "person-1"),
        )
        cur.execute(
            """INSERT INTO app.semantic_concept_versions
                 (id, concept_id, project_id, version_number, status, kind, name, label,
                  definition, value_type, unit, expression, aggregation, additivity_class,
                  semantic_type, allowed_grains, content_hash, created_by)
               VALUES
                 (%s,%s,%s,1,'published','metric','clicks','Clicks','Confirmed ad clicks',
                  'integer','count',%s::jsonb,%s::jsonb,'additive',NULL,%s,%s,'person-1'),
                 (%s,%s,%s,1,'published','dimension','reporting_date','Date','Reporting date',
                  'date',NULL,NULL,NULL,NULL,'time',%s,%s,'person-1')""",
            (
                # `aggregation` is JSONB and `allowed_grains` is a text ARRAY.
                # Both were written as a bare string and as JSON in the first
                # draft; the real database refused each in turn, which is the
                # whole reason this file exists next to the mocked suite.
                CLICKS_V, CLICKS, PROJECT, json.dumps({"op": "sum", "field": "clicks"}),
                json.dumps({"type": "sum"}), [], HASH,
                DATE_V, DATE, PROJECT, ["day", "week"], HASH,
            ),
        )
        cur.execute(
            """INSERT INTO app.semantic_views (id, project_id, name, lifecycle_status, created_by)
               VALUES (%s,%s,'search_performance','published',%s)""",
            (VIEW, PROJECT, "person-1"),
        )
        cur.execute(
            """INSERT INTO app.semantic_view_versions
                 (id, view_id, project_id, version_number, status, name, label, query_policy,
                  dependency_fingerprint, content_hash, created_by)
               VALUES (%s,%s,%s,1,'published','search_performance','Search performance',
                       %s::jsonb,%s,%s,'person-1')""",
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
               VALUES (%s,%s,%s,%s,'Clicks by day','person-1')""",
            (SPEC, ORG, PROJECT, VIEW),
        )
        cur.execute(
            """INSERT INTO app.query_spec_versions
                 (id, query_spec_id, org_id, project_id, version_number, semantic_view_id,
                  semantic_view_version_id, spec, content_hash, created_by)
               VALUES (%s,%s,%s,%s,1,%s,%s,%s::jsonb,%s,'person-1')""",
            (SPEC_VERSION, SPEC, ORG, PROJECT, VIEW, VIEW_VERSION, json.dumps(QUERY_SPEC), HASH),
        )
        cur.execute(
            "UPDATE app.query_specs SET current_version_id = %s WHERE id = %s",
            (SPEC_VERSION, SPEC),
        )
        cur.execute(
            """INSERT INTO app.query_execution_attempts
                 (id, org_id, project_id, query_spec_version_id, result_id, requested_by)
               VALUES (%s,%s,%s,%s,%s,'person-1')""",
            (ATTEMPT, ORG, PROJECT, SPEC_VERSION, RESULT),
        )
        cur.execute(
            """INSERT INTO app.query_results
                 (id, org_id, project_id, attempt_id, query_spec_version_id, outcome,
                  ai_path_absent_literal, content_hash, row_count, cell_count, byte_count,
                  truncated, started_at, ended_at)
               VALUES (%s,%s,%s,%s,%s,'unavailable','No AI path',%s,0,0,120,false,%s,%s)""",
            (RESULT, ORG, PROJECT, ATTEMPT, SPEC_VERSION, HASH, AT, AT),
        )
        cur.execute(
            """INSERT INTO app.query_result_payloads
                 (result_id, org_id, project_id, content_hash, result_schema, manifest, rows_chunk)
               VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)""",
            (
                RESULT, ORG, PROJECT, HASH,
                json.dumps({"fields": []}), json.dumps(MANIFEST), json.dumps([]),
            ),
        )
    return conn


def test_the_facet_composition_runs_against_the_real_schema(seeded):
    """Every column named in `compose_query_facets` exists and joins."""
    bundle = compose_query_facets(
        seeded,
        org_id=ORG,
        project_id=PROJECT,
        semantic_view_id=VIEW,
        semantic_view_version_id=VIEW_VERSION,
    )
    assert bundle["executable"] is True
    assert bundle["semantic_view_label"] == "Search performance"
    clicks = next(m for m in bundle["measures"] if m["concept_id"] == CLICKS)
    assert clicks["aggregation"] == {"type": "sum"}
    assert clicks["expression"] == {"op": "sum", "field": "clicks"}
    # Read from the stored `allowed_grains`, not from a list kept in the module.
    assert bundle["time"]["grains"] == ["day", "week"]
    # The query policy ceiling reached the offered limit through real JSONB.
    assert bundle["limits"]["max_row_limit"] == 5000


def test_all_six_lenses_compose_against_the_real_schema(seeded):
    for lens in LENSES:
        body = compose_result_lens(
            seeded, org_id=ORG, project_id=PROJECT, result_id=RESULT, lens=lens
        )
        assert body["result_id"] == RESULT
        assert body["outcome"] == "unavailable"
        assert lens.replace("-", "_") in body


def test_the_real_unavailable_result_names_its_missing_link(seeded):
    quality = compose_result_lens(
        seeded, org_id=ORG, project_id=PROJECT, result_id=RESULT, lens="quality"
    )["quality"]
    assert quality["missing_link"] == "datastream_output_versions"
    # Against real SQL and a real row whose stored `row_count` IS 0: the read
    # withholds it rather than reporting it, because zero rows and "we could not
    # ask" are different answers and the tile cannot tell them apart (AC9).
    assert quality["completeness"]["row_count"] is None
    assert quality["completeness"]["counts_unavailable_reason"]
    assert any(lim["code"] == "datastream_output_versions" for lim in quality["limitations"])


def test_the_real_provenance_chain_carries_all_six_contracted_links(seeded):
    """Against real SQL: the three links that used to be absent from the list.

    `analyze-and-test.md:114` contracts source, pull or virtual pull, mapping,
    publication, semantic query and result. This seeded Result reached no
    execution evidence, so the first three are `not_recorded` -- which is a state
    a reader can see, unlike being missing from the chain entirely.
    """
    body = compose_result_lens(
        seeded, org_id=ORG, project_id=PROJECT, result_id=RESULT, lens="provenance"
    )["provenance"]
    assert [entry["link"] for entry in body["chain"]] == [
        "source",
        "pull",
        "mapping",
        "published_output_relation",
        "semantic_view_version",
        "query_spec_version",
        "result",
    ]
    by_link = {entry["link"]: entry for entry in body["chain"]}
    for link in ("source", "pull", "mapping"):
        assert by_link[link]["status"] == "not_recorded"
        assert "core.query_execution.capture_evidence" in by_link[link]["reason"]


def test_definitions_resolve_the_pinned_versions_from_real_rows(seeded):
    body = compose_result_lens(
        seeded, org_id=ORG, project_id=PROJECT, result_id=RESULT, lens="definitions"
    )["definitions"]
    assert body["semantic_view"]["label"] == "Search performance"
    clicks = next(m for m in body["members"] if m["concept_id"] == CLICKS)
    assert clicks["resolved"] is True
    assert clicks["additivity_class"] == "additive"


def test_a_human_only_result_carries_the_exact_literal_from_the_database(seeded):
    body = compose_result_lens(
        seeded, org_id=ORG, project_id=PROJECT, result_id=RESULT, lens="ai-path"
    )["ai_path"]
    # The `observed-ai-path.v1` projection, not the pre-projection shape. This
    # asserted `body["ai_path"]` and `body["human_only"]`, two keys
    # `ai_paths.project_observed_ai_path` stopped returning when the projection was
    # made strict and closed so this reader could not become an identity oracle --
    # it answers `{schema_version, state, literal}` for every input. The unit-level
    # twin (`test_analyze_workbench.py::test_a_human_only_result_says_exactly_no_ai_path`)
    # was updated then and this one was not, so it had been raising KeyError since.
    assert body["state"] == "human_absent"
    assert body["literal"] == "No AI path"
    assert body["schema_version"] == "observed-ai-path.v1"


def test_the_query_spec_version_read_runs_and_finds_its_result(seeded):
    body = load_query_spec_version(
        seeded, org_id=ORG, project_id=PROJECT, query_spec_version_id=SPEC_VERSION
    )
    assert body["is_current_version"] is True
    assert [r["result_id"] for r in body["results"]] == [RESULT]


def test_a_foreign_org_cannot_read_the_result_even_with_the_exact_id(seeded):
    """Scope is in the WHERE clause, so the real query returns no row."""
    with pytest.raises(WorkbenchNotFound):
        compose_result_lens(
            seeded,
            org_id=FOREIGN_ORG,
            project_id=PROJECT,
            result_id=RESULT,
            lens="view",
        )
    with pytest.raises(WorkbenchNotFound):
        compose_query_facets(
            seeded,
            org_id=ORG,
            project_id=FOREIGN_PROJECT,
            semantic_view_id=VIEW,
            semantic_view_version_id=VIEW_VERSION,
        )
