"""Persist the Epic-14 benchmark artifact and file proven missing links.

The deterministic runner in ``scripts/run_evals.py`` deliberately owns only a
DuckDB connection.  This module is the separate Postgres projection seam: one
transaction records the run, its per-question path verdicts, and the governed
review request that a *proven* single-route gap warrants.

This is legacy benchmark evidence, not the version-pinned Evaluation Run model
owned by ``core.evaluation_runs`` (Epic 51).  Keeping the names explicit avoids
silently turning one object into the other.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from core.context_review import propose_missing_link
from core.evaluation_paths import record_evaluation_path_result

_PERSISTED_PATH_OUTCOMES = frozenset(
    {"pass", "missing_path", "wrong_domain", "path_version_drift"}
)
_REVIEWABLE_TARGETS = {
    "topic": ("app.context_topics", "app.context_topics_versions", "topic_id"),
    "procedure": ("app.procedures", "app.procedures_versions", "procedure_id"),
}


class BenchmarkEvaluationPersistenceError(ValueError):
    """The artifact cannot be projected into the requested active Project."""


def _project_org(conn: Any, project_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT org_id FROM app.projects WHERE id = %s AND status = 'active'",
            (project_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise BenchmarkEvaluationPersistenceError(
            f"Project '{project_id}' does not resolve to an active organization"
        )
    return str(row[0])


def _result_by_id(artifact: dict[str, Any]) -> dict[str, dict[str, Any]]:
    results = artifact.get("results") or []
    return {
        str(result["id"]): result
        for result in results
        if isinstance(result, dict) and result.get("id")
    }


def _upsert_question(
    conn: Any,
    *,
    project_id: str,
    question: dict[str, Any],
    result: dict[str, Any],
) -> str:
    question_id = str(question.get("id") or "").strip()
    prompt = str(question.get("question") or "").strip()
    if not question_id or not prompt:
        raise BenchmarkEvaluationPersistenceError(
            "Every persisted benchmark question needs a stable id and question text"
        )
    routes = question.get("expected_business_routes") or []
    if not isinstance(routes, list):
        raise BenchmarkEvaluationPersistenceError(
            f"Question '{question_id}' expected_business_routes must be a list"
        )
    citations = question.get("expected_citations") or []
    topic = str(question.get("surface") or "benchmark")
    last_result = "pass" if result.get("accuracy") == "PASS" else "fail"
    scoped_id = "ebq_" + hashlib.sha256(
        f"{project_id}:{question_id}".encode("utf-8")
    ).hexdigest()[:26]
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.eval_benchmark_questions
                (id, project_id, question, topic, expected_citations, last_result,
                 expected_business_routes)
            VALUES (
                COALESCE(
                    (SELECT id FROM app.eval_benchmark_questions
                     WHERE project_id = %s AND question = %s),
                    %s
                ),
                %s, %s, %s, %s, %s, %s::jsonb
            )
            ON CONFLICT (id) DO UPDATE SET
                question = EXCLUDED.question,
                topic = EXCLUDED.topic,
                expected_citations = EXCLUDED.expected_citations,
                last_result = EXCLUDED.last_result,
                expected_business_routes = EXCLUDED.expected_business_routes
            WHERE app.eval_benchmark_questions.project_id = EXCLUDED.project_id
            RETURNING id
            """,
            (
                project_id,
                prompt,
                scoped_id,
                project_id,
                prompt,
                topic,
                len(citations),
                last_result,
                json.dumps(routes, sort_keys=True),
            ),
        )
        row = cur.fetchone()
    if row is None:  # pragma: no cover - RETURNING is guaranteed by PostgreSQL
        raise BenchmarkEvaluationPersistenceError(
            f"Question '{question_id}' could not be persisted"
        )
    return str(row[0])


def _tally(summary: dict[str, Any], key: str) -> int:
    value = (summary.get("accuracy") or {}).get(key, 0)
    return int(value or 0)


