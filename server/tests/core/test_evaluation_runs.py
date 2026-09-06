"""Stories 51.2 / 51.3 -- proofs for the Evaluation Run, its pins and its verdicts.

WHAT THIS FILE CAN AND CANNOT PROVE. Constraints, triggers and RLS live in
migration `153_product_evaluation_evidence.sql` and only live PostgreSQL proves
them; the pg-gated suite is a separate file and a separate run. What is proved
HERE is everything that is a decision of `core.evaluation_runs`: the refusals it
raises before the database ever sees a row, the honest absences it records, the
comparison arithmetic, the regression rule, the gate decision, and the fact that
the routes exist and are ordered so a literal segment can never be read as an id.

The route tests are not mocked routing: they build a real Starlette application
from the exported `evaluation_run_routes` constant and drive it with a real
client, so a missing route, a wrong method or a shadowed literal fails here
rather than at the first click. Only the authorization seam and the database
connection are substituted -- everything between the URL and the module is real.

Fixture identities are `proj_EXAMPLE`-style throughout: no production identifier
and no real domain appears in this repository.
"""

from __future__ import annotations

import datetime as _dt
import io
import pathlib
import re
import tokenize
from contextlib import contextmanager

import pytest
from core import evaluation_runs as ev
from core.evaluation_runs import (
    DIMENSIONS,
    EvaluationNotFound,
    EvaluationRefused,
    add_run_case,
    approve_baseline,
    compare_pin_families,
    create_comparison,
    create_context_version_set,
    emit_gate_decision,
    finalize_evaluation_run,
    is_exact_version_pin,
    mechanical_case_verdicts,
    open_evaluation_run,
    record_case_verdicts,
    regressions_between,
    verdict_counts,
)
from core.evaluation_runs_api import evaluation_run_routes
from starlette.applications import Starlette
from starlette.testclient import TestClient

ORG = "org_EXAMPLE"
PROJECT = "proj_EXAMPLE"
PROFILE = "erp_00000000000000000000000001"
RUN = "erun_00000000000000000000000001"
OTHER_RUN = "erun_00000000000000000000000002"
CASE = "ecase_00000000000000000000000001"
QUESTION = "gqv_00000000000000000000000001"
CONTEXT_SET = "ecvs_00000000000000000000000001"
COMPARISON = "ecmp_00000000000000000000000001"
VIEW, VIEW_VERSION = "sv_example", "svv_example_1"
CATALOG_VERSION = "a" * 64
PATH = "aip_00000000000000000000000001"


# ---------------------------------------------------------------------------
# A connection that answers by statement, and records every statement it saw.
#
# Keying on the longest matching fragment (rather than on call order) keeps a
# test readable when the module legitimately reorders its reads. Each fragment
# holds a QUEUE, so a function that reads the same table twice -- a comparison
# reads cases for the baseline and then for the candidate -- gets two different
# answers without the test having to count statements.
# ---------------------------------------------------------------------------


class _Cursor:
    def __init__(self, conn: "_Conn"):
        self._conn = conn
        self._rows: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(str(sql).split())
        self._conn.executed.append((normalized, params))
        self._rows = self._conn.answer(normalized)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _Conn:
    def __init__(self, **answers):
        # `answers` keys use `__` for `.` and ` ` so they stay valid keywords.
        self._answers = {
            key.replace("__", " ").replace("app ", "app."): list(value)
            for key, value in answers.items()
        }
        self.executed: list[tuple[str, object]] = []
        self.commits = 0

    def answer(self, sql: str) -> list[tuple]:
        matches = [k for k in self._answers if k in sql]
        if not matches:
            return []
        key = max(matches, key=len)
        queue = self._answers[key]
        if not queue:
            return []
        return queue.pop(0) if len(queue) > 1 else list(queue[0])

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def statements(self, fragment: str) -> list[str]:
        return [sql for sql, _ in self.executed if fragment in sql]


def _run_row(**overrides):
    values = {
        "id": RUN,
        "run_profile_id": PROFILE,
        "lifecycle": "finalized",
        "evidence_mode": "offline",
        "question_set_fingerprint": "b" * 64,
        "semantic_view_id": VIEW,
        "semantic_view_version_id": VIEW_VERSION,
        "context_version_set_id": CONTEXT_SET,
        "model_ref": "model-example-1",
        "host_capability_profile": {"host": "console"},
        "tool_catalog_version": CATALOG_VERSION,
        "data_snapshot_ref": {"seed_set": "example"},
        "data_snapshot_hash": "c" * 64,
        "as_of": _dt.date(2026, 7, 30),
        "render_runtime_version": None,
        "observed_cohort_id": None,
        "unresolved_pins": [ev.render_unresolved_pin()],
        "pin_fingerprints": {family: family * 4 for family in ev.FINGERPRINTED_PIN_FAMILIES},
        "started_at": _dt.datetime(2026, 7, 30, 8, 0),
        "ended_at": _dt.datetime(2026, 7, 30, 9, 0),
        "created_by": "tester",
        "content_hash": "d" * 64,
    }
    values.update(overrides)
    return tuple(values[column] for column in ev._RUN_COLUMNS)


def _case_row(
    case_id=CASE,
    question=QUESTION,
    *,
    result_id="qr_example",
    path=None,
    literal=None,
    feedback_id=None,
    regression_case_id=None,
):
    return (
        case_id,
        question,
        result_id,
        path,
        literal,
        "bd_example",
        3,
        "revenue.reporting",
        "scalar",
        [ev.render_unresolved_pin()],
        None,
        feedback_id,
        regression_case_id,
    )


def _verdict_rows(case_id=CASE, verdict="unverifiable"):
    return [
        (case_id, f"edv_{index}", dimension, verdict, "reason_code_example", {})
        for index, dimension in enumerate(DIMENSIONS)
    ]


# ---------------------------------------------------------------------------
# AC3 / the `latest` refusal -- in the service, and mirrored from the database.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "candidate",
    ["latest", "Latest", "LATEST", "current", "HEAD", "", "  ", None, 7],
)
def test_latest_current_and_head_are_never_version_pins(candidate):
    assert is_exact_version_pin(candidate) is False


def test_an_exact_version_is_a_pin():
    assert is_exact_version_pin("svv_example_1") is True
    # A version that merely CONTAINS the word is not the unqualified keyword.
    assert is_exact_version_pin("latest_release_2026_07") is True


@pytest.mark.parametrize("forbidden", ["latest", "CURRENT", "head", ""])
def test_a_context_version_set_refuses_an_unqualified_latest(forbidden):
    conn = _Conn()
    with pytest.raises(EvaluationRefused) as excinfo:
        create_context_version_set(
            conn,
            org_id=ORG,
            project_id=PROJECT,
            entries=[
                {
                    "owner_workspace": "context-hub",
                    "owner_object_type": "knowledge-entry",
                    "owner_object_id": "ke_example",
                    "owner_version_id": forbidden,
                }
            ],
        )
    assert excinfo.value.code == "unpinned_version"
    # Nothing was written: the refusal happens before the first statement, so a
    # half-built Context Version Set cannot survive a failed request.
    assert conn.executed == []


