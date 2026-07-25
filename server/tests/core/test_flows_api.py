"""Unit tests for server/core/flows_api.py (Story 8.7, AC5).

Mounts FLOWS_ROUTES on a test Starlette app and exercises each handler via
TestClient. Auth is bypassed via patching _check_auth; core.flows is patched so
these tests stay DB-less (flows.py behaviour is covered in test_flows.py).

Tests:
  - GET  /api/flows            (list, missing project_id, bad kind, scope 404)
  - GET  /api/flows/{kind}/{id} (get, 404)
  - POST /api/flows/validate   (ok + errors, no scope check)
  - PUT  /api/flows            (upsert, validation 422, conflict 409, scope 404)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from core.flows import FlowConflictError, FlowScopeError, FlowValidationError
from core.flows_api import FLOWS_ROUTES
from starlette.routing import Router
from starlette.testclient import TestClient


@pytest.fixture()
def client():
    app = Router(routes=FLOWS_ROUTES)
    with (
        patch("core.flows_api._check_auth", new=AsyncMock(return_value=(True, "test@test"))),
        patch("core.flows_api.get_connection", create=True),
        patch("core.db.get_connection") as mock_conn,
    ):
        mock_conn.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


@pytest.fixture()
def client_unauth():
    app = Router(routes=FLOWS_ROUTES)
    with patch("core.flows_api._check_auth", new=AsyncMock(return_value=(False, ""))):
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


# ---------------------------------------------------------------------------
# GET /api/flows
# ---------------------------------------------------------------------------


class TestListFlows:
    def test_unauthorized(self, client_unauth):
        r = client_unauth.get("/api/flows?project_id=proj_a")
        assert r.status_code == 401

    def test_missing_project_id(self, client):
        r = client.get("/api/flows")
        assert r.status_code == 400

    def test_bad_kind(self, client):
        r = client.get("/api/flows?project_id=proj_a&kind=bogus")
        assert r.status_code == 422

    def test_list_ok(self, client):
        items = [{"kind": "datastream", "id": "ds_1", "name": "GA"}]
        with patch("core.flows.list_flows", return_value=items):
            r = client.get("/api/flows?project_id=proj_a")
        assert r.status_code == 200
        assert r.json() == items

    def test_scope_violation_404(self, client):
        with patch("core.flows.list_flows", side_effect=FlowScopeError("nope")):
            r = client.get("/api/flows?project_id=proj_x")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/flows/{kind}/{id}
# ---------------------------------------------------------------------------


class TestGetFlow:
    def test_get_ok(self, client):
        flow = {"kind": "datastream", "id": "ds_1", "name": "GA"}
        with patch("core.flows.get_flow", return_value=flow):
            r = client.get("/api/flows/datastream/ds_1?project_id=proj_a")
        assert r.status_code == 200
        assert r.json()["id"] == "ds_1"

    def test_not_found(self, client):
        with patch("core.flows.get_flow", return_value=None):
            r = client.get("/api/flows/report/m%2Fr?project_id=proj_a")
        assert r.status_code == 404

    def test_bad_kind(self, client):
        r = client.get("/api/flows/bogus/x?project_id=proj_a")
        assert r.status_code == 422

    def test_missing_project(self, client):
        r = client.get("/api/flows/datastream/ds_1")
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/flows/validate
# ---------------------------------------------------------------------------


class TestValidate:
    def test_valid(self, client):
        doc = {
            "schema_version": "1",
            "kind": "datastream",
            "project_id": "p",
            "name": "n",
            "module_name": "google-analytics",
        }
        r = client.post("/api/flows/validate", json=doc)
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_invalid_returns_errors(self, client):
        r = client.post("/api/flows/validate", json={"kind": "datastream"})
        body = r.json()
        assert r.status_code == 200
        assert body["ok"] is False
        assert body["errors"]

    def test_definition_wrapper_accepted(self, client):
        doc = {
            "schema_version": "1",
            "kind": "datastream",
            "project_id": "p",
            "name": "n",
            "module_name": "google-analytics",
        }
        r = client.post("/api/flows/validate", json={"definition": doc})
        assert r.json()["ok"] is True

    def test_secret_field_rejected(self, client):
        doc = {
            "schema_version": "1",
            "kind": "datastream",
            "project_id": "p",
            "name": "n",
            "module_name": "google-analytics",
            "token": "SECRET",
        }
        r = client.post("/api/flows/validate", json=doc)
        assert r.json()["ok"] is False


# ---------------------------------------------------------------------------
# PUT /api/flows
# ---------------------------------------------------------------------------


class TestUpsert:
    def _body(self):
        return {
            "project_id": "proj_a",
            "definition": {
                "schema_version": "1",
                "kind": "datastream",
                "project_id": "proj_a",
                "name": "GA",
                "module_name": "google-analytics",
            },
        }

    def test_missing_definition(self, client):
        r = client.put("/api/flows", json={"project_id": "proj_a"})
        assert r.status_code == 400

    def test_upsert_ok(self, client):
        result = {"kind": "datastream", "id": "ds_1", "changed": True, "flow": {}, "diff": {}}
        with patch("core.flows.upsert_flow", return_value=result):
            r = client.put("/api/flows", json=self._body())
        assert r.status_code == 200
        assert r.json()["changed"] is True

    def test_versioned_flow_uses_idempotency_header_without_echoing_it(self, client):
        from tests.core.test_datastream_intents import _intent

        body = {
            "project_id": "proj_a",
            "definition": {
                "schema_version": "2",
                "kind": "datastream",
                "project_id": "proj_a",
                "name": "Campaign feed",
                "intent": _intent(),
            },
        }
        result = {
            "kind": "datastream",
            "id": "ds_1",
            "changed": True,
            "flow": {"schema_version": "2"},
            "diff": {},
        }
        with patch("core.flows.upsert_flow", return_value=result) as upsert:
            response = client.put(
                "/api/flows",
                json=body,
                headers={"Idempotency-Key": "request-from-header"},
            )
        assert response.status_code == 200
        definition = upsert.call_args.args[1]
        assert definition["idempotency_key"] == "request-from-header"
        assert "request-from-header" not in response.text

    def test_unavailable_maps_to_503(self, client):
        from core.flows import FlowUnavailableError

        with patch("core.flows.upsert_flow", side_effect=FlowUnavailableError("catalog down")):
            response = client.put("/api/flows", json=self._body())
        assert response.status_code == 503
        assert response.json()["code"] == "unavailable"

    def test_validation_error_422(self, client):
        err = FlowValidationError([{"path": "/name", "message": "requis"}])
        with patch("core.flows.upsert_flow", side_effect=err):
            r = client.put("/api/flows", json=self._body())
        assert r.status_code == 422
        assert r.json()["errors"]

    def test_conflict_409(self, client):
        with patch("core.flows.upsert_flow", side_effect=FlowConflictError("dup")):
            r = client.put("/api/flows", json=self._body())
        assert r.status_code == 409

    def test_scope_404(self, client):
        with patch("core.flows.upsert_flow", side_effect=FlowScopeError("nope")):
            r = client.put("/api/flows", json=self._body())
        assert r.status_code == 404
