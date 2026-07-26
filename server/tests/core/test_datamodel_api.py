"""Unit tests for server/core/datamodel_api.py (Story 8.5, AC9 + Story 13.1, Epic 13).

Mounts DATAMODEL_ROUTES on a test Starlette app and exercises each handler
via TestClient. Auth is bypassed via patching _check_auth.

Tests:
  - GET /api/datamodel/fields (list, filters)
  - GET /api/datamodel/fields/{name} (detail, 404)
  - POST /api/datamodel/fields (create, validation errors, 409)
  - PATCH /api/datamodel/fields/{name} (update, immutability 422)
  - PUT /api/datamodel/mappings (upsert, validation, FK error)
  - POST /api/datamodel/fields/{name}/approve  [Story 13.1]
  - DELETE /api/datamodel/fields/{name}        [Story 13.1]
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from core.datamodel_api import DATAMODEL_ROUTES
from starlette.routing import Router
from starlette.testclient import TestClient

_NOW = datetime(2026, 7, 12, 10, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixture: test app + auth bypass
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    """Test client with auth disabled (always authorized as 'test@test')."""
    app = Router(routes=DATAMODEL_ROUTES)
    with patch(
        "core.datamodel_api._check_auth",
        new=AsyncMock(return_value=(True, "test@test")),
    ):
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


@pytest.fixture()
def client_unauth():
    """Test client that always returns unauthorized."""
    app = Router(routes=DATAMODEL_ROUTES)
    with patch(
        "core.datamodel_api._check_auth",
        new=AsyncMock(return_value=(False, "")),
    ):
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

_FIELD = {
    "name": "clicks",
    "display_name": "Clics",
    "data_type": "integer",
    "field_kind": "metric",
    "measure": "sum",
    "description": "Nombre de clics",
    "created_by": "system",
    "is_default": True,
    "created_at": _NOW.isoformat(),
    "used_by_count": 2,
    "status": "approved",
    "approved_at": None,
    "approved_by": None,
    "updated_at": _NOW.isoformat(),
}

_FIELD_DETAIL = {
    **_FIELD,
    "used_by": [
        {
            "datastream_id": "ds_001",
            "datastream_name": "Meta Ads",
            "module_name": "meta-ads",
            "project_id": "proj_a",
            "enabled": True,
            "source_field": "clicks",
            "last_loaded_at": _NOW.isoformat(),
            "last_verdict": "ok",
        }
    ],
    "conflicts": [],
}

_MAPPING = {
    "datastream_id": "ds_001",
    "source_field": "clicks",
    "target_field": "clicks",
    "is_key_column": False,
    "created_at": _NOW.isoformat(),
}


# ---------------------------------------------------------------------------
# GET /api/datamodel/fields
# ---------------------------------------------------------------------------


class TestListFields:
    def test_returns_200_with_fields(self, client):
        with patch("core.db.get_connection") as mock_conn, \
             patch("core.datamodel.list_target_fields", return_value=[_FIELD]):
            mock_conn.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_conn.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.get("/api/datamodel/fields")

        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    def test_401_without_auth(self, client_unauth):
        resp = client_unauth.get("/api/datamodel/fields")
        assert resp.status_code == 401

    def test_invalid_kind_returns_422(self, client):
        resp = client.get("/api/datamodel/fields?kind=invalid")
        assert resp.status_code == 422
        assert "kind" in resp.json()["message"]

    def test_invalid_usage_returns_422(self, client):
        resp = client.get("/api/datamodel/fields?usage=wrong")
        assert resp.status_code == 422
        assert "usage" in resp.json()["message"]

    def test_db_error_returns_500(self, client):
        with patch("core.db.get_connection") as mock_conn, \
             patch("core.datamodel.list_target_fields", side_effect=Exception("db down")):
            mock_conn.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_conn.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.get("/api/datamodel/fields")
        assert resp.status_code == 500

    def test_kind_filter_forwarded(self, client):
        calls = []

        def capture(**kwargs):
            calls.append(kwargs)
            return []

        with patch("core.db.get_connection") as mc, \
             patch(
                 "core.datamodel.list_target_fields",
                 side_effect=lambda conn, **kw: capture(**kw) or [],
             ):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            client.get("/api/datamodel/fields?kind=metric")

        assert calls and calls[0].get("kind") == "metric"

    def test_project_id_filter_forwarded(self, client):
        calls = []

        def capture(**kwargs):
            calls.append(kwargs)
            return []

        with patch("core.db.get_connection") as mc, \
             patch(
                 "core.datamodel.list_target_fields",
                 side_effect=lambda conn, **kw: capture(**kw) or [],
             ):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            client.get("/api/datamodel/fields?project_id=proj_a")

        assert calls and calls[0].get("project_id") == "proj_a"

    # AI-49: ?module= filter tests
    def test_module_filter_forwarded_to_list_target_fields(self, client):
        """AI-49: ?module= query param is forwarded to list_target_fields as module=."""
        calls = []

        def capture(**kwargs):
            calls.append(kwargs)
            return []

        with patch("core.db.get_connection") as mc, \
             patch(
                 "core.datamodel.list_target_fields",
                 side_effect=lambda conn, **kw: capture(**kw) or [],
             ):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.get("/api/datamodel/fields?module=google-analytics")

        assert resp.status_code == 200
        assert calls and calls[0].get("module") == "google-analytics"

    def test_unknown_module_returns_empty_200(self, client):
        """AI-49: unknown module -> empty list (200), not an error."""
        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.list_target_fields", return_value=[]):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.get("/api/datamodel/fields?module=no-such-module")

        assert resp.status_code == 200
        assert resp.json() == []

    def test_module_filter_with_project_id_ad5_scoping(self, client):
        """AI-49: module + project_id both forwarded (AD-5 scoping preserved)."""
        calls = []

        def capture(**kwargs):
            calls.append(kwargs)
            return [_FIELD]

        with patch("core.db.get_connection") as mc, \
             patch(
                 "core.datamodel.list_target_fields",
                 side_effect=lambda conn, **kw: capture(**kw),
             ):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.get(
                "/api/datamodel/fields?module=google-analytics&project_id=proj_a"
            )

        assert resp.status_code == 200
        assert calls[0].get("module") == "google-analytics"
        assert calls[0].get("project_id") == "proj_a"

    def test_no_module_param_calls_without_module_kwarg_or_none(self, client):
        """AI-49: absent ?module= -> module=None forwarded (unfiltered behavior unchanged)."""
        calls = []

        def capture(**kwargs):
            calls.append(kwargs)
            return []

        with patch("core.db.get_connection") as mc, \
             patch(
                 "core.datamodel.list_target_fields",
                 side_effect=lambda conn, **kw: capture(**kw) or [],
             ):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.get("/api/datamodel/fields")

        assert resp.status_code == 200
        # module kwarg should be None (or absent) -- never a non-None value
        assert calls[0].get("module") is None


# ---------------------------------------------------------------------------
# GET /api/datamodel/fields/{name}
# ---------------------------------------------------------------------------


class TestGetField:
    def test_returns_200_with_detail(self, client):
        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.get_target_field", return_value=_FIELD_DETAIL):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.get("/api/datamodel/fields/clicks")

        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "clicks"
        assert "used_by" in data
        assert "conflicts" in data

    def test_returns_404_when_not_found(self, client):
        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.get_target_field", return_value=None):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.get("/api/datamodel/fields/nonexistent")

        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_401_without_auth(self, client_unauth):
        resp = client_unauth.get("/api/datamodel/fields/clicks")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/datamodel/fields
# ---------------------------------------------------------------------------


class TestCreateField:
    def test_creates_field_returns_201(self, client):
        new_field = {**_FIELD, "is_default": False, "used_by_count": 0}
        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.create_target_field", return_value=new_field):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.post(
                "/api/datamodel/fields",
                content=json.dumps({
                    "name": "clicks",
                    "display_name": "Clics",
                    "data_type": "integer",
                    "field_kind": "metric",
                    "measure": "sum",
                }),
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 201

    def test_validation_error_returns_422(self, client):
        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.create_target_field",
                   side_effect=ValueError("name est requis")):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.post(
                "/api/datamodel/fields",
                content=json.dumps(
                    {"display_name": "X", "data_type": "integer", "field_kind": "metric"}
                ),
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 422
        assert "validation_error" in resp.json()["code"]

    def test_duplicate_name_returns_409(self, client):
        class FakeUniqueViolation(Exception):
            pass
        FakeUniqueViolation.__name__ = "UniqueViolation"

        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.create_target_field",
                   side_effect=FakeUniqueViolation("unique constraint")):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.post(
                "/api/datamodel/fields",
                content=json.dumps(
                    {
                        "name": "clicks", "display_name": "X",
                        "data_type": "integer", "field_kind": "metric",
                    }
                ),
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 409

    def test_invalid_json_returns_400(self, client):
        resp = client.post(
            "/api/datamodel/fields",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_401_without_auth(self, client_unauth):
        resp = client_unauth.post("/api/datamodel/fields", content=b"{}")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# PATCH /api/datamodel/fields/{name}
# ---------------------------------------------------------------------------


class TestPatchField:
    def test_updates_field_returns_200(self, client):
        updated = {**_FIELD, "display_name": "Clics (MaJ)"}
        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.update_target_field", return_value=updated):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.patch(
                "/api/datamodel/fields/clicks",
                content=json.dumps({"display_name": "Clics (MaJ)"}),
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Clics (MaJ)"

    def test_immutability_error_returns_422(self, client):
        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.update_target_field",
                   side_effect=ValueError("name est immuable")):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.patch(
                "/api/datamodel/fields/clicks",
                content=json.dumps({"name": "new_clicks"}),
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 422

    def test_returns_404_when_not_found(self, client):
        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.update_target_field", return_value=None):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.patch(
                "/api/datamodel/fields/gone",
                content=json.dumps({"description": "x"}),
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 404

    def test_401_without_auth(self, client_unauth):
        resp = client_unauth.patch("/api/datamodel/fields/clicks", content=b"{}")
        assert resp.status_code == 401

    # -----------------------------------------------------------------------
    # Story 44.8 finding #4: restored_from is validated + normalised HERE,
    # before it ever reaches core.datamodel.update_target_field.
    # -----------------------------------------------------------------------

    def test_restored_from_int_is_verified_and_forwarded_as_int(self, client):
        updated = {**_FIELD, "display_name": "Clics (restaure)"}
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone.return_value = (1,)  # (name, version_number) exists
        conn = MagicMock()
        conn.cursor.return_value = cur

        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.update_target_field", return_value=updated) as mock_update:
            mc.return_value.__enter__ = MagicMock(return_value=conn)
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.patch(
                "/api/datamodel/fields/clicks",
                content=json.dumps(
                    {"display_name": "Clics (restaure)", "restored_from": 2}
                ),
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 200
        # The store only ever sees the validated plain int.
        forwarded_patch = mock_update.call_args[0][1]
        assert forwarded_patch["restored_from"] == 2

    def test_restored_from_dict_with_version_number_is_normalised_to_int(self, client):
        """Some UI snapshots pass the whole version object back; only the int
        version_number is extracted and forwarded."""
        updated = {**_FIELD, "display_name": "Clics (restaure)"}
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone.return_value = (1,)
        conn = MagicMock()
        conn.cursor.return_value = cur

        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.update_target_field", return_value=updated) as mock_update:
            mc.return_value.__enter__ = MagicMock(return_value=conn)
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.patch(
                "/api/datamodel/fields/clicks",
                content=json.dumps(
                    {
                        "display_name": "Clics (restaure)",
                        "restored_from": {"version_number": 3},
                    }
                ),
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 200
        forwarded_patch = mock_update.call_args[0][1]
        assert forwarded_patch["restored_from"] == 3

    def test_restored_from_invalid_type_returns_422_without_db_call(self, client):
        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.update_target_field") as mock_update:
            resp = client.patch(
                "/api/datamodel/fields/clicks",
                content=json.dumps({"restored_from": "not-an-int"}),
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 422
        assert resp.json()["code"] == "validation_error"
        mc.assert_not_called()
        mock_update.assert_not_called()

    def test_restored_from_unknown_version_returns_422(self, client):
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone.return_value = None  # no such (name, version_number) row
        conn = MagicMock()
        conn.cursor.return_value = cur

        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.update_target_field") as mock_update:
            mc.return_value.__enter__ = MagicMock(return_value=conn)
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.patch(
                "/api/datamodel/fields/clicks",
                content=json.dumps(
                    {"display_name": "X", "restored_from": 999}
                ),
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 422
        assert resp.json()["code"] == "validation_error"
        mock_update.assert_not_called()


# ---------------------------------------------------------------------------
# GET /api/datamodel/fields/{name}/history  [Story 44.8]
# ---------------------------------------------------------------------------

_VERSION_ROWS = [
    {
        "name": "clicks",
        "version_number": 3,
        "display_name": "Clics (restaures)",
        "data_type": "integer",
        "field_kind": "metric",
        "measure": "sum",
        "description": "Nombre de clics",
        "created_by": "system",
        "is_default": False,
        "created_at": _NOW.isoformat(),
        "status": "approved",
        "approved_at": None,
        "approved_by": None,
        "updated_at": _NOW.isoformat(),
        "change_kind": "restored",
        "diff": {"display_name": {"before": "Clics", "after": "Clics (restaures)"},
                 "_restored_from": {"version_number": 1}},
        "changed_by": "alice@toorow.io",
        "changed_at": _NOW.isoformat(),
    },
    {
        "name": "clicks",
        "version_number": 2,
        "display_name": "Clics",
        "data_type": "integer",
        "field_kind": "metric",
        "measure": "sum",
        "description": "Nombre de clics",
        "created_by": "system",
        "is_default": False,
        "created_at": _NOW.isoformat(),
        "status": "deleted",
        "approved_at": None,
        "approved_by": None,
        "updated_at": _NOW.isoformat(),
        "change_kind": "deleted",
        "diff": None,
        "changed_by": "bob@toorow.io",
        "changed_at": _NOW.isoformat(),
    },
    {
        "name": "clicks",
        "version_number": 1,
        "display_name": "Clics",
        "data_type": "integer",
        "field_kind": "metric",
        "measure": "sum",
        "description": "Nombre de clics",
        "created_by": "system",
        "is_default": False,
        "created_at": _NOW.isoformat(),
        "status": "draft",
        "approved_at": None,
        "approved_by": None,
        "updated_at": _NOW.isoformat(),
        "change_kind": "created",
        "diff": None,
        "changed_by": "system",
        "changed_at": _NOW.isoformat(),
    },
]


class TestGetFieldHistory:
    def test_returns_200_with_versions_desc(self, client):
        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.target_field_exists", return_value=True), \
             patch("core.datamodel.list_field_versions", return_value=_VERSION_ROWS):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.get("/api/datamodel/fields/clicks/history")

        assert resp.status_code == 200
        data = resp.json()
        versions = data["versions"]
        assert [v["version_number"] for v in versions] == [3, 2, 1]
        assert versions[0]["change_kind"] == "restored"
        assert versions[0]["changed_by"] == "alice@toorow.io"
        assert versions[0]["snapshot"] == {
            "display_name": "Clics (restaures)",
            "measure": "sum",
            "description": "Nombre de clics",
            "status": "approved",
        }
        assert versions[0]["diff"]["_restored_from"] == {"version_number": 1}

    def test_serves_history_for_soft_deleted_field(self, client):
        """History outlives visibility: deleted snapshot must be present."""
        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.target_field_exists", return_value=True), \
             patch("core.datamodel.list_field_versions", return_value=_VERSION_ROWS):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.get("/api/datamodel/fields/clicks/history")

        assert resp.status_code == 200
        versions = resp.json()["versions"]
        deleted = next(v for v in versions if v["change_kind"] == "deleted")
        assert deleted["snapshot"]["status"] == "deleted"

    def test_field_exists_but_no_history_yet_returns_200_empty_list(self, client):
        """Field is real (target_field_exists=True) but has no version rows yet."""
        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.target_field_exists", return_value=True), \
             patch("core.datamodel.list_field_versions", return_value=[]):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.get("/api/datamodel/fields/clicks/history")

        assert resp.status_code == 200
        assert resp.json() == {"versions": []}

    def test_unknown_name_returns_404(self, client):
        """Story 44.8 finding #5: a name that never existed (in any status) is a
        genuine 404, not a 200 with an empty list."""
        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.target_field_exists", return_value=False), \
             patch("core.datamodel.list_field_versions") as mock_list:
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.get("/api/datamodel/fields/unknown/history")

        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"
        mock_list.assert_not_called()

    def test_401_without_auth(self, client_unauth):
        resp = client_unauth.get("/api/datamodel/fields/clicks/history")
        assert resp.status_code == 401

    def test_db_error_returns_500(self, client):
        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.target_field_exists", return_value=True), \
             patch("core.datamodel.list_field_versions", side_effect=Exception("db down")):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.get("/api/datamodel/fields/clicks/history")

        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# PUT /api/datamodel/mappings
# ---------------------------------------------------------------------------


class TestUpsertMapping:
    def test_upserts_returns_200(self, client):
        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.upsert_mapping", return_value=_MAPPING):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.put(
                "/api/datamodel/mappings",
                content=json.dumps({
                    "datastream_id": "ds_001",
                    "source_field": "clicks",
                    "target_field": "clicks",
                }),
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 200
        assert resp.json()["datastream_id"] == "ds_001"

    def test_missing_datastream_id_returns_400(self, client):
        resp = client.put(
            "/api/datamodel/mappings",
            content=json.dumps({"source_field": "clicks", "target_field": "clicks"}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert "datastream_id" in resp.json()["message"]

    def test_missing_source_field_returns_400(self, client):
        resp = client.put(
            "/api/datamodel/mappings",
            content=json.dumps({"datastream_id": "ds_001", "target_field": "clicks"}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert "source_field" in resp.json()["message"]

    def test_null_target_field_unmaps(self, client):
        unmap_result = {**_MAPPING, "target_field": None}
        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.upsert_mapping", return_value=unmap_result):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.put(
                "/api/datamodel/mappings",
                content=json.dumps({
                    "datastream_id": "ds_001",
                    "source_field": "clicks",
                    "target_field": None,
                }),
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 200
        assert resp.json()["target_field"] is None

    def test_fk_violation_returns_422(self, client):
        class FakeFKViolation(Exception):
            pass
        FakeFKViolation.__name__ = "ForeignKeyViolation"

        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.upsert_mapping",
                   side_effect=FakeFKViolation("foreign key constraint")):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.put(
                "/api/datamodel/mappings",
                content=json.dumps({
                    "datastream_id": "ds_001",
                    "source_field": "clicks",
                    "target_field": "nonexistent_field",
                }),
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 422
        assert resp.json()["code"] == "invalid_target"

    def test_invalid_json_returns_400(self, client):
        resp = client.put(
            "/api/datamodel/mappings",
            content=b"bad json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_401_without_auth(self, client_unauth):
        resp = client_unauth.put("/api/datamodel/mappings", content=b"{}")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Story 13.1: POST /api/datamodel/fields/{name}/approve
# ---------------------------------------------------------------------------


_APPROVED_FIELD = {
    **_FIELD,
    "status": "approved",
    "approved_at": _NOW.isoformat(),
    "approved_by": "test@test",
}


class TestApproveField:
    def test_approve_returns_200_with_approved_status(self, client):
        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.approve_target_field", return_value=_APPROVED_FIELD):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.post("/api/datamodel/fields/clicks/approve")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "approved"
        assert data["approved_by"] == "test@test"

    def test_approve_returns_404_when_field_not_found(self, client):
        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.approve_target_field", return_value=None):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.post("/api/datamodel/fields/nonexistent/approve")

        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_approve_returns_401_without_auth(self, client_unauth):
        resp = client_unauth.post("/api/datamodel/fields/clicks/approve")
        assert resp.status_code == 401

    def test_approve_returns_500_on_db_error(self, client):
        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.approve_target_field",
                   side_effect=Exception("db down")):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.post("/api/datamodel/fields/clicks/approve")

        assert resp.status_code == 500

    def test_approve_identity_forwarded(self, client):
        """The caller's identity is forwarded to approve_target_field."""
        calls = []

        def capture(name, identity, conn):
            calls.append({"name": name, "identity": identity})
            return _APPROVED_FIELD

        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.approve_target_field", side_effect=capture):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            client.post("/api/datamodel/fields/clicks/approve")

        assert calls and calls[0]["identity"] == "test@test"

    def test_approve_does_not_modify_immutable_fields(self, client):
        """Approve response preserves name/data_type/field_kind unchanged."""
        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.approve_target_field", return_value=_APPROVED_FIELD):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.post("/api/datamodel/fields/clicks/approve")

        data = resp.json()
        assert data["name"] == "clicks"
        assert data["data_type"] == "integer"
        assert data["field_kind"] == "metric"