def test_a_context_version_set_refuses_the_same_object_pinned_twice():
    entry = {
        "owner_workspace": "context-hub",
        "owner_object_type": "skill",
        "owner_object_id": "sk_example",
        "owner_version_id": "skv_1",
    }
    with pytest.raises(EvaluationRefused) as excinfo:
        create_context_version_set(
            _Conn(), org_id=ORG, project_id=PROJECT, entries=[entry, dict(entry)]
        )
    assert excinfo.value.code == "duplicate_entry"


def test_a_run_refuses_an_unpinned_model_reference():
    conn = _Conn(**{"FROM__app__evaluation_run_profiles": [[("offline",)]]})
    with pytest.raises(EvaluationRefused) as excinfo:
        _open(conn, model_ref="latest")
    assert excinfo.value.code == "unpinned_version"


def _open(conn, **overrides):
    kwargs = {
        "org_id": ORG,
        "project_id": PROJECT,
        "run_profile_id": PROFILE,
        "semantic_view_id": VIEW,
        "semantic_view_version_id": VIEW_VERSION,
        "context_version_set_id": CONTEXT_SET,
        "model_ref": "model-example-1",
        "host_capability_profile": {},
        "tool_catalog_version": CATALOG_VERSION,
        "data_snapshot_ref": {"seed_set": "example"},
        "as_of": "2026-07-30",
        "actor": "tester",
    }
    kwargs.update(overrides)
    return open_evaluation_run(conn, **kwargs)


# ---------------------------------------------------------------------------
# AC1 / AC2 -- opening, pinning, and the honest absence.
# ---------------------------------------------------------------------------


def test_opening_a_run_records_the_render_pin_as_unresolved_with_its_owner():
    conn = _Conn(
        **{
            "FROM__app__evaluation_run_profiles": [[("offline",)]],
            "INSERT__INTO__app__evaluation_runs": [[(RUN, "recording", "offline", None)]],
        }
    )
    opened = _open(conn)
    assert opened["lifecycle"] == "recording"
    (absence,) = opened["unresolved_pins"]
    assert absence["pin_family"] == "render"
    # Migration 166: the pin became fillable once Stories 50.4/50.5 delivered
    # `app.renders` and `app.renderer_runtime_builds`. The reason stopped being
    # "the owner has not delivered" -- no longer true about us -- and became a
    # statement about THIS subject.
    assert absence["reason_code"] == "render_not_pinned_for_this_subject"
    # The owner is still NAMED. A disposition with no owner is what had Story 49.6
    # rejected, and no reachable code path here can produce one.
    assert absence["owner"].strip()


def test_as_of_is_a_date_and_a_timestamp_is_refused():
    conn = _Conn(**{"FROM__app__evaluation_run_profiles": [[("offline",)]]})
    with pytest.raises(EvaluationRefused) as excinfo:
        _open(conn, as_of=_dt.datetime(2026, 7, 30, 13, 45))
    assert excinfo.value.code == "invalid_grain"


def test_a_run_inherits_its_profile_evidence_mode_and_never_the_request():
    conn = _Conn(
        **{
            "FROM__app__evaluation_run_profiles": [[("observed_cohort",)]],
            "INSERT__INTO__app__evaluation_runs": [[(RUN, "recording", "observed_cohort", None)]],
        }
    )
    opened = _open(conn, observed_cohort_id="ocoh_example")
    assert opened["evidence_mode"] == "observed_cohort"


def test_an_observed_cohort_run_without_a_cohort_is_refused():
    conn = _Conn(**{"FROM__app__evaluation_run_profiles": [[("observed_cohort",)]]})
    with pytest.raises(EvaluationRefused) as excinfo:
        _open(conn)
    assert excinfo.value.code == "missing_cohort"


def test_an_offline_run_may_not_name_a_cohort():
    conn = _Conn(**{"FROM__app__evaluation_run_profiles": [[("offline",)]]})
    with pytest.raises(EvaluationRefused) as excinfo:
        _open(conn, observed_cohort_id="ocoh_example")
    assert excinfo.value.code == "cohort_on_offline_run"


def test_an_empty_data_snapshot_reference_is_refused():
    conn = _Conn(**{"FROM__app__evaluation_run_profiles": [[("offline",)]]})
    with pytest.raises(EvaluationRefused) as excinfo:
        _open(conn, data_snapshot_ref={})
    assert excinfo.value.code == "empty_data_snapshot"


def test_a_foreign_run_profile_is_indistinguishable_from_an_absent_one():
    with pytest.raises(EvaluationNotFound):
        _open(_Conn())


# ---------------------------------------------------------------------------
# AC4 -- the classification axes come from the governed question version.
# ---------------------------------------------------------------------------


def _case_conn(*, tags=("revenue.reporting",), result_type="scalar", **extra):
    answers = {
        "FROM__app__evaluation_runs__WHERE": [[_run_row(lifecycle="recording")]],
        "FROM__app__golden_question_versions": [[("bd_example", 3, result_type, list(tags))]],
        "INSERT__INTO__app__evaluation_run_cases": [[(CASE, _dt.datetime(2026, 7, 30, 8, 5))]],
    }
    answers.update(extra)
    return _Conn(**answers)


def test_a_case_reads_its_business_domain_and_result_type_from_the_question_version():
    conn = _case_conn()
    case = add_run_case(
        conn,
        org_id=ORG,
        project_id=PROJECT,
        run_id=RUN,
        golden_question_version_id=QUESTION,
        result_id="qr_example",
        ai_path_expected=False,
    )
    assert case["business_domain_id"] == "bd_example"
    assert case["business_domain_version_number"] == 3
    assert case["result_type"] == "scalar"
    assert case["capability_key"] == "revenue.reporting"


def test_a_case_freezes_only_a_verified_result_owned_classification_hash():
    from core.feedback_review import canonical_hash

    classification = {
        "schema_version": "evaluation-classification.v1",
        "semantic_view": {"state": "attributed", "id": VIEW, "version_id": VIEW_VERSION},
        "business_domains": {"state": "attributed", "versions": []},
        "skills": {"state": "attributed", "versions": []},
        "capability": {"state": "unavailable", "reason": "capability_not_required"},
        "result_type": {"state": "attributed", "value": "scalar"},
    }
    classification["classification_hash"] = canonical_hash(classification)
    conn = _case_conn(
        FROM__app__query_results__r__JOIN__app__query_result_payloads=[[
            ({"evaluation_classification": classification},)
        ]]
    )
    case = add_run_case(
        conn,
        org_id=ORG,
        project_id=PROJECT,
        run_id=RUN,
        golden_question_version_id=QUESTION,
        result_id="qr_example",
        ai_path_expected=False,
    )
    assert case["result_classification_hash"] == classification["classification_hash"]


def test_a_capability_is_not_a_connector_name():
    # `meta-ads` is a directory under `server/modules`, i.e. a live connector.
    assert "meta-ads" in ev.connector_names()
    conn = _case_conn(tags=("meta-ads", "revenue.reporting"))
    with pytest.raises(EvaluationRefused) as excinfo:
        add_run_case(
            conn,
            org_id=ORG,
            project_id=PROJECT,
            run_id=RUN,
            golden_question_version_id=QUESTION,
            capability_key="meta-ads",
            ai_path_expected=False,
        )
    assert excinfo.value.code == "capability_is_a_connector"


