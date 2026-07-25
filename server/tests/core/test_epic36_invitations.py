"""Story 36.3 scoped single-use invitation tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def test_identity_normalization_and_hashing_never_persist_plaintext(monkeypatch):
    from core.invitations import normalize_invited_identity, prepare_identity_binding

    monkeypatch.setenv("TOOROW_INVITATION_PEPPER", "p" * 32)
    normalized = normalize_invited_identity("  Usér@ExAmPle.COM  ")
    assert normalized == "usér@example.com"
    binding = prepare_identity_binding(normalized)
    assert binding.identity_hash != normalized
    assert len(binding.identity_hash) == 64


def test_fragment_delivery_url_never_places_bearer_in_path_or_query(monkeypatch):
    from core.invitations import build_fragment_delivery_url

    monkeypatch.setenv("TOOROW_INVITATION_ORIGIN", "https://console.toorow.test")
    bearer = "raw-bearer-value"
    url = build_fragment_delivery_url(bearer)
    assert url == f"https://console.toorow.test/invite#invite={bearer}"
    assert "?" not in url
    assert url.split("#", 1)[0].endswith("/invite")


def test_issue_invitation_persists_only_hashes_and_returns_bearer_once(monkeypatch):
    from core import invitations, operations

    monkeypatch.setenv("TOOROW_INVITATION_PEPPER", "p" * 32)
    monkeypatch.setenv("TOOROW_INVITATION_ORIGIN", "https://console.toorow.test")
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    conn.cursor.return_value = cur
    captured = {}

    def execute(operation_conn, spec, *, mutation):
        captured["spec"] = spec
        changed = mutation(operation_conn, "op-1")
        captured["changed"] = changed
        return operations.OperationResult(
            "op-1", "succeeded", changed.result, "audit-1", "outbox-1", False
        )

    monkeypatch.setattr(invitations, "execute_operation", execute)
    result = invitations.issue_invitation(
        conn,
        invited_identity="User@example.com",
        org_id="org-1",
        role="member",
        project_grants=(invitations.InvitationGrant("project", "proj-1", "view"),),
        datastream_grants=(),
        issuer="owner-1",
        expires_in_hours=48,
        policy_version="p1",
        idempotency_key="invite-request-1",
        host_context={"host": "rest"},
        trace_id=None,
    )
    assert result.delivery_url.startswith("https://console.toorow.test/invite#invite=")
    raw = result.delivery_url.split("=", 1)[1]
    sql_params = repr(cur.execute.call_args_list)
    assert raw not in sql_params
    assert "User@example.com" not in sql_params
    assert "User@example.com" not in repr(captured["spec"])
    assert captured["changed"].outbox_payload == {
        "invitation_id": result.invitation_id,
        "delivery_kind": "invitation_email",
    }


def test_replay_does_not_reconstruct_plaintext_bearer(monkeypatch):
    from core import invitations, operations

    monkeypatch.setenv("TOOROW_INVITATION_PEPPER", "p" * 32)
    monkeypatch.setenv("TOOROW_INVITATION_ORIGIN", "https://console.toorow.test")
    monkeypatch.setattr(
        invitations,
        "execute_operation",
        lambda *_a, **_k: operations.OperationResult(
            "op-old",
            "succeeded",
            {"invitation_id": "invite-old", "state": "pending", "expires_at": None},
            "audit-old",
            "outbox-old",
            True,
        ),
    )
    result = invitations.issue_invitation(
        MagicMock(),
        invited_identity="user@example.com",
        org_id="org-1",
        role="viewer",
        project_grants=(),
        datastream_grants=(),
        issuer="owner-1",
        expires_in_hours=24,
        policy_version="p1",
        idempotency_key="same-key",
        host_context={"host": "rest"},
        trace_id=None,
    )
    assert result.replayed is True
    assert result.delivery_url is None


@pytest.mark.parametrize("hours", [0, 169, "24"])
def test_invitation_expiry_is_bounded(hours):
    from core.invitations import InvitationValidationError, validate_invitation_request

    with pytest.raises(InvitationValidationError):
        validate_invitation_request(
            invited_identity="user@example.com",
            role="member",
            project_grants=(),
            datastream_grants=(),
            expires_in_hours=hours,
        )


def test_migration_062_contains_immutable_invitation_contract():
    from pathlib import Path

    sql = Path("infra/nango/migrations/062_scoped_invitations.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS app.invitations" in sql
    assert "invited_identity_hash" in sql
    assert "bearer_hash" in sql
    assert "pending', 'delivered', 'accepted', 'expired', 'revoked', 'delivery_failed" in sql
    assert "raw_bearer" not in sql


def test_migration_066_hardens_immutable_binding_to_bearer_expiry_operation():
    from pathlib import Path

    sql = Path(
        "infra/nango/migrations/066_epic36_invitation_binding_hardening.sql"
    ).read_text()
    assert "CREATE OR REPLACE FUNCTION app.protect_invitation_binding" in sql
    for column in ("bearer_hash", "expires_at", "operation_id"):
        assert f"NEW.{column} IS DISTINCT FROM OLD.{column}" in sql


def test_confirmed_delivery_updates_invitation_and_generic_outbox(monkeypatch):
    from core import invitations, operations

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = ("op-1",)
    conn.cursor.return_value = cur
    generic = MagicMock()
    monkeypatch.setattr(operations, "record_delivery_result", generic)
    invitations.record_delivery_evidence(
        conn,
        event_id="opout-1",
        confirmed=True,
        delivered=True,
        evidence_class="smtp_accepted",
        evidence_hash="a" * 64,
    )
    sql = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "SET state = %s" in sql
    assert "state = 'pending'" in sql
    generic.assert_called_once_with(
        conn,
        event_id="opout-1",
        confirmed=True,
        uncertain=False,
        error_class=None,
    )
