"""Story 45.8 AC1: completed evaluation -> one governed agent request."""

from __future__ import annotations

import datetime as dt
import os

import pytest
from core.eval_benchmark_persistence import persist_benchmark_evaluation

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres proof skipped",
)

_PROJECT_ID = "proj_45_8_eval_producer"
_PROCEDURE_ID = "proc_45_8_eval_producer"
_DOMAIN_ID = "bdm_45_8_eval_producer"
_AUTHOR = "test"
_RUN_AT = dt.datetime(2026, 8, 16, 9, 0, tzinfo=dt.UTC)


def _seed(conn, org_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
            "VALUES (%s, %s, '45.8 eval producer', '45-8-eval-producer', 'active', %s) "
            "ON CONFLICT (id) DO NOTHING",
            (_PROJECT_ID, org_id, _AUTHOR),
        )
        cur.execute(
            """
            INSERT INTO app.procedures
                (id, project_id, name, description, frontmatter_yaml, body_md,
                 status, created_by)
            VALUES (%s, %s, 'eval-producer', 'Missing governed link',
                    'name: "eval-producer"\ndescription: "d"\n', 'body',
                    'active', %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (_PROCEDURE_ID, _PROJECT_ID, _AUTHOR),
        )
        cur.execute(
            """
            INSERT INTO app.procedures_versions
                (procedure_id, project_id, name, description, frontmatter_yaml,
                 body_md, status, created_by, created_at, updated_at,
                 version_number, changed_by)
            VALUES (%s, %s, 'eval-producer', 'd',
                    'name: "eval-producer"\ndescription: "d"\n', 'body',
                    'active', %s, now(), now(), 1, %s)
            ON CONFLICT (procedure_id, version_number) DO NOTHING
            """,
            (_PROCEDURE_ID, _PROJECT_ID, _AUTHOR, _AUTHOR),
        )
        cur.execute(
            "INSERT INTO app.mdm_business_domains (id, org_id, slug, name, created_by) "
            "VALUES (%s, %s, '45-8-eval-producer', '45.8 eval producer', %s) "
            "ON CONFLICT (id) DO NOTHING",
            (_DOMAIN_ID, org_id, _AUTHOR),
        )
    conn.commit()


def _clean(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM app.context_review_requests WHERE project_id = %s",
            (_PROJECT_ID,),
        )
        cur.execute("DELETE FROM app.eval_runs WHERE project_id = %s", (_PROJECT_ID,))
        cur.execute(
            "DELETE FROM app.eval_benchmark_questions WHERE project_id = %s",
            (_PROJECT_ID,),
        )
    conn.commit()


def _corpus() -> dict:
    return {
        "questions": [
            {
                "id": "q_45_8_missing_link",
                "question": "Which governed procedure explains revenue?",
                "surface": "expert_report",
                "expected_citations": [],
                "expected_business_routes": [
                    {
                        "domain_id": _DOMAIN_ID,
                        "target_type": "procedure",
                        "target_id": _PROCEDURE_ID,
                    }
                ],
            }
        ]
    }


def _artifact() -> dict:
    return {
        "summary": {
            "accuracy": {"PASS": 1, "FAIL": 0},
            "accuracy_score": 1.0,
            "business_paths": {
                "coverage_pct": 0.0,
                "missing_path": 1,
                "wrong_domain": 0,
                "path_version_drift": 0,
            },
        },
        "results": [
            {
                "id": "q_45_8_missing_link",
                "accuracy": "PASS",
                "business_path": {
                    "outcome": "missing_path",
                    "matched_path_keys": [],
                },
                "observed_path_keys": [],
                "trace_id": "trace_45_8_missing_link",
            }
        ],
    }


def test_completed_missing_path_run_persists_once_and_files_once(live_postgres, test_org):
    conn = live_postgres
    _seed(conn, test_org)
    _clean(conn)
    try:
        first = persist_benchmark_evaluation(
            conn,
            project_id=_PROJECT_ID,
            run_at=_RUN_AT,
            corpus=_corpus(),
            artifact=_artifact(),
        )
        conn.commit()
        second = persist_benchmark_evaluation(
            conn,
            project_id=_PROJECT_ID,
            run_at=_RUN_AT,
            corpus=_corpus(),
            artifact=_artifact(),
        )
        conn.commit()

        assert first["run_id"] == second["run_id"]
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM app.eval_runs WHERE project_id = %s", (_PROJECT_ID,))
            assert cur.fetchone()[0] == 1
            cur.execute(
                """
                SELECT result.outcome, result.trace_id, question.expected_business_routes
                FROM app.eval_business_path_results result
                JOIN app.eval_benchmark_questions question ON question.id = result.question_id
                JOIN app.eval_runs run ON run.id = result.run_id
                WHERE run.project_id = %s
                """,
                (_PROJECT_ID,),
            )
            outcome, trace_id, expected_routes = cur.fetchone()
            assert outcome == "missing_path"
            assert trace_id == "trace_45_8_missing_link"
            assert expected_routes[0]["target_id"] == _PROCEDURE_ID
            cur.execute(
                """
                SELECT origin, status, node_version, proposed_change, note
                FROM app.context_review_requests
                WHERE project_id = %s AND node_id = %s
                """,
                (_PROJECT_ID, _PROCEDURE_ID),
            )
            requests = cur.fetchall()
            assert len(requests) == 1
            origin, status, version, change, note = requests[0]
            assert (origin, status, version) == ("agent", "open", 1)
            assert change == {
                "kind": "business_link",
                "taxonomy_type": "business_domain",
                "taxonomy_id": _DOMAIN_ID,
                "relation_type": "applies_to",
            }
            assert "q_45_8_missing_link" in note
    finally:
        _clean(conn)
