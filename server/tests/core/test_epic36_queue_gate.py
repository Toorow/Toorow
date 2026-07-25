"""Story 36.1 provider-use authorization seam tests."""

from contextlib import nullcontext
from unittest.mock import MagicMock


def test_topology_guard_revalidates_exact_account_before_enqueue(monkeypatch):
    from core import account_topology, db, project_access, queue

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setenv("TOOROW_EPIC36_PRODUCTION_ENABLED", "true")
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.side_effect = [("acct-1",), ("org-1",)]
    conn.cursor.return_value = cur

    monkeypatch.setattr(db, "get_connection", lambda: nullcontext(conn))
    monkeypatch.setattr(db, "set_local_access_context", MagicMock())
    monkeypatch.setattr(
        queue,
        "_resolve_connection_ref",
        lambda _conn, _id: {"provider": "google-ads", "project_id": "proj-1"},
    )
    monkeypatch.setattr(account_topology, "get_topology_for_provider", lambda _p: {})
    monkeypatch.setattr(account_topology, "has_ready_scope", lambda _id, _conn: True)
    resolver = MagicMock(
        return_value=project_access.AccessDecision(False, "account_exposure_required")
    )
    monkeypatch.setattr(project_access, "resolve_provider_account_access", resolver)

    refusal = queue._topology_scope_refusal(
        "conn-1", requested_by="member-1", datastream_id=None
    )

    assert refusal == {
        "state": "refused",
        "code": "access_denied",
        "message": "connection/account scope is not authorized for this resource",
    }
    resolver.assert_called_once_with(
        "member-1",
        conn,
        credential_id="conn-1",
        external_account_id="acct-1",
        beneficiary_org_id="org-1",
        datastream_id=None,
        project_id="proj-1",
    )


def test_topology_guard_fails_closed_when_strict_gate_cannot_resolve(monkeypatch):
    from core import db, queue

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setenv("TOOROW_EPIC36_PRODUCTION_ENABLED", "true")

    def unavailable():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(db, "get_connection", unavailable)
    refusal = queue._topology_scope_refusal(
        "conn-1", requested_by="member-1", datastream_id="flux-1"
    )
    assert refusal is not None
    assert refusal["code"] == "access_denied"
