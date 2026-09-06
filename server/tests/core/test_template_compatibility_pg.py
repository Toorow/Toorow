"""Story 72.3 -- the reading gate of the compatibility verdict, against a real PostgreSQL.

WHY THIS CANNOT BE A UNIT TEST. `test_template_compatibility.py` proves the
JUDGEMENT against values: exhaustive, fast, and completely blind to whether the
columns the gate names exist. Three things can only be proved here:

  1. the two SELECTs join and project columns that really exist -- a misspelled
     column cannot fail a mocked cursor, and fails the first real request;
  2. AC12, that the verdict is DERIVED: no table of this story carries a
     compatibility column, and the gate reaches the database through nothing but
     SELECT. That is asserted on the real `information_schema`, not on a comment;
  3. that a template or a Result of another Project is UNREADABLE and not
     "incompatible" -- the scoping is in the WHERE clause, and a mocked cursor
     returns whatever it was told to.

Every test rolls back. Nothing is left behind, even on a disposable database.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from core import template_compatibility as tc
from core.query_specs import canonical_hash
from core.semantic_compiler import COMPILER_VERSION
from core.visualization_templates import CHART_TEMPLATE_CONTRACT_VERSION

pytestmark = pytest.mark.usefixtures("live_postgres")


def _id(prefix: str, tag: str) -> str:
    """Crockford base32 -- `I`, `L`, `O` and `U` are not legal identifier body."""
    body = (tag + "0" * 26)[:26]
    return f"{prefix}_{body}"


ORG = _id("org", "7230EXAMPTAG")
PROJECT = _id("proj", "7230EXAMPTAG")
OTHER_PROJECT = _id("proj", "7230EXAMPTWO")
VIEW = _id("sv", "7230EXAMPVEW")
VIEW_VERSION = _id("svv", "7230EXAMPVEW")
CLICKS = _id("sc", "7230EXAMPMET")
CLICKS_V = _id("scv", "7230EXAMPMET")
CHANNEL = _id("sc", "7230EXAMPDMN")
CHANNEL_V = _id("scv", "7230EXAMPDMN")
ARTIFACT = _id("sca", "7230EXAMPART")
SPEC = _id("qs", "7230EXAMPSPC")
SPEC_VERSION = _id("qsv", "7230EXAMPSPC")
ATTEMPT = _id("qea", "7230EXAMPATT")
RESULT = _id("qr", "7230EXAMPRES")
TEMPLATE = _id("vtpl", "7230EXAMPTPL")
TEMPLATE_VERSION = _id("vtv", "7230EXAMPTPL")
HASH = "a" * 64
AT = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)

#: The Result keys its rows by the COLUMN name and declares the member id beside
#: it. Keeping the two different is what makes the cardinality count meaningful
#: (AI-337): a count taken on the member id would read nothing here.
SCHEMA = {
    "fields": [
        {"id": CLICKS, "name": "clicks"},
        {"id": CHANNEL, "name": "channel"},
    ]
}

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

TEMPLATE_DOCUMENT = {
    "spec_contract_version": CHART_TEMPLATE_CONTRACT_VERSION,
    "schema_version": 1,
    "family": "bar",
    "answers_question": "How does one measure compare across a few categories?",
    "requires": {
        "measure": {"min": 1, "max": 1},
        "dimension": {"min": 1, "max": 1, "max_cardinality": 12},
    },
}


def _rows(channels: int) -> list[dict]:
    return [
        {"channel": f"channel-{index % channels}", "clicks": index} for index in range(channels)
    ]


def _insert_result(
    conn,
    *,
    result_id: str,
    attempt_id: str,
    schema: dict,
    rows: list[dict],
    outcome: str = "success",
):
    """One more immutable Result on the same question.

    A Result is INSERT-ONCE -- `app.reject_analytical_evidence_mutation` refuses
    an UPDATE of its payload, which is exactly right and is why every variation
    below is a new Result rather than an edit of one.
    """
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO app.query_execution_attempts
                 (id, org_id, project_id, query_spec_version_id, result_id, requested_by)
               VALUES (%s,%s,%s,%s,%s,'test')""",
            (attempt_id, ORG, PROJECT, SPEC_VERSION, result_id),
        )
        cur.execute(
            """INSERT INTO app.query_results
                 (id, org_id, project_id, attempt_id, query_spec_version_id, outcome,
                  ai_path_absent_literal, content_hash, row_count, cell_count, byte_count,
                  truncated, started_at, ended_at)
               VALUES (%s,%s,%s,%s,%s,%s,'No AI path',%s,%s,%s,240,false,%s,%s)""",
            (
                result_id, ORG, PROJECT, attempt_id, SPEC_VERSION, outcome, HASH,
                len(rows), len(rows) * 2, AT, AT,
            ),
        )
        cur.execute(
            """INSERT INTO app.query_result_payloads
                 (result_id, org_id, project_id, content_hash, result_schema, manifest, rows_chunk)
               VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)""",
            (
                result_id, ORG, PROJECT, HASH,
                json.dumps(schema), json.dumps({"grain": "day"}), json.dumps(rows),
            ),
        )
    return result_id