def test_a_capability_the_question_version_does_not_declare_is_refused():
    conn = _case_conn(tags=("revenue.reporting",))
    with pytest.raises(EvaluationRefused) as excinfo:
        add_run_case(
            conn,
            org_id=ORG,
            project_id=PROJECT,
            run_id=RUN,
            golden_question_version_id=QUESTION,
            capability_key="invented.capability",
            ai_path_expected=False,
        )
    assert excinfo.value.code == "capability_not_declared"


def test_a_case_on_a_finalized_run_is_refused():
    conn = _Conn(**{"FROM__app__evaluation_runs__WHERE": [[_run_row(lifecycle="finalized")]]})
    with pytest.raises(EvaluationRefused) as excinfo:
        add_run_case(
            conn,
            org_id=ORG,
            project_id=PROJECT,
            run_id=RUN,
            golden_question_version_id=QUESTION,
            ai_path_expected=False,
        )
    assert excinfo.value.code == "run_is_finalized"


def test_a_missing_path_is_an_unresolved_pin_and_not_the_no_ai_path_literal():
    """The third honest state. `No AI path` asserts that no AI was involved --
    a different claim from "the evidence is missing"."""
    conn = _case_conn()
    case = add_run_case(
        conn,
        org_id=ORG,
        project_id=PROJECT,
        run_id=RUN,
        golden_question_version_id=QUESTION,
        result_id="qr_example",
        ai_path_expected=True,
    )
    assert case["ai_path"] is None
    families = {p["pin_family"] for p in case["unresolved_pins"]}
    assert families == {"render", "ai_path"}
    absence = next(p for p in case["unresolved_pins"] if p["pin_family"] == "ai_path")
    assert absence["reason_code"] == "path_evidence_missing"
    assert "49.6" in absence["owner"]


def test_a_question_that_declares_no_ai_stores_the_exact_literal():
    conn = _case_conn()
    case = add_run_case(
        conn,
        org_id=ORG,
        project_id=PROJECT,
        run_id=RUN,
        golden_question_version_id=QUESTION,
        result_id="qr_example",
        ai_path_expected=False,
    )
    assert case["ai_path"] == "No AI path"


def test_a_missing_result_is_recorded_as_an_unresolved_pin():
    conn = _case_conn()
    case = add_run_case(
        conn,
        org_id=ORG,
        project_id=PROJECT,
        run_id=RUN,
        golden_question_version_id=QUESTION,
        result_id=None,
        ai_path_expected=False,
    )
    assert {p["pin_family"] for p in case["unresolved_pins"]} == {"render", "result"}


# ---------------------------------------------------------------------------
# AC1 -- finalization is complete or it does not happen, and it moves no baseline.
# ---------------------------------------------------------------------------


def _finalize_conn(**extra):
    answers = {
        "FROM__app__evaluation_runs__WHERE": [[_run_row(lifecycle="recording", ended_at=None)]],
        "FROM__app__evaluation_run_cases": [[_case_row(literal="No AI path")]],
        "FROM__app__evaluation_context_version_set_entries": [
            [(0, "context-hub", "skill", "sk_example", "skv_1")]
        ],
        "UPDATE__app__evaluation_runs": [
            [(RUN, "finalized", _dt.datetime(2026, 7, 30, 9, 0), "e" * 64)]
        ],
    }
    answers.update(extra)
    return _Conn(**answers)


def test_finalizing_freezes_the_run_and_derives_its_question_set_fingerprint():
    conn = _finalize_conn()
    frozen = finalize_evaluation_run(conn, org_id=ORG, project_id=PROJECT, run_id=RUN)
    assert frozen["lifecycle"] == "finalized"
    assert re.fullmatch(r"[0-9a-f]{64}", frozen["question_set_fingerprint"])
    assert set(frozen["pin_fingerprints"]) == set(ev.FINGERPRINTED_PIN_FAMILIES)


def test_finalizing_never_creates_moves_or_updates_a_baseline():
    """`analyze-and-test.md:346` -- a baseline is never updated automatically.

    Twenty successive finalizations, and not one statement touches
    `app.evaluation_baselines`. The absence is the point.
    """
    for _ in range(20):
        conn = _finalize_conn()
        finalize_evaluation_run(conn, org_id=ORG, project_id=PROJECT, run_id=RUN)
        assert conn.statements("evaluation_baselines") == []


def test_a_run_with_no_case_cannot_be_finalized():
    conn = _finalize_conn(**{"FROM__app__evaluation_run_cases": [[]]})
    with pytest.raises(EvaluationRefused) as excinfo:
        finalize_evaluation_run(conn, org_id=ORG, project_id=PROJECT, run_id=RUN)
    assert excinfo.value.code == "empty_run"


def test_a_finalized_run_cannot_be_finalized_again():
    conn = _Conn(**{"FROM__app__evaluation_runs__WHERE": [[_run_row(lifecycle="finalized")]]})
    with pytest.raises(EvaluationRefused) as excinfo:
        finalize_evaluation_run(conn, org_id=ORG, project_id=PROJECT, run_id=RUN)
    assert excinfo.value.code == "already_finalized"


def test_finalization_carries_every_case_absence_up_to_the_run():
    conn = _finalize_conn(
        **{"FROM__app__evaluation_run_cases": [[_case_row(result_id=None, literal="No AI path")]]}
    )
    conn._answers["FROM app.evaluation_run_cases"] = [
        [
            (
                CASE,
                QUESTION,
                None,
                None,
                None,
                "bd_example",
                3,
                "revenue.reporting",
                "scalar",
                [
                    ev.render_unresolved_pin(),
                    {
                        "pin_family": "ai_path",
                        "reason_code": "path_evidence_missing",
                        "owner": ev.AI_PATH_INSTRUMENTATION_OWNER,
                        "detail": "no finalized observed path",
                    },
                ],
                None,
            )
        ]
    ]
    frozen = finalize_evaluation_run(conn, org_id=ORG, project_id=PROJECT, run_id=RUN)
    assert {p["pin_family"] for p in frozen["unresolved_pins"]} == {"render", "ai_path"}


# ---------------------------------------------------------------------------
# AC8 / 51.3 AC1-AC2 -- six dimensions, four verdicts, and `Unverifiable`.
# ---------------------------------------------------------------------------


def _verdict_conn(**extra):
    answers = {
        # id, run_id, ai_path_id, ai_path_absent_literal, render_ref,
        # golden_question_version_id -- the sixth arrived with Story 51.3's real
        # path comparator: `record_case_verdicts` now needs the subject in order
        # to load the EXPECTED pattern, not only the observed path.
        "FROM__app__evaluation_run_cases": [[(CASE, RUN, None, None, None, None)]],
        "INSERT__INTO__app__evaluation_case_dimension_verdicts": [[]],
    }
    answers.update(extra)
    return _Conn(**answers)


def test_six_dimensions_are_written_per_case_or_none():
    conn = _verdict_conn()
    written = record_case_verdicts(conn, org_id=ORG, project_id=PROJECT, case_id=CASE)
    assert [row["dimension"] for row in written] == list(DIMENSIONS)
    assert len(conn.statements("INSERT INTO app.evaluation_case_dimension_verdicts")) == 6


