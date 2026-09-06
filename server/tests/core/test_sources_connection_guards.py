"""Fail-closed guards for Sources connection creation and project counts."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient


def _client() -> TestClient:
    from core.main import build_asgi_app

    return TestClient(build_asgi_app(), raise_server_exceptions=False)


def test_create_connection_requires_explicit_non_default_project():
    nango_list = AsyncMock()
    with patch("core.connections_api.nango_client._list_connections_async", nango_list):
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
    # The gate production calls. `identity_can_manage_org` sat on the `strict_gate`
    # else-branch that Story 46.4 made unreachable and this review deleted; patching
    # it left the real resolver running against the fake connection, so the handler
    # answered 503 and the guard proved nothing.
    denied = SimpleNamespace(allowed=False, org_id=None)
    with (
        patch("core.db.get_connection", new=get_connection),
        # An identity is required: the strict path refuses `anonymous` outright, so
        # without this the handler answered 503 and the test measured the absence of
        # a caller rather than the refusal of a foreign project.
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_a"))),
        patch("core.project_access.resolve_strict_resource_access", lambda *a, **k: denied),
        patch("core.connections_api.nango_client._list_connections_async", nango_list),
    ):
        response = _client().post(
            "/api/connections",
            json={
                "nango_connection_id": "nango-foreign",
                "provider": "meta-ads",
                "project_id": "project-foreign",
            },
        )

    # Non-disclosing: the refusal is a 404 with the unknown-resource code, not a
    # 403 that confirms the project exists. The pair (403, "not_found") this used
    # to assert could never both be true.
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "not_found"
    nango_list.assert_not_awaited()