class ReadOnlyConnection:
    """A connection that refuses everything but SELECT.

    AC12 has to be proved by the gate NOT NOTICING. A comment saying "this only
    reads" is honoured by everyone who reads it; this raises.
    """

    def __init__(self, conn):
        self._conn = conn
        self.statements: list[str] = []

    def cursor(self):
        return _ReadOnlyCursor(self._conn.cursor(), self.statements)


class _ReadOnlyCursor:
    def __init__(self, cursor, statements: list[str]):
        self._cursor = cursor
        self._statements = statements

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, *exc):
        return self._cursor.__exit__(*exc)

    def execute(self, statement, params=None):
        first = statement.strip().split(None, 1)[0].upper()
        if first != "SELECT":
            raise AssertionError(f"the compatibility gate wrote to the database: {first}")
        self._statements.append(statement)
        return self._cursor.execute(statement, params)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


@pytest.fixture
def seeded(live_postgres):
    """One complete chain: a question, an immutable Result, and a Chart Template."""
    conn = live_postgres
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s,%s,%s,'test')",
            (ORG, "Story 72.3 fixture", ORG.replace("_", "-")),
        )
        for project in (PROJECT, OTHER_PROJECT):
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
                "VALUES (%s,%s,%s,%s,'test')",
                (project, ORG, "Story 72.3 fixture", project.replace("_", "-")),
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
        cur.execute(
            """INSERT INTO app.visualization_templates
                 (id, org_id, project_id, label, seed_origin, created_by)
               VALUES (%s,%s,%s,'Measure by category','project','test')""",
            (TEMPLATE, ORG, PROJECT),
        )
        cur.execute(
            """INSERT INTO app.visualization_template_versions
                 (id, template_id, org_id, project_id, version_number, family,
                  spec_contract_version, schema_version, document, content_hash, created_by)
               VALUES (%s,%s,%s,%s,1,%s,%s,1,%s::jsonb,%s,'test')""",
            (
                TEMPLATE_VERSION, TEMPLATE, ORG, PROJECT,
                TEMPLATE_DOCUMENT["family"], TEMPLATE_DOCUMENT["spec_contract_version"],
                json.dumps(TEMPLATE_DOCUMENT), canonical_hash(TEMPLATE_DOCUMENT),
            ),
        )
        cur.execute(
            "UPDATE app.visualization_templates SET current_version_id = %s WHERE id = %s",
            (TEMPLATE_VERSION, TEMPLATE),
        )
    _insert_result(
        conn, result_id=RESULT, attempt_id=ATTEMPT, schema=SCHEMA, rows=_rows(3)
    )
    yield conn
    conn.rollback()


def _read(conn, *, project_id: str = PROJECT, template=TEMPLATE_VERSION, result=RESULT):
    return tc.read_template_compatibility(
        conn,
        org_id=ORG,
        project_id=project_id,
        template_version_id=template,
        result_id=result,
    )


# ---------------------------------------------------------------------------
# The gate answers, and every column it names exists.
# ---------------------------------------------------------------------------


def test_the_gate_reads_both_objects_and_answers_compatible(seeded):
    verdict = _read(seeded)
    assert verdict.state == tc.COMPATIBLE
    assert verdict.template_version_id == TEMPLATE_VERSION
    assert verdict.result_id == RESULT
    assert verdict.family == "bar"
    assert verdict.answers_question.startswith("How does one measure compare")


def test_the_verdict_moves_with_the_rows_the_result_carries(seeded):
    """Same template, same schema: only the data moved, and the bound is named."""
    wide = _insert_result(
        seeded,
        result_id=_id("qr", "7230EXAMPWID"),
        attempt_id=_id("qea", "7230EXAMPWID"),
        schema=SCHEMA,
        rows=_rows(40),
    )
    assert _read(seeded).state == tc.COMPATIBLE
    verdict = _read(seeded, result=wide)
    assert verdict.state == tc.INCOMPATIBLE
    (refusal,) = verdict.unmet
    assert refusal.code == "cardinality_over_limit"
    #  The canonical NAME of the member, read off the pinned question -- not the
    #  column, and never the identifier.
    assert "Channel (40 values)" in refusal.message
    assert CHANNEL not in refusal.message