def test_a_seventh_dimension_is_refused_and_nothing_is_written():
    conn = _verdict_conn()
    with pytest.raises(EvaluationRefused) as excinfo:
        record_case_verdicts(
            conn,
            org_id=ORG,
            project_id=PROJECT,
            case_id=CASE,
            verdicts={"tone_quality": {"verdict": "pass", "reason_code": "looks_nice"}},
        )
    assert excinfo.value.code == "unknown_dimension"
    assert conn.statements("INSERT INTO app.evaluation_case_dimension_verdicts") == []


def test_the_verdict_vocabulary_is_exactly_four_values():
    conn = _verdict_conn()
    with pytest.raises(EvaluationRefused) as excinfo:
        record_case_verdicts(
            conn,
            org_id=ORG,
            project_id=PROJECT,
            case_id=CASE,
            verdicts={
                "semantic_correctness": {"verdict": "mostly_pass", "reason_code": "close_enough"}
            },
        )
    assert excinfo.value.code == "unknown_value"


def test_mcp_app_behavior_can_never_be_written_pass_while_no_render_is_pinned():
    """The dimension exists, is written for every case, and is `unverifiable`.

    Reporting `pass` here would report on an object the repository does not
    contain: the rendered artifact of Stories 50.4 / 50.5 / 50.7 does not exist
    and the render stack is not installed.
    """
    conn = _verdict_conn()
    with pytest.raises(EvaluationRefused) as excinfo:
        record_case_verdicts(
            conn,
            org_id=ORG,
            project_id=PROJECT,
            case_id=CASE,
            verdicts={
                "mcp_app_behavior": {"verdict": "pass", "reason_code": "widget_rendered_fine"}
            },
        )
    assert excinfo.value.code == "mcp_app_behavior_without_render"
    assert conn.statements("INSERT INTO app.evaluation_case_dimension_verdicts") == []


def test_mcp_app_behavior_is_unverifiable_without_render():
    conn = _verdict_conn()
    written = record_case_verdicts(conn, org_id=ORG, project_id=PROJECT, case_id=CASE)
    entry = next(row for row in written if row["dimension"] == "mcp_app_behavior")
    assert entry["verdict"] == "unverifiable"
    assert entry["reason_code"] == "render_not_pinned_for_this_subject"
    # 50.6 is what this dimension still waits on -- and since migration 166 that is
    # its OWN reason, no longer an accident of a render CHECK.
    assert "50.6" in entry["evidence_refs"]["also_awaiting"]


def test_path_quality_cannot_pass_without_a_resolved_observed_path():
    conn = _verdict_conn()
    with pytest.raises(EvaluationRefused) as excinfo:
        record_case_verdicts(
            conn,
            org_id=ORG,
            project_id=PROJECT,
            case_id=CASE,
            verdicts={"path_quality": {"verdict": "pass", "reason_code": "looked_right"}},
        )
    assert excinfo.value.code == "path_quality_without_evidence"


def test_a_bare_adherence_flag_alone_is_unverifiable():
    """`analyze-and-test.md:284` -- a bare adherence flag without server-owned
    path evidence is `Unverifiable`."""
    verdicts = mechanical_case_verdicts(
        {"ai_path_id": None, "ai_path_absent_literal": None, "render_ref": None}
    )
    assert verdicts["context_adherence"]["verdict"] == "unverifiable"
    assert verdicts["context_adherence"]["reason_code"] == "adherence_evidence_missing"


def test_a_declared_absence_of_ai_is_not_applicable_not_unverifiable():
    verdicts = mechanical_case_verdicts(
        {"ai_path_id": None, "ai_path_absent_literal": "No AI path", "render_ref": None}
    )
    assert verdicts["path_quality"]["verdict"] == "not_applicable"
    assert verdicts["context_adherence"]["verdict"] == "not_applicable"


def test_no_mechanical_verdict_is_ever_pass():
    """Nothing that measures nothing may report green."""
    for case in (
        {"ai_path_id": None, "ai_path_absent_literal": None, "render_ref": None},
        {"ai_path_id": PATH, "ai_path_absent_literal": None, "render_ref": None},
        {"ai_path_id": None, "ai_path_absent_literal": "No AI path", "render_ref": None},
    ):
        verdicts = mechanical_case_verdicts(case)
        assert set(verdicts) == set(DIMENSIONS)
        assert all(entry["verdict"] != "pass" for entry in verdicts.values())


def test_unverifiable_is_not_fail_and_not_applicable_is_not_pass():
    """The read model never maps `unverifiable` onto either neighbour.

    `unverifiable` blames nobody: `fail` would accuse the product of a defect it
    has not been shown to have, and `pass` would launder a gap in evidence.
    """
    counts = verdict_counts(
        {
            "ecase_a": {d: {"verdict": "unverifiable"} for d in DIMENSIONS},
            "ecase_b": {d: {"verdict": "not_applicable"} for d in DIMENSIONS},
        }
    )
    for per_dimension in counts.values():
        assert per_dimension["unverifiable"] == 1
        assert per_dimension["not_applicable"] == 1
        assert per_dimension["fail"] == 0
        assert per_dimension["pass"] == 0
    assert ev._absent_verdict()["verdict"] == "unverifiable"


def test_verdict_counts_state_every_value_and_compute_no_ratio():
    counts = verdict_counts({CASE: {d: {"verdict": "unverifiable"} for d in DIMENSIONS}})
    assert set(counts) == set(DIMENSIONS)
    for per_dimension in counts.values():
        assert set(per_dimension) == set(ev.VERDICTS)
        assert per_dimension["unverifiable"] == 1
    # No total, no rate, no trust score: the six never collapse into one number.
    assert all(isinstance(value, int) for d in counts.values() for value in d.values())


# ---------------------------------------------------------------------------
# AC6 -- a baseline is an approval, and it never advances by itself.
# ---------------------------------------------------------------------------


def _baseline_conn(*, run=None, active=None):
    return _Conn(
        **{
            "FROM__app__evaluation_runs__WHERE": [[run or _run_row()]],
            "FROM__app__evaluation_baselines": [[active] if active else []],
            "INSERT__INTO__app__evaluation_baselines": [
                [("ebl_00000000000000000000000002", _dt.datetime(2026, 7, 30, 10, 0))]
            ],
        }
    )


def test_approving_a_baseline_records_actor_reason_and_compared_versions():
    conn = _baseline_conn()
    approved = approve_baseline(
        conn,
        org_id=ORG,
        project_id=PROJECT,
        run_id=RUN,
        approved_by="owner@example.com",
        approval_reason="reference run for the Q3 semantic candidate",
    )
    assert approved["approved_by"] == "owner@example.com"
    assert approved["compared_versions"]["semantic_view_version_id"] == VIEW_VERSION
    assert approved["superseded_baseline_id"] is None


@pytest.mark.parametrize("field", ["approved_by", "approval_reason"])
def test_a_baseline_without_an_actor_or_a_reason_is_refused(field):
    kwargs = {
        "approved_by": "owner@example.com",
        "approval_reason": "reference run",
        field: "   ",
    }
    with pytest.raises(EvaluationRefused) as excinfo:
        approve_baseline(
            _baseline_conn(), org_id=ORG, project_id=PROJECT, run_id=RUN, **kwargs
        )
    assert excinfo.value.code == "missing_field"


