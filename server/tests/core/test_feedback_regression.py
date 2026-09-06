"""Story 65.9 -- feedback promotion, trusted evaluation and resolution."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from core.feedback_regression import (
    RESULT_CASE_EVALUATOR,
    RESULT_CASE_EVALUATOR_HASH,
    FeedbackRegressionRefused,
    evaluate_feedback_regression_case,
    get_feedback_regression_draft,
    promote_feedback_regression,
    resolve_feedback_regression,
)

ORG = "org_EXAMPLE"
PROJECT = "proj_EXAMPLE"
FEEDBACK = "fba_00000000000000000000000001"
REVIEW = "fbrv_00000000000000000000000001"


class _Cursor:
    def __init__(self, script):
        self.script = script
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self.script.statements.append((sql, params))
        self.rows = list(self.script.answer(sql))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows


class _Script:
    def __init__(self, answers=None):
        self.answers = answers or {}
        self.statements = []

    def answer(self, sql):
        for marker, rows in self.answers.items():
            if marker in sql:
                return rows
        return []

    def cursor(self):
        return _Cursor(self)


def _classification():
    document = {
        "schema_version": "evaluation-classification.v1",
        "semantic_view": {"state": "attributed", "id": "sv_1", "version_id": "svv_1"},
        "business_domains": {
            "state": "attributed",
            "versions": [
                {"id": "bd_1", "version_number": 2},
                {"id": "bd_2", "version_number": 1},
            ],
        },
        "skills": {"state": "attributed", "versions": []},
        "capability": {"state": "attributed", "key": "revenue", "version_id": "pcv_1"},
        "result_type": {"state": "attributed", "value": "table"},
    }
    from core.feedback_review import canonical_hash

    return {**document, "classification_hash": canonical_hash(document)}


def _detail(**overrides):
    value = {
        "id": FEEDBACK,
        "source": "authenticated",
        "target_schema_version": "exact-feedback.v1",
        "polarity": "negative",
        "interaction_ref": "afi_1",
        "observed_surface": "console",
        "result": {
            "id": "qr_1",
            "content_hash": "a" * 64,
            "query_spec_version_id": "qsv_1",
            "outcome": "success",
        },
        "render": None,
        "target": {"kind": "answer"},
        "semantic_view": {"id": "sv_1", "version_id": "svv_1"},
        "ai_path": "No AI path",
        "classification": _classification(),
    }
    value.update(overrides)
    return value


def _draft_conn():
    return _Script(
        {
            "FROM app.feedback_reviews r": [
                (REVIEW, "accepted", "semantic_correctness", "fail", "critical", "wrong")
            ],
            "FROM app.feedback_eligible_observations": [
                (
                    {"target_kinds": ["answer"]},
                    _classification(),
                    _classification()["classification_hash"],
                )
            ],
        }
    )


def test_draft_freezes_authority_and_requires_explicit_overlapping_domain_selection():
    with patch("core.feedback_regression.get_annotation", return_value=_detail()):
        draft = get_feedback_regression_draft(
            _draft_conn(), org_id=ORG, project_id=PROJECT, feedback_id=FEEDBACK
        )
    assert draft["state"] == "available"
    assert draft["review"]["version_id"] == REVIEW
    assert draft["frozen"]["result"] == {
        "id": "qr_1",
        "content_hash": "a" * 64,
        "outcome": "success",
    }
    assert draft["frozen"]["query_spec_version_id"] == "qsv_1"
    assert draft["requires_domain_selection"] is True
    assert draft["domain_options"] == [
        {"domain_id": "bd_1", "version_number": 2},
        {"domain_id": "bd_2", "version_number": 1},
    ]


@pytest.mark.parametrize(
    ("detail", "answers", "reason"),
    [
        (_detail(polarity="positive"), _draft_conn().answers, "positive_feedback_not_promotable"),
        (_detail(target_schema_version=None), _draft_conn().answers, "exact_feedback_required"),
        (_detail(), {"FROM app.feedback_reviews r": []}, "accepted_fail_review_required"),
        (
            _detail(),
            {"FROM app.feedback_reviews r": _draft_conn().answers["FROM app.feedback_reviews r"]},
            "eligible_observation_missing",
        ),
    ],
)
def test_draft_unavailability_is_one_reason_and_never_an_action(detail, answers, reason):
    with patch("core.feedback_regression.get_annotation", return_value=detail):
        draft = get_feedback_regression_draft(
            _Script(answers), org_id=ORG, project_id=PROJECT, feedback_id=FEEDBACK
        )
    assert draft["state"] == "unavailable"
    assert draft["reason"] == reason
    assert draft["create_contract"] is None


def test_promotion_refuses_unknown_or_client_supplied_authority_before_writing():
    payload = {
        "schema_version": "feedback-regression-create.v1",
        "expected_review_version_id": REVIEW,
        "retry_key": "retry",
        "title": "Spend regression",
        "owner": "owner@example.com",
        "reproduction_reason": "Invoice differs",
        "selected_domain": {"domain_id": "bd_1", "version_number": 2},
        "question": "What is spend?",
        "time_boundary": {},
        "expected_result": [],
        "required_provenance": [],
        "expected_ai_path": {},
        "result_id": "qr_client_supplied",
    }
    script = _Script()
    with pytest.raises(FeedbackRegressionRefused) as exc:
        promote_feedback_regression(
            script,
            org_id=ORG,
            project_id=PROJECT,
            feedback_id=FEEDBACK,
            actor="person@example.com",
            payload=payload,
        )
    assert exc.value.code == "unknown_field"
    assert script.statements == []


def test_trusted_evaluation_writes_assertion_receipts_and_six_producer_verdicts():
    now = datetime(2026, 8, 11, tzinfo=UTC)
    script = _Script(
        {
            "FROM app.evaluation_run_cases c": [
                (
                    "ecase_1",
                    "erun_1",
                    "finalized",
                    "offline",
                    "gqv_1",
                    [
                        {
                            "assertion_type": "empty",
                            "selectors": [],
                            "operator": "is",
                            "tolerance": None,
                        }
                    ],
                    [{"link_kind": "source", "required": True}],
                    {},
                    "qr_1",
                    "a" * 64,
                    "success",
                    0,
                    [],
                    {
                        "evaluation_classification": _classification(),
                        "provenance": {"links": [{"link_kind": "source"}]},
                    },
                    None,
                    "No AI path",
                    _classification()["classification_hash"],
                    _classification()["classification_hash"],
                    False,
                )
            ],
            "FROM app.evaluation_assertion_results": [],
            "INSERT INTO app.evaluation_assertion_results": [],
            "INSERT INTO app.evaluation_case_dimension_verdicts": [],
        }
    )
    receipt = evaluate_feedback_regression_case(
        script,
        org_id=ORG,
        project_id=PROJECT,
        case_id="ecase_1",
        actor="person@example.com",
        payload={"schema_version": "evaluation-request.v1", "retry_key": "eval-retry"},
        now=now,
    )
    assert receipt["producer"] == RESULT_CASE_EVALUATOR
    assert receipt["owner_links"][0]["tab"] == "cases"
    assert len(receipt["assertion_results"]) == 1
    assert receipt["assertion_results"][0]["assertion_result_id"].startswith("ear_")
    assert all(item["verdict_id"].startswith("edv_") for item in receipt["verdicts"].values())
    passing_ids = [
        item["verdict_id"] for item in receipt["verdicts"].values() if item["verdict"] == "pass"
    ]
    assert [link["version_id"] for link in receipt["owner_links"]] == [
        None,
        "ecase_1",
        *passing_ids,
    ]
    assert all(link["object_id"] == "erun_1" for link in receipt["owner_links"])
    assert all(link["tab"] == "cases" for link in receipt["owner_links"])
    verdict_inserts = [
        sql
        for sql, _ in script.statements
        if "INSERT INTO app.evaluation_case_dimension_verdicts" in sql
    ]
    assert len(verdict_inserts) == 6
    assert all("producer_contract_hash" in sql for sql in verdict_inserts)
    context_insert = next(
        params
        for sql, params in script.statements
        if "INSERT INTO app.evaluation_case_dimension_verdicts" in sql
        and params[4] == "context_adherence"
    )
    assert context_insert[5:7] == (
        "unverifiable",
        "context_skill_evidence_unavailable",
    )
    from core.feedback_review import canonical_hash

    replay = _Script(
        {
            "FROM app.evaluation_run_cases c": script.answers["FROM app.evaluation_run_cases c"],
            "FROM app.evaluation_assertion_results": [
                (
                    item["assertion_result_id"],
                    canonical_hash(
                        {"schema_version": "evaluation-request.v1", "case_id": "ecase_1"}
                    ),
                    item["ordinal"],
                    item["assertion_type"],
                    item["verdict"],
                    item["reason_code"],
                    item["expected_hash"],
                    item["observed_hash"],
                    item["evidence_refs"],
                )
                for item in receipt["assertion_results"]
            ],
            "FROM app.evaluation_case_dimension_verdicts": [
                (
                    item["verdict_id"],
                    dimension,
                    item["verdict"],
                    item["reason_code"],
                    RESULT_CASE_EVALUATOR,
                    RESULT_CASE_EVALUATOR_HASH,
                    item["evidence_refs"],
                )
                for dimension, item in receipt["verdicts"].items()
            ],
        }
    )
    replay_receipt = evaluate_feedback_regression_case(
        replay,
        org_id=ORG,
        project_id=PROJECT,
        case_id="ecase_1",
        actor="person@example.com",
        payload={"schema_version": "evaluation-request.v1", "retry_key": "eval-retry"},
        now=now,
    )
    assert replay_receipt == receipt


def test_evaluation_replay_restores_the_six_stored_verdicts():
    assertion = (
        "ear_1",
        "request-hash",
        0,
        "cardinality",
        "pass",
        "assertion_matched",
        "e" * 64,
        "o" * 64,
        {},
    )
    dimensions = [
        (
            f"edv_{dimension}",
            dimension,
            "pass",
            "assertions_passed",
            RESULT_CASE_EVALUATOR,
            RESULT_CASE_EVALUATOR_HASH,
            {"dimension": dimension},
        )
        for dimension in (
            "semantic_correctness",
            "provenance_correctness",
            "context_adherence",
            "path_quality",
            "dq_handling",
            "mcp_app_behavior",
        )
    ]
    script = _Script(
        {
            "FROM app.evaluation_run_cases c": [
                (
                    "ecase_1",
                    "erun_1",
                    "finalized",
                    "offline",
                    "gqv_1",
                    [],
                    [],
                    {},
                    "qr_1",
                    "a" * 64,
                    "success",
                    0,
                    [],
                    {"evaluation_classification": _classification()},
                    None,
                    "No AI path",
                    _classification()["classification_hash"],
                    _classification()["classification_hash"],
                    False,
                )
            ],
            "FROM app.evaluation_assertion_results": [assertion],
            "FROM app.evaluation_case_dimension_verdicts": dimensions,
        }
    )
    with patch(
        "core.feedback_regression.canonical_hash", side_effect=["retry-hash", "request-hash"]
    ):
        receipt = evaluate_feedback_regression_case(
            script,
            org_id=ORG,
            project_id=PROJECT,
            case_id="ecase_1",
            actor="person@example.com",
            payload={"schema_version": "evaluation-request.v1", "retry_key": "retry"},
        )
    assert receipt["status"] == "evaluated"
    assert receipt["assertion_results"][0]["assertion_result_id"] == "ear_1"
    assert set(receipt["verdicts"]) == {row[1] for row in dimensions}
    assert {item["verdict_id"] for item in receipt["verdicts"].values()} == {
        row[0] for row in dimensions
    }
    assert all("evidence_refs" in item for item in receipt["verdicts"].values())


def test_promotion_receipt_is_byte_identical_on_first_write_and_replay():
    from core.feedback_regression import _promotion_receipt

    row = (
        "frc_1",
        "request",
        "gq_1",
        "gqv_1",
        REVIEW,
        "qr_1",
        "a" * 64,
        "b" * 64,
        FEEDBACK,
    )
    receipt = _promotion_receipt(
        row,
        status="created",
    )
    replay = _promotion_receipt(
        row,
        status="replayed",
    )
    assert replay == receipt
    assert receipt["status"] == "created"
    assert receipt["owner_links"][1]["object_id"] == FEEDBACK


def test_resolution_leaves_a_caller_declared_pass_unresolved_and_writes_nothing():
    script = _Script(
        {
            "FROM app.feedback_regression_cases p": [
                (
                    "frc_1",
                    REVIEW,
                    "gqv_1",
                    "semantic_correctness",
                    "qr_1",
                    "a" * 64,
                    "b" * 64,
                    "major",
                )
            ],
            "FROM app.evaluation_run_cases c": [
                (
                    "ecase_1",
                    "erun_1",
                    "finalized",
                    "offline",
                    "gqv_1",
                    "qr_1",
                    "a" * 64,
                    "b" * 64,
                    "edv_1",
                    "pass",
                    "caller-declared.v1",
                    None,
                )
            ],
        }
    )
    receipt = resolve_feedback_regression(
        script,
        org_id=ORG,
        project_id=PROJECT,
        feedback_id=FEEDBACK,
        actor="person@example.com",
        payload={
            "schema_version": "feedback-regression-resolve.v1",
            "regression_case_id": "frc_1",
            "evaluation_case_id": "ecase_1",
        },
    )
    assert receipt["status"] == "unresolved"
    assert receipt["reason"] == "trusted_producer_required"
    assert receipt["evaluation_run_id"] == "erun_1"
    assert receipt["verdict_id"] == "edv_1"
    assert [link["version_id"] for link in receipt["owner_links"]] == [
        None,
        "ecase_1",
        "edv_1",
    ]
    assert not any(
        "INSERT INTO app.feedback_regression_resolutions" in sql for sql, _ in script.statements
    )


def test_resolution_refuses_context_pass_without_exact_context_skill_evidence():
    script = _Script(
        {
            "FROM app.feedback_regression_cases p": [
                (
                    "frc_1",
                    REVIEW,
                    "gqv_1",
                    "context_adherence",
                    "qr_1",
                    "a" * 64,
                    "b" * 64,
                    "major",
                )
            ],
            "FROM app.evaluation_run_cases c": [
                (
                    "ecase_1",
                    "erun_1",
                    "finalized",
                    "offline",
                    "gqv_1",
                    "qr_1",
                    "a" * 64,
                    "b" * 64,
                    "edv_1",
                    "pass",
                    RESULT_CASE_EVALUATOR,
                    RESULT_CASE_EVALUATOR_HASH,
                )
            ],
        }
    )
    receipt = resolve_feedback_regression(
        script,
        org_id=ORG,
        project_id=PROJECT,
        feedback_id=FEEDBACK,
        actor="person@example.com",
        payload={
            "schema_version": "feedback-regression-resolve.v1",
            "regression_case_id": "frc_1",
            "evaluation_case_id": "ecase_1",
        },
    )
    assert receipt["status"] == "unresolved"
    assert receipt["reason"] == "context_skill_evidence_unavailable"
    assert receipt["evaluation_run_id"] == "erun_1"
    assert receipt["verdict_id"] == "edv_1"
    assert not any(
        "INSERT INTO app.feedback_regression_resolutions" in sql for sql, _ in script.statements
    )


def test_resolution_race_returns_the_stored_lineage_not_the_losing_candidate():
    script = _Script(
        {
            "FROM app.feedback_regression_cases p": [
                (
                    "frc_1",
                    REVIEW,
                    "gqv_1",
                    "semantic_correctness",
                    "qr_1",
                    "a" * 64,
                    "b" * 64,
                    "major",
                )
            ],
            "FROM app.evaluation_run_cases c": [
                (
                    "ecase_loser",
                    "erun_loser",
                    "finalized",
                    "offline",
                    "gqv_1",
                    "qr_1",
                    "a" * 64,
                    "b" * 64,
                    "edv_loser",
                    "pass",
                    RESULT_CASE_EVALUATOR,
                    RESULT_CASE_EVALUATOR_HASH,
                )
            ],
            "SELECT id, regression_case_id, evaluation_case_id": [
                ("frr_winner", "frc_1", "ecase_winner", "erun_winner", "edv_winner")
            ],
        }
    )
    receipt = resolve_feedback_regression(
        script,
        org_id=ORG,
        project_id=PROJECT,
        feedback_id=FEEDBACK,
        actor="person@example.com",
        payload={
            "schema_version": "feedback-regression-resolve.v1",
            "regression_case_id": "frc_1",
            "evaluation_case_id": "ecase_loser",
        },
    )
    assert receipt["resolution_id"] == "frr_winner"
    assert receipt["evaluation_case_id"] == "ecase_winner"
    assert receipt["evaluation_run_id"] == "erun_winner"
    assert receipt["verdict_id"] == "edv_winner"
