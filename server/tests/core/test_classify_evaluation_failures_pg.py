"""Story 75-7 -- the classifier's LOADER, replayed against a real Postgres.

WHY PG-GATED. The pure rules are proven beside this file, over dicts. What a
mock cannot prove is the half that costs sessions: that the SELECTs read the
tables the run writer actually filled, under their real names and their real
column shapes. `app.evaluation_case_dimension_verdicts` is not
`app.evaluation_case_verdicts`; `required_missing` holds two shapes at once;
`app.evaluation_assertion_results` only has rows when the trusted evaluator ran.
A fake cursor that recognises SQL by its text would be green on all three.

WHAT IS PROVEN HERE, in order: a run unrolled by the product's own executor
loads and classifies; a question nobody pinned a subject for is `no_subject` and
never `unverifiable_by_design`; a walk that SKIPPED the governed context loads a
non-empty `required_missing`, and one that read it AFTER the query loads a
non-empty `order_violations` -- both through the real loader, so the two shapes
the classifier reads are proven to be the two shapes the writer writes; a run
still `recording` is refused BY NAME; a comparison between two runs whose
question sets differ is refused by name; a comparison whose environment moved is
refused by name; the command a reader actually types (`main`) runs end to end and
says `SET TRANSACTION READ ONLY` to the database; and the classifier writes
nothing.

TRAPS THIS FILE ENCODES, both already paid for elsewhere in this suite:

1. A refused statement aborts the whole transaction in psycopg 3. Every expected
   refusal below is raised before any statement, or is wrapped in a savepoint.
2. `live_postgres` rolls back on teardown; nothing here depends on another
   test's rows.

No production identifier appears in this file: `example.com`, `proj_EXAMPLE`.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json

import pytest
from ulid import ULID

pytest.importorskip("psycopg")

from core.evaluation_failure_classes import (  # noqa: E402
    CLASS_MISSING_EXEMPLAR,
    CLASS_NO_SUBJECT,
    CLASS_PASSED,
    ENVIRONMENT_PINS,
    EnvironmentDrift,
    FingerprintMismatch,
    compare_runs,
)

_ACTOR = "owner@example.com"

#: The governed context node the expected pattern requires and the observed path
#: crosses. A fixture identity, never a production one.
_NOTE_ID = "kn_EXAMPLE"
_NOTE_KEY = f"context-hub/knowledge-note/{_NOTE_ID}"


def _hex64(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


def _classifier():
    """The SCRIPT's loader, imported from the script itself.

    Importing it rather than re-deriving the queries is the whole point: a test
    that wrote its own SELECTs would prove a second reader, not the one Jean will
    run.
    """
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "scripts" / "classify_evaluation_failures.py"
    spec = importlib.util.spec_from_file_location("classify_evaluation_failures", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# The chain a Golden Question and an Evaluation Run both need upstream. Same
# shape as `test_evaluation_run_executor_pg.py`, which is the file this one
# extends: one project, one domain, one published Semantic View version.
# ---------------------------------------------------------------------------


@pytest.fixture
def scope(live_postgres):
    conn = live_postgres
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('app.evaluation_path_comparisons')")
        if cur.fetchone()[0] is None:
            pytest.skip(
                "migration 153 is not applied to TEST_POSTGRES_DSN -- run "
                "scripts/apply_migrations.py before this suite"
            )

    org_id = _uid("org")
    project_id = _uid("proj")
    domain_id = _uid("bd")
    view_id = _uid("sv")
    view_version_id = _uid("svv")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s,%s,%s,%s)",
            (org_id, "Classifier test org", org_id.lower(), _ACTOR),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
            "VALUES (%s,%s,%s,%s,'active',%s)",
            (project_id, org_id, "Classifier test project", project_id.lower(), _ACTOR),
        )
        cur.execute(
            "INSERT INTO app.mdm_business_domains (id, org_id, slug, name, created_by) "
            "VALUES (%s,%s,%s,%s,%s)",
            (domain_id, org_id, "classify-test-domain", "Classifier test domain", _ACTOR),
        )
        cur.execute(
            """
            INSERT INTO app.mdm_business_domain_versions
                (domain_id, version_number, org_id, slug, name, status, created_by,
                 created_at, updated_at, change_kind, changed_by)
            VALUES (%s, 1, %s, %s, %s, 'active', %s, NOW(), NOW(), 'created', %s)
            """,
            (domain_id, org_id, "classify-test-domain", "Classifier test domain", _ACTOR, _ACTOR),
        )
        cur.execute(
            "INSERT INTO app.semantic_views (id, project_id, name, lifecycle_status, created_by) "
            "VALUES (%s,%s,%s,'published',%s)",
            (view_id, project_id, "classify_test_view", _ACTOR),
        )
        cur.execute(
            """
            INSERT INTO app.semantic_view_versions
                (id, view_id, project_id, version_number, status, name, label,
                 dependency_fingerprint, content_hash, created_by)
            VALUES (%s, %s, %s, 1, 'published', %s, %s, %s, %s, %s)
            """,
            (
                view_version_id,
                view_id,
                project_id,
                "classify_test_view",
                "Classifier test view",
                _hex64("dependency"),
                _hex64("content"),
                _ACTOR,
            ),
        )

    yield {
        "conn": conn,
        "org_id": org_id,
        "project_id": project_id,
        "domain_id": domain_id,
        "semantic_view_id": view_id,
        "semantic_view_version_id": view_version_id,
    }
    conn.rollback()


def _view_key(scope) -> str:
    return f"governance/semantic-view/{scope['semantic_view_version_id']}"


def _definition(scope, *, question: str):
    return {
        "business_domain_id": scope["domain_id"],
        "business_domain_version_number": 1,
        "semantic_view_id": scope["semantic_view_id"],
        "semantic_view_version_id": scope["semantic_view_version_id"],
        "semantic_view_version_role": "baseline",
        "question": question,
        "time_boundary": {"as_of": "2026-07-31", "grain": "month"},
        "expected_result": [
            {"assertion_type": "value", "member_id": "sc_spend", "tolerance": None},
        ],
        "required_provenance": [{"link_kind": "semantic_view", "required": True}],
        "expected_ai_path": {
            "grammar_version": 1,
            "required_nodes": [
                {"key": _NOTE_KEY, "step_kind": "knowledge_read"},
                {"key": _view_key(scope), "step_kind": "semantic_query"},
            ],
            "order_constraints": [{"before": _NOTE_KEY, "after": _view_key(scope)}],
        },
        "result_type": "breakdown",
        "capability_tags": ["media_spend_reporting"],
        "severity": "critical",
    }


def _golden_question(scope, *, question: str, title: str) -> str:
    """One active Golden Question, through the PRODUCT write path."""
    from core.golden_questions import (
        create_golden_question,
        set_lifecycle,
        validate_golden_question_version,
    )

    conn = scope["conn"]
    validated = validate_golden_question_version(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        payload=_definition(scope, question=question),
    )
    created = create_golden_question(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        title=title,
        owner=_ACTOR,
        validated=validated,
        actor=_ACTOR,
    )
    set_lifecycle(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        golden_question_id=created["golden_question_id"],
        lifecycle="active",
        actor=_ACTOR,
    )
    return created["version_id"]


_CONTEXT_STEP = {
    "step_kind": "knowledge_read",
    "outcome": "succeeded",
    "owner_workspace": "context-hub",
    "owner_object_type": "knowledge-note",
    "owner_object_id": _NOTE_ID,
    "tool_name": "search_context",
}


def _query_step(scope) -> dict:
    return {
        "step_kind": "semantic_query",
        "outcome": "succeeded",
        "owner_workspace": "governance",
        "owner_object_type": "semantic-view",
        "owner_object_id": scope["semantic_view_version_id"],
        "tool_name": "execute_analyze_query_spec",
    }


def _walk(scope, steps: tuple[dict, ...]) -> str:
    """A finalized observed path made of exactly these steps, in this order."""
    from core.ai_paths import append_step, begin_path, finalize_path

    conn = scope["conn"]
    path = begin_path(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        actor=_ACTOR,
        policy_snapshot={"pre_query_gate": "measured"},
        model_ref="model-example-1",
        tool_catalog_version=_hex64("catalog"),
    )
    for step in steps:
        append_step(conn, path_id=path["id"], project_id=scope["project_id"], **step)
    finalize_path(conn, path_id=path["id"], project_id=scope["project_id"], outcome="succeeded")
    return path["id"]


def _observed_path(scope) -> str:
    """A finalized path that reads the context FIRST, then queries the View."""
    return _walk(scope, (_CONTEXT_STEP, _query_step(scope)))


def _stored_comparison(scope, case_id: str) -> dict:
    """The row the run writer wrote, read back column by column.

    Asserted beside the classification so a green classification can never come
    from an EMPTY comparison the classifier happened to be tolerant of.
    """
    with scope["conn"].cursor() as cur:
        cur.execute(
            "SELECT evidence_state, path_verdict, required_missing, forbidden_present, "
            "       order_violations, version_mismatches "
            "  FROM app.evaluation_path_comparisons WHERE case_id = %s",
            (case_id,),
        )
        row = cur.fetchone()
    assert row is not None, "no path comparison was stored for this case"
    return {
        "evidence_state": row[0],
        "path_verdict": row[1],
        "required_missing": row[2],
        "forbidden_present": row[3],
        "order_violations": row[4],
        "version_mismatches": row[5],
    }


def _context_version_set(scope) -> str:
    """ONE frozen context resolution for every run of this scope.

    `analyze-and-test.md`, "The before/after protocol": the Context Version Set
    is one of the seven pins held constant. Two sets that freeze the same entries
    are still two ids, so a fixture that minted one per run made every comparison
    a comparison between two environments -- and the instrument now refuses that,
    correctly.
    """
    from core.evaluation_runs import create_context_version_set

    if scope.get("context_version_set_id"):
        return scope["context_version_set_id"]
    created = create_context_version_set(
        scope["conn"],
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        entries=[
            {
                "owner_workspace": "context-hub",
                "owner_object_type": "knowledge-note",
                "owner_object_id": _NOTE_ID,
                "owner_version_id": "knv_EXAMPLE",
            }
        ],
    )
    scope["context_version_set_id"] = created["id"]
    return created["id"]


def _open_run(scope) -> str:
    from core.evaluation_runs import create_run_profile, open_evaluation_run

    conn = scope["conn"]
    profile = create_run_profile(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        name=f"classifier fixture {ULID()}",
        evidence_mode="offline",
        actor=_ACTOR,
    )
    run = open_evaluation_run(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        run_profile_id=profile["id"],
        semantic_view_id=scope["semantic_view_id"],
        semantic_view_version_id=scope["semantic_view_version_id"],
        context_version_set_id=_context_version_set(scope),
        model_ref="model-example-1",
        host_capability_profile={"host": "example-host"},
        tool_catalog_version=_hex64("catalog"),
        data_snapshot_ref={"seed_set": "classifier-fixture"},
        as_of=_dt.date(2026, 7, 31),
        actor=_ACTOR,
    )
    return run["id"]


def _execute(scope, run_id, **kwargs):
    from core.evaluation_run_executor import execute_evaluation_run

    return execute_evaluation_run(
        scope["conn"],
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        run_id=run_id,
        actor=_ACTOR,
        **kwargs,
    )


# ===========================================================================


def test_the_loader_classifies_a_run_the_product_executor_unrolled(scope):
    """Two active questions, one walked and one not: two classes, not one number.

    The walked question's path matches its pattern, so `path_quality` passes and
    the case is `passed`. The other pins no subject at all, so it is `no_subject`
    -- the class that names the gesture that would make it judgeable, instead of
    six `unverifiable` verdicts a reader cannot act on.
    """
    walked = _golden_question(scope, question="Paid media spend by market?", title="Spend")
    unwalked = _golden_question(scope, question="Clicks by campaign?", title="Clicks")
    path_id = _observed_path(scope)
    run_id = _open_run(scope)

    report_from_executor = _execute(scope, run_id, subjects={walked: {"ai_path_id": path_id}})
    assert report_from_executor["question_count"] == 2

    module = _classifier()
    report = module.load_report(scope["conn"], run_id=run_id, project_id=scope["project_id"])

    assert report["lifecycle"] == "finalized"
    assert report["case_count"] == 2
    by_question = {item["golden_question_version_id"]: item for item in report["cases"]}
    assert by_question[walked]["class"] == CLASS_PASSED
    assert by_question[unwalked]["class"] == CLASS_NO_SUBJECT
    # The gesture is a door that exists, not a table name and not a deploy state.
    assert "evaluation-runs" in by_question[unwalked]["door"]
    assert report["class_counts"][CLASS_NO_SUBJECT] == 1
    assert report["class_counts"][CLASS_PASSED] == 1
    # The tally the classifier rebuilds is the tally the run wrote.
    assert report["dimension_counts"] == report_from_executor["verdict_counts"]


def test_the_unwalked_question_is_not_reported_as_unverifiable_by_design(scope):
    """The ordering rule, proven on real rows and not only on a fixture dict.

    A case nobody walked carries six `unverifiable` verdicts whose owners are all
    declared elsewhere -- exactly the shape of `unverifiable_by_design`. Reporting
    it there would blame an undelivered evaluator for an execution that never
    happened, so `no_subject` is first.
    """
    _golden_question(scope, question="Clicks by campaign?", title="Clicks")
    run_id = _open_run(scope)
    _execute(scope, run_id)

    module = _classifier()
    report = module.load_report(scope["conn"], run_id=run_id, project_id=scope["project_id"])

    (case,) = report["cases"]
    assert case["class"] == CLASS_NO_SUBJECT
    assert case["signals"] == ["no Result and no observed AI Path pinned on the case"]
    with scope["conn"].cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.evaluation_case_dimension_verdicts WHERE case_id = %s",
            (case["case_id"],),
        )
        assert cur.fetchone()[0] == 6


def test_a_recording_run_is_refused_by_name(scope):
    """A run that can still grow would have its classification invalidated.

    The refusal names the state and the gesture that ends it, never a table.
    """
    _golden_question(scope, question="Paid media spend by market?", title="Spend")
    run_id = _open_run(scope)

    module = _classifier()
    with pytest.raises(module.Refused) as refusal:
        module.load_report(scope["conn"], run_id=run_id, project_id=scope["project_id"])
    message = str(refusal.value)
    assert "`recording`" in message
    assert "finalize" in message


def test_a_run_of_another_project_is_refused_by_name(scope):
    _golden_question(scope, question="Paid media spend by market?", title="Spend")
    run_id = _open_run(scope)
    _execute(scope, run_id)

    module = _classifier()
    with pytest.raises(module.Refused) as refusal:
        module.load_report(scope["conn"], run_id=run_id, project_id="proj_EXAMPLE")
    assert "proj_EXAMPLE" in str(refusal.value)


def test_an_unknown_run_is_refused_by_name(scope):
    module = _classifier()
    with pytest.raises(module.Refused) as refusal:
        module.load_report(scope["conn"], run_id="erun_EXAMPLE", project_id=None)
    assert "erun_EXAMPLE" in str(refusal.value)


def test_compare_refuses_two_runs_over_different_question_sets(scope):
    """Two real runs, two different cohorts, one named refusal.

    The second run judges one more question than the first, so their
    `question_set_fingerprint` differs and the delta would compare two different
    cohorts. The instrument says so by name and prints nothing.
    """
    first_question = _golden_question(
        scope, question="Paid media spend by market?", title="Spend"
    )
    before_run = _open_run(scope)
    _execute(scope, before_run, golden_question_version_ids=[first_question])

    _golden_question(scope, question="Clicks by campaign?", title="Clicks")
    after_run = _open_run(scope)
    _execute(scope, after_run)

    module = _classifier()
    before = module.load_report(
        scope["conn"], run_id=before_run, project_id=scope["project_id"]
    )
    after = module.load_report(scope["conn"], run_id=after_run, project_id=scope["project_id"])
    assert before["question_set_fingerprint"] != after["question_set_fingerprint"]

    with pytest.raises(FingerprintMismatch) as refusal:
        compare_runs(before, after)
    assert "different question sets" in str(refusal.value)


def test_two_runs_over_the_same_question_set_compare(scope):
    """The before/after the epic asks for, on the same fingerprint.

    Nothing was repaired between these two runs, so every delta is zero -- which
    is exactly what makes it a measurement: the instrument does not manufacture
    movement.
    """
    version_id = _golden_question(scope, question="Paid media spend by market?", title="Spend")
    before_run = _open_run(scope)
    _execute(scope, before_run, golden_question_version_ids=[version_id])
    after_run = _open_run(scope)
    _execute(scope, after_run, golden_question_version_ids=[version_id])

    module = _classifier()
    before = module.load_report(
        scope["conn"], run_id=before_run, project_id=scope["project_id"]
    )
    after = module.load_report(scope["conn"], run_id=after_run, project_id=scope["project_id"])
    assert before["question_set_fingerprint"] == after["question_set_fingerprint"]

    delta = compare_runs(before, after)
    assert delta["classes"][CLASS_NO_SUBJECT]["delta"] == 0
    assert all(row["pass_delta"] == 0 for row in delta["dimensions"].values())


def test_the_classifier_writes_nothing(scope):
    """A lecture n'ecrit pas. Counted, not asserted by intention."""
    _golden_question(scope, question="Paid media spend by market?", title="Spend")
    run_id = _open_run(scope)
    _execute(scope, run_id)

    def _counts():
        with scope["conn"].cursor() as cur:
            cur.execute(
                "SELECT (SELECT count(*) FROM app.evaluation_run_cases), "
                "       (SELECT count(*) FROM app.evaluation_case_dimension_verdicts), "
                "       (SELECT count(*) FROM app.evaluation_path_comparisons), "
                "       (SELECT count(*) FROM app.evaluation_runs)"
            )
            return cur.fetchone()

    before = _counts()
    module = _classifier()
    module.load_report(scope["conn"], run_id=run_id, project_id=scope["project_id"])
    assert _counts() == before


