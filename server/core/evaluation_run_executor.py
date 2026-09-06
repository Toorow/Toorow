"""The half of a Regression Run that the word *execute* names.

`analyze-and-test.md:239` says a Regression Run "executes or observes a
version-pinned cohort". Only the *observe* half existed: `open_evaluation_run`,
`add_run_case` and `record_case_verdicts` had no production caller but the HTTP
API, so a person had to unroll a question set by hand, one request per question,
and no run was ever unrolled at all. The evidence model was a tape recorder with
nobody playing.

WHAT THIS MODULE IS. The loop, and only the loop. It takes a run that is already
`recording` with its environment pinned, resolves the set of Golden Question
versions it judges, resolves the pinned subject of each one, and walks them
through the EXISTING writers. It contains no comparison of its own: the
expected-versus-observed judgement is `expected_ai_path`, reached exactly where
it already lives, inside `record_case_verdicts`. A second comparator here is how
two dialects of one object start, and this repository has just finished paying
for one (commit 2cb09041).

WHAT IT REFUSES TO BE.

  * It is not a dimension evaluator. `semantic_correctness`,
    `provenance_correctness` and `dq_handling` stay `unverifiable` naming their
    owner. Filling them from here would be inventing measurement, which is the
    single thing the six-verdict writer exists to prevent.
  * It is not a subject inventor. When nothing pins a Result for a question, the
    case is written with its absence recorded and its dimensions unverifiable.
    An executor that fabricated a subject so a run "completed" would produce
    evidence about nothing.
  * It is not a second lifecycle. The database allows `recording -> finalized`
    and nothing else (migration 153). *Running* is `recording`, *completed* is
    `finalized`. There is no `failed` row, because a run that could not be
    unrolled has written nothing: the transaction is rolled back by the caller
    and the run stays `recording`, executable again once the refusal is fixed.

ATOMIC OR NOTHING. Every write below happens on the caller's transaction. A
refusal on the seventh question leaves the first six unwritten, so a run never
carries a question set that is a partial accident of where the walk stopped --
its `question_set_fingerprint` would then describe a set nobody chose.

ATTRIBUTION LIVES IN THE JOURNAL. `app.evaluation_runs` records who OPENED a run
and its evidence rows are immutable; there is no column for who unrolled it and
inventing one would mean altering frozen evidence. The executing identity, the
run, the question count and the outcome are written as one audit action on the
same transaction, so the execution and its attribution commit together or not at
all.
"""

from __future__ import annotations

from typing import Any

from core.ai_paths import NO_AI_PATH
from core.audit import declare_action, insert_audit_row
from core.evaluation_runs import (
    EvaluationNotFound,
    EvaluationRefused,
    Refusal,
    add_run_case,
    finalize_evaluation_run,
    record_case_verdicts,
    run_overview,
)
from core.expected_ai_path import REASON_PATH_NOT_FINALIZED, VERDICT_UNVERIFIABLE

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42: declared beside the code that writes it, never retyped at the call
# site, so an action cannot be told apart from a typo only by eye.
ACTION_TEST_EVALUATION_RUN_EXECUTED = declare_action("test.evaluation_run.executed")

#: Where the question set came from. It travels in the payload because "every
#: active question" and "these exact versions" are two different claims about
#: what a run judged, and a reader cannot recover which one from the cases.
QUESTION_SET_DECLARED = "declared"
QUESTION_SET_ACTIVE_QUESTIONS = "active_questions"

#: How the subject of one case was resolved. Same reason: a subject the caller
#: pinned and a subject found by promotion are not equally deliberate.
SUBJECT_DECLARED = "declared"
SUBJECT_PROMOTED_REGRESSION_CASE = "promoted_regression_case"
SUBJECT_ABSENT = "no_pinned_subject"

