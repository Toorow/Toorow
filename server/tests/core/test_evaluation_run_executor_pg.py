"""Story 67.15 A -- the Regression Run executor and the adherence reader, replayed live.

WHY THIS FILE IS PG-GATED AND NOT MOCKED. Both halves under test are defects of
the *seam*, and a mocked cursor cannot fail a seam:

  * `trg_evaluation_case_verdicts_path_evidence` (migration 153) refuses
    `path_quality = 'pass'` unless `app.evaluation_path_comparisons` resolves an
    observed path for the case. Until this chantier nothing wrote that row from
    the run path, so the FIRST case whose observed path actually matched its
    expected pattern would have been refused BY THE DATABASE -- at the exact
    moment the loop finally worked. Against a mock, the same code is green and
    proves nothing.
  * `app.query_adherence` had no reader at all. A test that asserted a Python
    dict would not have noticed.

WHAT IS PROVEN HERE, in order: a recorded run unrolls its whole question set in
one call; a matching path passes and the comparison row lands; a forbidden tool
that was actually used fails the case; a question with no pinned subject is
`unverifiable` and never `pass`; a run of another Project does not resolve; and
the adherence measure reads back, with an honest empty state when nothing was
measured.

TRAPS THIS FILE ENCODES (both have already cost a session a green run that
proved nothing -- see `test_evaluation_persistence_pg.py`):

1. A refused statement aborts the whole transaction in psycopg 3. Every expected
   refusal below is a Python-level refusal raised BEFORE any statement, or is
   wrapped in `conn.transaction()` (a savepoint).
2. `live_postgres` rolls back on teardown. Nothing here depends on another
   test's rows; each builds its own chain.

No production identifier appears in this file: `example.com`, `proj_EXAMPLE`.
"""

from __future__ import annotations

import datetime as _dt
import hashlib

import pytest
from ulid import ULID

pytest.importorskip("psycopg")

_ACTOR = "owner@example.com"


def _hex64(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


# ---------------------------------------------------------------------------
# The chain a Golden Question and an Evaluation Run both need upstream.
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
    other_project_id = _uid("proj")
    domain_id = _uid("bd")
    view_id = _uid("sv")
    view_version_id = _uid("svv")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s,%s,%s,%s)",
            (org_id, "Executor test org", org_id.lower(), _ACTOR),
        )
        for pid, label in ((project_id, "A"), (other_project_id, "B")):
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
                "VALUES (%s,%s,%s,%s,'active',%s)",
                (pid, org_id, f"Executor test project {label}", pid.lower(), _ACTOR),
            )
        cur.execute(
            "INSERT INTO app.mdm_business_domains (id, org_id, slug, name, created_by) "
            "VALUES (%s,%s,%s,%s,%s)",
            (domain_id, org_id, "exec-test-domain", "Executor test domain", _ACTOR),
        )
        cur.execute(
            """
            INSERT INTO app.mdm_business_domain_versions
                (domain_id, version_number, org_id, slug, name, status, created_by,
                 created_at, updated_at, change_kind, changed_by)
            VALUES (%s, 1, %s, %s, %s, 'active', %s, NOW(), NOW(), 'created', %s)
            """,
            (domain_id, org_id, "exec-test-domain", "Executor test domain", _ACTOR, _ACTOR),
        )
        cur.execute(
            "INSERT INTO app.semantic_views (id, project_id, name, lifecycle_status, created_by) "
            "VALUES (%s,%s,%s,'published',%s)",
            (view_id, project_id, "exec_test_view", _ACTOR),
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
                "exec_test_view",
                "Executor test view",
                _hex64("dependency"),
                _hex64("content"),
                _ACTOR,
            ),
        )

    yield {
        "conn": conn,
        "org_id": org_id,
        "project_id": project_id,
        "other_project_id": other_project_id,
        "domain_id": domain_id,
        "semantic_view_id": view_id,
        "semantic_view_version_id": view_version_id,
    }
    conn.rollback()


#: The two governed objects the expected pattern names, and the observed path
#: crosses. Owner ids are fixture identities, never production ones.
_NOTE_ID = "kn_EXAMPLE"
_NOTE_KEY = f"context-hub/knowledge-note/{_NOTE_ID}"


def _view_key(scope) -> str:
    return f"governance/semantic-view/{scope['semantic_view_version_id']}"


