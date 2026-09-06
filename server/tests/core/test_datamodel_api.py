"""Unit tests for server/core/datamodel_api.py (Story 8.5, AC9 + Story 13.1, Epic 13).

Mounts DATAMODEL_ROUTES on a test Starlette app and exercises each handler
via TestClient. Auth is bypassed via patching _check_auth.

Tests:
  - GET /api/datamodel/fields (list, filters)
  - GET /api/datamodel/fields/{name} (detail, 404)
  - GET /api/datamodel/fields/{name}/history (timeline, 404) [Story 44.8]
  - the five write doors REFUSE (409 legacy_store_is_read_only) [story 49.3 AC1]
"""

from __future__ import annotations

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
        with patch("core.admin_api._strict_project_capability_allowed", return_value=True):
            with patch("core.datamodel_api._datastream_in_project", return_value=True):
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
            resp = client.get("/api/datamodel/fields?project_id=proj_a")

        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    def test_missing_project_id_returns_422(self, client):
        resp = client.get("/api/datamodel/fields")
        assert resp.status_code == 422
        assert resp.json()["code"] == "missing_param"
    def test_401_without_auth(self, client_unauth):
        resp = client_unauth.get("/api/datamodel/fields")
        assert resp.status_code == 401

    def test_invalid_kind_returns_422(self, client):
        resp = client.get("/api/datamodel/fields?project_id=proj_a&kind=invalid")
        assert resp.status_code == 422
        assert "kind" in resp.json()["message"]

    def test_invalid_usage_returns_422(self, client):
        resp = client.get("/api/datamodel/fields?project_id=proj_a&usage=wrong")
        assert resp.status_code == 422
        assert "usage" in resp.json()["message"]

    def test_db_error_returns_500(self, client):
        with patch("core.db.get_connection") as mock_conn, \
             patch("core.datamodel.list_target_fields", side_effect=Exception("db down")):
            mock_conn.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_conn.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.get("/api/datamodel/fields?project_id=proj_a")
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
            client.get("/api/datamodel/fields?project_id=proj_a&kind=metric")

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
            resp = client.get("/api/datamodel/fields?project_id=proj_a&module=google-analytics")

        assert resp.status_code == 200
        assert calls and calls[0].get("module") == "google-analytics"

    def test_unknown_module_returns_empty_200(self, client):
        """AI-49: unknown module -> empty list (200), not an error."""
        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.list_target_fields", return_value=[]):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.get("/api/datamodel/fields?project_id=proj_a&module=no-such-module")

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
            resp = client.get("/api/datamodel/fields?project_id=proj_a")

        assert resp.status_code == 200
        # module kwarg should be None (or absent) -- never a non-None value
        assert calls[0].get("module") is None


# ---------------------------------------------------------------------------
# GET /api/datamodel/fields/{name}
# ---------------------------------------------------------------------------


class TestGetField:
    def test_returns_200_with_detail(self, client):
        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.get_target_field", return_value=_FIELD_DETAIL) as mock_get:
            connection = MagicMock()
            mc.return_value.__enter__ = MagicMock(return_value=connection)
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.get("/api/datamodel/fields/clicks?project_id=proj_a")

        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "clicks"
        assert "used_by" in data
        assert "conflicts" in data
        mock_get.assert_called_once_with("clicks", connection, project_id="proj_a")

    def test_returns_404_when_not_found(self, client):
        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.get_target_field", return_value=None):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.get("/api/datamodel/fields/nonexistent?project_id=proj_a")

        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_401_without_auth(self, client_unauth):
        resp = client_unauth.get("/api/datamodel/fields/clicks?project_id=proj_a")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# THE FIVE WRITE DOORS REFUSE -- story 49.3 AC1, cutover of 2026-08-25.
#
# WHAT THESE TESTS REPLACE. Six classes used to prove the writes: TestCreateField,
# TestPatchField, TestUpsertMapping, TestApproveField, TestDeleteField and
# TestApproveFieldIdempotency. They proved a behaviour the product no longer has,
# so keeping them would have been the greenest possible way to hide the cutover.
# What they proved is now proved on the Semantic Model, which is where a measure,
# a dimension and a binding are declared.
#
# TWO ATTACKS, BECAUSE ONE IS NOT ENOUGH. The first fixes the CONTRACT: 409,
# `legacy_store_is_read_only`, and a sentence that names the surface that works
# rather than a table. The second reads the module's own SOURCE and refuses the
# store calls: a handler restored by hand would answer 200 again and the contract
# test alone would go red one release too late -- the source test goes red in the
# same edit.
# ---------------------------------------------------------------------------


_WRITE_DOORS = (
    ("post", "/api/datamodel/fields?project_id=proj_a"),
    ("patch", "/api/datamodel/fields/clicks?project_id=proj_a"),
    ("post", "/api/datamodel/fields/clicks/approve?project_id=proj_a"),
    ("delete", "/api/datamodel/fields/clicks?project_id=proj_a"),
    ("put", "/api/datamodel/mappings"),
)