#: The state of the observed AI Path behind a subject, kept at three values for
#: the same reason `add_run_case` keeps three: "no path was taken" and "the path
#: is not readable as evidence" are different statements about an execution.
PATH_FINALIZED = "finalized"
PATH_NOT_FINALIZED = "not_finalized"
PATH_DECLARED_ABSENT = "declared_absent"
PATH_MISSING = "missing"

OUTCOME_COMPLETED = "completed"


def _refuse(code: str, message: str, subject: str | None = None) -> None:
    raise EvaluationRefused(code, message, [Refusal(code, message, subject)])


# ---------------------------------------------------------------------------
# The question set.
# ---------------------------------------------------------------------------


def resolve_question_set(
    conn,
    *,
    org_id: str,
    project_id: str,
    golden_question_version_ids: list[str] | None = None,
) -> tuple[list[str], str]:
    """Return `(version_ids, source)` -- the exact versions this run will judge.

    Declared ids are honoured in the caller's order and each is checked against
    this Project. Absent a declaration the set is the current version of every
    `active` Golden Question: a DERIVED set, never an invented one, and the exact
    version ids it resolved to are what the cases pin, so the run's evidence
    stays reproducible even after a question mints a new version.
    """
    if golden_question_version_ids:
        wanted: list[str] = []
        for index, raw in enumerate(golden_question_version_ids):
            subject = f"golden_question_version_ids[{index}]"
            if not isinstance(raw, str) or not raw.strip():
                _refuse("missing_field", "a Golden Question version id is required", subject)
            version_id = raw.strip()
            if version_id in wanted:
                _refuse(
                    "duplicate_question",
                    f"`{version_id}` is declared twice: one question is one case, and "
                    "two cases over one question would double its weight in the counts",
                    subject,
                )
            wanted.append(version_id)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM app.golden_question_versions
                 WHERE id = ANY(%s) AND org_id = %s AND project_id = %s
                """,
                (wanted, org_id, project_id),
            )
            found = {str(row[0]) for row in cur.fetchall()}
        missing = [v for v in wanted if v not in found]
        if missing:
            # Foreign, denied and absent read identically, exactly as they do
            # everywhere else in this workspace.
            _refuse(
                "unknown_golden_question_version",
                f"{', '.join(missing)} is not a Golden Question version of this Project",
                "golden_question_version_ids",
            )
        return wanted, QUESTION_SET_DECLARED

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT q.current_version_id
              FROM app.golden_questions q
             WHERE q.org_id = %s AND q.project_id = %s
               AND q.lifecycle = 'active'
               AND q.current_version_id IS NOT NULL
             ORDER BY q.created_at, q.id
            """,
            (org_id, project_id),
        )
        rows = [str(row[0]) for row in cur.fetchall()]
    if not rows:
        # The refusal names the gesture that fills the set, not the state of a
        # table: a caller who reads "no rows" learns nothing it can act on.
        _refuse(
            "no_question_to_unroll",
            "this Project has no active Golden Question to unroll. Activate a "
            "Golden Question, or name the exact versions this run should judge.",
            "golden_question_version_ids",
        )
    return rows, QUESTION_SET_ACTIVE_QUESTIONS


# ---------------------------------------------------------------------------
# The subject of one case: which Result, and which observed path behind it.
# ---------------------------------------------------------------------------


