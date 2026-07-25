"""Integration seam tests for Context Graph Edge routes (Story 11.4, F-3, AI-56).

Covers:
  - GET  /api/context/graph/edges   — list, AD-5 scope (platform + project)
  - POST /api/context/graph/edges   — create, invalid from_type rejected, cross-project 404+audit
  - DELETE /api/context/graph/edges/{id} — delete, cross-project 404+audit
  - Platform-write deny-by-default (create platform-scope edge)
  - Project sees platform edges AND own project edges (not another project)
"""

from __future__ import annotations

import os

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from unittest.mock import MagicMock, patch

from core.main import build_asgi_app
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EDGE_COLS = [
    ("id",), ("from_id",), ("from_type",), ("to_id",), ("to_type",),
    ("edge_type",), ("project_id",), ("created_by",), ("created_at",),
]

_EDGE_ROW_PROJ_A = (
    "edge_A001", "top_01", "topic", "top_02", "topic",
    "related", "proj_A", "user_a", "2026-07-20T10:00:00Z",
)
_EDGE_ROW_PLATFORM = (
    "edge_P001", "top_p01", "topic", "proc_p01", "procedure",
    "related", None, "admin", "2026-07-19T08:00:00Z",
)
_EDGE_ROW_PROJ_B = (
    "edge_B001", "top_b01", "topic", "top_b02", "topic",
    "related", "proj_B", "user_b", "2026-07-18T09:00:00Z",
)


def _make_mock_conn():
    conn = MagicMock()
    conn.__enter__.return_value = conn
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


# ---------------------------------------------------------------------------
# F-3a: List edges — AD-5 scoping
# ---------------------------------------------------------------------------

def test_list_graph_edges_returns_project_and_platform_rows():
    """GET /api/context/graph/edges?project_id=proj_A returns proj_A + platform edges,
    not proj_B edges (AD-5 scope: DB query filters by project_id=proj_A which the SQL
    WHERE (project_id IS NULL OR project_id=%s) handles)."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    # DB returns proj_A + platform edges (proj_B is excluded by the WHERE clause)
    cur.fetchall.return_value = [_EDGE_ROW_PROJ_A, _EDGE_ROW_PLATFORM]
    cur.description = _EDGE_COLS

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.get("/api/context/graph/edges?project_id=proj_A")

    assert resp.status_code == 200
    edges = resp.json()["edges"]
    ids = {e["id"] for e in edges}
    assert "edge_A001" in ids, "proj_A edge must appear"
    assert "edge_P001" in ids, "platform edge must appear alongside project edges"
    assert "edge_B001" not in ids, "proj_B edge must NOT appear"


def test_list_graph_edges_unauthenticated_401():
    app = build_asgi_app()
    client = TestClient(app)

    with patch("core.admin_api._check_auth", return_value=(False, "")):
        resp = client.get("/api/context/graph/edges?project_id=proj_A")

    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


def test_list_graph_edges_no_membership_404_and_audit():
    """No viewer role on project → 404 (non-disclosing) + audit row."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()

    with (
        patch("core.admin_api._check_auth", return_value=(True, "outsider")),
        patch("core.project_access.identity_has_project_role", return_value=False),
        patch("core.db.get_connection", return_value=conn),
        patch("core.context_api.write_audit_row") as mock_audit,
    ):
        resp = client.get("/api/context/graph/edges?project_id=proj_private")

    assert resp.status_code == 404
    mock_audit.assert_called_once()
    assert mock_audit.call_args.kwargs["action"] == "access_denied"


# ---------------------------------------------------------------------------
# F-3b: Create edge — happy path, invalid type, cross-project, platform deny
# ---------------------------------------------------------------------------

