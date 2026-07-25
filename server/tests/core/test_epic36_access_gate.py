"""Story 36.1 strict organization and provider-account access tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _conn(*rows):
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.side_effect = list(rows)
    conn.cursor.return_value = cur
    return conn


def test_strict_project_access_denies_zero_grant_admin(monkeypatch):
    from core.project_access import resolve_strict_resource_access

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    conn = _conn(("org_a", "active", "admin", "active"), None)
    decision = resolve_strict_resource_access(
        "admin-a", conn, project_id="proj_a", minimum_capability="view"
    )
    assert decision.allowed is False
    assert decision.reason == "grant_required"


def test_strict_project_access_owner_floor_and_exact_member_grant(monkeypatch):
    from core.project_access import resolve_strict_resource_access

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    owner = resolve_strict_resource_access(
        "owner-a",
        _conn(("org_a", "active", "owner", "active")),
        project_id="proj_a",
        minimum_capability="manage",
    )
    assert owner.allowed is True
    assert owner.capability == "manage"

    member = resolve_strict_resource_access(
        "member-a",
        _conn(
            ("org_a", "active", "member", "active"),
            ("edit",),
        ),
        project_id="proj_a",
        minimum_capability="edit",
    )
    assert member.allowed is True
    assert member.capability == "edit"


@pytest.mark.parametrize("mode,identity", [("disabled", "anonymous"), ("oauth", "anonymous")])
def test_strict_access_never_allows_anonymous(monkeypatch, mode, identity):
    from core.project_access import resolve_strict_resource_access

    monkeypatch.setenv("TOOROW_AUTH_MODE", mode)
    decision = resolve_strict_resource_access(
        identity, _conn(), project_id="proj_a", minimum_capability="view"
    )
    assert decision.allowed is False
    assert decision.reason == "production_identity_required"


def test_strict_access_fails_closed_on_db_error(monkeypatch):
    from core.project_access import resolve_strict_resource_access

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    conn = MagicMock()
    conn.cursor.side_effect = RuntimeError("db down")
    decision = resolve_strict_resource_access(
        "member-a", conn, project_id="proj_a", minimum_capability="view"
    )
    assert decision.allowed is False
    assert decision.reason == "access_unavailable"


def test_provider_account_defaults_none_for_non_owner(monkeypatch):
    from core.project_access import resolve_provider_account_access

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    conn = _conn(
        ("org_b", "active", "member", "active"),
        ("view",),
        ("org_a", "active", "ok", "ready", "acct_1", True),
        None,
    )
    decision = resolve_provider_account_access(
        "member-b",
        conn,
        credential_id="conn_1",
        external_account_id="acct_1",
        beneficiary_org_id="org_b",
        project_id="proj_b",
    )
    assert decision.allowed is False
    assert decision.reason == "account_exposure_required"


def test_provider_account_requires_exact_active_exposure(monkeypatch):
    from core.project_access import resolve_provider_account_access

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    conn = _conn(
        ("org_b", "active", "member", "active"),
        ("view",),
        ("org_a", "active", "ok", "ready", "acct_1", True),
        ("cgrant_1",),
    )
    decision = resolve_provider_account_access(
        "member-b",
        conn,
        credential_id="conn_1",
        external_account_id="acct_1",
        beneficiary_org_id="org_b",
        project_id="proj_b",
    )
    assert decision.allowed is True
    assert decision.resource_path[-1] == "account:acct_1"


def test_provider_account_denies_unhealthy_or_wrong_selected_account(monkeypatch):
    from core.project_access import resolve_provider_account_access

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    for state in (
        ("org_a", "active", "revoked", "ready", "acct_1", True),
        ("org_a", "active", "ok", "ready", "acct_other", True),
    ):
        decision = resolve_provider_account_access(
            "owner-a",
            _conn(("org_a", "active", "owner", "active"), state),
            credential_id="conn_1",
            external_account_id="acct_1",
            beneficiary_org_id="org_a",
            project_id="proj_a",
        )
        assert decision.allowed is False


def test_epic36_production_gate_is_default_off(monkeypatch):
    from core.project_access import epic36_production_access_enabled

    monkeypatch.delenv("TOOROW_EPIC36_PRODUCTION_ENABLED", raising=False)
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    assert epic36_production_access_enabled() is False
    monkeypatch.setenv("TOOROW_EPIC36_PRODUCTION_ENABLED", "true")
    assert epic36_production_access_enabled() is True
    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    assert epic36_production_access_enabled() is False

def test_rls_context_is_transaction_local():
    from core.db import set_local_access_context

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    conn.cursor.return_value = cur
    set_local_access_context(conn, "subject-1", enforce_epic36=True)
    calls = [call.args for call in cur.execute.call_args_list]
    assert calls == [
        ("SELECT set_config('toorow.identity', %s, true)", ("subject-1",)),
        ("SELECT set_config('toorow.enforce_epic36', %s, true)", ("on",)),
    ]


def test_migration_059_contains_lifecycle_and_rls_contract():
    from pathlib import Path

    sql = Path("infra/nango/migrations/059_provider_account_exposure_gate.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS available" in sql
    assert "ADD COLUMN IF NOT EXISTS status" in sql
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "current_setting('toorow.enforce_epic36', true)" in sql

def test_last_owner_floor_ignores_admin():
    from core.admin_api import _would_orphan_last_owner

    cur = MagicMock()
    cur.fetchall.return_value = [("owner-1", "owner")]
    cur.fetchone.return_value = ("owner", "active")
    assert _would_orphan_last_owner(
        cur, "org-1", "owner-1", new_role="admin", new_status="active"
    ) is True
    assert "role = 'owner'" in cur.execute.call_args_list[0].args[0]


def test_atomic_owner_transfer_promotes_before_demoting():
    from core.admin_api import _transfer_org_ownership

    cur = MagicMock()
    cur.fetchall.return_value = [("owner-1", "owner")]
    cur.fetchone.return_value = ("active",)
    assert _transfer_org_ownership(cur, "org-1", "owner-1", "member-2") is True
    sql = [call.args[0] for call in cur.execute.call_args_list]
    assert "SET role = 'owner'" in sql[-2]
    assert "SET role = 'admin'" in sql[-1]


def test_exposure_invalidation_is_explicit_and_bounded():
    from core.account_topology import invalidate_credential_exposures

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    conn.cursor.return_value = cur
    invalidate_credential_exposures("conn-1", "revocation", conn)
    sql = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "credential_account_grants" in sql
    assert "pending_account_selection" in sql
    with pytest.raises(ValueError):
        invalidate_credential_exposures("conn-1", "arbitrary", conn)
