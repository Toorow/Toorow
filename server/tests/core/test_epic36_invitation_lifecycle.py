"""Story 36.5 invitation lifecycle tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest


def _conn(*rows, all_rows=()):
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.side_effect = list(rows)
    cur.fetchall.return_value = list(all_rows)
    cur.rowcount = 1
    conn.cursor.return_value = cur
    return conn, cur


def test_safe_lifecycle_projection_derives_expiry_without_secret_material():
    from core.invitations import list_safe_invitations

    now = datetime.now(timezone.utc)
    conn, cur = _conn(
        all_rows=[
            (
                "invite-1",
                "member",
                [{"scope_type": "project", "scope_id": "proj-1", "capability": "view"}],
                "owner@example.com",
                "policy-v1",
                "pending",
                now - timedelta(minutes=1),
                "smtp_accepted",
                "a" * 64,
                now - timedelta(days=1),
                None,
                None,
                None,
            )
        ]
    )

    rows = list_safe_invitations(conn, org_id="org-1", now=now)

    assert rows[0]["state"] == "expired"
    assert rows[0]["explicit_grants"][0]["scope_id"] == "proj-1"
    serialized = repr(rows)
    assert "identity_hash" not in serialized
    assert "bearer" not in serialized
    assert "session" not in serialized
    assert "smtp_accepted" in serialized
    sql = cur.execute.call_args.args[0]
    assert "bearer_hash" not in sql
    assert "invited_identity_hash" not in sql


def test_revoke_is_operation_atomic_and_invalidates_exchange(monkeypatch):
    from core import invitations, operations

    conn, cur = _conn(
        (
            "invite-1",
            "org-1",
            "delivered",
            datetime.now(timezone.utc) + timedelta(hours=1),
            None,
            None,
        )
    )
    captured = {}

    def execute(operation_conn, spec, *, mutation):
        captured["spec"] = spec
        changed = mutation(operation_conn, "op-revoke")
        captured["changed"] = changed
        return operations.OperationResult(
            "op-revoke", "succeeded", changed.result, "audit-1", "outbox-1", False
        )

    monkeypatch.setattr(invitations, "execute_operation", execute)
    result = invitations.transition_invitation(
        conn,
        invitation_id="invite-1",
        actor="owner@example.com",
        transition="revoke",
        idempotency_key="revoke-1",
        host_context={"host": "rest"},
        trace_id=None,
    )

    assert result.state == "revoked"
    assert captured["spec"].command_type == "invitation.revoke"
    assert captured["changed"].outbox_payload == {
        "invitation_id": "invite-1",
        "transition": "revoked",
    }
    sql = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "FOR UPDATE" in sql
    assert "SET state = 'revoked'" in sql
    assert "invitation_exchange_sessions" in sql
    conn.commit.assert_not_called()


def test_expiry_before_deadline_is_rejected_without_operation(monkeypatch):
    from core import invitations

    conn, _ = _conn(
        (
            "invite-1",
            "org-1",
            "pending",
            datetime.now(timezone.utc) + timedelta(hours=1),
            None,
            None,
        )
    )
    execute = MagicMock()
    monkeypatch.setattr(invitations, "execute_operation", execute)

    with pytest.raises(invitations.InvitationLifecycleConflict):
        invitations.transition_invitation(
            conn,
            invitation_id="invite-1",
            actor="owner@example.com",
            transition="expire",
            idempotency_key="expire-1",
            host_context={"host": "rest"},
            trace_id=None,
        )
    execute.assert_not_called()


def test_accepted_invitation_cannot_be_revoked_or_resent(monkeypatch):
    from core import invitations

    accepted = (
        "invite-1",
        "org-1",
        "accepted",
        datetime.now(timezone.utc) + timedelta(hours=1),
        None,
        datetime.now(timezone.utc),
    )
    conn, _ = _conn(accepted)
    with pytest.raises(invitations.InvitationLifecycleConflict):
        invitations.transition_invitation(
            conn,
            invitation_id="invite-1",
            actor="owner@example.com",
            transition="revoke",
            idempotency_key="revoke-accepted",
            host_context={"host": "rest"},
            trace_id=None,
        )


def test_resend_supersedes_old_and_returns_only_new_handoff_once(monkeypatch):
    from core import invitations, operations

    monkeypatch.setenv("TOOROW_INVITATION_PEPPER", "p" * 32)
    monkeypatch.setenv("TOOROW_INVITATION_ORIGIN", "https://console.toorow.test")
    conn, cur = _conn(
        (
            "invite-old",
            "b" * 64,
            "org-1",
            "member",
            [{"scope_type": "project", "scope_id": "proj-1", "capability": "view"}],
            "policy-v1",
            "delivery_failed",
            datetime.now(timezone.utc) - timedelta(hours=1),
            None,
            None,
        )
    )
    captured = {}

    def execute(operation_conn, spec, *, mutation):
        changed = mutation(operation_conn, "op-resend")
        captured["changed"] = changed
        return operations.OperationResult(
            "op-resend", "succeeded", changed.result, "audit-1", "outbox-1", False
        )

    monkeypatch.setattr(invitations, "execute_operation", execute)
    result = invitations.resend_invitation(
        conn,
        invitation_id="invite-old",
        actor="owner@example.com",
        expires_in_hours=48,
        idempotency_key="resend-1",
        host_context={"host": "rest"},
        trace_id=None,
    )

    assert result.delivery_url.startswith("https://console.toorow.test/invite#invite=")
    raw = result.delivery_url.split("=", 1)[1]
    assert raw not in repr(cur.execute.call_args_list)
    assert captured["changed"].outbox_payload["delivery_kind"] == "invitation_email"
    sql = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "superseded_by" in sql
    assert "INSERT INTO app.invitations" in sql
    assert "invitation_exchange_sessions" in sql


def test_migration_064_preserves_accepted_and_constrains_lifecycle():
    from pathlib import Path

    sql = Path("infra/nango/migrations/064_invitation_lifecycle.sql").read_text()
    assert "accepted invitation is immutable" in sql
    assert "pending', 'delivered', 'delivery_failed" in sql
    assert "superseded_by" in sql
    assert "raw_bearer" not in sql


def test_resend_replay_returns_replacement_without_new_handoff(monkeypatch):
    from core import invitations

    conn, cur = _conn(
        (
            "invite-old",
            "b" * 64,
            "org-1",
            "member",
            [],
            "policy-v1",
            "revoked",
            datetime.now(timezone.utc),
            "invite-new",
            None,
        ),
        (
            "invite-new",
            "pending",
            datetime.now(timezone.utc) + timedelta(hours=2),
            "op-resend",
            "audit-resend",
        ),
    )
    execute = MagicMock()
    monkeypatch.setattr(invitations, "execute_operation", execute)

    result = invitations.resend_invitation(
        conn,
        invitation_id="invite-old",
        actor="owner@example.com",
        expires_in_hours=48,
        idempotency_key="resend-retry",
        host_context={"host": "rest"},
        trace_id=None,
    )

    assert result.invitation_id == "invite-new"
    assert result.delivery_url is None
    assert result.replayed is True
    execute.assert_not_called()
    assert "INSERT INTO app.invitations" not in " ".join(
        call.args[0] for call in cur.execute.call_args_list
    )