def _path_state(
    conn,
    *,
    org_id: str,
    project_id: str,
    ai_path_id: str | None,
    absent_literal: str | None,
) -> tuple[str | None, str]:
    """`(pinnable_path_id, state)` for one pinned execution.

    A path that is still `recording` is NOT pinned. `ai_path_reference` refuses
    one, and rightly: a case pinned to a growing path would describe evidence
    that changes after its verdict was written. The reason travels as its own
    state rather than collapsing into "missing", which would blame
    instrumentation for a run that is simply still in flight.
    """
    if not ai_path_id:
        return None, PATH_DECLARED_ABSENT if absent_literal == NO_AI_PATH else PATH_MISSING
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT lifecycle FROM app.ai_paths
             WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (ai_path_id, org_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return None, PATH_MISSING
    if row[0] != PATH_FINALIZED:
        return None, PATH_NOT_FINALIZED
    return ai_path_id, PATH_FINALIZED


def _result_evidence(
    conn, *, org_id: str, project_id: str, result_id: str
) -> dict[str, Any] | None:
    """The observed path a Result already carries, Project-scoped.

    Story 50.1 AC7 stores the path reference ON the Result, so the executor never
    has to guess which execution answered a question: the Result says it. Reading
    it here rather than accepting it from a request is what stops a case from
    pinning a path that belongs to a different execution.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ai_path_id, ai_path_absent_literal
              FROM app.query_results
             WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (result_id, org_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    pinnable, state = _path_state(
        conn, org_id=org_id, project_id=project_id, ai_path_id=row[0], absent_literal=row[1]
    )
    return {
        "result_id": result_id,
        "ai_path_id": pinnable,
        "unpinned_ai_path_id": row[0] if pinnable is None else None,
        "path_state": state,
        "capability_key": None,
    }


def resolve_subject(
    conn,
    *,
    org_id: str,
    project_id: str,
    golden_question_version_id: str,
    declared: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """What this run pins for one question, and how it was found.

    Two sources, in this order, and no third:

      1. the caller's declaration -- a deliberate replay of an exact Result;
      2. the Result promoted for this question version into regression evidence
         (`app.feedback_regression_cases`), which is the only link in this
         repository between a governed question and an execution that answered
         it -- and the only subject the trusted result-case evaluator will judge.

    Neither is a fallback for the other: a declared Result that does not resolve
    is a refusal, not a reason to go looking for a different one. Silently
    judging a subject the caller did not ask for is worse than refusing.

    The promoted branch carries the promotion's OWN `capability_key` and pinned
    path rather than re-deriving them. The trusted evaluator joins the case to
    the promotion on the classification axes, so a case that classified itself
    differently would simply never be evaluated -- and would look, from the
    outside, like a promotion that was never made.
    """
    if declared:
        result_id = declared.get("result_id")
        if result_id:
            evidence = _result_evidence(
                conn, org_id=org_id, project_id=project_id, result_id=str(result_id)
            )
            if evidence is None:
                raise EvaluationNotFound("result not found in this Project")
            return {**evidence, "source": SUBJECT_DECLARED}
        declared_path = declared.get("ai_path_id")
        if declared_path:
            # A path with no Result. This is what an observed cohort leaves
            # behind, and replaying one offline is the half of the loop that had
            # no caller at all: a cohort could propose a Golden Question, and
            # nothing could then judge the cohort's own paths against it.
            pinnable, state = _path_state(
                conn,
                org_id=org_id,
                project_id=project_id,
                ai_path_id=str(declared_path),
                absent_literal=None,
            )
            if state == PATH_MISSING:
                raise EvaluationNotFound("AI Path not found in this Project")
            return {
                "result_id": None,
                "ai_path_id": pinnable,
                "unpinned_ai_path_id": str(declared_path) if pinnable is None else None,
                "path_state": state,
                "capability_key": declared.get("capability_key"),
                "source": SUBJECT_DECLARED,
            }
        if declared.get("ai_path_expected") is False:
            return {
                "result_id": None,
                "ai_path_id": None,
                "unpinned_ai_path_id": None,
                "path_state": PATH_DECLARED_ABSENT,
                "capability_key": None,
                "source": SUBJECT_DECLARED,
            }

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT result_id, capability_key, ai_path_id, ai_path_absent_literal
              FROM app.feedback_regression_cases
             WHERE golden_question_version_id = %s AND org_id = %s AND project_id = %s
             ORDER BY id DESC
             LIMIT 1
            """,
            (golden_question_version_id, org_id, project_id),
        )
        row = cur.fetchone()
    if row is not None:
        pinnable, state = _path_state(
            conn, org_id=org_id, project_id=project_id, ai_path_id=row[2], absent_literal=row[3]
        )
        return {
            "result_id": row[0],
            "ai_path_id": pinnable,
            "unpinned_ai_path_id": row[2] if pinnable is None else None,
            "path_state": state,
            "capability_key": row[1],
            "source": SUBJECT_PROMOTED_REGRESSION_CASE,
        }

    return {
        "result_id": None,
        "ai_path_id": None,
        "unpinned_ai_path_id": None,
        "path_state": PATH_MISSING,
        "capability_key": None,
        "source": SUBJECT_ABSENT,
    }