def test_replacing_a_baseline_inserts_a_new_row_and_only_marks_the_old_superseded():
    conn = _baseline_conn(active=("ebl_00000000000000000000000001",))
    approved = approve_baseline(
        conn,
        org_id=ORG,
        project_id=PROJECT,
        run_id=RUN,
        approved_by="owner@example.com",
        approval_reason="replacing the previous reference",
    )
    assert approved["superseded_baseline_id"] == "ebl_00000000000000000000000001"
    (update,) = conn.statements("UPDATE app.evaluation_baselines")
    # Exactly one column changes. Every other column of the approval that was
    # made stays as it was written -- the single mutation migration 153 allows.
    assert "SET superseded_by_baseline_id = %s" in update
    assert update.count("SET") == 1
    assert " approved_by" not in update and " approval_reason" not in update


def test_an_unfinalized_run_cannot_be_approved():
    conn = _baseline_conn(run=_run_row(lifecycle="recording"))
    with pytest.raises(EvaluationRefused) as excinfo:
        approve_baseline(
            conn,
            org_id=ORG,
            project_id=PROJECT,
            run_id=RUN,
            approved_by="owner@example.com",
            approval_reason="too early",
        )
    assert excinfo.value.code == "run_not_finalized"


def test_an_observed_cohort_can_never_be_approved_as_a_baseline():
    """`analyze-and-test.md:357-358` -- observed cohorts use reference windows,
    not deterministic baselines."""
    conn = _baseline_conn(
        run=_run_row(evidence_mode="observed_cohort", observed_cohort_id="ocoh_example")
    )
    with pytest.raises(EvaluationRefused) as excinfo:
        approve_baseline(
            conn,
            org_id=ORG,
            project_id=PROJECT,
            run_id=RUN,
            approved_by="owner@example.com",
            approval_reason="observed drift looks good",
        )
    assert excinfo.value.code == "cohort_is_not_a_baseline"


# ---------------------------------------------------------------------------
# AC7 -- a comparison changes only the declared families, and kinds never pool.
# ---------------------------------------------------------------------------


def _fingerprints(**overrides):
    base = {family: f"{family}-v1" for family in ev.FINGERPRINTED_PIN_FAMILIES}
    base.update(overrides)
    return base


def _comparison_conn(baseline_run, candidate_run):
    return _Conn(
        **{
            "FROM__app__evaluation_runs__WHERE": [[baseline_run], [candidate_run]],
            "INSERT__INTO__app__evaluation_comparisons": [
                [(COMPARISON, _dt.datetime(2026, 7, 30, 11, 0))]
            ],
        }
    )


def _compare(conn, kind="semantic", families=("semantic_view",)):
    return create_comparison(
        conn,
        org_id=ORG,
        project_id=PROJECT,
        baseline_run_id=RUN,
        candidate_run_id=OTHER_RUN,
        comparison_kind=kind,
        changed_pin_families=list(families),
        actor="tester",
    )


def test_a_comparison_accepts_exactly_the_declared_drift():
    baseline = _run_row(id=RUN, pin_fingerprints=_fingerprints(), unresolved_pins=[])
    candidate = _run_row(
        id=OTHER_RUN,
        pin_fingerprints=_fingerprints(semantic_view="semantic_view-v2"),
        unresolved_pins=[],
    )
    comparison = _comparison_conn(baseline, candidate)
    result = _compare(comparison)
    assert result["changed_pin_families"] == ["semantic_view"]
    assert result["unverifiable_families"] == []


def test_a_comparison_refuses_undeclared_drift_when_a_family_moved():
    baseline = _run_row(id=RUN, pin_fingerprints=_fingerprints(), unresolved_pins=[])
    candidate = _run_row(
        id=OTHER_RUN,
        pin_fingerprints=_fingerprints(
            semantic_view="semantic_view-v2", data_snapshot="data_snapshot-v2"
        ),
        unresolved_pins=[],
    )
    with pytest.raises(EvaluationRefused) as excinfo:
        _compare(_comparison_conn(baseline, candidate))
    assert excinfo.value.code == "undeclared_drift"
    assert "data_snapshot" in str(excinfo.value)


def test_a_comparison_refuses_a_declared_family_that_did_not_move():
    baseline = _run_row(id=RUN, pin_fingerprints=_fingerprints(), unresolved_pins=[])
    candidate = _run_row(id=OTHER_RUN, pin_fingerprints=_fingerprints(), unresolved_pins=[])
    with pytest.raises(EvaluationRefused) as excinfo:
        _compare(_comparison_conn(baseline, candidate))
    assert excinfo.value.code == "undeclared_drift"


def test_comparison_kinds_are_never_pooled_into_one_qualification():
    """`analyze-and-test.md:342-343` -- results are never pooled silently."""
    baseline = _run_row(id=RUN, pin_fingerprints=_fingerprints(), unresolved_pins=[])
    candidate = _run_row(
        id=OTHER_RUN,
        pin_fingerprints=_fingerprints(semantic_view="semantic_view-v2", model="model-v2"),
        unresolved_pins=[],
    )
    with pytest.raises(EvaluationRefused) as excinfo:
        _compare(_comparison_conn(baseline, candidate), kind="model",
                 families=("model", "semantic_view"))
    assert excinfo.value.code == "families_not_in_kind"


def test_a_comparison_must_declare_at_least_one_changed_family():
    baseline = _run_row(id=RUN)
    candidate = _run_row(id=OTHER_RUN)
    with pytest.raises(EvaluationRefused) as excinfo:
        _compare(_comparison_conn(baseline, candidate), families=())
    assert excinfo.value.code == "no_declared_change"


def test_unresolved_pin_families_are_unverifiable_and_never_counted_as_unchanged():
    """Two runs that both failed to resolve a family are NOT equal on it."""
    drifted, unverifiable = compare_pin_families(
        _fingerprints(),
        _fingerprints(),
        [{"pin_family": "render", "reason_code": "render_owner_not_delivered"}],
        [{"pin_family": "render", "reason_code": "render_owner_not_delivered"}],
    )
    assert drifted == set()
    assert "render" in unverifiable


def test_pin_families_missing_from_one_side_are_unverifiable_not_drift():
    baseline = _fingerprints()
    candidate = dict(_fingerprints())
    candidate.pop("skill")
    drifted, unverifiable = compare_pin_families(baseline, candidate, [], [])
    assert "skill" in unverifiable
    assert "skill" not in drifted


def test_a_comparison_carries_the_render_absence_as_unverifiable():
    baseline = _run_row(id=RUN, pin_fingerprints=_fingerprints())
    candidate = _run_row(
        id=OTHER_RUN, pin_fingerprints=_fingerprints(semantic_view="semantic_view-v2")
    )
    result = _compare(_comparison_conn(baseline, candidate))
    assert result["unverifiable_families"] == ["render"]


def test_a_recording_run_has_no_comparable_pins():
    baseline = _run_row(id=RUN, lifecycle="recording", unresolved_pins=[])
    candidate = _run_row(id=OTHER_RUN, unresolved_pins=[])
    with pytest.raises(EvaluationRefused) as excinfo:
        _compare(_comparison_conn(baseline, candidate))
    assert excinfo.value.code == "run_not_finalized"