def _upsert_run(
    conn: Any,
    *,
    project_id: str,
    run_at: Any,
    artifact: dict[str, Any],
) -> str:
    summary = artifact.get("summary") or {}
    path_summary = summary.get("business_paths") or {}
    results = artifact.get("results") or []
    passed = _tally(summary, "PASS")
    failed = _tally(summary, "FAIL")
    score_total = passed + failed
    accuracy_score = summary.get("accuracy_score")
    precision_pct = round(float(accuracy_score or 0) * 100, 2)
    observed_path_keys = sorted(
        {
            str(path_key)
            for result in results
            if isinstance(result, dict)
            for path_key in (result.get("observed_path_keys") or [])
            if path_key
        }
    )
    trace_ids = sorted(
        {
            str(result["trace_id"])
            for result in results
            if isinstance(result, dict) and result.get("trace_id")
        }
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.eval_runs
                (project_id, run_at, score_passed, score_total, precision_pct,
                 regressions, status, observed_path_keys, observed_trace_ids,
                 path_coverage_pct, missing_path_count, wrong_domain_count,
                 path_version_drift_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                    %s, %s, %s, %s)
            ON CONFLICT (project_id, run_at) DO UPDATE SET
                score_passed = EXCLUDED.score_passed,
                score_total = EXCLUDED.score_total,
                precision_pct = EXCLUDED.precision_pct,
                regressions = EXCLUDED.regressions,
                status = EXCLUDED.status,
                observed_path_keys = EXCLUDED.observed_path_keys,
                observed_trace_ids = EXCLUDED.observed_trace_ids,
                path_coverage_pct = EXCLUDED.path_coverage_pct,
                missing_path_count = EXCLUDED.missing_path_count,
                wrong_domain_count = EXCLUDED.wrong_domain_count,
                path_version_drift_count = EXCLUDED.path_version_drift_count
            RETURNING id
            """,
            (
                project_id,
                run_at,
                passed,
                score_total,
                precision_pct,
                failed,
                "passed" if failed == 0 else "regressed",
                json.dumps(observed_path_keys),
                json.dumps(trace_ids),
                path_summary.get("coverage_pct"),
                int(path_summary.get("missing_path") or 0),
                int(path_summary.get("wrong_domain") or 0),
                int(path_summary.get("path_version_drift") or 0),
            ),
        )
        row = cur.fetchone()
    if row is None:  # pragma: no cover - RETURNING is guaranteed by PostgreSQL
        raise BenchmarkEvaluationPersistenceError("Evaluation run could not be persisted")
    return str(row[0])


def _reviewable_target_version(
    conn: Any, *, project_id: str, target_type: str, target_id: str
) -> int | None:
    source = _REVIEWABLE_TARGETS.get(target_type)
    if source is None:
        return None
    head_table, version_table, version_fk = source
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT head.project_id, max(version.version_number)
            FROM {head_table} head
            LEFT JOIN {version_table} version ON version.{version_fk} = head.id
            WHERE head.id = %s AND (head.project_id IS NULL OR head.project_id = %s)
            GROUP BY head.project_id
            """,
            (target_id, project_id),
        )
        row = cur.fetchone()
    if row is None or row[1] is None:
        return None
    return int(row[1])


def _stable_gap_evidence(question_id: str, route: dict[str, Any]) -> str:
    """Evidence detailed enough to review and stable enough to deduplicate.

    A run id, timestamp or trace id would change on every observation and defeat
    the queue's idempotence key.  Those volatile facts remain on the persisted
    result; the request names the stable benchmark and expected route.
    """
    route_json = json.dumps(route, sort_keys=True, separators=(",", ":"))
    return (
        f"benchmark question {question_id} produced missing_path for expected "
        f"route {route_json}; run and trace evidence are retained in "
        "app.eval_business_path_results"
    )


def persist_benchmark_evaluation(
    conn: Any,
    *,
    project_id: str,
    run_at: Any,
    corpus: dict[str, Any],
    artifact: dict[str, Any],
) -> dict[str, Any]:
    """Project one completed offline evaluation into Postgres atomically.

    All path verdicts supported by the database are retained.  Filing is more
    conservative: only a single expected route ending at a versioned Context
    Hub node can prove the exact link to propose.  Multi-route aggregates and
    non-reviewable targets are recorded but never guessed into a human queue.
    """
    project_id = (project_id or "").strip()
    if not project_id:
        raise BenchmarkEvaluationPersistenceError("project_id is required")
    questions = corpus.get("questions") or []
    if not isinstance(questions, list):
        raise BenchmarkEvaluationPersistenceError("corpus.questions must be a list")
    results = _result_by_id(artifact)

    persisted_results = 0
    agent_requests = 0
    with conn.transaction():
        org_id = _project_org(conn, project_id)
        question_rows: list[tuple[dict[str, Any], dict[str, Any], str]] = []
        for question in questions:
            if not isinstance(question, dict):
                continue
            question_id = str(question.get("id") or "")
            result = results.get(question_id)
            if result is None:
                continue
            database_question_id = _upsert_question(
                conn,
                project_id=project_id,
                question=question,
                result=result,
            )
            question_rows.append((question, result, database_question_id))

        run_id = _upsert_run(
            conn,
            project_id=project_id,
            run_at=run_at,
            artifact=artifact,
        )

        for question, result, database_question_id in question_rows:
            path_result = result.get("business_path") or {}
            outcome = path_result.get("outcome")
            routes = question.get("expected_business_routes") or []
            if outcome not in _PERSISTED_PATH_OUTCOMES or not routes:
                continue
            expected_route = routes[0] if len(routes) == 1 else {"routes": routes}
            matched = path_result.get("matched_path_keys") or []
            record_evaluation_path_result(
                conn,
                run_id=run_id,
                question_id=database_question_id,
                expected_route=expected_route,
                observed_path_key=str(matched[0]) if matched else None,
                trace_id=str(result["trace_id"]) if result.get("trace_id") else None,
                outcome=str(outcome),
            )
            persisted_results += 1

            if outcome != "missing_path" or len(routes) != 1:
                continue
            route = routes[0]
            target_type = str(route.get("target_type") or "")
            target_id = str(route.get("target_id") or "")
            version = _reviewable_target_version(
                conn,
                project_id=project_id,
                target_type=target_type,
                target_id=target_id,
            )
            if version is None:
                continue
            classification_id = route.get("classification_id")
            propose_missing_link(
                conn,
                org_id=org_id,
                project_id=project_id,
                node_type=target_type,
                node_id=target_id,
                node_version=version,
                taxonomy_type=(
                    "business_classification" if classification_id else "business_domain"
                ),
                taxonomy_id=str(classification_id or route.get("domain_id") or ""),
                relation_type="applies_to",
                evidence=_stable_gap_evidence(str(question.get("id")), route),
            )
            agent_requests += 1

    return {
        "run_id": run_id,
        "question_results": persisted_results,
        "agent_requests": agent_requests,
    }
