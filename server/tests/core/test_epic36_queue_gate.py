"""Story 36.1 provider-use authorization seam tests."""

from contextlib import nullcontext
from unittest.mock import MagicMock


def test_topology_guard_revalidates_exact_account_before_enqueue(monkeypatch):
    from core import account_topology, db, project_access, queue

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setenv("TOOROW_EPIC36_PRODUCTION_ENABLED", "true")
    # THE CURSOR ANSWERS BY QUERY, NOT BY POSITION. `side_effect=[...]` pinned
    # this test to the exact NUMBER and ORDER of reads the guard made the day it
    # was written. A read added since exhausted the list, `StopIteration` was
    # swallowed by `_resolve_selected_account`'s "best effort, never fails a
    # pull" `except` -- the empty `resolve_selected_account_failed:` in the log
    # is that exception's blank message -- and the guard short-circuited on
    # `account_not_selected` before ever reaching the access resolver this test
    # exists to exercise. A red about nothing the test claims to defend.
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False

    def _answer():
        last = cur.execute.call_args_list[-1:]
        sql = " ".join(str(call.args[0]) for call in last)
        if "connection_account_scope" in sql or "credential_accounts" in sql:
            return ("acct-1",)
        return ("org-1",)

    cur.fetchone.side_effect = _answer
    # 2026-08-30: the credential-wide fallback COUNTS its candidates (`fetchall`)
    # instead of taking the freshest one, so a fixture has to say how many there
    # are. One ready account of this connector -- no ambiguity, and this subject
    # is about the access decision that follows, not about the count.
    cur.fetchall.return_value = [("acct-1", "google-ads")]
    conn.cursor.return_value = cur

    monkeypatch.setattr(db, "get_connection", lambda: nullcontext(conn))
    monkeypatch.setattr(db, "set_local_access_context", MagicMock())
    monkeypatch.setattr(
        queue,
        "_resolve_connection_ref",
        lambda _conn, _id: {"provider": "google-ads", "project_id": "proj-1"},
    )
    monkeypatch.setattr(account_topology, "get_topology_for_provider", lambda _p: {})
    # Account-exact since migration 211: the guard resolves WHICH account this
    # enqueue is about (the Datastream's, or the credential's verified one when
    # it names none) and asks whether THAT account is ready.
    monkeypatch.setattr(account_topology, "is_account_ready", lambda _id, _acct, _conn: True)
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