# ---------------------------------------------------------------------------
# Regression, per question and per dimension. Nothing compensates anything.
# ---------------------------------------------------------------------------


def _dimensions(**overrides):
    entries = {d: {"verdict": "pass"} for d in DIMENSIONS}
    entries.update({k: {"verdict": v} for k, v in overrides.items()})
    return entries


def test_pass_to_fail_is_a_regression():
    found = regressions_between(
        {QUESTION: _dimensions()},
        {QUESTION: _dimensions(semantic_correctness="fail")},
    )
    assert found == [
        {
            "golden_question_version_id": QUESTION,
            "dimension": "semantic_correctness",
            "kind": "pass_to_fail",
            "from": "pass",
            "to": "fail",
        }
    ]


def test_newly_unverifiable_is_a_regression():
    found = regressions_between(
        {QUESTION: _dimensions()},
        {QUESTION: _dimensions(path_quality="unverifiable")},
    )
    assert [r["kind"] for r in found] == ["newly_unverifiable"]


def test_a_dimension_that_was_already_unverifiable_is_not_a_new_regression():
    found = regressions_between(
        {QUESTION: _dimensions(mcp_app_behavior="unverifiable")},
        {QUESTION: _dimensions(mcp_app_behavior="unverifiable")},
    )
    assert found == []


def test_a_question_that_disappeared_is_lost_coverage():
    found = regressions_between({QUESTION: _dimensions()}, {})
    assert [r["kind"] for r in found] == ["lost_coverage"]


def test_an_improvement_elsewhere_does_not_cancel_a_regression():
    found = regressions_between(
        {QUESTION: _dimensions(dq_handling="fail")},
        {QUESTION: _dimensions(semantic_correctness="fail", dq_handling="pass")},
    )
    assert [r["dimension"] for r in found] == ["semantic_correctness"]


# ---------------------------------------------------------------------------
# AC10 -- the Gate Decision, and the owner tables it never writes.
# ---------------------------------------------------------------------------


def _gate_conn(baseline_verdict="pass", candidate_verdict="pass", *, unverifiable_families=None):
    return _Conn(
        **{
            "FROM__app__evaluation_comparisons": [
                [(RUN, OTHER_RUN, "semantic", list(unverifiable_families or []))]
            ],
            "FROM__app__evaluation_run_cases": [[_case_row()], [_case_row()]],
            "FROM__app__evaluation_case_dimension_verdicts": [
                _verdict_rows(verdict=baseline_verdict),
                _verdict_rows(verdict=candidate_verdict),
            ],
            "INSERT__INTO__app__evaluation_gate_decisions": [
                [("egd_00000000000000000000000001", _dt.datetime(2026, 7, 30, 12, 0))]
            ],
        }
    )


def _gate(conn):
    return emit_gate_decision(
        conn,
        org_id=ORG,
        project_id=PROJECT,
        comparison_id=COMPARISON,
        candidate_owner_workspace="governance",
        candidate_object_type="semantic-view",
        candidate_object_id="sv_example",
        candidate_version_id="svv_example_2",
        decided_by="owner@example.com",
    )


def test_a_gate_decision_is_unverifiable_when_any_dimension_is_unverifiable():
    decision = _gate(_gate_conn(candidate_verdict="unverifiable"))
    assert decision["decision"] == "unverifiable"
    assert decision["coverage"]["eligible"] == 1
    assert "unverifiable" in decision["decision_reason"]


def test_a_gate_decision_is_unverifiable_when_a_pin_family_is_unverifiable():
    decision = _gate(_gate_conn(unverifiable_families=["render"]))
    assert decision["decision"] == "unverifiable"
    assert decision["unverifiable_families"] == ["render"]


def test_a_gate_decision_blocks_on_a_regression():
    decision = _gate(_gate_conn(baseline_verdict="pass", candidate_verdict="fail"))
    assert decision["decision"] == "block"
    assert decision["failing_dimensions"] == sorted(DIMENSIONS)


def test_a_gate_decision_passes_only_when_nothing_is_failing_or_missing():
    decision = _gate(_gate_conn())
    assert decision["decision"] == "pass"
    assert decision["failing_dimensions"] == []
    assert decision["coverage"]["missing"] == 0


def test_missing_coverage_is_unverifiable_and_never_green():
    conn = _gate_conn()
    # The candidate evaluated only five of the six dimensions on its one case:
    # incomplete evidence, not a clean run.
    conn._answers["FROM app.evaluation_case_dimension_verdicts"] = [
        _verdict_rows(verdict="pass"),
        _verdict_rows(verdict="pass")[:-1],
    ]
    decision = _gate(conn)
    assert decision["decision"] == "unverifiable"
    assert decision["coverage"] == {"eligible": 1, "evaluated": 0, "missing": 1}


def test_a_gate_decision_writes_no_owner_table():
    conn = _gate_conn()
    _gate(conn)
    written = [sql for sql in conn.statements("INSERT") + conn.statements("UPDATE")]
    for sql in written:
        assert "app.semantic_" not in sql
        assert "app.mdm_" not in sql
        assert "app.context_" not in sql
        assert "app.knowledge_" not in sql
    assert any("app.evaluation_gate_decisions" in sql for sql in written)


def test_a_gate_decision_refuses_an_unpinned_candidate_version():
    with pytest.raises(EvaluationRefused) as excinfo:
        emit_gate_decision(
            _gate_conn(),
            org_id=ORG,
            project_id=PROJECT,
            comparison_id=COMPARISON,
            candidate_owner_workspace="governance",
            candidate_object_type="semantic-view",
            candidate_object_id="sv_example",
            candidate_version_id="latest",
            decided_by="owner@example.com",
        )
    assert excinfo.value.code == "unpinned_version"


def test_a_gate_decision_refuses_a_workspace_that_does_not_own_a_candidate():
    with pytest.raises(EvaluationRefused) as excinfo:
        emit_gate_decision(
            _gate_conn(),
            org_id=ORG,
            project_id=PROJECT,
            comparison_id=COMPARISON,
            candidate_owner_workspace="analyze",
            candidate_object_type="semantic-view",
            candidate_object_id="sv_example",
            candidate_version_id="svv_example_2",
            decided_by="owner@example.com",
        )
    assert excinfo.value.code == "unknown_value"


# ---------------------------------------------------------------------------
# AC11 -- every statement is Project-scoped, and denial is non-disclosing.
# ---------------------------------------------------------------------------