class TestLegacyWritesRefuse:
    @pytest.mark.parametrize(("verb", "path"), _WRITE_DOORS)
    def test_every_write_door_answers_409_legacy_store_is_read_only(
        self, client, verb, path
    ):
        resp = client.request(verb.upper(), path, content=b"{}")

        assert resp.status_code == 409, (verb, path, resp.text)
        assert resp.json()["code"] == "legacy_store_is_read_only"

    @pytest.mark.parametrize(("verb", "path"), _WRITE_DOORS)
    def test_the_refusal_names_a_gesture_and_never_a_table(self, client, verb, path):
        """A refusal names what to do, not what failed (CLAUDE.md, `L'ecran`)."""
        message = client.request(verb.upper(), path, content=b"{}").json()["message"]

        assert "Semantic Model" in message or "Mapping tab" in message, message
        for db_word in ("target_fields", "datastream_mappings", "app.", "table"):
            assert db_word not in message, (db_word, message)

    @pytest.mark.parametrize(("verb", "path"), _WRITE_DOORS)
    def test_a_refused_write_never_reaches_the_store(self, client, verb, path):
        """The refusal is BEFORE the store, not a rollback after it."""
        writers = {
            name: patch(f"core.datamodel.{name}")
            for name in (
                "create_target_field",
                "update_target_field",
                "approve_target_field",
                "delete_target_field",
                "upsert_mapping",
            )
        }
        started = {name: cm.start() for name, cm in writers.items()}
        try:
            with patch("core.db.get_connection") as mc:
                client.request(verb.upper(), path, content=b"{}")
            mc.assert_not_called()
            for name, mock in started.items():
                assert not mock.called, name
        finally:
            for cm in writers.values():
                cm.stop()

    def test_the_module_no_longer_calls_a_single_field_write(self):
        """The permanent attack: re-wiring a handler goes red HERE, in the same edit.

        The five store functions still EXIST -- `update_target_field` is called by
        `conflict_resolutions_api` to resolve a MEASURE_NULL conflict, a governed
        path that did not go with these doors. What must never come back is a call
        FROM THIS MODULE, and the way in is the lazy import every handler here uses
        (`from core.datamodel import ...`). So the module's own AST is read: a
        handler cannot call a store function this module never imports, and a
        re-wiring has to add the import back on the very same edit.

        A text search would not do: `_upsert_mapping` is still the NAME of the
        refusing handler, so `"upsert_mapping(" in source` is true and always will
        be. The import list is the exact question.
        """
        import ast  # noqa: PLC0415
        import inspect  # noqa: PLC0415

        from core import datamodel_api  # noqa: PLC0415

        tree = ast.parse(inspect.getsource(datamodel_api))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "core.datamodel"
            for alias in node.names
        }
        # Not vacuous: the READS are still imported from the same module.
        assert imported, "no core.datamodel import found -- the guard reads nothing"
        assert "list_target_fields" in imported, sorted(imported)

        forbidden = imported & {
            "create_target_field",
            "update_target_field",
            "approve_target_field",
            "delete_target_field",
            "upsert_mapping",
        }
        assert not forbidden, sorted(forbidden)

    def test_the_reads_stayed(self, client):
        """The other half of the cutover, and the one a lens depends on.

        `GET /api/datamodel/fields` is what the governed `mapping-coverage` lens
        of Governance reads (`ui/admin/src/shell/pages/ProjectMapping.tsx`).
        Retiring it with the writes would have blinded it, so a test says so.
        """
        with patch("core.db.get_connection") as mc:
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            with patch("core.datamodel.list_target_fields", return_value=[]):
                resp = client.get("/api/datamodel/fields?project_id=proj_a")

        assert resp.status_code == 200

    @pytest.mark.parametrize(("verb", "path"), _WRITE_DOORS)
    def test_the_refusal_is_unconditional_including_without_a_token(
        self, client_unauth, verb, path
    ):
        """409 even unauthenticated, and that is deliberate -- same as `notebooks_api`.

        The refusal carries no tenant fact: one constant code and one constant
        sentence, identical for every caller. Checking a token first would only
        answer 401 to a request that was going to be refused anyway, and would put
        an auth branch back on a handler that must have no branches at all.
        """
        resp = client_unauth.request(verb.upper(), path, content=b"{}")

        assert resp.status_code == 409
        assert resp.json()["code"] == "legacy_store_is_read_only"


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
            resp = client.get("/api/datamodel/fields/clicks/history?project_id=proj_a")

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
            resp = client.get("/api/datamodel/fields/clicks/history?project_id=proj_a")

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
            resp = client.get("/api/datamodel/fields/clicks/history?project_id=proj_a")

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
            resp = client.get("/api/datamodel/fields/unknown/history?project_id=proj_a")

        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"
        mock_list.assert_not_called()

    def test_401_without_auth(self, client_unauth):
        resp = client_unauth.get("/api/datamodel/fields/clicks/history?project_id=proj_a")
        assert resp.status_code == 401

    def test_db_error_returns_500(self, client):
        with patch("core.db.get_connection") as mc, \
             patch("core.datamodel.target_field_exists", return_value=True), \
             patch("core.datamodel.list_field_versions", side_effect=Exception("db down")):
            mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mc.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.get("/api/datamodel/fields/clicks/history?project_id=proj_a")

        assert resp.status_code == 500
