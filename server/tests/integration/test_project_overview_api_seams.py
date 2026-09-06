from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")


class _Connection:
    pass


@contextmanager
def _connection():
    yield _Connection()


def test_project_overview_is_registered_through_real_asgi_app() -> None:
    from core.main import build_asgi_app

    envelope = {"schema_version": "project-overview.v1", "project": {"id": "proj-1"}}
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person-1"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=True),
        patch("core.db.get_connection", side_effect=_connection),
        patch("core.project_overview_api.compose_project_overview", return_value=envelope),
    ):
        response = TestClient(build_asgi_app(), raise_server_exceptions=False).get(
            "/api/projects/proj-1/overview"
        )

    assert response.status_code == 200
    assert response.json() == envelope
    assert response.headers["cache-control"] == "no-store"


def test_legacy_overview_routes_are_not_registered() -> None:
    from core.main import build_asgi_app

    client = TestClient(build_asgi_app(), raise_server_exceptions=False)
    assert client.get("/api/overview?project_id=proj-1").status_code == 404
    assert client.get("/api/overview/summary?project_id=proj-1").status_code == 404