def _all_statements() -> list[str]:
    """Every statement the module issues across a full lifecycle."""
    seen: list[str] = []
    profiles = _Conn(
        **{
            "FROM__app__evaluation_run_profiles": [[("offline",)]],
            "INSERT__INTO__app__evaluation_runs": [[(RUN, "recording", "offline", None)]],
        }
    )
    _open(profiles)
    seen += [sql for sql, _ in profiles.executed]

    cases = _case_conn()
    add_run_case(
        cases,
        org_id=ORG,
        project_id=PROJECT,
        run_id=RUN,
        golden_question_version_id=QUESTION,
        result_id="qr_example",
        ai_path_expected=False,
    )
    seen += [sql for sql, _ in cases.executed]

    frozen = _finalize_conn()
    finalize_evaluation_run(frozen, org_id=ORG, project_id=PROJECT, run_id=RUN)
    seen += [sql for sql, _ in frozen.executed]

    verdicts = _verdict_conn()
    record_case_verdicts(verdicts, org_id=ORG, project_id=PROJECT, case_id=CASE)
    seen += [sql for sql, _ in verdicts.executed]

    baseline = _baseline_conn()
    approve_baseline(
        baseline,
        org_id=ORG,
        project_id=PROJECT,
        run_id=RUN,
        approved_by="owner@example.com",
        approval_reason="reference",
    )
    seen += [sql for sql, _ in baseline.executed]

    gate = _gate_conn()
    _gate(gate)
    seen += [sql for sql, _ in gate.executed]
    return seen


def test_every_statement_against_an_evaluation_table_is_project_scoped():
    """No bare id is ever trusted: scope is part of the lookup, not a check after.

    A statement that resolved a row by id alone would let a caller in one Project
    read or write a row in another whenever the application layer is wrong -- the
    exact failure the composite Project-scoped foreign keys exist to stop.
    """
    for sql in _all_statements():
        if "app.evaluation_" not in sql and "app.golden_question" not in sql:
            continue
        assert "project_id" in sql, sql
        assert "org_id" in sql, sql


def test_a_foreign_run_and_an_absent_run_raise_the_same_error():
    for run_id in (RUN, "erun_00000000000000000000000009"):
        with pytest.raises(EvaluationNotFound):
            finalize_evaluation_run(_Conn(), org_id=ORG, project_id=PROJECT, run_id=run_id)


# ---------------------------------------------------------------------------
# The routes exist, are ordered, and answer. Built from the exported constant.
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch):
    from core import evaluation_runs_api as api

    async def _allow(_request, _role="viewer"):
        return ("tester", ORG)

    monkeypatch.setattr(api, "_authorize", _allow)
    return TestClient(Starlette(routes=evaluation_run_routes))


@contextmanager
def _connection(conn):
    yield conn


def _patch_db(monkeypatch, conn):
    import core.db as db

    monkeypatch.setattr(db, "get_connection", lambda *a, **k: _connection(conn))


def test_the_five_workbench_tabs_are_five_distinct_addresses():
    paths = {route.path for route in evaluation_run_routes}
    base = "/api/projects/{project_id}/test/evaluation-runs/{run_id}"
    for tab in ("", "/cases", "/comparisons", "/environment", "/gate-decision"):
        assert f"{base}{tab}" in paths, tab


def test_no_parameterized_route_shadows_a_literal_declared_after_it():
    """Route order is load-bearing.

    Starlette matches in declaration order, so a `{run_id}` route declared before
    a literal of the same shape would swallow it. This walks the exported list
    and fails if any earlier parameterized route would match a later literal
    path -- the regression that turns `/evaluation-runs/recent` into a run id.
    """
    seen: list[tuple[str, str]] = []
    for route in evaluation_run_routes:
        segments = route.path.split("/")
        for earlier, methods in seen:
            earlier_segments = earlier.split("/")
            if len(earlier_segments) != len(segments):
                continue
            if not set(methods) & set(route.methods or ()):
                continue
            shadows = all(
                a == b or (a.startswith("{") and not b.startswith("{"))
                for a, b in zip(earlier_segments, segments, strict=True)
            )
            assert not (shadows and earlier != route.path), (
                f"`{earlier}` is declared before `{route.path}` and would capture it"
            )
        seen.append((route.path, tuple(route.methods or ())))


def test_the_collection_route_answers_with_distinct_evidence_modes(monkeypatch, client):
    conn = _Conn(
        **{
            "FROM__app__evaluation_runs": [
                [
                    (
                        RUN,
                        "finalized",
                        "offline",
                        "nightly",
                        _dt.date(2026, 7, 30),
                        _dt.datetime(2026, 7, 30, 8, 0),
                        _dt.datetime(2026, 7, 30, 9, 0),
                        "b" * 64,
                        [ev.render_unresolved_pin()],
                        3,
                    ),
                    (
                        OTHER_RUN,
                        "finalized",
                        "observed_cohort",
                        "drift watch",
                        _dt.date(2026, 7, 29),
                        _dt.datetime(2026, 7, 29, 8, 0),
                        _dt.datetime(2026, 7, 29, 9, 0),
                        "f" * 64,
                        [ev.render_unresolved_pin()],
                        5,
                    ),
                ]
            ]
        }
    )
    _patch_db(monkeypatch, conn)
    response = client.get(f"/api/projects/{PROJECT}/test/evaluation-runs")
    assert response.status_code == 200
    runs = response.json()["evaluation_runs"]
    assert [r["evidence_mode"] for r in runs] == ["offline", "observed_cohort"]
    # No merged figure survives anywhere in the payload.
    body = response.text
    for merged in ("pass_rate", "passRate", "precision_pct", "trust_score", "latest_score"):
        assert merged not in body


def test_the_environment_tab_resolves_the_seven_pin_families_or_names_the_owner(
    monkeypatch, client
):
    conn = _Conn(
        **{
            "FROM__app__evaluation_runs__WHERE": [[_run_row()]],
            "FROM__app__evaluation_context_version_set_entries": [
                [(0, "context-hub", "skill", "sk_example", "skv_1")]
            ],
            "FROM__app__evaluation_run_cases": [[_case_row(literal="No AI path")]],
        }
    )
    _patch_db(monkeypatch, conn)
    response = client.get(f"/api/projects/{PROJECT}/test/evaluation-runs/{RUN}/environment")
    assert response.status_code == 200
    families = {entry["family"]: entry for entry in response.json()["families"]}
    assert families["render"]["state"] == "unresolved"
    assert families["render"]["absence"]["reason_code"] == "render_not_pinned_for_this_subject"
    assert families["semantic_view"]["state"] == "pinned"


def test_foreign_and_absent_share_one_not_found_envelope(monkeypatch, client):
    _patch_db(monkeypatch, _Conn())
    first = client.get(f"/api/projects/{PROJECT}/test/evaluation-runs/{RUN}")
    second = client.get(
        f"/api/projects/{PROJECT}/test/evaluation-runs/erun_00000000000000000000000009"
    )
    assert first.status_code == second.status_code == 404
    assert first.json() == second.json() == {"code": "not_found", "message": "Not found"}


def test_the_cases_tab_reports_six_verdicts_and_never_an_empty_cell(monkeypatch, client):
    conn = _Conn(
        **{
            "FROM__app__evaluation_runs__WHERE": [[_run_row()]],
            "FROM__app__evaluation_run_cases": [[
                _case_row(
                    literal="No AI path",
                    feedback_id="fba_example",
                    regression_case_id="frc_example",
                )
            ]],
            "FROM__app__evaluation_case_dimension_verdicts": [[_verdict_rows()[0]]],
        }
    )
    _patch_db(monkeypatch, conn)
    response = client.get(f"/api/projects/{PROJECT}/test/evaluation-runs/{RUN}/cases")
    assert response.status_code == 200
    (case,) = response.json()["cases"]
    assert [v["dimension"] for v in case["verdicts"]] == list(DIMENSIONS)
    # A dimension nobody measured reads `unverifiable`, not a blank a reader
    # would take for green.
    assert {v["verdict"] for v in case["verdicts"]} == {"unverifiable"}
    assert case["verdicts"][0]["verdict_id"] == "edv_0"
    assert all(item["verdict_id"] is None for item in case["verdicts"][1:])
    assert case["feedback_regression"] == {
        "feedback_id": "fba_example",
        "regression_case_id": "frc_example",
    }


