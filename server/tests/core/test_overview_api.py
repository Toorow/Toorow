"""Route tests for server/core/overview.py -- OVERVIEW_ROUTES (Story 8.4, AC12).

Mounts OVERVIEW_ROUTES on a test Starlette app and exercises the handler
via TestClient. Auth is bypassed via patching _check_auth.

Tests:
  - GET /api/overview?project_id=proj_a -> 200 with correct shape
  - GET /api/overview (missing project_id) -> 400
  - Unauthorized -> 401
  - DB error -> 500 with code=db_error
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from core.overview import OVERVIEW_ROUTES
from starlette.routing import Router
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Sample overview payload
# ---------------------------------------------------------------------------

_OVERVIEW_DATA = {
    "kpis": {
        "extracts_today_ok": 5,
        "extracts_today_failed": 1,
        "extracts_running": 2,
        "vs_yesterday_ok_delta": 2,
    },
    "fleet": [
        {
            "id": "ds_001",
            "name": "GA Standard",
            "module_name": "google-analytics",
            "enabled": True,
            "auth_status": "active",
            "issues_count": 0,
            "last_extract": {"date": "2026-07-12", "status": "ok"},
            "next_run": "Demain 02h00",
            "volume_7d": [100, 120, 110, 130, 0, 150, 140],
        }
    ],
    "deadletters_24h": 0,
    "mirror_last_sync": "2026-07-13T02:00:00+00:00",
    "breakers": [
        {"platform": "google-ads", "state": "closed", "budget_remaining": 90},
    ],
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    """Test client with auth disabled and get_overview mocked."""
    app = Router(routes=OVERVIEW_ROUTES)
    with patch(
        "core.overview._check_auth",
        new=AsyncMock(return_value=(True, "test@test")),
    ):
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


@pytest.fixture()
def client_unauth():
    """Test client that always returns unauthorized."""
    app = Router(routes=OVERVIEW_ROUTES)
    with patch(
        "core.overview._check_auth",
        new=AsyncMock(return_value=(False, "")),
    ):
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_get_overview_200(client):
    """Happy path: GET /api/overview?project_id=proj_a returns 200 with correct shape.

    The handler uses a lazy `from core.db import get_connection` inside the async
    handler, so we patch at the module level where the name is resolved at call time.
    We also patch get_overview itself to avoid real DB calls.
    """
    mock_conn = MagicMock()
    mock_conn.__enter__ = lambda s: mock_conn
    mock_conn.__exit__ = MagicMock(return_value=False)

    with (
        patch("core.db.get_connection", return_value=mock_conn),
        patch("core.overview.get_overview", return_value=_OVERVIEW_DATA),
    ):
        resp = client.get("/api/overview?project_id=proj_a")

    assert resp.status_code == 200
    data = resp.json()
    assert "kpis" in data
    assert "fleet" in data
    assert "deadletters_24h" in data
    assert "mirror_last_sync" in data
    assert "breakers" in data


def test_get_overview_missing_project_id(client):
    """GET /api/overview without project_id -> 400."""
    resp = client.get("/api/overview")
    assert resp.status_code == 400
    assert resp.json()["code"] == "missing_param"


def test_get_overview_unauthorized(client_unauth):
    """Unauthorized request -> 401."""
    resp = client_unauth.get("/api/overview?project_id=proj_a")
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


def test_get_overview_db_error(client):
    """DB exception in get_overview -> 500 with code=db_error."""
    with patch("core.db.get_connection") as mock_gc:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(side_effect=Exception("DB down"))
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_gc.return_value = mock_conn

        resp = client.get("/api/overview?project_id=proj_a")

    assert resp.status_code == 500
    assert resp.json()["code"] == "db_error"


def test_overview_routes_list():
    """OVERVIEW_ROUTES exports exactly one Route at /api/overview."""
    from starlette.routing import Route as StarletteRoute

    assert len(OVERVIEW_ROUTES) == 1
    route = OVERVIEW_ROUTES[0]
    assert isinstance(route, StarletteRoute)
    assert route.path == "/api/overview"
