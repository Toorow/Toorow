"""Role-aware fail-closed project access tests (Story 12.2)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _conn(row=None, error: Exception | None = None):
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    if error is not None:
        cur.execute.side_effect = error
    cur.fetchone.return_value = row
    conn.cursor.return_value = cur
    return conn


def test_production_zero_membership_denies_instead_of_legacy_default_open(monkeypatch):
    from core.project_access import resolve_project_role

    monkeypatch.setenv("TOOROW_AUTH_MODE", "static")
    assert resolve_project_role("proj_a", "user-1", _conn((None, "active"))) is None


@pytest.mark.parametrize(
    ("actual", "minimum", "allowed"),
    [
        ("viewer", "viewer", True),
        ("viewer", "member", False),
        ("member", "viewer", True),
        ("member", "member", True),
        ("member", "owner", False),
        ("owner", "member", True),
        ("owner", "owner", True),
    ],
)
def test_role_hierarchy(monkeypatch, actual: str, minimum: str, allowed: bool):
    from core.project_access import identity_has_project_role

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    assert (
        identity_has_project_role("proj_a", "user-1", minimum, _conn((actual, "active"))) is allowed
    )


def test_anonymous_is_owner_only_in_disabled_auth(monkeypatch):
    from core.project_access import resolve_project_role

    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    assert resolve_project_role("proj_a", "anonymous", _conn((None, "active"))) == "owner"
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    assert resolve_project_role("proj_a", "anonymous", _conn((None, "active"))) is None


def test_archived_project_and_unavailable_membership_fail_closed(monkeypatch):
    from core.project_access import ProjectAccessUnavailable, resolve_project_role

    monkeypatch.setenv("TOOROW_AUTH_MODE", "static")
    assert resolve_project_role("proj_a", "owner-1", _conn(("owner", "archived"))) is None
    with pytest.raises(ProjectAccessUnavailable):
        resolve_project_role("proj_a", "owner-1", _conn(error=RuntimeError("db down")))


def test_nonexistent_project_row_returns_none(monkeypatch):
    """fetchone()==None (project does not exist) must return None, not raise 503."""
    from core.project_access import resolve_project_role

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    assert resolve_project_role("proj_missing", "user-1", _conn(None)) is None


def test_nonexistent_project_row_never_grants_owner_in_disabled_auth(monkeypatch):
    """Anonymous in disabled-auth mode must not become owner for a nonexistent project."""
    from core.project_access import resolve_project_role

    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    assert resolve_project_role("proj_missing", "anonymous", _conn(None)) is None