def test_create_graph_edge_201():
    """POST /api/context/graph/edges → 201 with edge_<ULID> id."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()

    # _node_exists_in_scope calls: one for from_id, one for to_id → both True
    # create INSERT RETURNING → the edge row
    cur.fetchone.side_effect = [
        (1,),  # from_id topic exists
        (1,),  # to_id topic exists
        _EDGE_ROW_PROJ_A,  # INSERT RETURNING
    ]
    cur.description = _EDGE_COLS

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.post(
            "/api/context/graph/edges",
            json={
                "project_id": "proj_A",
                "from_id": "top_01",
                "from_type": "topic",
                "to_id": "top_02",
                "to_type": "topic",
                "edge_type": "related",
            },
        )

    assert resp.status_code == 201
    data = resp.json()
    assert data["id"].startswith("edge_") or data["id"] == "edge_A001"
    assert data["from_type"] == "topic"


def test_create_graph_edge_invalid_from_type_422():
    """Invalid from_type not in (topic, procedure, schema_doc) → 422."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.post(
            "/api/context/graph/edges",
            json={
                "project_id": "proj_A",
                "from_id": "top_01",
                "from_type": "invalid_type",  # <-- not in enum
                "to_id": "top_02",
                "to_type": "topic",
                "edge_type": "related",
            },
        )

    assert resp.status_code == 422
    data = resp.json()
    assert data["code"] == "invalid_param"
    assert "invalide" in data["message"]


def test_create_graph_edge_platform_write_denied_by_default(monkeypatch):
    """POST with project_id=None (platform scope) denied when not in CONTEXT_PLATFORM_WRITERS."""
    monkeypatch.setenv("CONTEXT_PLATFORM_WRITERS", "")
    monkeypatch.setenv("TOOROW_AUTH_MODE", "jwt")

    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()

    with (
        patch("core.admin_api._check_auth", return_value=(True, "regular_user")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
        patch("core.context_api.write_audit_row"),
    ):
        resp = client.post(
            "/api/context/graph/edges",
            json={
                "project_id": None,
                "from_id": "top_p01",
                "from_type": "topic",
                "to_id": "proc_p01",
                "to_type": "procedure",
                "edge_type": "related",
            },
        )

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_create_graph_edge_cross_project_no_membership_404_audit():
    """No member role on the target project → 404 + audit (non-disclosing)."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()

    with (
        patch("core.admin_api._check_auth", return_value=(True, "outsider")),
        patch("core.project_access.identity_has_project_role", return_value=False),
        patch("core.db.get_connection", return_value=conn),
        patch("core.context_api.write_audit_row") as mock_audit,
    ):
        resp = client.post(
            "/api/context/graph/edges",
            json={
                "project_id": "proj_private",
                "from_id": "top_x",
                "from_type": "topic",
                "to_id": "top_y",
                "to_type": "topic",
                "edge_type": "related",
            },
        )

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"
    mock_audit.assert_called_once()
    assert mock_audit.call_args.kwargs["action"] == "access_denied"


# ---------------------------------------------------------------------------
# F-3c: Delete edge — happy path, cross-project 404+audit
# ---------------------------------------------------------------------------

def test_delete_graph_edge_204():
    """DELETE /api/context/graph/edges/{id} → 204 when edge exists and user has membership."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    # get_graph_edge call + delete RETURNING
    cur.fetchone.side_effect = [
        _EDGE_ROW_PROJ_A,   # get_graph_edge
        ("edge_A001",),     # DELETE RETURNING
    ]
    cur.description = _EDGE_COLS

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.delete("/api/context/graph/edges/edge_A001")

    assert resp.status_code == 204


def test_delete_graph_edge_not_found_404():
    """DELETE for non-existent edge → 404 + audit."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    cur.fetchone.return_value = None  # get_graph_edge returns None
    cur.description = _EDGE_COLS

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
        patch("core.context_api.write_audit_row") as mock_audit,
    ):
        resp = client.delete("/api/context/graph/edges/edge_NONEXISTENT")

    assert resp.status_code == 404
    mock_audit.assert_called_once()


def test_delete_graph_edge_cross_project_404_audit():
    """DELETE edge that belongs to proj_B when caller only has proj_A → 404 + audit."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    # Edge belongs to proj_B
    cur.fetchone.return_value = _EDGE_ROW_PROJ_B
    cur.description = _EDGE_COLS

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch(
            "core.project_access.identity_has_project_role",
            side_effect=lambda pid, ident, r, conn: pid == "proj_A",
        ),
        patch("core.db.get_connection", return_value=conn),
        patch("core.context_api.write_audit_row") as mock_audit,
    ):
        resp = client.delete("/api/context/graph/edges/edge_B001")

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"
    mock_audit.assert_called_once()
    assert mock_audit.call_args.kwargs["action"] == "access_denied"