def _contract_versions(
    conn, *, org_id: str, project_id: str, version_ids: list[str]
) -> dict[str, str]:
    """Which contract each question version speaks.

    Only `golden-question.v2` carries the typed assertion array the trusted
    result-case evaluator reads, so this decides which of the two writers may
    judge a case -- rather than discovering it as a refusal mid-walk.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, contract_version FROM app.golden_question_versions
             WHERE id = ANY(%s) AND org_id = %s AND project_id = %s
            """,
            (version_ids, org_id, project_id),
        )
        return {str(row[0]): str(row[1]) for row in cur.fetchall()}


def _supplied_verdicts(subject: dict[str, Any]) -> dict[str, dict[str, Any]] | None:
    """The one verdict the mechanical pass cannot state precisely.

    A path that exists and is not finalized reads, mechanically, as no path at
    all -- `path_evidence_missing`, owner: instrumentation. That sends a reader
    to the wrong person. `expected_ai_path` already has the exact reason code for
    this state, so it is supplied through the ordinary `verdicts` argument rather
    than by teaching the writer a new state.
    """
    if subject.get("path_state") != PATH_NOT_FINALIZED:
        return None
    return {
        "path_quality": {
            "verdict": VERDICT_UNVERIFIABLE,
            "reason_code": REASON_PATH_NOT_FINALIZED,
            "evidence_refs": {
                "observed_ai_path_id": subject.get("ai_path_id"),
                "detail": "the observed AI Path of this Result is still recording, so it "
                "is not referenceable as evidence",
            },
        }
    }


# ---------------------------------------------------------------------------
# Judging one case: which writer, and why that one.
# ---------------------------------------------------------------------------

#: The two producers migration 252 lets a verdict carry. They are not
#: interchangeable: one measured assertions against a Result, the other stated
#: what follows mechanically from the pins.
EVALUATOR_RESULT_CASE = "result-case-evaluator.v1"
EVALUATOR_MECHANICAL = "caller-declared.v1"

_EVALUATION_REQUEST = "evaluation-request.v1"