# ---------------------------------------------------------------------------
# Story 13.1: DELETE /api/datamodel/fields/{name}
# ---------------------------------------------------------------------------


class TestDeleteField:
    def test_delete_orphan_field_returns_204(self, client):
        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.delete_target_field", return_value=None):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.delete("/api/datamodel/fields/orphan_field")

        assert resp.status_code == 204

    def test_delete_field_in_use_returns_409_with_fr_message(self, client):
        from core.datamodel import FieldInUseError

        err_msg = (
            "Impossible de supprimer le champ 'clicks' : il est utilisé par "
            "3 mapping(s) actif(s). Retirez les mappings avant de supprimer ce champ."
        )

        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.delete_target_field",
                   side_effect=FieldInUseError(err_msg)):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.delete("/api/datamodel/fields/clicks")

        assert resp.status_code == 409
        data = resp.json()
        assert data["code"] == "field_in_use"
        assert "clicks" in data["message"]
        assert "3" in data["message"]

    def test_delete_nonexistent_field_returns_404(self, client):
        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.delete_target_field",
                   side_effect=ValueError("Champ 'gone' introuvable")):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.delete("/api/datamodel/fields/gone")

        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_delete_returns_401_without_auth(self, client_unauth):
        resp = client_unauth.delete("/api/datamodel/fields/clicks")
        assert resp.status_code == 401

    def test_delete_returns_500_on_db_error(self, client):
        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.delete_target_field",
                   side_effect=Exception("db error")):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.delete("/api/datamodel/fields/clicks")

        assert resp.status_code == 500

    def test_delete_draft_field_returns_204(self, client):
        """A draft (unnapproved) field with no mappings can be deleted."""
        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.delete_target_field", return_value=None):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.delete("/api/datamodel/fields/my_draft_field")

        assert resp.status_code == 204

    def test_delete_default_field_returns_409(self, client):
        """H2: deleting a field with is_default=TRUE raises FieldInUseError -> 409."""
        from core.datamodel import FieldInUseError

        err_msg = "Les champs par defaut du socle ne peuvent pas etre supprimes."

        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.delete_target_field",
                   side_effect=FieldInUseError(err_msg)):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.delete("/api/datamodel/fields/clicks")

        assert resp.status_code == 409
        data = resp.json()
        assert data["code"] == "field_in_use"
        # Message must convey the is_default restriction
        assert "defaut" in data["message"].lower() or "socle" in data["message"].lower()


# ---------------------------------------------------------------------------
# Story 13.1 (M2 fix): approve idempotency at API layer
# ---------------------------------------------------------------------------


class TestApproveFieldIdempotency:
    def test_re_approve_already_approved_returns_200(self, client):
        """Re-approving an already-approved field must return 200 (no error)."""
        already_approved = {
            **_FIELD,
            "status": "approved",
            "approved_by": "original@test",
            "approved_at": _NOW.isoformat(),
        }
        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.approve_target_field", return_value=already_approved):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.post("/api/datamodel/fields/clicks/approve")

        assert resp.status_code == 200
        data = resp.json()
        # Original approver must be preserved (not overwritten)
        assert data["approved_by"] == "original@test"
        assert data["status"] == "approved"