def _pattern(scope, **overrides):
    """A pattern in the ONE grammar the comparator reads (commit 2cb09041)."""
    pattern = {
        "grammar_version": 1,
        "required_nodes": [
            {"key": _NOTE_KEY, "step_kind": "knowledge_read"},
            {"key": _view_key(scope), "step_kind": "semantic_query"},
        ],
        "order_constraints": [{"before": _NOTE_KEY, "after": _view_key(scope)}],
    }
    pattern.update(overrides)
    return pattern


def _definition(scope, **overrides):
    payload = {
        "business_domain_id": scope["domain_id"],
        "business_domain_version_number": 1,
        "semantic_view_id": scope["semantic_view_id"],
        "semantic_view_version_id": scope["semantic_view_version_id"],
        "semantic_view_version_role": "baseline",
        "question": "What was paid media spend last completed month, by market?",
        "time_boundary": {"as_of": "2026-07-31", "grain": "month"},
        "expected_result": [
            {"assertion_type": "value", "member_id": "sc_spend", "tolerance": None},
        ],
        "required_provenance": [{"link_kind": "semantic_view", "required": True}],
        "expected_ai_path": _pattern(scope),
        "result_type": "breakdown",
        "capability_tags": ["media_spend_reporting"],
        "severity": "critical",
    }
    payload.update(overrides)
    return payload


def _golden_question(scope, *, activate: bool = True, **overrides) -> str:
    """Create one Golden Question through the PRODUCT write path, return its version id.

    Through `validate_golden_question_version` / `create_golden_question` and not
    by INSERT: a fixture that wrote the row itself would prove the comparator
    against a pattern the product cannot actually author -- which is exactly the
    gap that let two dialects live side by side for three weeks.
    """
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
        payload=_definition(scope, **overrides),
    )
    created = create_golden_question(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        title="Paid media spend by market",
        owner=_ACTOR,
        validated=validated,
        actor=_ACTOR,
    )
    if activate:
        set_lifecycle(
            conn,
            org_id=scope["org_id"],
            project_id=scope["project_id"],
            golden_question_id=created["golden_question_id"],
            lifecycle="active",
            actor=_ACTOR,
        )
    return created["version_id"]


def _observed_path(scope, steps, *, outcome: str = "succeeded") -> str:
    """One finalized observed AI Path with the steps given, in order."""
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
    finalize_path(conn, path_id=path["id"], project_id=scope["project_id"], outcome=outcome)
    return path["id"]


def _adherent_path_steps(scope):
    """Context first, then the governed query -- the pattern's own order."""
    return [
        {
            "step_kind": "knowledge_read",
            "outcome": "succeeded",
            "owner_workspace": "context-hub",
            "owner_object_type": "knowledge-note",
            "owner_object_id": _NOTE_ID,
            "tool_name": "search_context",
        },
        {
            "step_kind": "semantic_query",
            "outcome": "succeeded",
            "owner_workspace": "governance",
            "owner_object_type": "semantic-view",
            "owner_object_id": scope["semantic_view_version_id"],
            "tool_name": "execute_analyze_query_spec",
        },
    ]


