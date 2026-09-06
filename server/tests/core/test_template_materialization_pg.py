"""Story 72.6 -- materialising a Chart Template, against a real PostgreSQL.

WHY THIS CANNOT BE A UNIT TEST. `test_template_materialization.py` proves the
choice and the inversion against values -- fast, exhaustive, and completely blind
to rows. Five sentences of this story are ABOUT ROWS, and only a real database
can answer them:

  1. AC23 -- the Spec produced is INDISTINGUISHABLE from one a person composed:
     same validator, same `content_hash`, same table, same refusals. Proved by
     building the identical presentation by hand and comparing the two stored
     rows column by column;
  2. AC23 again -- the template's provenance is recorded AS PROVENANCE: a column
     migration 335 added, outside the hashed document, that no validator reads;
  3. AC24 -- the seed is never mutated: the template version's own `content_hash`
     and its whole row are re-read after a derived visualization is edited;
  4. AC25 -- an incompatible template is refused BEFORE any write, with the named
     predicate of story 72.3, and no partial Spec is left behind. Proved by
     counting rows before and after the refusal;
  5. AC26 -- no query is re-executed and no Result is created: every statement the
     materialisation issues is recorded, and the Spec pins the
     `query_spec_version_id` the Result already carried.

Every test rolls back. Nothing is left behind, even on a disposable database.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from core import template_materialization as tmz
from core.query_specs import canonical_hash
from core.semantic_compiler import COMPILER_VERSION
from core.visualization_specs import (
    VISUALIZATION_SPEC_CONTRACT_VERSION,
    VisualizationSpecRefused,
    create_visualization_spec_version,
    validate_visualization_spec,
)
from core.visualization_templates import CHART_TEMPLATE_CONTRACT_VERSION

pytestmark = pytest.mark.usefixtures("live_postgres")


def _id(prefix: str, tag: str) -> str:
    """Crockford base32 -- `I`, `L`, `O` and `U` are not legal identifier body."""
    body = (tag + "0" * 26)[:26]
    return f"{prefix}_{body}"


ORG = _id("org", "7260EXAMPTAG")
PROJECT = _id("proj", "7260EXAMPTAG")
OTHER_PROJECT = _id("proj", "7260EXAMPTWO")
VIEW = _id("sv", "7260EXAMPVEW")
VIEW_VERSION = _id("svv", "7260EXAMPVEW")
CLICKS = _id("sc", "7260EXAMPMET")
CLICKS_V = _id("scv", "7260EXAMPMET")
CHANNEL = _id("sc", "7260EXAMPDMN")
CHANNEL_V = _id("scv", "7260EXAMPDMN")
ARTIFACT = _id("sca", "7260EXAMPART")
SPEC = _id("qs", "7260EXAMPSPC")
SPEC_VERSION = _id("qsv", "7260EXAMPSPC")
ATTEMPT = _id("qea", "7260EXAMPATT")
RESULT = _id("qr", "7260EXAMPRES")
TEMPLATE = _id("vtpl", "7260EXAMPTPL")
TEMPLATE_VERSION = _id("vtv", "7260EXAMPTPL")
DEMANDING = _id("vtpl", "7260EXAMPTWO")
DEMANDING_VERSION = _id("vtv", "7260EXAMPTWO")
HASH = "a" * 64
AT = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
ACTOR = "owner@example.com"

#: The Result keys its rows by the COLUMN name and declares the member id beside
#: it (AI-337). Keeping the two different is what makes a binding meaningful.
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

#: A platform seed, so "the seed is never mutated" is literal rather than a
#: paraphrase: this document is what the deployment ships.
TEMPLATE_DOCUMENT = {
    "spec_contract_version": CHART_TEMPLATE_CONTRACT_VERSION,
    "schema_version": 1,
    "family": "bar",
    "answers_question": "How does one measure compare across a few categories?",
    "requires": {
        "measure": {"min": 1, "max": 1},
        "dimension": {"min": 1, "max": 1, "max_cardinality": 12},
    },
    "legend": {"position": "bottom", "visible": True},
    "thresholds": [{"well": "measure", "comparator": "gt", "value": 100}],
    "labels": {"override": {"measure": "Confirmed clicks"}},
}

#: The same family, asking for one measure more than this Result carries.
DEMANDING_DOCUMENT = {
    "spec_contract_version": CHART_TEMPLATE_CONTRACT_VERSION,
    "schema_version": 1,
    "family": "bar",
    "answers_question": "How do two measures compare across a few categories?",
    "requires": {"measure": {"min": 2}, "dimension": {"min": 1}},
}


def _rows(channels: int = 3) -> list[dict]:
    return [{"channel": f"channel-{index % channels}", "clicks": index} for index in range(6)]


class RecordingConnection:
    """A connection that records every statement, so a WRITE cannot hide.

    AC26 is a negative claim -- no query re-executed, no Result created -- and a
    negative claim proved by "the count did not change" is one fixture away from
    being proved by nothing. This records what was actually sent.
    """

    def __init__(self, conn):
        self._conn = conn
        self.statements: list[str] = []

    def cursor(self):
        return _RecordingCursor(self._conn.cursor(), self.statements)


class _RecordingCursor:
    def __init__(self, cursor, statements: list[str]):
        self._cursor = cursor
        self._statements = statements

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, *exc):
        return self._cursor.__exit__(*exc)

    def execute(self, statement, params=None):
        self._statements.append(" ".join(str(statement).split()))
        return self._cursor.execute(statement, params)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


def _writes(statements: list[str]) -> list[str]:
    return [s for s in statements if not s.upper().startswith("SELECT")]


@pytest.fixture
def seeded(live_postgres):
    """One question, one immutable Result, and two Chart Templates on one family."""
    conn = live_postgres
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s,%s,%s,'test')",
            (ORG, "Story 72.6 fixture", ORG.replace("_", "-")),
        )
        for project in (PROJECT, OTHER_PROJECT):
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
                "VALUES (%s,%s,%s,%s,'test')",
                (project, ORG, "Story 72.6 fixture", project.replace("_", "-")),
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
        for head, version, label, origin, document in (
            (TEMPLATE, TEMPLATE_VERSION, "Measure by category", "platform_seed", TEMPLATE_DOCUMENT),
            (DEMANDING, DEMANDING_VERSION, "Two measures by category", "project",
             DEMANDING_DOCUMENT),
        ):
            cur.execute(
                """INSERT INTO app.visualization_templates
                     (id, org_id, project_id, label, seed_origin, created_by)
                   VALUES (%s,%s,%s,%s,%s,'platform')""",
                (head, ORG, PROJECT, label, origin),
            )
            cur.execute(
                """INSERT INTO app.visualization_template_versions
                     (id, template_id, org_id, project_id, version_number, family,
                      spec_contract_version, schema_version, document, content_hash, created_by)
                   VALUES (%s,%s,%s,%s,1,%s,%s,1,%s::jsonb,%s,'platform')""",
                (
                    version, head, ORG, PROJECT, document["family"],
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


def _materialize(conn, *, template=TEMPLATE_VERSION, project_id: str = PROJECT, **kwargs):
    return tmz.materialize_template(
        conn,
        org_id=ORG,
        project_id=project_id,
        template_version_id=template,
        result_id=RESULT,
        actor=ACTOR,
        **kwargs,
    )


def _counts(conn) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT (SELECT count(*) FROM app.visualizations),
                   (SELECT count(*) FROM app.visualization_spec_versions),
                   (SELECT count(*) FROM app.query_results),
                   (SELECT count(*) FROM app.query_execution_attempts),
                   (SELECT count(*) FROM app.visualization_template_versions),
                   (SELECT count(*) FROM app.presentation_version_registry)
            """
        )
        row = cur.fetchone()
    return {
        "visualizations": row[0],
        "spec_versions": row[1],
        "results": row[2],
        "attempts": row[3],
        "template_versions": row[4],
        "registered_pins": row[5],
    }