def test_a_walk_that_skipped_the_context_loads_a_non_empty_required_missing(scope):
    """The shape the classifier's first rail reads, written by the real writer.

    The expected path requires the knowledge note before the View; this walk
    queried the View and never read the note. `required_missing` therefore holds
    one node, the class is `missing_exemplar` and the signal names the node --
    which is the whole of what epic 75 asks a failing case to say.
    """
    version_id = _golden_question(scope, question="Paid media spend by market?", title="Spend")
    path_id = _walk(scope, (_query_step(scope),))
    run_id = _open_run(scope)
    _execute(scope, run_id, subjects={version_id: {"ai_path_id": path_id}})

    module = _classifier()
    report = module.load_report(scope["conn"], run_id=run_id, project_id=scope["project_id"])
    (case,) = report["cases"]

    stored = _stored_comparison(scope, case["case_id"])
    assert stored["required_missing"], stored
    assert any(_NOTE_KEY in str(item.get("key")) for item in stored["required_missing"])
    assert case["class"] == CLASS_MISSING_EXEMPLAR
    assert case["story"] == "75-3"
    assert any(_NOTE_KEY in signal for signal in case["signals"])


def test_a_walk_that_read_the_context_last_loads_a_non_empty_order_violations(scope):
    """Both nodes crossed, in the wrong order: nothing is MISSING, a rule is broken.

    This is the shape that would be lost if the classifier only read
    `required_missing`: the model consulted the governed context, but after it
    had already answered, which the amendment names explicitly ("the query ran
    before the context was read").
    """
    version_id = _golden_question(scope, question="Paid media spend by market?", title="Spend")
    path_id = _walk(scope, (_query_step(scope), _CONTEXT_STEP))
    run_id = _open_run(scope)
    _execute(scope, run_id, subjects={version_id: {"ai_path_id": path_id}})

    module = _classifier()
    report = module.load_report(scope["conn"], run_id=run_id, project_id=scope["project_id"])
    (case,) = report["cases"]

    stored = _stored_comparison(scope, case["case_id"])
    assert stored["order_violations"], stored
    assert not stored["required_missing"], stored
    assert any(_NOTE_KEY in str(item.get("before")) for item in stored["order_violations"])
    assert case["class"] == CLASS_MISSING_EXEMPLAR
    assert any("prerequisite violated" in signal for signal in case["signals"])


