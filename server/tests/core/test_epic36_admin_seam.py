"""Story 36.1 strict admin/API authorization seam tests."""

from unittest.mock import MagicMock


def test_strict_connection_scope_denial_is_nondisclosing(monkeypatch):
    from core import admin_api, db, project_access

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = ("proj-1", "google-ads")
    conn.cursor.return_value = cur
    monkeypatch.setattr(project_access, "epic36_production_access_enabled", lambda: True)
    monkeypatch.setattr(
        project_access,
        "resolve_strict_resource_access",
        lambda *_a, **_k: project_access.AccessDecision(False, "grant_required"),
    )
    local_context = MagicMock()
    monkeypatch.setattr(db, "set_local_access_context", local_context)
    audit = MagicMock()
    monkeypatch.setattr(admin_api, "write_audit_row", audit)

    scope, response = admin_api._resolve_conn_project_scoped(
        "conn-1", "member-1", conn
    )

    assert scope is None
    assert response.status_code == 404
    assert response.body == b'{"code":"not_found","message":"Connection not found"}'
    local_context.assert_called_once_with(conn, "member-1", enforce_epic36=True)
    audit.assert_called_once()


def test_strict_connection_scope_allows_explicit_project_decision(monkeypatch):
    from core import admin_api, db, project_access

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = ("proj-1", "google-ads")
    conn.cursor.return_value = cur
    monkeypatch.setattr(project_access, "epic36_production_access_enabled", lambda: True)
    monkeypatch.setattr(
        project_access,
        "resolve_strict_resource_access",
        lambda *_a, **_k: project_access.AccessDecision(
            True, "explicit_grant", "view", "org-1"
        ),
    )
    monkeypatch.setattr(db, "set_local_access_context", MagicMock())

    scope, response = admin_api._resolve_conn_project_scoped(
        "conn-1", "member-1", conn
    )

    assert response is None
    assert scope == {"project_id": "proj-1", "provider": "google-ads"}
