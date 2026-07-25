"""Tests for server/core/conflict_resolutions_api.py (Story 13.2, Epic 13).

Mounts CONFLICT_RESOLUTION_ROUTES on a test Starlette Router and exercises
each handler via TestClient. Auth is bypassed via patching _check_auth.

Tests:
  GET  /api/mdm/conflicts                   -- list conflicts
  GET  /api/mdm/conflicts/resolutions        -- list resolutions
  POST /api/mdm/conflicts/resolutions        -- create/upsert
  DELETE /api/mdm/conflicts/resolutions/{p}/{f}/{m}  -- delete
  PATCH /api/mdm/conflicts/measure/{name}    -- resolve MEASURE_NULL

AD-5 (HIGH-1): project_id OMIS sur les 2 endpoints READ -> 422 (pas de donnees).
AD-5: cross-project denial (project access guard -> 404 non-disclosant).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from core.conflict_resolutions_api import CONFLICT_RESOLUTION_ROUTES
from starlette.routing import Router
from starlette.testclient import TestClient

_NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)

_RES = {
    "id": 1,
    "project_id": "proj_a",
    "target_field": "cost",
    "source_module": "meta-ads",
    "resolved_source_currency": "USD",
    "decided_by": "alice@test",
    "decided_at": _NOW.isoformat(),
    "note": None,
}

_CONFLICT = {
    "field": {
        "name": "cost",
        "display_name": "Coût",
        "data_type": "currency",
        "field_kind": "metric",
        "measure": "sum",
        "status": "approved",
        "used_by": [],
    },
    "conflict": {
        "code": "CURRENCY_CONFLICT",
        "message": "modules distincts",
        "affected_streams": ["Meta Ads"],
    },
    "resolutions_by_module": {"meta-ads": None},
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    """TestClient with auth bypassed (always authorized as 'alice@test')."""
    app = Router(routes=CONFLICT_RESOLUTION_ROUTES)
    with patch(
        "core.conflict_resolutions_api._check_auth",
        new=AsyncMock(return_value=(True, "alice@test")),
    ):
        with patch(
            "core.conflict_resolutions_api._guard_project_access",
            new=AsyncMock(return_value=True),
        ):
            with TestClient(app, raise_server_exceptions=True) as c:
                yield c


@pytest.fixture()
def client_unauth():
    """TestClient that always returns unauthorized."""
    app = Router(routes=CONFLICT_RESOLUTION_ROUTES)
    with patch(
        "core.conflict_resolutions_api._check_auth",
        new=AsyncMock(return_value=(False, "")),
    ):
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


@pytest.fixture()
def client_cross_project():
    """TestClient where project access is denied (AD-5 cross-project test)."""
    app = Router(routes=CONFLICT_RESOLUTION_ROUTES)
    with patch(
        "core.conflict_resolutions_api._check_auth",
        new=AsyncMock(return_value=(True, "eve@attacker")),
    ):
        with patch(
            "core.conflict_resolutions_api._guard_project_access",
            new=AsyncMock(return_value=False),
        ):
            with TestClient(app, raise_server_exceptions=True) as c:
                yield c


# ---------------------------------------------------------------------------
# GET /api/mdm/conflicts
# ---------------------------------------------------------------------------


class TestListConflicts:
    def test_not_401_when_authorized(self, client):
        """When authorized, GET /api/mdm/conflicts does not return 401."""
        with patch("core.conflict_resolutions.list_conflicts", return_value=[_CONFLICT]):
            with patch("core.db.get_connection") as mock_gc:
                mock_conn = MagicMock()
                mock_gc.return_value.__enter__ = MagicMock(return_value=mock_conn)
                mock_gc.return_value.__exit__ = MagicMock(return_value=False)
                resp = client.get("/api/mdm/conflicts?project_id=proj_a")
        # With auth bypassed, response must NOT be 401
        assert resp.status_code != 401

    def test_401_unauthorized(self, client_unauth):
        resp = client_unauth.get("/api/mdm/conflicts")
        assert resp.status_code == 401

    def test_missing_project_id_422(self, client):
        """HIGH-1: omitting project_id must return 422 (no data leaked, no guard skip)."""
        resp = client.get("/api/mdm/conflicts")
        assert resp.status_code == 422
        data = resp.json()
        assert data["code"] == "missing_param"
        assert "project_id" in data["message"]

    def test_cross_project_404(self, client_cross_project):
        """AD-5: cross-project access returns 404 (non-disclosant)."""
        with patch("core.db.get_connection") as mock_gc:
            mock_gc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_gc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client_cross_project.get("/api/mdm/conflicts?project_id=proj_other")
        assert resp.status_code == 404
        assert "Projet introuvable" in resp.text


# ---------------------------------------------------------------------------
# GET /api/mdm/conflicts/resolutions
# ---------------------------------------------------------------------------


class TestListResolutions:
    def test_401_unauthorized(self, client_unauth):
        resp = client_unauth.get("/api/mdm/conflicts/resolutions")
        assert resp.status_code == 401

    def test_missing_project_id_422(self, client):
        """HIGH-1: omitting project_id must return 422.

        Without this guard, list_fx_resolutions(project_id=None) issues a full
        SELECT on app.fx_conflict_resolutions with no WHERE clause and returns
        resolutions from all projects to any bearer token holder (cross-project leak).
        """
        resp = client.get("/api/mdm/conflicts/resolutions")
        assert resp.status_code == 422
        data = resp.json()
        assert data["code"] == "missing_param"
        assert "project_id" in data["message"]

    def test_cross_project_404(self, client_cross_project):
        with patch("core.db.get_connection") as mock_gc:
            mock_gc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_gc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client_cross_project.get(
                "/api/mdm/conflicts/resolutions?project_id=proj_other"
            )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/mdm/conflicts/resolutions
# ---------------------------------------------------------------------------


class TestCreateResolution:
    def test_missing_project_id(self, client):
        resp = client.post(
            "/api/mdm/conflicts/resolutions",
            json={"target_field": "cost", "source_module": "meta-ads",
                  "resolved_source_currency": "USD"},
        )
        assert resp.status_code == 400
        assert "project_id" in resp.text

    def test_missing_target_field(self, client):
        resp = client.post(
            "/api/mdm/conflicts/resolutions",
            json={"project_id": "proj_a", "source_module": "meta-ads",
                  "resolved_source_currency": "USD"},
        )
        assert resp.status_code == 400
        assert "target_field" in resp.text

    def test_missing_source_module(self, client):
        resp = client.post(
            "/api/mdm/conflicts/resolutions",
            json={"project_id": "proj_a", "target_field": "cost",
                  "resolved_source_currency": "USD"},
        )
        assert resp.status_code == 400
        assert "source_module" in resp.text

    def test_invalid_json_body(self, client):
        resp = client.post(
            "/api/mdm/conflicts/resolutions",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_401_unauthorized(self, client_unauth):
        resp = client_unauth.post(
            "/api/mdm/conflicts/resolutions",
            json={"project_id": "proj_a", "target_field": "cost",
                  "source_module": "meta-ads", "resolved_source_currency": "USD"},
        )
        assert resp.status_code == 401

    def test_cross_project_404(self, client_cross_project):
        with patch("core.db.get_connection") as mock_gc:
            mock_gc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_gc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client_cross_project.post(
                "/api/mdm/conflicts/resolutions",
                json={"project_id": "proj_other", "target_field": "cost",
                      "source_module": "meta-ads", "resolved_source_currency": "USD"},
            )
        assert resp.status_code == 404

    def test_invalid_currency_422(self, client):
        """Invalid currency -> 422 validation error."""
        with patch("core.db.get_connection") as mock_gc:
            mock_gc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_gc.return_value.__exit__ = MagicMock(return_value=False)
            with patch(
                "core.conflict_resolutions.upsert_fx_resolution",
                side_effect=ValueError("resolved_source_currency invalide : 'FAKE'"),
            ):
                resp = client.post(
                    "/api/mdm/conflicts/resolutions",
                    json={"project_id": "proj_a", "target_field": "cost",
                          "source_module": "meta-ads", "resolved_source_currency": "FAKE"},
                )
        assert resp.status_code == 422
        assert "validation_error" in resp.text


# ---------------------------------------------------------------------------
# DELETE /api/mdm/conflicts/resolutions/{project_id}/{target_field}/{source_module}
# ---------------------------------------------------------------------------


class TestDeleteResolution:
    def test_401_unauthorized(self, client_unauth):
        resp = client_unauth.delete(
            "/api/mdm/conflicts/resolutions/proj_a/cost/meta-ads"
        )
        assert resp.status_code == 401

    def test_delete_not_found_404(self, client):
        with patch("core.db.get_connection") as mock_gc:
            mock_gc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_gc.return_value.__exit__ = MagicMock(return_value=False)
            with patch(
                "core.conflict_resolutions.delete_fx_resolution",
                side_effect=ValueError("introuvable"),
            ):
                resp = client.delete(
                    "/api/mdm/conflicts/resolutions/proj_a/cost/meta-ads"
                )
        assert resp.status_code == 404

    def test_delete_success_204(self, client):
        with patch("core.db.get_connection") as mock_gc:
            mock_gc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_gc.return_value.__exit__ = MagicMock(return_value=False)
            with patch("core.conflict_resolutions.delete_fx_resolution"):
                resp = client.delete(
                    "/api/mdm/conflicts/resolutions/proj_a/cost/meta-ads"
                )
        assert resp.status_code == 204

    def test_cross_project_404(self, client_cross_project):
        with patch("core.db.get_connection") as mock_gc:
            mock_gc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_gc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client_cross_project.delete(
                "/api/mdm/conflicts/resolutions/proj_other/cost/meta-ads"
            )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /api/mdm/conflicts/measure/{field_name}
# ---------------------------------------------------------------------------


class TestResolveMeasure:
    def test_401_unauthorized(self, client_unauth):
        resp = client_unauth.patch(
            "/api/mdm/conflicts/measure/revenue",
            json={"measure": "sum"},
        )
        assert resp.status_code == 401

    def test_missing_measure_in_body(self, client):
        resp = client.patch(
            "/api/mdm/conflicts/measure/revenue",
            json={},
        )
        assert resp.status_code == 400
        assert "measure" in resp.text

    def test_field_not_found_404(self, client):
        with patch("core.db.get_connection") as mock_gc:
            mock_gc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_gc.return_value.__exit__ = MagicMock(return_value=False)
            with patch("core.datamodel.update_target_field", return_value=None):
                resp = client.patch(
                    "/api/mdm/conflicts/measure/nonexistent",
                    json={"measure": "sum"},
                )
        assert resp.status_code == 404

    def test_invalid_measure_422(self, client):
        with patch("core.db.get_connection") as mock_gc:
            mock_gc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_gc.return_value.__exit__ = MagicMock(return_value=False)
            with patch(
                "core.datamodel.update_target_field",
                side_effect=ValueError("measure invalide : 'bad_measure'"),
            ):
                resp = client.patch(
                    "/api/mdm/conflicts/measure/revenue",
                    json={"measure": "bad_measure"},
                )
        assert resp.status_code == 422

    def test_invalid_json_body(self, client):
        resp = client.patch(
            "/api/mdm/conflicts/measure/revenue",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