def test_two_runs_whose_environment_moved_are_refused_by_name(scope):
    """The seven pins, read off the real columns, and one of them moved.

    The second run is opened on a second Context Version Set that freezes the
    same entries. Same question set, same everything else -- and still not a
    before/after of one repair, because the environment is not the one that was
    measured.
    """
    from core.evaluation_runs import (
        create_context_version_set,
        create_run_profile,
        open_evaluation_run,
    )

    version_id = _golden_question(scope, question="Paid media spend by market?", title="Spend")
    before_run = _open_run(scope)
    _execute(scope, before_run, golden_question_version_ids=[version_id])

    other_set = create_context_version_set(
        scope["conn"],
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        entries=[
            {
                "owner_workspace": "context-hub",
                "owner_object_type": "knowledge-note",
                "owner_object_id": _NOTE_ID,
                "owner_version_id": "knv_EXAMPLE",
            }
        ],
    )
    profile = create_run_profile(
        scope["conn"],
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        name=f"classifier fixture {ULID()}",
        evidence_mode="offline",
        actor=_ACTOR,
    )
    after = open_evaluation_run(
        scope["conn"],
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        run_profile_id=profile["id"],
        semantic_view_id=scope["semantic_view_id"],
        semantic_view_version_id=scope["semantic_view_version_id"],
        context_version_set_id=other_set["id"],
        model_ref="model-example-1",
        host_capability_profile={"host": "example-host"},
        tool_catalog_version=_hex64("catalog"),
        data_snapshot_ref={"seed_set": "classifier-fixture"},
        as_of=_dt.date(2026, 7, 31),
        actor=_ACTOR,
    )
    _execute(scope, after["id"], golden_question_version_ids=[version_id])

    module = _classifier()
    before_report = module.load_report(
        scope["conn"], run_id=before_run, project_id=scope["project_id"]
    )
    after_report = module.load_report(
        scope["conn"], run_id=after["id"], project_id=scope["project_id"]
    )
    assert set(before_report["environment"]) == set(ENVIRONMENT_PINS)
    assert before_report["question_set_fingerprint"] == after_report["question_set_fingerprint"]

    with pytest.raises(EnvironmentDrift) as refusal:
        compare_runs(before_report, after_report)
    assert [item["pin"] for item in refusal.value.differences] == ["context_version_set_id"]