def test_a_result_that_carries_no_dimension_is_incompatible_by_name(seeded):
    thin = _insert_result(
        seeded,
        result_id=_id("qr", "7230EXAMPTHN"),
        attempt_id=_id("qea", "7230EXAMPTHN"),
        schema={"fields": [{"id": CLICKS, "name": "clicks"}]},
        rows=[{"clicks": 1}, {"clicks": 2}],
    )
    verdict = _read(seeded, result=thin)
    assert verdict.state == tc.INCOMPATIBLE
    (refusal,) = verdict.unmet
    assert refusal.code == "missing_role"
    assert refusal.subject == "/requires/dimension/min"


def test_a_result_with_no_schema_is_unavailable_against_the_real_row(seeded):
    """An empty Result declares no field, and that is unreadable, not a failure."""
    empty = _insert_result(
        seeded,
        result_id=_id("qr", "7230EXAMPMTY"),
        attempt_id=_id("qea", "7230EXAMPMTY"),
        schema={"fields": []},
        rows=[],
    )
    verdict = _read(seeded, result=empty)
    assert verdict.state == tc.UNAVAILABLE
    assert verdict.unmet == ()
    assert verdict.unreadable is not None
    assert verdict.unreadable.code == "result_unreadable"


# ---------------------------------------------------------------------------
# The Project is in the WHERE clause, and a stranger is UNREADABLE.
# ---------------------------------------------------------------------------


def test_a_template_of_another_project_is_unavailable_and_not_incompatible(seeded):
    verdict = _read(seeded, project_id=OTHER_PROJECT)
    assert verdict.state == tc.UNAVAILABLE
    assert verdict.unreadable is not None
    assert verdict.unreadable.code == "template_unreadable"
    assert "does not resolve in this Project" in verdict.unreadable.message


def test_a_template_that_does_not_exist_answers_exactly_like_a_foreign_one(seeded):
    """Absent, foreign or unreachable are one answer: there is no enumeration oracle."""
    absent = _read(seeded, template=_id("vtv", "7230EXAMPGHT")).as_dict()
    foreign = _read(seeded, project_id=OTHER_PROJECT).as_dict()
    assert absent["unreadable"] == foreign["unreadable"]


def test_a_result_of_another_project_is_unavailable_and_not_incompatible(seeded):
    verdict = _read(seeded, result=_id("qr", "7230EXAMPGHT"))
    assert verdict.state == tc.UNAVAILABLE
    assert verdict.unreadable is not None
    assert verdict.unreadable.code == "result_unreadable"


# ---------------------------------------------------------------------------
# AC12 -- derived, never stored.
# ---------------------------------------------------------------------------


def test_the_gate_reaches_the_database_through_nothing_but_select(seeded):
    guarded = ReadOnlyConnection(seeded)
    verdict = _read(guarded)
    assert verdict.state == tc.COMPATIBLE
    # The template document, the Result envelope, the pinned question, and the
    # names of its members. Four reads and not one write.
    assert len(guarded.statements) == 4


def test_no_table_of_this_story_carries_a_compatibility_column(seeded):
    """AC12 on the schema itself: there is nowhere to store a verdict."""
    with seeded.cursor() as cur:
        cur.execute(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'app'
              AND table_name IN ('visualization_templates', 'visualization_template_versions',
                                 'query_results', 'query_result_payloads')
              AND (column_name ILIKE '%compat%' OR column_name ILIKE '%verdict%')
            """
        )
        assert cur.fetchall() == []


def test_reading_the_verdict_twice_changes_nothing_in_the_database(seeded):
    """A derived value leaves no trace, including no `last_evaluated_at`."""
    with seeded.cursor() as cur:
        cur.execute(
            "SELECT md5(t::text) FROM app.visualization_template_versions t WHERE id = %s",
            (TEMPLATE_VERSION,),
        )
        before = cur.fetchone()[0]
    assert _read(seeded).state == tc.COMPATIBLE
    assert _read(seeded).state == tc.COMPATIBLE
    with seeded.cursor() as cur:
        cur.execute(
            "SELECT md5(t::text) FROM app.visualization_template_versions t WHERE id = %s",
            (TEMPLATE_VERSION,),
        )
        assert cur.fetchone()[0] == before
