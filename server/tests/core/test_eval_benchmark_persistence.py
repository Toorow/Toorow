"""The missing production half of Story 45.8's evaluation producer."""

from __future__ import annotations

import datetime as dt
from contextlib import nullcontext
from unittest.mock import patch

import pytest


class _Cursor:
    def __init__(self, rows):
        self._rows = iter(rows)
        self.executed: list[tuple[str, tuple | None]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return next(self._rows)


class _Conn:
    def __init__(self, rows):
        self.cur = _Cursor(rows)

    def cursor(self):
        return self.cur

    def transaction(self):
        return nullcontext()


def _corpus(*, target_type="procedure", routes=1):
    expected = [
        {
            "domain_id": "bdm_sales",
            "target_type": target_type,
            "target_id": "proc_revenue",
        }
        for _ in range(routes)
    ]
    return {
        "questions": [
            {
                "id": "q_revenue",
                "question": "Why did revenue fall?",
                "surface": "expert_report",
                "expected_citations": [{"source_system": "stripe"}],
                "expected_business_routes": expected,
            }
        ]
    }


def _artifact(*, outcome="missing_path"):
    return {
        "summary": {
            "accuracy": {"PASS": 0, "FAIL": 1},
            "accuracy_score": 0.0,
            "business_paths": {
                "coverage_pct": 0.0,
                "missing_path": int(outcome == "missing_path"),
                "wrong_domain": int(outcome == "wrong_domain"),
                "path_version_drift": int(outcome == "path_version_drift"),
            },
        },
        "results": [
            {
                "id": "q_revenue",
                "accuracy": "FAIL",
                "business_path": {
                    "outcome": outcome,
                    "matched_path_keys": [],
                },
                "observed_path_keys": [],
                "trace_id": "trace_45_8",
            }
        ],
    }


def test_missing_path_is_persisted_and_files_one_stable_agent_proposal():
    from core.eval_benchmark_persistence import persist_benchmark_evaluation

    conn = _Conn(
        [
            ("org_1",),
            ("q_db",),
            ("run_1",),
            ("proj_1", 4),
        ]
    )
    run_at = dt.datetime(2026, 8, 16, 8, 0, tzinfo=dt.UTC)

    with (
        patch("core.eval_benchmark_persistence.record_evaluation_path_result") as record,
        patch("core.eval_benchmark_persistence.propose_missing_link") as propose,
    ):
        persisted = persist_benchmark_evaluation(
            conn,
            project_id="proj_1",
            run_at=run_at,
            corpus=_corpus(),
            artifact=_artifact(),
        )

    assert persisted == {
        "run_id": "run_1",
        "question_results": 1,
        "agent_requests": 1,
    }
    record.assert_called_once_with(
        conn,
        run_id="run_1",
        question_id="q_db",
        expected_route={
            "domain_id": "bdm_sales",
            "target_type": "procedure",
            "target_id": "proc_revenue",
        },
        observed_path_key=None,
        trace_id="trace_45_8",
        outcome="missing_path",
    )
    proposal = propose.call_args.kwargs
    assert proposal["org_id"] == "org_1"
    assert proposal["project_id"] == "proj_1"
    assert proposal["node_type"] == "procedure"
    assert proposal["node_id"] == "proc_revenue"
    assert proposal["node_version"] == 4
    assert proposal["taxonomy_type"] == "business_domain"
    assert proposal["taxonomy_id"] == "bdm_sales"
    assert proposal["relation_type"] == "applies_to"
    assert "q_revenue" in proposal["evidence"]
    assert "2026" not in proposal["evidence"]


@pytest.mark.parametrize("outcome", ["pass", "wrong_domain", "path_version_drift"])
def test_only_a_proven_missing_path_files_a_proposal(outcome):
    from core.eval_benchmark_persistence import persist_benchmark_evaluation

    conn = _Conn([("org_1",), ("q_db",), ("run_1",)])
    with (
        patch("core.eval_benchmark_persistence.record_evaluation_path_result"),
        patch("core.eval_benchmark_persistence.propose_missing_link") as propose,
    ):
        persisted = persist_benchmark_evaluation(
            conn,
            project_id="proj_1",
            run_at=dt.datetime(2026, 8, 16, tzinfo=dt.UTC),
            corpus=_corpus(),
            artifact=_artifact(outcome=outcome),
        )

    assert persisted["agent_requests"] == 0
    propose.assert_not_called()


def test_ambiguous_or_non_reviewable_routes_are_persisted_but_never_guessed():
    from core.eval_benchmark_persistence import persist_benchmark_evaluation

    for corpus in (_corpus(routes=2), _corpus(target_type="report_view")):
        conn = _Conn([("org_1",), ("q_db",), ("run_1",)])
        with (
            patch("core.eval_benchmark_persistence.record_evaluation_path_result") as record,
            patch("core.eval_benchmark_persistence.propose_missing_link") as propose,
        ):
            persisted = persist_benchmark_evaluation(
                conn,
                project_id="proj_1",
                run_at=dt.datetime(2026, 8, 16, tzinfo=dt.UTC),
                corpus=corpus,
                artifact=_artifact(),
            )

        assert persisted["question_results"] == 1
        assert persisted["agent_requests"] == 0
        record.assert_called_once()
        propose.assert_not_called()


def test_unverifiable_evidence_is_not_written_as_a_database_verdict():
    from core.eval_benchmark_persistence import persist_benchmark_evaluation

    conn = _Conn([("org_1",), ("q_db",), ("run_1",)])
    with (
        patch("core.eval_benchmark_persistence.record_evaluation_path_result") as record,
        patch("core.eval_benchmark_persistence.propose_missing_link") as propose,
    ):
        persisted = persist_benchmark_evaluation(
            conn,
            project_id="proj_1",
            run_at=dt.datetime(2026, 8, 16, tzinfo=dt.UTC),
            corpus=_corpus(),
            artifact=_artifact(outcome="unverifiable"),
        )

    assert persisted["question_results"] == 0
    assert persisted["agent_requests"] == 0
    record.assert_not_called()
    propose.assert_not_called()
