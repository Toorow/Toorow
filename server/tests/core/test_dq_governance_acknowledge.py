"""`Mark as reviewed`, on `app.dq_issues` -- story 59.1, arbitrage 1.

WHY THIS EXISTS AT ALL. Nothing mounted could acknowledge an issue:
`dq_governance.transition_issue` had ZERO application callers (only its own
`def`), the seven `/api/dq/*` routes were unmounted by story 49.4
(`admin_api.py#api/dq`), and the one that carried the word acknowledged
`app.alert_firings.acknowledged_at` -- a different table under the same word,
whose issue identity was PARSED out of an alert message. So the gesture was
either impossible or aimed at the wrong object, and `epic-59:107` still names the
removed route. The removal wins; this is the replacement.

Three facts, and each is one the schema or a refusal enforces:
  * an acknowledgement appends exactly ONE `app.dq_issue_events` row -- the
    history is append-only and nothing is ever rewritten;
  * `suppressed` without an end date is refused, as `ck_dq_issues_suppression`
    refuses it one layer down;
  * an issue outside the Project (or outside the Datastream the route is scoped
    to) is `not_found`, and the two cases are indistinguishable from outside.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, timedelta

import pytest
from ulid import ULID

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("TEST_POSTGRES_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="TEST_POSTGRES_DSN not set -- skipping live Postgres test"
)


@pytest.fixture
def conn():
    with psycopg.connect(DSN) as connection:
        yield connection
        connection.rollback()


@pytest.fixture
def scope(conn):
    """A Project, a Datastream and a run, all fictional and all rolled back."""
    suffix = uuid.uuid4().hex[:12]
    org_id, project_id = f"org_{suffix}", f"proj_{suffix}"
    datastream_id = f"ds_{suffix}"
    plan_id, mapping_id = f"dpv_{suffix}", f"dmv_{suffix}"
    execution_id = f"dse_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) "
            "VALUES (%s, %s, %s, 'owner@example.com')",
            (org_id, f"Org {suffix}", f"org-{suffix}"),
        )
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, created_by, org_id) "
            "VALUES (%s, %s, %s, 'owner@example.com', %s)",
            (project_id, f"Project {suffix}", f"project-{suffix}", org_id),
        )
        cur.execute(
            "INSERT INTO app.datastreams (id, project_id, org_id, name, module_name, enabled) "
            "VALUES (%s, %s, %s, %s, 'example_connector', TRUE)",
            (datastream_id, project_id, org_id, f"Flux {suffix}"),
        )
        cur.execute(
            "INSERT INTO app.project_flux (project_id, flux_id, org_id) VALUES (%s, %s, %s)",
            (project_id, datastream_id, org_id),
        )
        cur.execute(
            """INSERT INTO app.datastream_plan_versions
                 (id, datastream_id, project_id, version_number, contract_version,
                  source_kind, writer_kind, destination_policy, normalized_payload,
                  content_hash, idempotency_key_hash, created_by)
               VALUES (%s, %s, %s, 1, 'v1', 'connector_pull', 'toorow', 'managed_raw',
                       '{}'::jsonb, %s, %s, 'system')""",
            (plan_id, datastream_id, project_id, "a" * 64, "b" * 64),
        )
        cur.execute(
            """INSERT INTO app.datastream_mapping_versions
                 (id, datastream_id, project_id, version_number, mapping_contract_version,
                  source_schema_hash, plan_version_id, content_hash, ossie_spec_version,
                  toorow_extension_version, executable, mapping_payload, ossie_projection,
                  idempotency_key_hash, created_by)
               VALUES (%s, %s, %s, 1, 'v1', %s, %s, %s, '1.0', '1.0', TRUE,
                       '{}'::jsonb, '{}'::jsonb, %s, 'system')""",
            (mapping_id, datastream_id, project_id, "c" * 64, plan_id, "d" * 64, "e" * 64),
        )
        cur.execute(
            """INSERT INTO app.datastream_executions
                 (id, datastream_id, project_id, plan_version_id, mapping_version_id,
                  state, created_by)
               VALUES (%s, %s, %s, %s, %s, 'published', 'system')""",
            (execution_id, datastream_id, project_id, plan_id, mapping_id),
        )
    return {
        "org_id": org_id,
        "project_id": project_id,
        "datastream_id": datastream_id,
        "execution_id": execution_id,
    }


@pytest.fixture
def issue(conn, scope):
    """One open issue on that run, opened by `open_issue` and by nothing else."""
    from core.dq_governance import ensure_monitor, open_issue, publish_version

    head = ensure_monitor(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        name=f"null_rate_{uuid.uuid4().hex[:10]}",
        label="Null rate: example flux",
        target_kind="datastream",
        target_id=scope["datastream_id"],
        actor="system",
    )
    publish_version(
        conn,
        project_id=scope["project_id"],
        monitor_id=head["id"],
        check_profile="null_rate",
        severity="degrading",
        actor="system",
        parameters={"thresholds": {"null_rate": 0.0}},
        window_days=1,
    )
    opened = open_issue(
        conn,
        project_id=scope["project_id"],
        monitor_id=str(head["id"]),
        root_cause_fingerprint="f" * 64,
        severity="degrading",
        actor="system",
        datastream_id=scope["datastream_id"],
        execution_id=scope["execution_id"],
    )
    return str(opened["id"])


def _events(conn, issue_id: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT event_kind FROM app.dq_issue_events WHERE issue_id = %s ORDER BY id",
            (issue_id,),
        )
        return [row[0] for row in cur.fetchall()]


def test_acknowledging_moves_the_issue_and_appends_exactly_one_event(conn, scope, issue):
    from core.dq_issue_rows import transition_run_issue

    before = _events(conn, issue)
    result = transition_run_issue(
        conn,
        project_id=scope["project_id"],
        datastream_id=scope["datastream_id"],
        issue_id=issue,
        event_kind="acknowledged",
        actor="reviewer@example.com",
        reason="reviewed from the run",
    )

    assert result == {"issue_id": issue, "status": "acknowledged"}
    after = _events(conn, issue)
    assert len(after) == len(before) + 1
    assert after[-1] == "acknowledged"


def test_the_returned_status_is_the_one_the_row_holds(conn, scope, issue):
    """Read back, never composed: what the console shows is what the table says."""
    from core.dq_issue_rows import transition_run_issue

    result = transition_run_issue(
        conn,
        project_id=scope["project_id"],
        datastream_id=scope["datastream_id"],
        issue_id=issue,
        event_kind="reopened",
        actor="reviewer@example.com",
    )
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM app.dq_issues WHERE id = %s", (issue,))
        assert cur.fetchone()[0] == result["status"] == "open"


def test_a_suppression_without_an_end_date_is_refused(conn, scope, issue):
    """`ck_dq_issues_suppression` says it too; this says it before the write."""
    from core.dq_issue_rows import InvalidIssueStatus, transition_run_issue

    with pytest.raises(InvalidIssueStatus):
        transition_run_issue(
            conn,
            project_id=scope["project_id"],
            datastream_id=scope["datastream_id"],
            issue_id=issue,
            event_kind="suppressed",
            actor="reviewer@example.com",
        )
    # Nothing was written: the refusal is before the transaction touches anything.
    assert _events(conn, issue) == ["observed"]

    transition_run_issue(
        conn,
        project_id=scope["project_id"],
        datastream_id=scope["datastream_id"],
        issue_id=issue,
        event_kind="suppressed",
        actor="reviewer@example.com",
        suppressed_until=date.today() + timedelta(days=7),
    )
    assert _events(conn, issue)[-1] == "suppressed"


def test_an_event_this_schema_cannot_hold_is_refused(conn, scope, issue):
    from core.dq_issue_rows import InvalidIssueStatus, transition_run_issue

    with pytest.raises(InvalidIssueStatus):
        transition_run_issue(
            conn,
            project_id=scope["project_id"],
            datastream_id=scope["datastream_id"],
            issue_id=issue,
            event_kind="observed",
            actor="reviewer@example.com",
        )


def test_an_issue_of_another_project_is_not_found(conn, scope, issue):
    """Non-disclosing: the same answer as an issue that never existed."""
    from core.dq_issue_rows import IssueNotFound, transition_run_issue

    with pytest.raises(IssueNotFound):
        transition_run_issue(
            conn,
            project_id="proj_EXAMPLE",
            datastream_id=scope["datastream_id"],
            issue_id=issue,
            event_kind="acknowledged",
            actor="reviewer@example.com",
        )
    with pytest.raises(IssueNotFound):
        transition_run_issue(
            conn,
            project_id=scope["project_id"],
            datastream_id=scope["datastream_id"],
            issue_id=f"dqi_{ULID()}",
            event_kind="acknowledged",
            actor="reviewer@example.com",
        )
    assert _events(conn, issue) == ["observed"]


def test_the_acknowledgement_never_touches_alert_firings(conn):
    """The object is `app.dq_issues`. Story 49.4 named the defect; this keeps it closed."""
    from core import dq_issue_rows

    # The STATEMENTS, not the prose: the docstring names the table it refuses,
    # and a text search over the whole source would be red for saying so.
    statements = [
        constant
        for constant in dq_issue_rows.transition_run_issue.__code__.co_consts
        if isinstance(constant, str) and "SELECT" in constant.upper()
    ]
    assert statements and all("app.dq_issues" in text for text in statements)
    assert not any("alert_firings" in text for text in statements)

    # The read takes the same treatment. It used to be pinned to a module
    # constant `_ISSUE_SQL`; the SQL was inlined into `_read_issue` and the
    # constant vanished, so the two assertions below died on AttributeError
    # instead of judging anything -- unnoticed, because this file is pg-gated
    # and had never been run against a live database. Reading the STATEMENTS of
    # the function survives that move, which is why the block above already did.
    read_statements = [
        constant
        for constant in dq_issue_rows._read_issue.__code__.co_consts
        if isinstance(constant, str) and "SELECT" in constant.upper()
    ]
    assert read_statements and all("app.dq_issues" in text for text in read_statements)
    assert not any("alert_firings" in text for text in read_statements)
