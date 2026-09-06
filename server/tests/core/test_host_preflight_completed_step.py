"""A COMPLETED host-connection step still starts a preflight (2026-09-04).

The preflight is prepared on the organization's `host_connection` setup step and
the first bind completes that step. Measured on the harness organization: its
Insights bind had completed the step, and its rebinding with `operations` was
refused 409 by every door of the product -- as every customer's second host, or
rebinding with another profile, would have been. The step is the organization's
FIRST host connection, not its only one. Offline, MagicMock-style like
`test_epic36_host_preflight.py`.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _cur(*fetchone_rows):
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.side_effect = list(fetchone_rows)
    cur.fetchall.return_value = []
    cur.rowcount = 1
    return cur


def _conn_with(*curs):
    conn = MagicMock()
    conn.cursor.side_effect = list(curs)
    return conn


def _stub_operation(monkeypatch, module):
    from core import operations

    def execute(operation_conn, spec, *, mutation):
        changed = mutation(operation_conn, "op-1")
        return operations.OperationResult(
            "op-1", "succeeded", changed.result, "audit-1", "outbox-1", False
        )

    monkeypatch.setattr(module, "execute_operation", execute)


def _task_row(state: str):
    # id, journey_id, org_id, state, step_key, return_condition
    return (
        "task-h",
        "journey-1",
        "org-1",
        state,
        "host_connection",
        {"kind": "host_connected", "resource_id": "proj-1"},
    )


def _prepare(hp, conn, key: str):
    return hp.preflight_host(
        conn,
        host_key="host_app_ui_v1",
        task_id="task-h",
        org_id="org-1",
        project_id="proj-1",
        actor="op@example.com",
        idempotency_key=key,
        host_context={"host": "rest"},
        trace_id=None,
    )


def test_a_completed_host_connection_step_still_starts_a_preflight(monkeypatch):
    from core import host_preflight as hp

    _stub_operation(monkeypatch, hp)
    result = _prepare(hp, _conn_with(_cur(_task_row("completed")), _cur()), "pf-2")

    assert result["state"] == "prepared"
    # The second binding is minted like the first (migration 343).
    assert result["workspace_evidence_hash"]
    assert result["interactive_presence_evidence_hash"]


def test_a_step_that_is_not_there_to_be_resumed_still_refuses(monkeypatch):
    from core import host_preflight as hp

    _stub_operation(monkeypatch, hp)
    with pytest.raises(hp.HostPreflightConflict):
        _prepare(hp, _conn_with(_cur(_task_row("cancelled")), _cur()), "pf-3")
