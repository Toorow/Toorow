"""Story 44.10 -- GET /api/datamodel/mappings?target_field= ("Fed by").

The knowledge-graph drawer needs "which datastreams feed this dictionary
field?". The route only accepted ?datastream_id=, so this story added the
inverse lookup rather than making the UI fetch every stream and filter client
-side.

Assertions are on the RESPONSE BODY and on the SQL/params actually issued
(the fake cursor plays back a real-shaped result set); the pre-existing
?datastream_id= behaviour is pinned as a regression guard.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from core.datamodel_api import DATAMODEL_ROUTES
from starlette.routing import Router
from starlette.testclient import TestClient

_FED_BY_COLS = [
    ("datastream_id",), ("datastream_name",), ("module_name",),
    ("project_id",), ("enabled",), ("source_field",), ("is_key_column",),
]
_FED_BY_ROWS = [
    ("ds_meta", "Meta Ads — FR", "meta-ads", "proj_A", True, "clicks", False),
    ("ds_ga4", "GA4 — web", "ga4", "proj_A", True, "sessions_clicks", False),
]

_STREAM_MAPPING_COLS = [("source_field",), ("target_field",), ("is_key_column",)]


@pytest.fixture()
def client():
    app = Router(routes=DATAMODEL_ROUTES)
    with patch(
        "core.datamodel_api._check_auth",
        new=AsyncMock(return_value=(True, "ann@toorow.com")),
    ):
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


def _conn_returning(rows, cols):
    conn = MagicMock()
    conn.__enter__.return_value = conn
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.description = cols
    cur.fetchall.return_value = rows
    return conn, cur


def _last_sql(cur):
    return " ".join(cur.execute.call_args.args[0].split())


def test_lookup_by_target_field_returns_the_feeding_streams(client):
    conn, cur = _conn_returning(_FED_BY_ROWS, _FED_BY_COLS)

    with patch("core.db.get_connection", return_value=conn):
        resp = client.get("/api/datamodel/mappings?target_field=clicks&project_id=proj_A")

    assert resp.status_code == 200
    mappings = resp.json()["mappings"]
    assert [m["datastream_id"] for m in mappings] == ["ds_meta", "ds_ga4"]
    # The panel names the SOURCE, not just the column.
    assert mappings[0]["datastream_name"] == "Meta Ads — FR"
    assert mappings[0]["module_name"] == "meta-ads"
    assert mappings[0]["source_field"] == "clicks"
    assert mappings[0]["project_id"] == "proj_A"

    sql = _last_sql(cur)
    assert "dm.target_field = %s" in sql
    assert "JOIN app.datastreams ds ON ds.id = dm.datastream_id" in sql
    assert cur.execute.call_args.args[1] == ["clicks", "proj_A"]


def test_lookup_by_target_field_without_project_id_is_refused(client):
    """44.10 re-review: the platform-wide cross-project answer must never be
    reachable by accident -- project_id is REQUIRED on the target_field branch."""
    resp_client = client.get("/api/datamodel/mappings?target_field=clicks")
    assert resp_client.status_code == 400
    body = resp_client.json()
    assert body["code"] == "missing_field"
    assert "project_id" in body["message"]


def test_lookup_by_target_field_scopes_to_project_when_given(client):
    conn, cur = _conn_returning(_FED_BY_ROWS[:1], _FED_BY_COLS)

    with patch("core.db.get_connection", return_value=conn):
        resp = client.get("/api/datamodel/mappings?target_field=clicks&project_id=proj_A")

    assert resp.status_code == 200
    assert len(resp.json()["mappings"]) == 1

    sql = _last_sql(cur)
    assert "ds.project_id = %s" in sql
    assert cur.execute.call_args.args[1] == ["clicks", "proj_A"]


def test_lookup_by_target_field_with_nothing_feeding_it_is_an_empty_list(client):
    conn, _cur = _conn_returning([], _FED_BY_COLS)

    with patch("core.db.get_connection", return_value=conn):
        resp = client.get(
            "/api/datamodel/mappings?target_field=orphan_metric&project_id=proj_A"
        )

    assert resp.status_code == 200
    assert resp.json() == {"mappings": []}


def test_neither_parameter_is_a_400_in_french(client):
    resp = client.get("/api/datamodel/mappings")
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "missing_field"
    assert body["message"] == "datastream_id ou target_field est requis"


def test_both_parameters_together_are_refused(client):
    resp = client.get("/api/datamodel/mappings?datastream_id=ds_1&target_field=clicks")
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_param"


def test_datastream_lookup_still_works_unchanged(client):
    """Regression guard: the original ?datastream_id= shape must be untouched."""
    conn, cur = _conn_returning([("clicks", "clicks", False)], _STREAM_MAPPING_COLS)

    with patch("core.db.get_connection", return_value=conn):
        resp = client.get("/api/datamodel/mappings?datastream_id=ds_meta")

    assert resp.status_code == 200
    assert resp.json() == {
        "mappings": [{"source_field": "clicks", "target_field": "clicks", "is_key_column": False}]
    }
    assert "WHERE datastream_id = %s" in _last_sql(cur)


def test_unauthenticated_is_401():
    app = Router(routes=DATAMODEL_ROUTES)
    with patch(
        "core.datamodel_api._check_auth", new=AsyncMock(return_value=(False, ""))
    ):
        with TestClient(app, raise_server_exceptions=True) as c:
            resp = c.get("/api/datamodel/mappings?target_field=clicks")
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"