# ---------------------------------------------------------------------------
# The command a reader actually types.
# ---------------------------------------------------------------------------


class _ReadOnlyProbe:
    """The live connection, with the two things `main` owns taken back.

    `main` opens its own connection, says `SET TRANSACTION READ ONLY` to it and
    rolls it back at the end. Neither is possible on the fixture's connection --
    it has already written, and Postgres refuses a mode change after the first
    statement (SQLSTATE 25001, which would abort the whole fixture) -- so the
    statement is RECORDED here instead of sent, and the rollback is refused so
    the rows survive the assertions.

    What this proves is the half a fake cursor cannot fake: `main` really loads
    the run, classifies it and prints it. That the database honours
    `SET TRANSACTION READ ONLY` is Postgres's own contract, and the test below
    states that the instrument says it.
    """

    def __init__(self, conn, statements):
        self._conn = conn
        self._statements = statements

    def cursor(self, *args, **kwargs):
        return _ProbeCursor(self._conn, self._statements, args, kwargs)

    def transaction(self):
        return self._conn.transaction()

    def rollback(self):
        self._statements.append("ROLLBACK (refused: the fixture owns this transaction)")

    def close(self):
        self._statements.append("CLOSE (refused: the fixture owns this connection)")

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _ProbeCursor:
    def __init__(self, conn, statements, args, kwargs):
        self._conn = conn
        self._statements = statements
        self._cursor = None
        self._args = args
        self._kwargs = kwargs

    def __enter__(self):
        self._cursor = self._conn.cursor(*self._args, **self._kwargs).__enter__()
        return self

    def __exit__(self, *exc):
        return self._cursor.__exit__(*exc)

    def execute(self, statement, params=None):
        self._statements.append(" ".join(str(statement).split()))
        if "SET TRANSACTION READ ONLY" in str(statement):
            return None
        return self._cursor.execute(statement, params)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


