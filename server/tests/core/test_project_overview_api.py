from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

from core.project_overview_api import project_overview_routes
from starlette.applications import Starlette
from starlette.testclient import TestClient


class _Connection:
    pass


@contextmanager
def _connection():
    yield _Connection()


def _client() -> TestClient:
    return TestClient(Starlette(routes=project_overview_routes), raise_server_exceptions=False)


def test_project_overview_route_is_canonical_and_no_store() -> None:
    envelope = {
        "schema_version": "project-overview.v1",
        "project": {"id": "proj-1"},
    }
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person-1"))),
        patch("core.admin_api._strict_project_capability_allowed", side_effect=[True, True]),
        patch("core.db.get_connection", side_effect=_connection),
        patch(
            "core.project_overview_api.compose_project_overview", return_value=envelope
        ) as compose,
    ):
        response = _client().get("/api/projects/proj-1/overview")

    assert response.status_code == 200
    assert response.json() == envelope
    assert response.headers["cache-control"] == "no-store"
    compose.assert_called_once_with(
        "proj-1",
        compose.call_args.args[1],
        actor="person-1",
        can_edit=True,
    )


def test_project_overview_denial_is_non_disclosing() -> None:
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person-foreign"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=False),
        patch("core.db.get_connection", side_effect=_connection),
        patch("core.project_overview_api.compose_project_overview") as compose,
    ):
        response = _client().get("/api/projects/hidden/overview")

    assert response.status_code == 404
    assert response.json() == {"code": "not_found", "message": "Project not found"}
    assert response.headers["cache-control"] == "no-store"
    compose.assert_not_called()


def test_project_overview_required_composer_failure_is_generic() -> None:
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person-1"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=True),
        patch("core.db.get_connection", side_effect=_connection),
        patch(
            "core.project_overview_api.compose_project_overview",
            side_effect=RuntimeError("secret database detail"),
        ),
    ):
        response = _client().get("/api/projects/proj-1/overview")

    assert response.status_code == 503
    assert response.json() == {
        "code": "project_overview_unavailable",
        "message": "Project Overview is unavailable",
    }
    assert "secret" not in response.text