def _judge_case(
    conn,
    *,
    org_id: str,
    project_id: str,
    actor: str,
    case: dict[str, Any],
    subject: dict[str, Any],
    contract_version: str,
    run_is_offline: bool,
) -> dict[str, Any]:
    """Write the six verdicts of one case through the writer that can judge it.

    TWO WRITERS EXIST, AND THE CHOICE IS NOT A PREFERENCE.

      * `evaluate_feedback_regression_case` measures the typed assertions and the
        required provenance of a promoted Result. It is the only thing in this
        repository that produces `semantic_correctness` and
        `provenance_correctness` from evidence rather than from an absence. It
        needs all of: a promotion, a `golden-question.v2` contract, and a
        finalized offline case.
      * `record_case_verdicts` states what follows mechanically from the pins,
        and runs the expected-versus-observed path comparison.

    Choosing the mechanical writer where the trusted one applies would be
    actively destructive, not merely weaker: dimension verdicts are immutable, so
    a `caller-declared` row makes the trusted evaluator refuse the case for the
    rest of its life (`case_already_evaluated`).
    """
    from core.feedback_regression import (  # noqa: PLC0415 -- kept off the import graph's hot path
        FeedbackRegressionNotFound,
        FeedbackRegressionRefused,
        evaluate_feedback_regression_case,
    )

    declined: dict[str, Any] | None = None
    trusted = (
        subject["source"] == SUBJECT_PROMOTED_REGRESSION_CASE
        and bool(subject["result_id"])
        and contract_version == "golden-question.v2"
        and run_is_offline
    )
    if trusted:
        try:
            receipt = evaluate_feedback_regression_case(
                conn,
                org_id=org_id,
                project_id=project_id,
                case_id=case["id"],
                actor=actor,
                payload={
                    "schema_version": _EVALUATION_REQUEST,
                    # One case is judged once per execution. The case id is
                    # minted by this run, so the key cannot collide with a
                    # hand-made evaluation of an earlier run's case.
                    "retry_key": f"run-execution:{case['id']}",
                },
            )
            verdicts = receipt.get("verdicts") or {}
            return {
                "case_id": case["id"],
                "subject_source": subject["source"],
                "result_id": subject["result_id"],
                "ai_path": case["ai_path"],
                "path_state": subject["path_state"],
                "evaluator": EVALUATOR_RESULT_CASE,
                "path_quality": {
                    "verdict": (verdicts.get("path_quality") or {}).get("verdict"),
                    "reason_code": (verdicts.get("path_quality") or {}).get("reason_code"),
                },
                "assertion_results": len(receipt.get("assertion_results") or []),
                "evaluator_declined": None,
                "unresolved_pins": case["unresolved_pins"],
            }
        except (FeedbackRegressionRefused, FeedbackRegressionNotFound) as exc:
            # The trusted evaluator declined this subject. Falling through to the
            # mechanical writer is honest -- the case still gets its six
            # verdicts, none of them invented -- but WHY it declined has to
            # travel, or the run reads as if no promotion existed.
            declined = (
                exc.as_dict()
                if isinstance(exc, FeedbackRegressionRefused)
                else {"code": "not_evaluable", "message": str(exc)}
            )

    verdicts_written = record_case_verdicts(
        conn,
        org_id=org_id,
        project_id=project_id,
        case_id=case["id"],
        verdicts=_supplied_verdicts(subject),
    )
    path_verdict = next((v for v in verdicts_written if v["dimension"] == "path_quality"), {})
    return {
        "case_id": case["id"],
        "subject_source": subject["source"],
        "result_id": subject["result_id"],
        "ai_path": case["ai_path"],
        "path_state": subject["path_state"],
        "evaluator": EVALUATOR_MECHANICAL,
        "path_quality": {
            "verdict": path_verdict.get("verdict"),
            "reason_code": path_verdict.get("reason_code"),
        },
        "assertion_results": 0,
        "evaluator_declined": declined,
        "unresolved_pins": case["unresolved_pins"],
    }


# ---------------------------------------------------------------------------
# The walk.
# ---------------------------------------------------------------------------


