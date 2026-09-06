"""Fail-closed project access invariants.

Story 12.2 wrote these against ``resolve_project_role``. Story 46.4 removed that
helper together with ``identity_has_project_access`` and the
``TOOROW_EPIC36_PRODUCTION_ENABLED`` switch: every governed Project read now goes
through ``resolve_strict_resource_access``, which requires an ACTIVE Organization
membership plus an exact ``app.resource_grants`` row, with an owner floor.

The invariants below are the same ones; only the resolver they are asked of
changed. Deleting them with the helper would have retired the guarantee along
with the implementation.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _conn(*rows, error: Exception | None = None):
    """A connection whose cursor yields *rows* in order, then None.

    The strict resolver asks two questions per decision — the scope+membership
    join, then the exact grant — so a single fixed ``fetchone`` would answer the
    grant query with the membership row.
    """
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    if error is not None:
        cur.execute.side_effect = error
    pending = list(rows)
    cur.fetchone.side_effect = lambda: pending.pop(0) if pending else None
    conn.cursor.return_value = cur
    return conn


# `SELECT p.org_id, o.status, m.role, m.status`
def _scope(role: str | None, *, org_status: str = "active", member_status: str | None = "active"):
    return ("org_a", org_status, role, member_status if role is not None else None)


def test_production_zero_membership_denies_instead_of_legacy_default_open(monkeypatch):
    from core.project_access import resolve_strict_resource_access

    monkeypatch.setenv("TOOROW_AUTH_MODE", "static")
    decision = resolve_strict_resource_access(
        "user-1", _conn(_scope(None)), project_id="proj_a"
    )
    assert decision.allowed is False
    assert decision.reason == "not_found"


def test_the_legacy_default_open_helpers_are_gone():
    """The guarantee is structural: there is no second, laxer door to try."""
    from core import project_access

    assert not hasattr(project_access, "identity_has_project_access")
    assert not hasattr(project_access, "resolve_project_role")


@pytest.mark.parametrize(
    ("org_role", "granted", "minimum", "allowed"),
    [
        ("viewer", "view", "viewer", True),
        ("viewer", "view", "member", False),
        # The Organization role is a CEILING: a wider grant cannot lift a viewer.
        ("viewer", "manage", "member", False),
        ("member", "edit", "viewer", True),
        ("member", "edit", "member", True),
        ("member", "edit", "owner", False),
        # ...and the grant is a ceiling too: an admin with a view grant reads only.
        ("admin", "view", "member", False),
        ("admin", "manage", "member", True),
    ],
)
def test_role_and_grant_are_both_ceilings(
    monkeypatch, org_role: str, granted: str, minimum: str, allowed: bool
):
    from core.project_access import identity_has_project_role

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    assert (
        identity_has_project_role(
            "proj_a", "user-1", minimum, _conn(_scope(org_role), (granted,))
        )
        is allowed
    )


def test_owner_keeps_the_explicit_floor_without_a_grant(monkeypatch):
    from core.project_access import resolve_strict_resource_access

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    decision = resolve_strict_resource_access(
        "owner-1", _conn(_scope("owner")), project_id="proj_a", minimum_capability="manage"
    )
    assert decision.allowed is True
    assert decision.reason == "owner_floor"
    assert decision.capability == "manage"


def test_membership_without_an_exact_grant_is_refused(monkeypatch):
    from core.project_access import resolve_strict_resource_access

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    decision = resolve_strict_resource_access(
        "user-1", _conn(_scope("member")), project_id="proj_a"
    )
    assert decision.allowed is False
    assert decision.reason == "grant_required"


def test_anonymous_and_disabled_auth_are_never_granted(monkeypatch):
    """Story 46.4 closed the last door: 'anonymous' is not an owner anywhere."""
    from core.project_access import resolve_strict_resource_access

    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    assert (
        resolve_strict_resource_access(
            "anonymous", _conn(_scope("owner")), project_id="proj_a"
        ).reason
        == "production_identity_required"
    )
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    assert (
        resolve_strict_resource_access(
            "anonymous", _conn(_scope("owner")), project_id="proj_a"
        ).reason
        == "production_identity_required"
    )
    # A real identity in disabled-auth mode is refused too: the mode itself is
    # not a production identity source.
    assert (
        resolve_strict_resource_access(
            "user-1", _conn(_scope("owner")), project_id="proj_a", auth_mode="disabled"
        ).reason
        == "production_identity_required"
    )


def test_archived_organization_and_inactive_membership_fail_closed(monkeypatch):
    from core.project_access import resolve_strict_resource_access

    monkeypatch.setenv("TOOROW_AUTH_MODE", "static")
    assert (
        resolve_strict_resource_access(
            "owner-1", _conn(_scope("owner", org_status="archived")), project_id="proj_a"
        ).allowed
        is False
    )
    assert (
        resolve_strict_resource_access(
            "owner-1",
            _conn(("org_a", "active", "owner", "revoked")),
            project_id="proj_a",
        ).allowed
        is False
    )


def test_a_database_failure_denies_rather_than_leaking_a_decision(monkeypatch):
    from core.project_access import resolve_strict_resource_access

    monkeypatch.setenv("TOOROW_AUTH_MODE", "static")
    decision = resolve_strict_resource_access(
        "owner-1", _conn(error=RuntimeError("db down")), project_id="proj_a"
    )
    assert decision.allowed is False
    assert decision.reason == "access_unavailable"


def test_organization_role_resolution_surfaces_an_unavailable_database(monkeypatch):
    from core.project_access import ProjectAccessUnavailable, resolve_org_role

    monkeypatch.setenv("TOOROW_AUTH_MODE", "static")
    with pytest.raises(ProjectAccessUnavailable):
        resolve_org_role("org_a", "owner-1", _conn(error=RuntimeError("db down")))


def test_nonexistent_project_row_denies_without_raising(monkeypatch):
    """fetchone()==None (the project does not exist) is a denial, never a 503."""
    from core.project_access import identity_can_read_project

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    assert identity_can_read_project("proj_missing", "user-1", _conn()) is False


def test_nonexistent_project_never_grants_owner_in_disabled_auth(monkeypatch):
    from core.project_access import identity_can_read_project

    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    assert identity_can_read_project("proj_missing", "anonymous", _conn()) is False


def test_a_resource_must_be_exactly_one_of_project_or_datastream(monkeypatch):
    from core.project_access import resolve_strict_resource_access

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    assert resolve_strict_resource_access("user-1", _conn()).reason == "invalid_resource"
    assert (
        resolve_strict_resource_access(
            "user-1", _conn(), project_id="proj_a", datastream_id="ds_1"
        ).reason
        == "invalid_resource"
    )