def _stored(conn, version_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT visualization_id, query_spec_id, query_spec_version_id,
                   spec_contract_version, schema_version, family, spec, content_hash,
                   predecessor_version_id, proposed_by, created_by, version_number,
                   materialized_from_template_version_id
            FROM app.visualization_spec_versions WHERE id = %s
            """,
            (version_id,),
        )
        row = cur.fetchone()
    keys = (
        "visualization_id", "query_spec_id", "query_spec_version_id", "spec_contract_version",
        "schema_version", "family", "spec", "content_hash", "predecessor_version_id",
        "proposed_by", "created_by", "version_number", "materialized_from_template_version_id",
    )
    return dict(zip(keys, row))


# ---------------------------------------------------------------------------
# AC23 -- indistinguishable from a Spec a person composed.
# ---------------------------------------------------------------------------


def test_the_materialised_spec_lands_in_the_ordinary_table_through_the_ordinary_writer(seeded):
    created = _materialize(seeded)
    stored = _stored(seeded, created["id"])
    assert stored["spec_contract_version"] == VISUALIZATION_SPEC_CONTRACT_VERSION
    assert stored["family"] == "bar"
    assert stored["version_number"] == 1
    assert stored["predecessor_version_id"] is None
    assert stored["created_by"] == ACTOR
    #  Migration 333's trigger registers the pin of every Spec version. A
    #  materialised one is registered like any other, or a Report could not pin it.
    with seeded.cursor() as cur:
        cur.execute(
            """SELECT count(*) FROM app.presentation_version_registry
               WHERE presentation_kind = 'visualization_spec_version'
                 AND presentation_version_id = %s""",
            (created["id"],),
        )
        assert cur.fetchone()[0] == 1


def test_a_hand_built_spec_of_the_same_presentation_has_the_same_content_hash(seeded):
    """AC23, stated as the only test that can prove it: build the twin by hand."""
    created = _materialize(seeded)
    by_hand = dict(created["spec"])
    validated = validate_visualization_spec(
        seeded, project_id=PROJECT, query_spec_version_id=SPEC_VERSION, payload=by_hand
    )
    manual = create_visualization_spec_version(
        seeded,
        org_id=ORG,
        project_id=PROJECT,
        validated=validated,
        actor=ACTOR,
        name="Composed in the Builder",
    )
    assert manual["content_hash"] == created["content_hash"]

    left, right = _stored(seeded, created["id"]), _stored(seeded, manual["id"])
    #  Everything the two rows say about the DOCUMENT is identical. What differs
    #  is the version's own identity and its provenance -- and nothing else.
    differing = {key for key in left if left[key] != right[key]}
    assert differing == {"visualization_id", "materialized_from_template_version_id"}
    assert left["spec"] == right["spec"]


def test_the_template_provenance_is_a_column_and_never_a_key_of_the_document(seeded):
    created = _materialize(seeded)
    stored = _stored(seeded, created["id"])
    assert stored["materialized_from_template_version_id"] == TEMPLATE_VERSION
    #  Not in the document, at any depth: the hash must not depend on how the
    #  presentation was born.
    assert TEMPLATE_VERSION not in json.dumps(stored["spec"])
    assert TEMPLATE not in json.dumps(stored["spec"])
    assert "answers_question" not in stored["spec"]
    assert stored["content_hash"] == canonical_hash(stored["spec"])


def test_the_wells_are_filled_with_the_members_the_result_declares(seeded):
    created = _materialize(seeded)
    spec = created["spec"]
    assert spec["bindings"]["measure"] == [CLICKS]
    assert spec["bindings"]["dimension"] == [CHANNEL]
    #  The three re-anchored controls of this document, all resolved to members.
    assert spec["thresholds"][0]["member_id"] == CLICKS
    assert "well" not in spec["thresholds"][0]
    assert spec["labels"]["override"] == {CLICKS: "Confirmed clicks"}
    #  And the presentation intent crossed over untouched.
    assert spec["legend"] == {"position": "bottom", "visible": True}


def test_a_template_that_says_something_else_materialises_something_else(seeded):
    """The mutation AC23 needs: the Spec is DERIVED from the document, not fixed.

    A second version of the same template, one presentation key changed, and the
    materialised Spec must move with it -- otherwise the hash above proves only
    that two constants are equal.
    """
    first = _materialize(seeded)
    revised = dict(TEMPLATE_DOCUMENT)
    revised["legend"] = {"position": "top", "visible": False}
    second_version = _id("vtv", "7260EXAMPTPB")
    with seeded.cursor() as cur:
        cur.execute(
            """INSERT INTO app.visualization_template_versions
                 (id, template_id, org_id, project_id, version_number, family,
                  spec_contract_version, schema_version, document, content_hash,
                  predecessor_version_id, created_by)
               VALUES (%s,%s,%s,%s,2,'bar',%s,1,%s::jsonb,%s,%s,'platform')""",
            (second_version, TEMPLATE, ORG, PROJECT, CHART_TEMPLATE_CONTRACT_VERSION,
             json.dumps(revised), canonical_hash(revised), TEMPLATE_VERSION),
        )
    second = _materialize(seeded, template=second_version)
    assert second["spec"]["legend"] == {"position": "top", "visible": False}
    assert second["content_hash"] != first["content_hash"]
    assert (
        _stored(seeded, second["id"])["materialized_from_template_version_id"]
        == second_version
    )


def test_the_head_takes_the_name_of_the_template_it_came_from(seeded):
    created = _materialize(seeded)
    with seeded.cursor() as cur:
        cur.execute("SELECT name, project_id FROM app.visualizations WHERE id = %s",
                    (created["visualization_id"],))
        name, project = cur.fetchone()
    assert name == "Measure by category"
    assert project == PROJECT


# ---------------------------------------------------------------------------
# AC24 -- the seed is never mutated.
# ---------------------------------------------------------------------------


def _template_row(conn, version_id: str = TEMPLATE_VERSION) -> tuple:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT content_hash, md5(t::text) FROM app.visualization_template_versions t "
            "WHERE id = %s",
            (version_id,),
        )
        return cur.fetchone()


def test_editing_the_derived_visualization_creates_a_project_owned_version(seeded):
    before = _template_row(seeded)
    created = _materialize(seeded)

    edited = dict(created["spec"])
    edited["legend"] = {"position": "top", "visible": True}
    validated = validate_visualization_spec(
        seeded, project_id=PROJECT, query_spec_version_id=SPEC_VERSION, payload=edited
    )
    second = create_visualization_spec_version(
        seeded,
        org_id=ORG,
        project_id=PROJECT,
        validated=validated,
        actor=ACTOR,
        visualization_id=created["visualization_id"],
    )

    assert second["content_hash"] != created["content_hash"]
    stored = _stored(seeded, second["id"])
    assert stored["version_number"] == 2
    assert stored["predecessor_version_id"] == created["id"]
    assert stored["visualization_id"] == created["visualization_id"]
    #  A hand edit is not a materialisation: the lineage to the template is the
    #  predecessor, not a re-attribution of a document the template never made.
    assert stored["materialized_from_template_version_id"] is None

    #  AND THE SEED DID NOT MOVE. Its hash, and every other byte of its row.
    assert _template_row(seeded) == before


def test_two_materialisations_of_one_seed_leave_the_seed_alone_and_produce_two_heads(seeded):
    before = _template_row(seeded)
    first = _materialize(seeded)
    second = _materialize(seeded)
    assert first["visualization_id"] != second["visualization_id"]
    assert first["content_hash"] == second["content_hash"]
    assert _template_row(seeded) == before


# ---------------------------------------------------------------------------
# AC25 -- refused before any write, with the named predicate of story 72.3.
# ---------------------------------------------------------------------------


def test_an_incompatible_template_is_refused_by_the_predicate_that_fails(seeded):
    before = _counts(seeded)
    with pytest.raises(tmz.MaterializationRefused) as raised:
        _materialize(seeded, template=DEMANDING_VERSION)
    refusal = raised.value
    assert refusal.code == "chart_template_incompatible"
    assert refusal.verdict.state == "incompatible"
    #  The predicate, named, with its pointer into the TEMPLATE document and the
    #  gesture that repairs it. Never a bare boolean.
    assert [r.code for r in refusal.refusals] == ["missing_role"]
    assert refusal.refusals[0].subject == "/requires/measure/min"
    assert refusal.refusals[0].remedy
    #  The member is named by its NAME, never by its identifier.
    assert "Clicks" in refusal.refusals[0].message
    assert CLICKS not in refusal.refusals[0].message
    assert _counts(seeded) == before


def test_a_refusal_leaves_no_partial_spec_and_no_orphan_head(seeded):
    before = _counts(seeded)
    with pytest.raises(tmz.MaterializationRefused):
        _materialize(seeded, template=DEMANDING_VERSION)
    after = _counts(seeded)
    assert after["visualizations"] == before["visualizations"]
    assert after["spec_versions"] == before["spec_versions"]
    assert after["registered_pins"] == before["registered_pins"]


def test_a_refusal_issues_no_write_statement_at_all(seeded):
    guarded = RecordingConnection(seeded)
    with pytest.raises(tmz.MaterializationRefused):
        _materialize(guarded, template=DEMANDING_VERSION)
    assert _writes(guarded.statements) == []


def test_a_template_of_another_project_is_unavailable_and_not_incompatible(seeded):
    """AC25 meets AC11: a reading that failed is never rendered as a failed predicate."""
    before = _counts(seeded)
    with pytest.raises(tmz.MaterializationRefused) as raised:
        _materialize(seeded, project_id=OTHER_PROJECT)
    assert raised.value.code == "chart_template_unavailable"
    assert raised.value.verdict.state == "unavailable"
    assert raised.value.verdict.unmet == ()
    assert _counts(seeded) == before


# ---------------------------------------------------------------------------
# AC26 -- no query re-executed, no Result created.
# ---------------------------------------------------------------------------


def test_the_spec_pins_the_query_spec_version_the_result_already_carried(seeded):
    created = _materialize(seeded)
    stored = _stored(seeded, created["id"])
    assert stored["query_spec_version_id"] == SPEC_VERSION
    assert stored["query_spec_id"] == SPEC
    assert created["result_id"] == RESULT


def test_no_result_and_no_attempt_is_created(seeded):
    before = _counts(seeded)
    _materialize(seeded)
    after = _counts(seeded)
    assert after["results"] == before["results"]
    assert after["attempts"] == before["attempts"]
    assert after["template_versions"] == before["template_versions"]
    assert after["spec_versions"] == before["spec_versions"] + 1


def test_every_write_the_materialisation_issues_names_a_visualization_table(seeded):
    guarded = RecordingConnection(seeded)
    _materialize(guarded)
    written = _writes(guarded.statements)
    assert written, "the materialisation wrote nothing at all"
    for statement in written:
        assert "app.query_results" not in statement
        assert "app.query_execution_attempts" not in statement
        assert "app.visualization_template" not in statement
        assert (
            "app.visualizations" in statement
            or "app.visualization_spec_versions" in statement
        ), statement


# ---------------------------------------------------------------------------
# The document still has to survive the ordinary validator.
# ---------------------------------------------------------------------------


def test_a_template_whose_family_needs_a_grain_the_question_has_not_is_refused(seeded):
    """The family predicate is decided by the same function for both objects."""
    ungrained = dict(QUERY_SPEC)
    ungrained.pop("grain")
    other_version = _id("qsv", "7260EXAMPNOG")
    other_attempt = _id("qea", "7260EXAMPNOG")
    other_result = _id("qr", "7260EXAMPNOG")
    line_template = _id("vtpl", "7260EXAMPLIN")
    line_version = _id("vtv", "7260EXAMPLIN")
    with seeded.cursor() as cur:
        cur.execute(
            """INSERT INTO app.query_spec_versions
                 (id, query_spec_id, org_id, project_id, version_number, semantic_view_id,
                  semantic_view_version_id, spec, content_hash, predecessor_version_id,
                  created_by)
               VALUES (%s,%s,%s,%s,2,%s,%s,%s::jsonb,%s,%s,'test')""",
            (other_version, SPEC, ORG, PROJECT, VIEW, VIEW_VERSION,
             json.dumps(ungrained), "b" * 64, SPEC_VERSION),
        )
        cur.execute(
            """INSERT INTO app.query_execution_attempts
                 (id, org_id, project_id, query_spec_version_id, result_id, requested_by)
               VALUES (%s,%s,%s,%s,%s,'test')""",
            (other_attempt, ORG, PROJECT, other_version, other_result),
        )
        cur.execute(
            """INSERT INTO app.query_results
                 (id, org_id, project_id, attempt_id, query_spec_version_id, outcome,
                  ai_path_absent_literal, content_hash, row_count, cell_count, byte_count,
                  truncated, started_at, ended_at)
               VALUES (%s,%s,%s,%s,%s,'success','No AI path',%s,6,12,240,false,%s,%s)""",
            (other_result, ORG, PROJECT, other_attempt, other_version, "b" * 64, AT, AT),
        )
        cur.execute(
            """INSERT INTO app.query_result_payloads
                 (result_id, org_id, project_id, content_hash, result_schema, manifest, rows_chunk)
               VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)""",
            (other_result, ORG, PROJECT, "b" * 64, json.dumps(SCHEMA),
             json.dumps({}), json.dumps(_rows())),
        )
        document = {
            "spec_contract_version": CHART_TEMPLATE_CONTRACT_VERSION,
            "schema_version": 1,
            "family": "line",
            "answers_question": "How does one measure move over time?",
            "requires": {"measure": {"min": 1}, "dimension": {"min": 1}},
        }
        cur.execute(
            """INSERT INTO app.visualization_templates
                 (id, org_id, project_id, label, seed_origin, created_by)
               VALUES (%s,%s,%s,'Measure over time','platform_seed','platform')""",
            (line_template, ORG, PROJECT),
        )
        cur.execute(
            """INSERT INTO app.visualization_template_versions
                 (id, template_id, org_id, project_id, version_number, family,
                  spec_contract_version, schema_version, document, content_hash, created_by)
               VALUES (%s,%s,%s,%s,1,'line',%s,1,%s::jsonb,%s,'platform')""",
            (line_version, line_template, ORG, PROJECT, CHART_TEMPLATE_CONTRACT_VERSION,
             json.dumps(document), canonical_hash(document)),
        )
        cur.execute(
            "UPDATE app.visualization_templates SET current_version_id = %s WHERE id = %s",
            (line_version, line_template),
        )

    before = _counts(seeded)
    with pytest.raises(VisualizationSpecRefused) as raised:
        tmz.materialize_template(
            seeded,
            org_id=ORG,
            project_id=PROJECT,
            template_version_id=line_version,
            result_id=other_result,
            actor=ACTOR,
        )
    assert [r.code for r in raised.value.refusals] == ["missing_grain"]
    assert _counts(seeded) == before