def execute_evaluation_run(
    conn,
    *,
    org_id: str,
    project_id: str,
    run_id: str,
    actor: str,
    golden_question_version_ids: list[str] | None = None,
    subjects: dict[str, dict[str, Any]] | None = None,
    finalize: bool = True,
) -> dict[str, Any]:
    """Unroll one recording run: one case and six verdicts per question.

    Returns the execution report -- what was judged, what could not be, and the
    per-dimension verdict counts of the run. There is no pass rate in it and no
    aggregate score: `analyze-and-test.md:336-337` forbids the figure that pays
    for a correctness regression with better feedback, and a run report is
    exactly where such a figure would first appear.
    """
    overview = run_overview(conn, org_id=org_id, project_id=project_id, run_id=run_id)
    if overview["lifecycle"] != "recording":
        _refuse(
            "run_is_finalized",
            "this Evaluation Run is frozen evidence and cannot be unrolled again. "
            "Open a new run to judge the question set once more.",
            run_id,
        )
    if overview["case_count"]:
        # Half a run unrolled by hand and half by the executor would produce one
        # question set nobody chose, and its fingerprint would describe the
        # accident rather than a decision.
        _refuse(
            "run_already_has_cases",
            "this Evaluation Run already pins cases, so unrolling it would judge a "
            "question set that is part hand-written and part derived. Open a new run.",
            run_id,
        )

    version_ids, question_set_source = resolve_question_set(
        conn,
        org_id=org_id,
        project_id=project_id,
        golden_question_version_ids=golden_question_version_ids,
    )
    declared_subjects = subjects or {}
    contracts = _contract_versions(
        conn, org_id=org_id, project_id=project_id, version_ids=version_ids
    )

    # PASS 1 -- pin every subject. The question set has to be complete before the
    # run freezes, because `question_set_fingerprint` is computed from the cases:
    # judging as we go and freezing afterwards would let a mid-walk refusal
    # define the set.
    pinned: list[dict[str, Any]] = []
    for version_id in version_ids:
        subject = resolve_subject(
            conn,
            org_id=org_id,
            project_id=project_id,
            golden_question_version_id=version_id,
            declared=declared_subjects.get(version_id),
        )
        case = add_run_case(
            conn,
            org_id=org_id,
            project_id=project_id,
            run_id=run_id,
            golden_question_version_id=version_id,
            result_id=subject["result_id"],
            ai_path_id=subject["ai_path_id"],
            # Only a promoted pin that says so declares that no AI was involved.
            # Any other absence is an absence, and `add_run_case` records it as
            # an unresolved pin rather than as a claim.
            ai_path_expected=subject["path_state"] != PATH_DECLARED_ABSENT,
            capability_key=subject["capability_key"],
        )
        pinned.append({"version_id": version_id, "case": case, "subject": subject})

    # The trusted evaluator judges only a FINALIZED offline case, so the freeze
    # happens between pinning and judging rather than at the end.
    frozen = (
        finalize_evaluation_run(conn, org_id=org_id, project_id=project_id, run_id=run_id)
        if finalize
        else None
    )

    # PASS 2 -- judge. Which writer judges a case is decided by what the case
    # actually has, never by preference.
    executed: list[dict[str, Any]] = []
    for entry in pinned:
        report = _judge_case(
            conn,
            org_id=org_id,
            project_id=project_id,
            actor=actor,
            case=entry["case"],
            subject=entry["subject"],
            contract_version=contracts.get(entry["version_id"], ""),
            run_is_offline=overview["evidence_mode"] == "offline" and finalize,
        )
        executed.append({"golden_question_version_id": entry["version_id"], **report})

    final = run_overview(conn, org_id=org_id, project_id=project_id, run_id=run_id)

    report = {
        "run_id": run_id,
        "outcome": OUTCOME_COMPLETED,
        "lifecycle": final["lifecycle"],
        "started_at": final["started_at"],
        "ended_at": final["ended_at"],
        "executed_by": actor,
        "question_set_source": question_set_source,
        "question_set_fingerprint": final["question_set_fingerprint"],
        "question_count": len(version_ids),
        "cases": executed,
        # Per dimension, per verdict, with every verdict value listed so the
        # denominator travels with the counts. No ratio, here or anywhere.
        "verdict_counts": final["verdict_counts"],
        "unresolved_pins": (frozen or final)["unresolved_pins"],
        "subjects_not_pinned": [
            {
                "golden_question_version_id": entry["golden_question_version_id"],
                "reason_code": "no_pinned_subject",
                "next_step": "promote a reviewed Result for this question, or name the "
                "Result this run should judge",
            }
            for entry in executed
            if entry["subject_source"] == SUBJECT_ABSENT
        ],
    }

    insert_audit_row(
        conn,
        identity=actor,
        action=ACTION_TEST_EVALUATION_RUN_EXECUTED,
        provider_account=project_id,
        connection_ref=run_id,
        metadata={
            "question_set_source": question_set_source,
            "question_count": len(version_ids),
            "lifecycle": final["lifecycle"],
            "subjects_not_pinned": len(report["subjects_not_pinned"]),
        },
    )
    return report