def test_a_refusal_answers_422_with_every_reason(monkeypatch, client):
    conn = _Conn(**{"FROM__app__evaluation_run_profiles": [[("offline",)]]})
    _patch_db(monkeypatch, conn)
    response = client.post(
        f"/api/projects/{PROJECT}/test/evaluation-runs/{RUN}/baseline",
        json={"approved_by": "owner@example.com", "approval_reason": ""},
    )
    assert response.status_code == 422
    assert response.json()["refusals"]


# ---------------------------------------------------------------------------
# AC12 -- no legacy object becomes the evaluation authority.
# ---------------------------------------------------------------------------

_CORE = pathlib.Path(__file__).resolve().parents[2] / "core"
# The executor joined this list the day it was written. Both guards below are
# about a CLASS of defect -- Test writing what it only judges, and Test minting a
# figure that compensates a regression -- and a new module in the workspace that
# the class guard does not cover is how the class comes back.
_OWNED = ("evaluation_runs.py", "evaluation_runs_api.py", "evaluation_run_executor.py")


def _code(name: str) -> str:
    """The module's CODE, with comments and docstrings removed.

    Scanning raw text would flag the very prose that explains why these
    boundaries exist. The boundary is about what the code DOES.
    """
    raw = (_CORE / name).read_text(encoding="utf-8")
    kept: list[str] = []
    previous = tokenize.INDENT
    for token in tokenize.generate_tokens(io.StringIO(raw).readline):
        if token.type == tokenize.COMMENT:
            continue
        if token.type == tokenize.STRING and previous in (
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.NEWLINE,
            tokenize.NL,
        ):
            previous = token.type
            continue
        if token.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
            previous = token.type
        kept.append(token.string)
    return " ".join(kept)


@pytest.mark.parametrize("module", _OWNED)
def test_no_legacy_alias_of_the_epic_14_score_row(module):
    body = _code(module)
    for legacy in (
        "app.eval_runs",
        "app.eval_benchmark_questions",
        "eval_business_path_results",
        "precision_pct",
        "score_passed",
    ):
        assert legacy not in body, (
            f"{module} touches `{legacy}`. The Epic 14 benchmark row is one "
            "compensating percentage and is not the Evaluation Run contract."
        )


@pytest.mark.parametrize("module", _OWNED)
def test_corpus_is_not_runtime_knowledge(module):
    body = _code(module)
    for reference in ("corpus.yaml", "tests/evals", "tests.evals"):
        assert reference not in body, (
            f"{module} reads `{reference}`. `analyze-and-test.md:243-247`: the "
            "repository corpus remains test code and never becomes runtime "
            "knowledge."
        )


@pytest.mark.parametrize("module", _OWNED)
def test_no_render_table_or_render_identifier_is_invented(module):
    body = _code(module)
    # `app.renders` is NO LONGER banned: Stories 50.4/50.5 delivered it and
    # migration 166 points the pins at it. What stays banned is MINTING an identity
    # of our own. Referencing another owner's object is the opposite of inventing.
    for invented in ("render_id", "CREATE TABLE"):
        assert invented not in body, (
            f"{module} contains `{invented}`. The rendered-artifact object is owned "
            "by Stories 50.4 / 50.5 / 50.7: this epic references it, never mints it."
        )


@pytest.mark.parametrize("module", _OWNED)
def test_the_modules_write_no_owner_table_at_all(module):
    body = _code(module)
    for statement in ("INSERT INTO", "UPDATE"):
        for owner in ("app.semantic_", "app.mdm_", "app.context_", "app.knowledge_", "app.dq_"):
            assert f"{statement} {owner}" not in body, (
                f"{module} writes `{owner}`. Test emits a Gate Decision the "
                "owning workflow reads; it never edits the candidate "
                "(analyze-and-test.md:368-371)."
            )


@pytest.mark.parametrize("module", _OWNED)
def test_no_aggregate_score_exists_anywhere_in_this_story(module):
    body = _code(module)
    for aggregate in (
        "trust_score",
        "overall_score",
        "pass_rate",
        "precision_pct",
        "weighted_average",
        "confidence_score",
    ):
        assert aggregate not in body, (
            f"{module} computes `{aggregate}`. `analyze-and-test.md:336-337`: an "
            "overall percentage cannot hide a critical failure or compensate a "
            "correctness regression with better feedback."
        )


def test_the_six_dimensions_are_exactly_the_ratified_six():
    assert DIMENSIONS == (
        "semantic_correctness",
        "provenance_correctness",
        "context_adherence",
        "path_quality",
        "dq_handling",
        "mcp_app_behavior",
    )
    assert ev.VERDICTS == ("pass", "fail", "unverifiable", "not_applicable")


# ---------------------------------------------------------------------------
# Story 67.15 A -- the two addresses this chantier adds.
# ---------------------------------------------------------------------------


def test_the_execute_address_exists_and_is_a_post():
    """`analyze-and-test.md:239` says a Regression Run EXECUTES or observes.

    Until this chantier only the observe half had an address: a caller had to
    issue one request per question and remember the order. Route order is proven
    by `test_no_parameterized_route_shadows_a_literal_declared_after_it`, which
    walks the whole exported list.
    """
    execute = [
        route
        for route in evaluation_run_routes
        if route.path == "/api/projects/{project_id}/test/evaluation-runs/{run_id}/execute"
    ]
    assert len(execute) == 1
    assert set(execute[0].methods or ()) >= {"POST"}


def test_the_adherence_address_exists_and_is_a_read():
    """The reader `app.query_adherence` never had.

    It lives beside the evaluations rather than on `ai-path-stats`: the audit of
    2026-08-17 left two candidate homes open and named picking ONE as the point.
    """
    adherence = [
        route
        for route in evaluation_run_routes
        if route.path == "/api/projects/{project_id}/test/context-adherence"
    ]
    assert len(adherence) == 1
    assert set(adherence[0].methods or ()) >= {"GET"}
    assert "POST" not in set(adherence[0].methods or ()), "a read does not write"


def test_a_window_that_is_not_a_number_is_refused_before_any_work(client):
    response = client.get(
        "/api/projects/proj_EXAMPLE/test/context-adherence?days=last-quarter"
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_body"


def test_the_executor_is_reached_through_the_one_module_that_owns_the_walk():
    """The handler translates HTTP and nothing else.

    A second copy of the walk inside the API is how the console and any later
    adapter stop unrolling a run the same way.
    """
    body = _code("evaluation_runs_api.py")
    assert "execute_evaluation_run" in body
    for writer in ("add_run_case(", "record_case_verdicts("):
        assert body.count(writer) <= 1, (
            "the API may hand one case to a writer; it may not walk a question set"
        )
