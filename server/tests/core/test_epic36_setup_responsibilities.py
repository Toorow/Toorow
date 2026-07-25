"""Story 36.6 responsibility, reconciliation and handoff contract tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def test_migration_persists_constrained_tasks_and_hashed_one_time_handoffs():
    sql = Path("infra/nango/migrations/065_setup_responsibilities.sql").read_text()
    for fragment in (
        "app.setup_journeys",
        "app.setup_tasks",
        "app.setup_handoffs",
        "app.setup_handoff_sessions",
        "invited_operator",
        "toorow_admin",
        "credential_owner",
        "source_admin",
        "host_admin",
        "bearer_hash",
        "superseded_by",
        "return_condition",
        "reminder_policy",
    ):
        assert fragment in sql
    assert "raw_bearer" not in sql
    assert "provider_token" not in sql


def test_initial_tasks_keep_actor_classes_distinct_and_complete_contract():
    from core.setup_responsibilities import derive_initial_tasks

    tasks = derive_initial_tasks(
        operator_identity="camille@example.com",
        toorow_admin_identity="nadia@example.com",
        credential_owner_identity="louis@example.com",
        host_admin_identity="ines@example.com",
        project_id="proj-1",
        accepted_at=datetime(2026, 7, 22, 8, 14, tzinfo=timezone.utc),
    )

    assert [task["actor_type"] for task in tasks] == [
        "toorow_admin",
        "invited_operator",
        "credential_owner",
        "invited_operator",
        "host_admin",
    ]
    required = {
        "owner",
        "state",
        "expires_at",
        "handoff_method",
        "reminder_policy",
        "return_condition",
        "return_path",
        "safe_scope",
    }
    assert all(required <= task.keys() for task in tasks)
    assert tasks[2]["state"] == "waiting"
    assert tasks[2]["safe_scope"] == {"project_id": "proj-1", "action": "authorize_source"}
    assert tasks[0]["title"] == "Invitation acceptée"
    assert tasks[1]["title"] == "Accès au projet confirmé"


def test_org_only_invitation_derives_an_accessible_org_scoped_journey():
    from core.setup_responsibilities import derive_initial_tasks

    tasks = derive_initial_tasks(
        operator_identity="camille@example.com",
        toorow_admin_identity="nadia@example.com",
        credential_owner_identity=None,
        host_admin_identity=None,
        org_id="org-1",
        accepted_at=datetime(2026, 7, 22, 8, 14, tzinfo=timezone.utc),
    )

    assert tasks[1]["step_key"] == "organization_access"
    assert tasks[1]["return_condition"] == {
        "kind": "organization_access",
        "resource_id": "org-1",
    }
    assert tasks[2]["safe_scope"] == {"org_id": "org-1", "action": "authorize_source"}

def test_reconciliation_uses_allowlisted_server_evidence_not_client_completion():
    from core.setup_responsibilities import reconcile_task_state

    condition = {"kind": "source_authorized", "resource_id": "connection-1"}
    assert (
        reconcile_task_state(
            current_state="waiting",
            return_condition=condition,
            server_evidence={"source_authorized": {"connection-1": True}},
            client_payload={"completed": False},
        )
        == "completed"
    )
    assert (
        reconcile_task_state(
            current_state="waiting",
            return_condition=condition,
            server_evidence={},
            client_payload={"completed": True, "state": "completed"},
        )
        == "waiting"
    )
    with pytest.raises(ValueError):
        reconcile_task_state(
            current_state="waiting",
            return_condition={"kind": "arbitrary_sql", "resource_id": "x"},
            server_evidence={},
            client_payload={},
        )


def test_safe_projection_is_recursive_secret_free_and_expiry_is_server_derived():
    from core.setup_responsibilities import project_safe_task

    now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
    projected = project_safe_task(
        {
            "task_id": "task_12345678",
            "step_key": "source_authorization",
            "title": "Autoriser la source",
            "actor_type": "credential_owner",
            "owner": "Louis",
            "state": "waiting",
            "expires_at": now - timedelta(seconds=1),
            "handoff_method": "link",
            "reminder_policy": {"mode": "none"},
            "return_condition": {"kind": "source_authorized", "resource_id": "conn-1"},
            "return_path": "/onboarding/responsibilities",
            "safe_scope": {"action": "authorize_source", "project_id": "proj-1"},
            "bearer_hash": "a" * 64,
            "provider_token": "never",
        },
        now=now,
    )
    assert projected["state"] == "expired"
    assert projected["safe_id_suffix"] == "345678"
    dumped = json.dumps(projected)
    assert "bearer" not in dumped
    assert "token" not in dumped
    assert projected["reminder"]["label"] == "Aucun rappel automatique"


def test_handoff_is_fragment_only_and_bearer_never_reaches_sql_or_outbox(monkeypatch):
    from core import operations
    from core import setup_responsibilities as setup

    monkeypatch.setenv("TOOROW_HANDOFF_PEPPER", "p" * 32)
    monkeypatch.setenv("TOOROW_HANDOFF_ORIGIN", "https://console.toorow.test")
    cur = MagicMock()
    cur.fetchone.side_effect = [
        (
            "task-1",
            "journey-1",
            "org-1",
            "credential_owner",
            "louis@example.com",
            "waiting",
            {"action": "authorize_source", "project_id": "proj-1"},
            {"kind": "source_authorized", "resource_id": "conn-1"},
            "/onboarding/responsibilities",
        )
    ]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    captured = {}

    def execute(operation_conn, spec, *, mutation):
        changed = mutation(operation_conn, "op-1")
        captured["spec"] = spec
        captured["changed"] = changed
        return operations.OperationResult(
            "op-1", "succeeded", changed.result, "audit-1", "outbox-1", False
        )

    monkeypatch.setattr(setup, "execute_operation", execute)
    result = setup.prepare_handoff(
        conn,
        task_id="task-1",
        actor="camille@example.com",
        expires_in_hours=48,
        idempotency_key="handoff-1",
        host_context={"host": "rest"},
        trace_id=None,
    )

    assert result.delivery_url.startswith("https://console.toorow.test/handoff#handoff=")
    bearer = result.delivery_url.split("=", 1)[1]
    evidence = repr(cur.execute.call_args_list) + repr(captured)
    assert bearer not in evidence
    assert captured["changed"].outbox_payload["delivery_kind"] == "setup_handoff"
    assert "safe_scope" not in captured["changed"].outbox_payload
    assert "superseded_by=%s" in evidence


def test_reassignment_supersedes_active_authority_inside_operation(monkeypatch):
    from core import operations
    from core import setup_responsibilities as setup

    cur = MagicMock()
    cur.rowcount = 1
    cur.fetchall.return_value = [("handoff-old",)]
    cur.fetchone.return_value = (
        "task-1",
        "journey-1",
        "org-1",
        "credential_owner",
        "old@example.com",
        "waiting",
        3,
    )
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    def execute(operation_conn, spec, *, mutation):
        changed = mutation(operation_conn, "op-reassign")
        return operations.OperationResult(
            "op-reassign", "succeeded", changed.result, "audit-2", "outbox-2", False
        )

    monkeypatch.setattr(setup, "execute_operation", execute)
    result = setup.reassign_task(
        conn,
        task_id="task-1",
        actor="admin@example.com",
        actor_type="source_admin",
        assigned_identity="new@example.com",
        idempotency_key="reassign-1",
        host_context={"host": "rest"},
        trace_id=None,
    )

    sql = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "FOR UPDATE" in sql
    assert "setup_handoffs" in sql
    assert "setup_handoff_sessions" in sql
    assert result["actor_type"] == "source_admin"
    assert result["version"] == 4


def test_exchange_consumes_fragment_bearer_into_one_minimum_session(monkeypatch):
    from core import setup_responsibilities as setup

    monkeypatch.setenv("TOOROW_HANDOFF_PEPPER", "p" * 32)
    cur = MagicMock()
    cur.fetchone.return_value = (
        "handoff-1",
        "task-1",
        "authorize_source",
        "credential_owner",
        {"action": "authorize_source", "project_id": "proj-1"},
        "/onboarding/responsibilities",
        datetime.now(timezone.utc) + timedelta(hours=1),
        "created",
        None,  # assigned_identity_hash: anonymous, open handoff
    )
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    result = setup.exchange_handoff(conn, bearer="h" * 48)

    assert result.purpose == "authorize_source"
    assert result.safe_scope == {"action": "authorize_source", "project_id": "proj-1"}
    sql = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "setup_handoff_sessions" in sql
    assert "state='delivered'" in sql
    assert "session_hash" in sql
    assert result.session_value not in repr(cur.execute.call_args_list)


def _bound_handoff_conn(monkeypatch, bound_identity):
    """A handoff row bound to ``bound_identity`` (its peppered hash)."""
    from core import setup_responsibilities as setup

    monkeypatch.setenv("TOOROW_HANDOFF_PEPPER", "p" * 32)
    identity_hash = setup._keyed_hash(bound_identity.strip().lower(), "handoff-identity")
    cur = MagicMock()
    cur.fetchone.return_value = (
        "handoff-1",
        "task-1",
        "authorize_source",
        "credential_owner",
        {"action": "authorize_source", "project_id": "proj-1"},
        "/onboarding/responsibilities",
        datetime.now(timezone.utc) + timedelta(hours=1),
        "created",
        identity_hash,
    )
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    return conn


def test_bound_handoff_rejects_non_matching_or_absent_identity(monkeypatch):
    """A leaked/forwarded bound handoff link cannot be burned by another party."""
    from core import setup_responsibilities as setup

    # Absent identity (anonymous caller) cannot consume a bound handoff.
    conn = _bound_handoff_conn(monkeypatch, "owner@corp.example")
    with pytest.raises(setup.SetupUnavailable):
        setup.exchange_handoff(conn, bearer="h" * 48, presented_identity=None)

    # A different authenticated identity is rejected without disclosing scope.
    conn = _bound_handoff_conn(monkeypatch, "owner@corp.example")
    with pytest.raises(setup.SetupUnavailable):
        setup.exchange_handoff(
            conn, bearer="h" * 48, presented_identity="attacker@corp.example"
        )
    # The one-time bearer is not consumed on a rejected exchange.
    cur = conn.cursor.return_value.__enter__.return_value
    sql = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "state='delivered'" not in sql


def test_bound_handoff_accepts_the_matching_identity(monkeypatch):
    from core import setup_responsibilities as setup

    conn = _bound_handoff_conn(monkeypatch, "Owner@Corp.Example")
    # Case-insensitive normalized match succeeds.
    result = setup.exchange_handoff(
        conn, bearer="h" * 48, presented_identity="owner@corp.example"
    )
    assert result.purpose == "authorize_source"