def test_main_runs_the_whole_command_and_says_read_only_to_the_database(scope, capsys, monkeypatch):
    """`python scripts/classify_evaluation_failures.py --run-id ... --json`, end to end."""
    version_id = _golden_question(scope, question="Paid media spend by market?", title="Spend")
    path_id = _observed_path(scope)
    run_id = _open_run(scope)
    _execute(scope, run_id, subjects={version_id: {"ai_path_id": path_id}})

    module = _classifier()
    statements: list[str] = []
    monkeypatch.setattr(module, "_connect", lambda _dsn: _ReadOnlyProbe(scope["conn"], statements))

    code = module.main(
        ["--dsn", "postgresql://connector@127.0.0.1/example", "--run-id", run_id, "--json"]
    )
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["run_id"] == run_id
    assert printed["lifecycle"] == "finalized"
    assert printed["class_counts"][CLASS_PASSED] == 1
    assert set(printed["environment"]) == set(ENVIRONMENT_PINS)
    #  Dit à la base, pas seulement voulu : c'est la première instruction.
    assert statements[0] == "SET TRANSACTION READ ONLY"
    assert any(statement.startswith("ROLLBACK") for statement in statements)


def test_main_refuses_a_run_that_is_not_finalized_and_prints_nothing(scope, capsys, monkeypatch):
    """A `recording` run: exit code 2, the refusal on stderr, no report on stdout."""
    _golden_question(scope, question="Paid media spend by market?", title="Spend")
    run_id = _open_run(scope)

    module = _classifier()
    statements: list[str] = []
    monkeypatch.setattr(module, "_connect", lambda _dsn: _ReadOnlyProbe(scope["conn"], statements))

    code = module.main(
        ["--dsn", "postgresql://connector@127.0.0.1/example", "--run-id", run_id, "--json"]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "REFUSED" in captured.err
    assert "`recording`" in captured.err


def test_main_refuses_two_output_formats_and_a_missing_dsn(monkeypatch):
    module = _classifier()
    monkeypatch.delenv("PLATFORM_DB_URL", raising=False)
    monkeypatch.delenv("TEST_POSTGRES_DSN", raising=False)
    assert module.main(["--run-id", "erun_EXAMPLE", "--json", "--markdown"]) == 2
    assert module.main(["--run-id", "erun_EXAMPLE"]) == 2