def _open_run(scope) -> str:
    from core.evaluation_runs import (
        create_context_version_set,
        create_run_profile,
        open_evaluation_run,
    )

    conn = scope["conn"]
    profile = create_run_profile(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        name=f"executor fixture {ULID()}",
        evidence_mode="offline",
        actor=_ACTOR,
    )
    context_set = create_context_version_set(
        conn,
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
    run = open_evaluation_run(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        run_profile_id=profile["id"],
        semantic_view_id=scope["semantic_view_id"],
        semantic_view_version_id=scope["semantic_view_version_id"],
        context_version_set_id=context_set["id"],
        model_ref="model-example-1",
        host_capability_profile={"host": "example-host"},
        tool_catalog_version=_hex64("catalog"),
        data_snapshot_ref={"seed_set": "executor-fixture"},
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
# The executor: the half of a Regression Run that the word `execute` names.
# ===========================================================================


def test_a_recorded_run_unrolls_its_whole_question_set_in_one_call(scope):
    """The defect this closes, stated as the test: nothing walked a question set.

    Before the executor, `open_evaluation_run` / `add_run_case` /
    `record_case_verdicts` had no production caller but one HTTP request each.
    One call now pins the case, freezes the run and writes six verdicts.
    """
    version_id = _golden_question(scope)
    path_id = _observed_path(scope, _adherent_path_steps(scope))
    run_id = _open_run(scope)

    report = _execute(scope, run_id, subjects={version_id: {"ai_path_id": path_id}})

    assert report["outcome"] == "completed"
    assert report["lifecycle"] == "finalized"
    assert report["question_set_source"] == "active_questions"
    assert report["question_count"] == 1
    assert report["executed_by"] == _ACTOR
    assert report["started_at"] and report["ended_at"]
    assert report["question_set_fingerprint"]

    (case,) = report["cases"]
    assert case["golden_question_version_id"] == version_id
    assert case["ai_path"] == path_id
    assert case["path_quality"] == {
        "verdict": "pass",
        "reason_code": "path_matches_expected_pattern",
    }

    # Six rows or none, and the six are the ratified six.
    with scope["conn"].cursor() as cur:
        cur.execute(
            "SELECT dimension, verdict FROM app.evaluation_case_dimension_verdicts "
            "WHERE case_id = %s ORDER BY dimension",
            (case["case_id"],),
        )
        verdicts = dict(cur.fetchall())
    assert set(verdicts) == {
        "context_adherence",
        "dq_handling",
        "mcp_app_behavior",
        "path_quality",
        "provenance_correctness",
        "semantic_correctness",
    }
    assert verdicts["path_quality"] == "pass"
    # Nothing measured these, so nothing claims them. A `pass` here would be the
    # launder the six-verdict writer exists to prevent.
    assert verdicts["semantic_correctness"] == "unverifiable"
    assert verdicts["mcp_app_behavior"] == "unverifiable"


def test_the_passing_path_verdict_lands_with_the_comparison_the_database_demands(scope):
    """`trg_evaluation_case_verdicts_path_evidence` is the reason this exists.

    A passing `path_quality` is REFUSED unless a comparison row resolves an
    observed path for the case. `evaluate_case_path` computed that comparison and
    nobody stored it, so this assertion is what stands between the executor and a
    CheckViolation the first time a path actually matched.
    """
    version_id = _golden_question(scope)
    path_id = _observed_path(scope, _adherent_path_steps(scope))
    run_id = _open_run(scope)

    report = _execute(scope, run_id, subjects={version_id: {"ai_path_id": path_id}})
    (case,) = report["cases"]

    with scope["conn"].cursor() as cur:
        cur.execute(
            "SELECT observed_ai_path_id, evidence_state, path_verdict, "
            "       expected_pattern_hash, required_missing, forbidden_present "
            "  FROM app.evaluation_path_comparisons WHERE case_id = %s",
            (case["case_id"],),
        )
        row = cur.fetchone()
    assert row is not None, "the comparison behind the verdict was not persisted"
    observed, state, verdict, pattern_hash, missing, forbidden = row
    assert observed == path_id
    assert state == "observed"
    assert verdict == "pass"
    assert len(pattern_hash) == 64
    assert missing == [] and forbidden == []


def test_a_forbidden_tool_that_was_actually_used_fails_the_case(scope):
    """The vacuous pass, closed at the level above the comparator.

    A pattern whose only statement is a forbidden tool used to pass ANY path,
    because `compare()` never read the authoring field. It reads the grammar's
    `forbidden_nodes` now -- and the executor is what finally puts a real
    observed path in front of it.
    """
    version_id = _golden_question(
        scope,
        expected_ai_path=_pattern(scope, forbidden_nodes=[{"tool_name": "raw_sql_passthrough"}]),
    )
    path_id = _observed_path(
        scope,
        [
            *_adherent_path_steps(scope),
            {
                "step_kind": "tool_call",
                "outcome": "succeeded",
                "tool_name": "raw_sql_passthrough",
            },
        ],
    )
    run_id = _open_run(scope)

    report = _execute(scope, run_id, subjects={version_id: {"ai_path_id": path_id}})
    (case,) = report["cases"]
    assert case["path_quality"]["verdict"] == "fail"
    assert case["path_quality"]["reason_code"] == "forbidden_node_observed"

    with scope["conn"].cursor() as cur:
        cur.execute(
            "SELECT path_verdict, forbidden_present FROM app.evaluation_path_comparisons "
            "WHERE case_id = %s",
            (case["case_id"],),
        )
        verdict, forbidden = cur.fetchone()
    assert verdict == "fail"
    assert forbidden, "the finding that produced the failure was not recorded"


def test_a_question_with_no_pinned_subject_is_unverifiable_and_says_what_to_do(scope):
    """An executor that invented a subject so a run could `complete` would
    produce evidence about nothing. The absence is recorded, and it names the
    gesture that fills it."""
    version_id = _golden_question(scope)
    run_id = _open_run(scope)

    report = _execute(scope, run_id)

    (case,) = report["cases"]
    assert case["subject_source"] == "no_pinned_subject"
    assert case["result_id"] is None
    assert case["path_quality"]["verdict"] == "unverifiable"
    assert [p["pin_family"] for p in case["unresolved_pins"]]

    (absent,) = report["subjects_not_pinned"]
    assert absent["golden_question_version_id"] == version_id
    assert "promote" in absent["next_step"]

    with scope["conn"].cursor() as cur:
        cur.execute(
            "SELECT verdict FROM app.evaluation_case_dimension_verdicts WHERE case_id = %s",
            (case["case_id"],),
        )
        assert {row[0] for row in cur.fetchall()} == {"unverifiable"}


def test_the_declared_question_set_is_judged_in_the_order_it_was_named(scope):
    version_id = _golden_question(scope, activate=False)
    path_id = _observed_path(scope, _adherent_path_steps(scope))
    run_id = _open_run(scope)

    report = _execute(
        scope,
        run_id,
        golden_question_version_ids=[version_id],
        subjects={version_id: {"ai_path_id": path_id}},
    )
    assert report["question_set_source"] == "declared"
    # A draft question is judged when it is NAMED, and never swept in by default:
    # the default set is what the Project declared active.
    assert [c["golden_question_version_id"] for c in report["cases"]] == [version_id]


def test_a_run_of_another_project_does_not_resolve(scope):
    """Foreign, denied and absent share one answer. A refusal that told a caller
    the run exists elsewhere would be an enumeration oracle."""
    from core.evaluation_run_executor import execute_evaluation_run
    from core.evaluation_runs import EvaluationNotFound

    _golden_question(scope)
    run_id = _open_run(scope)

    with pytest.raises(EvaluationNotFound):
        execute_evaluation_run(
            scope["conn"],
            org_id=scope["org_id"],
            project_id=scope["other_project_id"],
            run_id=run_id,
            actor=_ACTOR,
        )


def test_a_question_set_that_names_a_foreign_version_is_refused_whole(scope):
    from core.evaluation_runs import EvaluationRefused

    _golden_question(scope)
    run_id = _open_run(scope)

    with pytest.raises(EvaluationRefused) as excinfo:
        _execute(scope, run_id, golden_question_version_ids=[_uid("gqv")])
    assert excinfo.value.code == "unknown_golden_question_version"

    # Nothing was pinned: a refusal mid-walk must not leave a question set that
    # is an accident of where the walk stopped.
    with scope["conn"].cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.evaluation_run_cases WHERE run_id = %s", (run_id,)
        )
        assert cur.fetchone()[0] == 0


def test_a_frozen_run_is_not_unrolled_twice(scope):
    from core.evaluation_runs import EvaluationRefused

    version_id = _golden_question(scope)
    path_id = _observed_path(scope, _adherent_path_steps(scope))
    run_id = _open_run(scope)
    _execute(scope, run_id, subjects={version_id: {"ai_path_id": path_id}})

    with pytest.raises(EvaluationRefused) as excinfo:
        _execute(scope, run_id)
    assert excinfo.value.code == "run_is_finalized"


def test_a_project_with_no_active_question_names_the_gesture_not_a_table(scope):
    from core.evaluation_runs import EvaluationRefused

    _golden_question(scope, activate=False)
    run_id = _open_run(scope)

    with pytest.raises(EvaluationRefused) as excinfo:
        _execute(scope, run_id)
    assert excinfo.value.code == "no_question_to_unroll"
    message = str(excinfo.value)
    assert "Activate a Golden Question" in message
    assert "golden_question" not in message.replace("Golden Question", "")


def test_the_execution_is_attributed_in_the_journal(scope):
    """Evidence rows are immutable and have no column for who unrolled a run, so
    the attribution lands in the journal -- on the same transaction, or not at
    all."""
    version_id = _golden_question(scope)
    run_id = _open_run(scope)
    _execute(scope, run_id, golden_question_version_ids=[version_id])

    with scope["conn"].cursor() as cur:
        cur.execute(
            "SELECT identity, action, connection_ref FROM app.audit_log "
            " WHERE action = 'test.evaluation_run.executed' AND connection_ref = %s",
            (run_id,),
        )
        row = cur.fetchone()
    assert row == (_ACTOR, "test.evaluation_run.executed", run_id)


# ===========================================================================
# The adherence reader: the measure that was written and never read.
# ===========================================================================


def _record_adherence(scope, *, adherent: bool, data_tool: str, session_kind: str, context_tool):
    with scope["conn"].cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.query_adherence
                (id, project_id, session_key, session_kind, data_tool, adherent,
                 context_tool, trace_id, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
            """,
            (
                _uid("adh"),
                scope["project_id"],
                f"trace:{ULID()}",
                session_kind,
                data_tool,
                adherent,
                context_tool,
                None,
            ),
        )


def test_adherence_reads_back_with_its_denominator_beside_every_count(scope):
    from core.adherence import adherence_overview

    _record_adherence(
        scope,
        adherent=True,
        data_tool="get_report",
        session_kind="trace",
        context_tool="search_context",
    )
    _record_adherence(
        scope, adherent=False, data_tool="get_report", session_kind="trace", context_tool=None
    )
    _record_adherence(
        scope,
        adherent=True,
        data_tool="get_card",
        session_kind="time_window",
        context_tool="get_procedure",
    )

    overview = adherence_overview(scope["conn"], project_id=scope["project_id"], days=7)

    assert overview["observations"] == 3
    assert overview["empty_state"] is None
    assert overview["window"]["days"] == 7
    # NO POOLED ADHERENCE FIGURE. `analyze-and-test.md:1330` -- the two bases are
    # "reported apart and never merged", and a top-level `adherent` over a mixed
    # denominator is that merge wearing the friendliest number on the payload.
    for pooled in ("adherent", "not_adherent", "share_adherent"):
        assert pooled not in overview

    by_tool = {(row["data_tool"], row["basis"]): row for row in overview["by_data_tool"]}
    assert by_tool[("get_report", "observed_session")]["observations"] == 2
    assert by_tool[("get_report", "observed_session")]["adherent"] == 1
    # The denominator travels in the same object as the share, always.
    assert by_tool[("get_report", "observed_session")]["share_adherent"] == 0.5
    assert by_tool[("get_card", "inferred_window")]["observations"] == 1

    # A trace-identified session and a wall-clock inference are never merged: one
    # is known to be one exchange, the other is an approximation, and a reader
    # that could not tell them apart would read an inference as a measure.
    by_basis = {row["basis"]: row for row in overview["by_basis"]}
    assert by_basis["observed_session"]["observations"] == 2
    assert by_basis["inferred_window"]["observations"] == 1
    assert "few minutes" in by_basis["inferred_window"]["means"]

    assert {row["context_tool"] for row in overview["context_tools"]} == {
        "search_context",
        "get_procedure",
    }


def test_an_unmeasured_project_says_why_it_is_empty_and_names_the_gesture(scope):
    from core.adherence import adherence_overview

    overview = adherence_overview(scope["conn"], project_id=scope["project_id"])

    assert overview["observations"] == 0
    assert overview["by_basis"] == [] and overview["by_data_tool"] == []
    empty = overview["empty_state"]
    assert empty["headline"] == "Nothing recorded yet."
    # A gesture, not a deployment state and not a table name.
    assert "ask it one question" in empty["next_step"]
    for forbidden in ("query_adherence", "migration", "deploy", "table", "row"):
        assert forbidden not in (empty["headline"] + empty["detail"] + empty["next_step"]).lower()


def test_the_window_is_a_boundary_not_a_suggestion(scope):
    from core.adherence import adherence_overview

    _record_adherence(
        scope,
        adherent=True,
        data_tool="get_daily_report",
        session_kind="trace",
        context_tool="search_context",
    )
    with scope["conn"].cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.query_adherence
                (id, project_id, session_key, session_kind, data_tool, adherent,
                 context_tool, trace_id, created_at)
            VALUES (%s, %s, %s, 'trace', 'get_report', TRUE, 'search_context', NULL,
                    now() - INTERVAL '40 days')
            """,
            (_uid("adh"), scope["project_id"], f"trace:{ULID()}"),
        )

    recent = adherence_overview(scope["conn"], project_id=scope["project_id"], days=7)
    assert recent["observations"] == 1
    wider = adherence_overview(scope["conn"], project_id=scope["project_id"], days=90)
    assert wider["observations"] == 2


def test_adherence_of_another_project_is_not_pooled_in(scope):
    from core.adherence import adherence_overview

    _record_adherence(
        scope,
        adherent=True,
        data_tool="get_report",
        session_kind="trace",
        context_tool="search_context",
    )
    overview = adherence_overview(scope["conn"], project_id=scope["other_project_id"])
    assert overview["observations"] == 0
    assert overview["empty_state"] is not None
