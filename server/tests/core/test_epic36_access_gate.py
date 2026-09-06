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


def test_there_is_no_runtime_gate_left_to_turn_strict_access_off(monkeypatch):
    """Story 46.4 retired ``TOOROW_EPIC36_PRODUCTION_ENABLED``.

    The gate used to be default-off, which meant the strict resolver could be
    bypassed by an unset variable. It is gone: enforcement is unconditional, and
    setting the old variable changes nothing.
    """
    from core import project_access

    assert not hasattr(project_access, "epic36_production_access_enabled")

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setenv("TOOROW_EPIC36_PRODUCTION_ENABLED", "false")
    denied = project_access.resolve_strict_resource_access(
        "outsider@example.com", _conn(None, None), project_id="proj_a"
    )
    assert denied.allowed is False

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
    from tests.conftest import REPO_ROOT

    sql = (REPO_ROOT / "infra/nango/migrations/059_provider_account_exposure_gate.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS available" in sql
    assert "ADD COLUMN IF NOT EXISTS status" in sql
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "current_setting('toorow.enforce_epic36', true)" in sql

def test_the_floor_counts_managers_so_a_last_owner_may_become_admin():
    """The floor was WIDENED from owner to manager, and this test held the old rule.

    `_would_orphan_last_owner` guarded `role = 'owner'` alone while its own
    docstring had promised, since the story 21.5 security follow-up, that "an org
    whose sole active manager is an ADMIN can no longer be emptied". The code was
    moved to the docstring on 2026-08-04 rather than the docstring to the code,
    and the query now reads `role IN ('owner', 'admin')`.

    Under the widened rule, demoting the last owner to ADMIN leaves an active
    manager, so the floor is preserved and the operation is allowed. This test
    asserted the opposite -- it was named `..._ignores_admin`, which is precisely
    the behaviour that was retired.
    """
    from core.org_lifecycle import _would_orphan_last_owner  # noqa: PLC0415

    cur = MagicMock()
    cur.fetchall.return_value = [("owner-1", "owner")]
    cur.fetchone.return_value = ("owner", "active")
    assert _would_orphan_last_owner(
        cur, "org-1", "owner-1", new_role="admin", new_status="active"
    ) is False
    assert "role IN ('owner', 'admin')" in cur.execute.call_args_list[0].args[0]


def test_the_floor_still_refuses_dropping_the_last_manager():
    """The property the widening exists to keep, and the half worth guarding.

    An org whose active managers reach zero has no owner AND no admin: it becomes
    unmanageable, and short of that it silently reopens under
    default-open-until-enrolled. Demoting the sole active manager to `member` is
    exactly that, and it must be refused whether that manager is an owner or an
    admin.
    """
    from core.org_lifecycle import _would_orphan_last_owner  # noqa: PLC0415

    for role in ("owner", "admin"):
        cur = MagicMock()
        cur.fetchall.return_value = [("manager-1", role)]
        cur.fetchone.return_value = (role, "active")
        assert _would_orphan_last_owner(
            cur, "org-1", "manager-1", new_role="member", new_status="active"
        ) is True, f"a sole active {role} could be demoted to member"

        removal = MagicMock()
        removal.fetchall.return_value = [("manager-1", role)]
        removal.fetchone.return_value = (role, "active")
        assert _would_orphan_last_owner(
            removal, "org-1", "manager-1", new_role=None, new_status=None
        ) is True, f"a sole active {role} could be removed outright"


def test_atomic_owner_transfer_promotes_before_demoting():
    from core.org_lifecycle import _transfer_org_ownership  # noqa: PLC0415

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


# ---------------------------------------------------------------------------
# AI-301 -- the clock's authority is the ACTIVATION, and it is re-read nightly.
#
# `resolve_strict_resource_access` answers from `app.org_members`. The nightly
# dispatcher has no identity and no membership, so it was denied `not_found` on
# every window: measured on production 2026-08-17 with the deployed
# TOOROW_AUTH_MODE=oauth, `scheduler` -> not_found while the owner ->
# owner_account_floor. Nothing collected from 2026-08-12 to 2026-08-17.
#
# Giving the clock a service identity was refused in writing (it would fabricate
# an `app.org_members` row for nobody), so the authority is the one that already
# exists: a person activated this Datastream against this account.
# ---------------------------------------------------------------------------


def _scheduled(monkeypatch, *rows):
    from core.project_access import resolve_scheduled_account_access

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    return resolve_scheduled_account_access(
        _conn(*rows),
        credential_id="conn_1",
        external_account_id="acct_1",
        beneficiary_org_id="org_a",
        datastream_id="ds_1",
    )


def test_the_clock_pulls_on_the_activation_that_already_stands(monkeypatch):
    """An active Datastream of this org, on a ready account its owner owns."""
    decision = _scheduled(
        monkeypatch,
        ("org_a", True, "active", None),  # the activation
        ("org_a", "active", "ok", "ready", "acct_1", True),  # owner floor
    )
    assert decision.allowed is True
    assert decision.reason == "owner_account_floor"


def test_a_datastream_turned_off_carries_no_authority_tonight(monkeypatch):
    """The activation is RE-READ each dispatch, never trusted once."""
    assert _scheduled(monkeypatch, ("org_a", False, "active", None)).reason == (
        "datastream_not_active"
    )


def test_a_draft_or_archived_datastream_carries_no_authority(monkeypatch):
    assert _scheduled(monkeypatch, ("org_a", True, "draft", None)).reason == (
        "datastream_not_active"
    )
    assert _scheduled(monkeypatch, ("org_a", True, "active", "2026-08-12")).reason == (
        "datastream_not_active"
    )


def test_the_clock_cannot_pull_for_another_organization(monkeypatch):
    """The beneficiary must be the org that owns the Datastream, not any org."""
    decision = _scheduled(monkeypatch, ("org_OTHER", True, "active", None))
    assert decision.allowed is False
    assert decision.reason == "not_found"


def test_a_datastream_that_does_not_exist_authorizes_nothing(monkeypatch):
    assert _scheduled(monkeypatch, None).reason == "not_found"


def test_the_clock_is_still_stopped_by_everything_after_the_first_link(monkeypatch):
    """Only the identity link changes. Revoking the exposure stops the clock.

    Account ready and available, but the credential belongs to ANOTHER org and
    carries no active `credential_account_grants` row for the beneficiary -- the
    exact case `account_exposure_required` exists for.
    """
    decision = _scheduled(
        monkeypatch,
        ("org_a", True, "active", None),
        ("org_OWNER", "active", "ok", "ready", "acct_1", True),
        None,  # no exposure grant
    )
    assert decision.allowed is False
    assert decision.reason == "account_exposure_required"


def test_an_unverified_account_stops_the_clock(monkeypatch):
    decision = _scheduled(
        monkeypatch,
        ("org_a", True, "active", None),
        ("org_a", "active", "ok", "pending", "acct_1", True),
    )
    assert decision.allowed is False
    assert decision.reason == "account_not_ready"


def test_a_dead_connection_stops_the_clock(monkeypatch):
    decision = _scheduled(
        monkeypatch,
        ("org_a", True, "active", None),
        ("org_a", "active", "down", "ready", "acct_1", True),
    )
    assert decision.allowed is False
    assert decision.reason == "connection_unhealthy"
