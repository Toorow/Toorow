"""Fail-closed guards for Sources connection creation and project counts."""

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient


def _client() -> TestClient:
    from core.main import build_asgi_app

    return TestClient(build_asgi_app(), raise_server_exceptions=False)


def test_create_connection_requires_explicit_non_default_project():
    nango_list = AsyncMock()
    with patch("core.admin_api.nango_client._list_connections_async", nango_list):
        missing = _client().post(
            "/api/connections",
            json={"nango_connection_id": "nango-1", "provider": "meta-ads"},
        )
        placeholder = _client().post(
            "/api/connections",
            json={
                "nango_connection_id": "nango-1",
                "provider": "meta-ads",
                "project_id": "default",
            },
        )

    assert missing.status_code == 400
    assert missing.json()["code"] == "missing_field"
    assert placeholder.status_code == 400
    assert placeholder.json()["code"] == "invalid_project"
    nango_list.assert_not_awaited()


def test_create_connection_denies_foreign_project_before_nango():
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    cursor.fetchone.return_value = ("org_foreign",)
    connection = MagicMock()
    connection.cursor.return_value = cursor

    @contextmanager
    def get_connection():
        yield connection

    nango_list = AsyncMock()
    with (
        patch("core.db.get_connection", new=get_connection),
        patch("core.project_access.epic36_production_access_enabled", return_value=False),
        patch("core.project_access.identity_can_manage_org", return_value=False),
        patch("core.admin_api.nango_client._list_connections_async", nango_list),
    ):
        response = _client().post(
            "/api/connections",
            json={
                "nango_connection_id": "nango-foreign",
                "provider": "meta-ads",
                "project_id": "project-foreign",
            },
        )

    assert response.status_code == 403
    assert response.json()["code"] == "not_found"
    nango_list.assert_not_awaited()
