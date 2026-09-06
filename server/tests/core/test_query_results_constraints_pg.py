"""Story 50.1 -- the analytical invariants, proved against a real Postgres.

WHY THESE CANNOT BE UNIT TESTS. Every property below is enforced by a CHECK, a
UNIQUE, a composite foreign key or a trigger. A mocked cursor accepts all of them
happily, which is precisely how a schema promise becomes a comment. Migration 151
puts the guarantees in the database on purpose, so the proof has to go there too.

These skip when `TEST_POSTGRES_DSN` is unset. That is deliberate: they INSERT, and
this repository has already paid once for a test suite that wrote its fixtures
into the production database. The conftest guard refuses a remote database that
has not been explicitly declared disposable.

Every test rolls back. Nothing here is left behind even on a disposable database.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from ulid import ULID

pytestmark = pytest.mark.usefixtures("live_postgres")


def _seed_open_attempt(conn):
    suffix = uuid.uuid4().hex[:12]
    org_id = f"org_aip_{suffix}"
    project_id = f"proj_aip_{suffix}"
    view_id = f"sv_{ULID()}"
    version_id = f"svv_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, status, created_by) "
            "VALUES (%s, 'AI Path Result fixture', %s, 'active', 'test')",
            (org_id, org_id.replace("_", "-")),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
            "VALUES (%s, %s, 'AI Path Result fixture', %s, 'test')",
            (project_id, org_id, project_id.replace("_", "-")),
        )
        cur.execute(
            "INSERT INTO app.semantic_views (id, project_id, name, created_by) "
            "VALUES (%s, %s, 'ai_path_fixture', 'test')",
            (view_id, project_id),
        )
        cur.execute(
            """
            INSERT INTO app.semantic_view_versions
                (id, view_id, project_id, version_number, status, name, label,
                 dependency_fingerprint, content_hash, created_by)
            VALUES (%s, %s, %s, 1, 'published', 'ai_path_fixture', 'AI Path fixture',
                    %s, %s, 'test')
            """,
            (version_id, view_id, project_id, "a" * 64, "a" * 64),
        )
        query_spec_id = f"qs_aip_{suffix}"
        version_ref = f"qsv_aip_{suffix}"
        attempt_id = f"qea_aip_{suffix}"
        result_id = f"qr_aip_{suffix}"
        cur.execute(
            "INSERT INTO app.query_specs "
            "(id, org_id, project_id, semantic_view_id, created_by) "
            "VALUES (%s, %s, %s, %s, 'test')",
            (query_spec_id, org_id, project_id, view_id),
        )
        cur.execute(
            """
            INSERT INTO app.query_spec_versions
                (id, query_spec_id, org_id, project_id, version_number, semantic_view_id,
                 semantic_view_version_id, spec, content_hash, created_by)
            VALUES (%s, %s, %s, %s, 1, %s, %s, '{}'::jsonb, %s, 'test')
            """,
            (version_ref, query_spec_id, org_id, project_id, view_id, version_id, "a" * 64),
        )
        cur.execute(
            """
            INSERT INTO app.query_execution_attempts
                (id, org_id, project_id, query_spec_version_id, result_id, requested_by)
            VALUES (%s, %s, %s, %s, %s, 'test')
            """,
            (attempt_id, org_id, project_id, version_ref, result_id),
        )
    return {
        "org_id": str(org_id),
        "project_id": str(project_id),
        "attempt_id": attempt_id,
        "result_id": result_id,
        "query_spec_version_id": version_ref,
    }


def test_result_path_step_and_finalization_are_one_real_postgres_claim(live_postgres):
    from core.ai_path_recorder import RESULT_EXECUTION_TOOL_NAME, record_result_execution
    from core.query_execution import terminalize

    attempt = _seed_open_attempt(live_postgres)

    def execute(path_id):
        return terminalize(
            live_postgres,
            attempt=attempt,
            org_id=attempt["org_id"],
            project_id=attempt["project_id"],
            outcome="empty",
            started_at=datetime.now(timezone.utc),
            manifest={"reason": "no rows matched"},
            ai_path_id=path_id,
        )

    result = record_result_execution(
        live_postgres,
        org_id=attempt["org_id"],
        project_id=attempt["project_id"],
        actor="test",
        tool_name=RESULT_EXECUTION_TOOL_NAME,
        execute=execute,
    )

    with live_postgres.cursor() as cur:
        cur.execute(
            """
            SELECT r.ai_path_id, r.ai_path_absent_literal, p.lifecycle, p.outcome,
                   s.ordinal, s.tool_name, s.outcome
            FROM app.query_results r
            JOIN app.ai_paths p
              ON p.id = r.ai_path_id AND p.org_id = r.org_id AND p.project_id = r.project_id
            JOIN app.ai_path_steps s
              ON s.path_id = p.id AND s.org_id = p.org_id AND s.project_id = p.project_id
            WHERE r.id = %s
            """,
            (attempt["result_id"],),
        )
        row = cur.fetchone()
    assert row == (
        result["ai_path"],
        None,
        "finalized",
        "succeeded",
        0,
        RESULT_EXECUTION_TOOL_NAME,
        "succeeded",
    )
    live_postgres.rollback()


def test_infrastructure_failure_rolls_back_both_result_and_open_path(live_postgres):
    from core.ai_path_recorder import RESULT_EXECUTION_TOOL_NAME, record_result_execution
    from core.query_execution import terminalize

    attempt = _seed_open_attempt(live_postgres)
    opened_path = []

    def execute(path_id):
        opened_path.append(path_id)
        terminalize(
            live_postgres,
            attempt=attempt,
            org_id=attempt["org_id"],
            project_id=attempt["project_id"],
            outcome="empty",
            started_at=datetime.now(timezone.utc),
            manifest={"reason": "no rows matched"},
            ai_path_id=path_id,
        )
        raise RuntimeError("ToolResult composition failed")

    with pytest.raises(RuntimeError, match="ToolResult composition failed"):
        record_result_execution(
            live_postgres,
            org_id=attempt["org_id"],
            project_id=attempt["project_id"],
            actor="test",
            tool_name=RESULT_EXECUTION_TOOL_NAME,
            execute=execute,
        )
    live_postgres.rollback()

    with live_postgres.cursor() as cur:
        cur.execute("SELECT count(*) FROM app.query_results WHERE id = %s", (attempt["result_id"],))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM app.ai_paths WHERE id = %s", (opened_path[0],))
        assert cur.fetchone()[0] == 0
    live_postgres.rollback()


def _seed(conn):
    """Create one Query Spec version + accepted attempt, inside the caller's tx.

    Reuses whatever published Semantic View version the database already has,
    because the composite foreign key on `query_spec_versions` refuses anything
    else -- which is itself part of what is being proved.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT v.id, v.view_id, v.project_id, p.org_id
            FROM app.semantic_view_versions v
            JOIN app.projects p ON p.id = v.project_id
            LIMIT 1
            """
        )
        row = cur.fetchone()
        if row is None:
            pytest.skip("no Semantic View version in this database to bind a Query Spec to")
        version_id, view_id, project_id, org_id = row

        cur.execute(
            """
            INSERT INTO app.query_specs (id, org_id, project_id, semantic_view_id, created_by)
            VALUES ('qs_T', %s, %s, %s, 'test')
            """,
            (org_id, project_id, view_id),
        )
        cur.execute(
            """
            INSERT INTO app.query_spec_versions
                (id, query_spec_id, org_id, project_id, version_number, semantic_view_id,
                 semantic_view_version_id, spec, content_hash, created_by)
            VALUES ('qsv_T', 'qs_T', %s, %s, 1, %s, %s, '{}'::jsonb, %s, 'test')
            """,
            (org_id, project_id, view_id, version_id, "a" * 64),
        )
        cur.execute(
            """
            INSERT INTO app.query_execution_attempts
                (id, org_id, project_id, query_spec_version_id, result_id, requested_by)
            VALUES ('qea_T', %s, %s, 'qsv_T', 'qr_T', 'test')
            """,
            (org_id, project_id),
        )
        cur.execute(
            """
            INSERT INTO app.query_results
                (id, org_id, project_id, attempt_id, query_spec_version_id, outcome,
                 ai_path_absent_literal, content_hash, started_at, ended_at)
            VALUES ('qr_T', %s, %s, 'qea_T', 'qsv_T', 'success', 'No AI path', %s, NOW(), NOW())
            """,
            (org_id, project_id, "b" * 64),
        )
    return org_id, project_id


def test_a_result_cannot_be_updated_refresh_creates_a_new_one(live_postgres):
    org_id, project_id = _seed(live_postgres)
    with live_postgres.cursor() as cur, pytest.raises(Exception):
        cur.execute("UPDATE app.query_results SET outcome = 'empty' WHERE id = 'qr_T'")
    live_postgres.rollback()


def test_one_attempt_can_only_ever_have_one_result(live_postgres):
    org_id, project_id = _seed(live_postgres)
    with live_postgres.cursor() as cur, pytest.raises(Exception):
        cur.execute(
            """
            INSERT INTO app.query_results
                (id, org_id, project_id, attempt_id, query_spec_version_id, outcome,
                 ai_path_absent_literal, content_hash, started_at, ended_at)
            VALUES ('qr_T2', %s, %s, 'qea_T', 'qsv_T', 'success', 'No AI path', %s, NOW(), NOW())
            """,
            (org_id, project_id, "c" * 64),
        )
    live_postgres.rollback()


def test_a_result_must_name_an_ai_path_or_the_exact_literal(live_postgres):
    """AC7 has no third state: not null, not 'deferred', not an empty string."""
    org_id, project_id = _seed(live_postgres)
    for literal in (None, "deferred", "", "no ai path"):
        with live_postgres.cursor() as cur, pytest.raises(Exception):
            cur.execute(
                """
                INSERT INTO app.query_results
                    (id, org_id, project_id, attempt_id, query_spec_version_id, outcome,
                     ai_path_absent_literal, content_hash, started_at, ended_at)
                VALUES ('qr_T3', %s, %s, 'qea_T', 'qsv_T', 'success', %s, %s, NOW(), NOW())
                """,
                (org_id, project_id, literal, "d" * 64),
            )
        live_postgres.rollback()


def test_a_non_success_result_cannot_claim_rows(live_postgres):
    org_id, project_id = _seed(live_postgres)
    with live_postgres.cursor() as cur, pytest.raises(Exception):
        cur.execute(
            """
            INSERT INTO app.query_results
                (id, org_id, project_id, attempt_id, query_spec_version_id, outcome,
                 ai_path_absent_literal, content_hash, row_count, started_at, ended_at)
            VALUES ('qr_T4', %s, %s, 'qea_T', 'qsv_T', 'empty', 'No AI path', %s, 5, NOW(), NOW())
            """,
            (org_id, project_id, "e" * 64),
        )
    live_postgres.rollback()


def test_a_query_spec_version_cannot_point_at_another_projects_semantic_view(live_postgres):
    """The composite scoped foreign key, not an application check (AC10)."""
    org_id, project_id = _seed(live_postgres)
    with live_postgres.cursor() as cur, pytest.raises(Exception):
        cur.execute(
            """
            INSERT INTO app.query_spec_versions
                (id, query_spec_id, org_id, project_id, version_number, semantic_view_id,
                 semantic_view_version_id, spec, content_hash, predecessor_version_id, created_by)
            VALUES ('qsv_T2', 'qs_T', %s, %s, 2, 'sv_foreign', 'svv_foreign',
                    '{}'::jsonb, %s, 'qsv_T', 'test')
            """,
            (org_id, project_id, "f" * 64),
        )
    live_postgres.rollback()


def test_a_revision_must_keep_its_lineage(live_postgres):
    """Version > 1 without a predecessor is indistinguishable from a fresh intent."""
    org_id, project_id = _seed(live_postgres)
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT semantic_view_id, semantic_view_version_id FROM app.query_spec_versions "
            "WHERE id = 'qsv_T'"
        )
        view_id, version_id = cur.fetchone()
    with live_postgres.cursor() as cur, pytest.raises(Exception):
        cur.execute(
            """
            INSERT INTO app.query_spec_versions
                (id, query_spec_id, org_id, project_id, version_number, semantic_view_id,
                 semantic_view_version_id, spec, content_hash, created_by)
            VALUES ('qsv_T3', 'qs_T', %s, %s, 2, %s, %s, '{}'::jsonb, %s, 'test')
            """,
            (org_id, project_id, view_id, version_id, "0" * 64),
        )
    live_postgres.rollback()


def test_a_terminal_attempt_cannot_be_reopened(live_postgres):
    org_id, project_id = _seed(live_postgres)
    with live_postgres.cursor() as cur:
        cur.execute(
            "UPDATE app.query_execution_attempts SET state='terminal', terminal_at=NOW() "
            "WHERE id='qea_T'"
        )
    with live_postgres.cursor() as cur, pytest.raises(Exception):
        cur.execute("UPDATE app.query_execution_attempts SET state='running' WHERE id='qea_T'")
    live_postgres.rollback()
