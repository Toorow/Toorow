"""Integration seam tests for Context Graph Edge routes (Story 11.4, F-3, AI-56).

Covers:
  - GET  /api/context/graph/edges   — list, AD-5 scope (platform + project)
  - POST /api/context/graph/edges   — create, invalid from_type rejected, cross-project 404+audit
  - DELETE /api/context/graph/edges/{id} — delete, cross-project 404+audit
  - Platform-write deny-by-default (create platform-scope edge)
  - Project sees platform edges AND own project edges (not another project)

  - GET  /api/context/graph          — one-bundle read: nodes (topic/procedure/
    schema_doc, orphans included) + edges (Story 44.3); doc_kind disambiguation
    for same-relation schema_doc nodes; dangling-edge filtering
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

#: One row of `app.context_relationships` in the column order
#: `context_relationships._HEAD_COLUMNS` names -- the authority behind
#: `_EDGE_ROW_PROJ_A`. Story 49-6 AC5.
_RELATION_ROW = (
    "crel_01J0000000000000000000AA",  # id
    "org_1",                           # org_id
    "proj_A",                          # project_id
    "topic",                           # source_type
    "top_01",                          # source_id
    "topic",                           # target_type
    "top_02",                          # target_id
    "relates_to",                      # relationship_kind
    "active",                          # status
    "console",                         # provenance
    "edge_A001",                       # projection_edge_id
    "crelv_01J0000000000000000000BB",  # current_version_id
    "user_a",                          # created_by
    "2026-07-20T10:00:00Z",            # created_at
    "2026-07-20T10:00:00Z",            # updated_at
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

    # STORY 49-6 AC5: the door calls `context_relationships.create_relationship`,
    # which reads more before it writes -- and every one of those reads is the
    # authority doing something the old door did not do at all.
    cur.fetchone.side_effect = [
        (1,),               # source topic exists, asked of context_store's owner
        (1,),               # target topic exists
        ("org_1",),         # the organization, DERIVED from the project graph
        None,               # no live relation already joins these two this way
        None,               # nothing occupies the projection's endpoint tuple
        _EDGE_ROW_PROJ_A,   # the projection INSERT ... RETURNING
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
                "edge_type": "relates_to",
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
                "edge_type": "relates_to",
            },
        )

    assert resp.status_code == 422
    data = resp.json()
    assert data["code"] == "invalid_param"
    assert "Invalid" in data["message"]


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
                "edge_type": "relates_to",
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
                "edge_type": "relates_to",
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
    # STORY 49-6 AC5: a retirement is a SUPERSEDE. The route still reads the edge
    # first (so a cross-scope attempt is audited without disclosure), then finds
    # the relation the edge projects and retires THAT -- the projection row is
    # deleted, the relation and its version history are not.
    cur.fetchone.side_effect = [
        _EDGE_ROW_PROJ_A,   # get_graph_edge -- the scope check reads it unfiltered
        _RELATION_ROW,      # find_by_projection_edge
        _RELATION_ROW,      # supersede_relationship's own scoped read
        (2,),               # the next version number
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


# ---------------------------------------------------------------------------
# Story 44.3: GET /api/context/graph — one-bundle read (nodes + edges)
# ---------------------------------------------------------------------------

_TOPIC_COLS = [
    ("id",), ("project_id",), ("title",), ("body_md",), ("status",), ("owner",),
    ("created_by",), ("created_at",), ("updated_at",), ("version_number",),
]
_PROC_COLS = [
    ("id",), ("project_id",), ("name",), ("description",), ("frontmatter_yaml",),
    ("body_md",), ("status",), ("owner",), ("created_by",), ("created_at",), ("updated_at",),
    ("version_number",),
]
_SCHEMA_DOC_COLS = [
    ("id",), ("project_id",), ("relation",), ("doc_kind",), ("body_md",),
    ("generated_at",), ("created_at",), ("version_number",),
]


def test_get_graph_returns_one_bundle_with_all_node_types_and_platform_flag():
    """GET /api/context/graph returns topics + procedures + schema_docs as nodes
    (including an orphan with no edges) plus edges, with platform scope flagged."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()

    topics_rows = [
        (
            "top_a", "proj_A", "Project Topic", "project body", "active", None,
            "user_a", "2026-07-20T10:00:00Z", "2026-07-20T10:00:00Z", 1,
        ),
        (
            "top_platform", None, "Platform Topic", "platform body", "active", "ops@toorow.com",
            "admin", "2026-07-19T09:00:00Z", "2026-07-19T09:00:00Z", 3,
        ),
    ]
    procs_rows = [
        (
            "proc_a", "proj_A", "my_proc", "desc", "name: my_proc\ndescription: desc",
            "proc body", "active", None, "user_a", "2026-07-20T10:00:00Z",
            "2026-07-20T10:00:00Z", 1,
        ),
    ]
    schema_doc_rows = [
        (
            "sctx_orphan", "proj_A", "fact_orphan_table", "columns", "col body",
            "2026-07-20T08:00:00Z", "2026-07-20T08:00:00Z", 1,
        ),
    ]
    # Local edge whose endpoints ARE nodes of this fixture: the handler's
    # dangling-edge filter (44.3 re-review) keeps only edges with both
    # endpoints in the node set, so reusing the module-level _EDGE_ROW_PROJ_A
    # (top_01 -> top_02, absent here) would be silently dropped and the
    # edges assertion below would prove nothing.
    edge_rows = [
        (
            "edge_A001", "top_a", "topic", "proc_a", "procedure",
            "related", "proj_A", "user_a", "2026-07-20T10:00:00Z",
        ),
    ]

    cur.fetchall.side_effect = [topics_rows, procs_rows, schema_doc_rows, edge_rows]

    def _description_by_query(sql, *_a, **_k):
        if "app.context_topics" in sql:
            cur.description = _TOPIC_COLS
        elif "app.procedures" in sql:
            cur.description = _PROC_COLS
        elif "app.schema_context" in sql:
            cur.description = _SCHEMA_DOC_COLS
        elif "app.context_graph" in sql:
            cur.description = _EDGE_COLS

    cur.execute.side_effect = _description_by_query

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.get("/api/context/graph?project_id=proj_A")

    assert resp.status_code == 200
    data = resp.json()

    node_ids = {n["id"] for n in data["nodes"]}
    assert node_ids == {"top_a", "top_platform", "proc_a", "sctx_orphan"}

    by_id = {n["id"]: n for n in data["nodes"]}
    assert by_id["top_a"]["node_type"] == "topic"
    assert by_id["top_a"]["scope"] == "project"
    assert by_id["top_platform"]["scope"] == "platform"
    assert by_id["proc_a"]["node_type"] == "procedure"
    assert by_id["proc_a"]["title"] == "my_proc"

    # Story 44.11: no explicit owner -> resolved display falls back to
    # created_by; owner_raw stays None so an editor never mistakes the
    # fallback for an explicit value the curator actually set.
    assert by_id["top_a"]["owner"] == "user_a"
    assert by_id["top_a"]["owner_raw"] is None
    assert by_id["proc_a"]["owner"] == "user_a"
    assert by_id["proc_a"]["owner_raw"] is None
    # An explicit owner wins over created_by.
    assert by_id["top_platform"]["owner"] == "ops@toorow.com"
    assert by_id["top_platform"]["owner_raw"] == "ops@toorow.com"

    # Orphan schema_doc node present with no edge referencing it.
    orphan = by_id["sctx_orphan"]
    assert orphan["node_type"] == "schema_doc"
    assert orphan["title"] == "fact_orphan_table"
    assert orphan["scope"] == "project"
    edge_endpoints = {e["from_id"] for e in data["edges"]} | {e["to_id"] for e in data["edges"]}
    assert "sctx_orphan" not in edge_endpoints

    assert {e["id"] for e in data["edges"]} == {"edge_A001"}


def test_resolve_owner_falls_back_to_auto_when_neither_exists():
    """44.11 third clause: neither explicit owner nor created_by -> 'auto'.

    Unreachable through the fixtures (created_by is NOT NULL on both tables),
    so the pure function is pinned directly (44.11 re-review: the branch was
    delivered untested).
    """
    from core.context_api import _resolve_owner

    assert _resolve_owner(None, None) == "auto"
    assert _resolve_owner("", "") == "auto"
    assert _resolve_owner(None, "who@x") == "who@x"
    assert _resolve_owner("owner@x", "who@x") == "owner@x"


def test_get_graph_excerpt_is_exact_280_char_verbatim_prefix():
    """A 10k-char topic body must produce an excerpt that is exactly the verbatim
    280-char prefix (R44-UX03)."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()

    long_body = "".join(chr(ord("a") + (i % 26)) for i in range(10_000))
    topics_rows = [
        (
            "top_long", "proj_A", "Long Topic", long_body, "active", None,
            "user_a", "2026-07-20T10:00:00Z", "2026-07-20T10:00:00Z", 1,
        ),
    ]

    def _description_by_query(sql, *_a, **_k):
        if "app.context_topics" in sql:
            cur.description = _TOPIC_COLS
        elif "app.procedures" in sql:
            cur.description = _PROC_COLS
        elif "app.schema_context" in sql:
            cur.description = _SCHEMA_DOC_COLS
        elif "app.context_graph" in sql:
            cur.description = _EDGE_COLS

    cur.execute.side_effect = _description_by_query
    cur.fetchall.side_effect = [topics_rows, [], [], []]

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.get("/api/context/graph?project_id=proj_A")

    assert resp.status_code == 200
    nodes = resp.json()["nodes"]
    assert len(nodes) == 1
    assert nodes[0]["excerpt"] == long_body[:280]
    assert len(nodes[0]["excerpt"]) == 280


def test_get_graph_missing_project_id_422():
    app = build_asgi_app()
    client = TestClient(app)

    with patch("core.admin_api._check_auth", return_value=(True, "user_a")):
        resp = client.get("/api/context/graph")

    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_param"


def test_get_graph_unauthenticated_401():
    app = build_asgi_app()
    client = TestClient(app)

    with patch("core.admin_api._check_auth", return_value=(False, "")):
        resp = client.get("/api/context/graph?project_id=proj_A")

    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


def test_get_graph_no_membership_404_and_audit():
    """Caller without project access → non-disclosing 404 + cross-scope audit row."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()

    with (
        patch("core.admin_api._check_auth", return_value=(True, "outsider")),
        patch("core.project_access.identity_has_project_role", return_value=False),
        patch("core.db.get_connection", return_value=conn),
        patch("core.context_api.write_audit_row") as mock_audit,
    ):
        resp = client.get("/api/context/graph?project_id=proj_private")

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"
    mock_audit.assert_called_once()
    assert mock_audit.call_args.kwargs["action"] == "access_denied"


def test_get_graph_two_schema_docs_same_relation_distinguished_by_doc_kind():
    """Two schema_doc rows sharing the same relation title (columns vs sample_values)
    must remain distinguishable in the node payload via the "doc_kind" key (Finding 2,
    44.3) -- title alone collides."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()

    schema_doc_rows = [
        (
            "sctx_cols", "proj_A", "fact_ga4_sessions", "columns", "col body",
            "2026-07-20T08:00:00Z", "2026-07-20T08:00:00Z", 1,
        ),
        (
            "sctx_samples", "proj_A", "fact_ga4_sessions", "sample_values", "sample body",
            "2026-07-20T08:00:00Z", "2026-07-20T08:00:00Z", 1,
        ),
    ]

    def _description_by_query(sql, *_a, **_k):
        if "app.context_topics" in sql:
            cur.description = _TOPIC_COLS
        elif "app.procedures" in sql:
            cur.description = _PROC_COLS
        elif "app.schema_context" in sql:
            cur.description = _SCHEMA_DOC_COLS
        elif "app.context_graph" in sql:
            cur.description = _EDGE_COLS

    cur.execute.side_effect = _description_by_query
    cur.fetchall.side_effect = [[], [], schema_doc_rows, []]

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.get("/api/context/graph?project_id=proj_A")

    assert resp.status_code == 200
    nodes = resp.json()["nodes"]
    assert len(nodes) == 2
    by_id = {n["id"]: n for n in nodes}
    assert by_id["sctx_cols"]["title"] == by_id["sctx_samples"]["title"] == "fact_ga4_sessions"
    assert by_id["sctx_cols"]["doc_kind"] == "columns"
    assert by_id["sctx_samples"]["doc_kind"] == "sample_values"


def test_get_graph_drops_edge_pointing_at_archived_node():
    """An edge whose endpoint is archived (excluded from list_topics'
    include_archived=False result, so absent from the node list) must be filtered out
    of the response rather than left dangling for the React Flow consumer (Finding 3,
    44.3)."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()

    topics_rows = [
        (
            "top_active", "proj_A", "Active Topic", "body", "active", None,
            "user_a", "2026-07-20T10:00:00Z", "2026-07-20T10:00:00Z", 1,
        ),
    ]
    # Edge references top_archived, which is NOT in topics_rows (archived, excluded
    # from the node-building query) -- a dangling edge.
    dangling_edge_row = (
        "edge_dangling", "top_active", "topic", "top_archived", "topic",
        "related", "proj_A", "user_a", "2026-07-20T10:00:00Z",
    )

    def _description_by_query(sql, *_a, **_k):
        if "app.context_topics" in sql:
            cur.description = _TOPIC_COLS
        elif "app.procedures" in sql:
            cur.description = _PROC_COLS
        elif "app.schema_context" in sql:
            cur.description = _SCHEMA_DOC_COLS
        elif "app.context_graph" in sql:
            cur.description = _EDGE_COLS

    cur.execute.side_effect = _description_by_query
    cur.fetchall.side_effect = [topics_rows, [], [], [dangling_edge_row]]

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.get("/api/context/graph?project_id=proj_A")

    assert resp.status_code == 200
    data = resp.json()
    assert {n["id"] for n in data["nodes"]} == {"top_active"}
    assert data["edges"] == [], "edge pointing at an archived/out-of-scope node must be dropped"


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


# ---------------------------------------------------------------------------
# Live finding A1 (2026-08-05): disabled-auth local mode (`make dev`)
# ---------------------------------------------------------------------------


def test_list_graph_edges_disabled_auth_keeps_the_local_developer_workflow(monkeypatch):
    """Under `make dev` the graph endpoints answered 404 while every other
    context surface answered -- two seams, two truths. The anonymous developer
    identity must pass `_check_project_role` WITHOUT the strict resolver ever
    running (it would deny on `production_identity_required`)."""
    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")

    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    cur.fetchall.return_value = [_EDGE_ROW_PROJ_A, _EDGE_ROW_PLATFORM]
    cur.description = _EDGE_COLS

    with (
        patch("core.admin_api._check_auth", return_value=(True, "anonymous")),
        patch(
            "core.project_access.identity_has_project_role",
            side_effect=AssertionError("strict resolver must not run in disabled dev mode"),
        ),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.get("/api/context/graph/edges?project_id=proj_A")

    assert resp.status_code == 200
    ids = {e["id"] for e in resp.json()["edges"]}
    assert "edge_A001" in ids


def test_get_graph_disabled_auth_returns_the_bundle(monkeypatch):
    """Same bypass on the one-bundle read: the Knowledge Graph screen was
    broken in the default local config because this route 404'd."""
    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")

    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    cur.fetchall.side_effect = [[], [], [], []]

    def _description_by_query(sql, *_a, **_k):
        if "app.context_topics" in sql:
            cur.description = _TOPIC_COLS
        elif "app.procedures" in sql:
            cur.description = _PROC_COLS
        elif "app.schema_context" in sql:
            cur.description = _SCHEMA_DOC_COLS
        elif "app.context_graph" in sql:
            cur.description = _EDGE_COLS

    cur.execute.side_effect = _description_by_query

    with (
        patch("core.admin_api._check_auth", return_value=(True, "anonymous")),
        patch(
            "core.project_access.identity_has_project_role",
            side_effect=AssertionError("strict resolver must not run in disabled dev mode"),
        ),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.get("/api/context/graph?project_id=proj_A")

    assert resp.status_code == 200
    data = resp.json()
    assert data["nodes"] == []
    assert data["edges"] == []


def test_create_graph_edge_rejects_an_edge_type_outside_the_vocabulary():
    """Live finding F2 (2026-08-05): any string was accepted as edge_type
    (`relation_invalide_xyz` -> 201), so the graph accumulated one vocabulary
    per caller. The canonical set is the console's EDGE_TYPE_SUGGESTIONS plus
    the business projection's "applies-to"."""
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
                "from_type": "topic",
                "from_id": "top_01",
                "to_type": "topic",
                "to_id": "top_02",
                "edge_type": "relation_invalide_xyz",
            },
        )

    assert resp.status_code == 422
    # STORY 49-6 AC5: the vocabulary is the authority's, so the CODE is now the
    # named refusal rather than the generic one -- a caller can branch on it,
    # where `invalid_param` said only "something in your body". Same 422, same
    # vocabulary in the sentence.
    assert resp.json()["code"] == "unknown_relationship_kind"
    assert "relates_to" in resp.json()["message"]


# ---------------------------------------------------------------------------
# Live finding F3 (2026-08-07): the scope of a write travels in the query
# ---------------------------------------------------------------------------


def test_create_graph_edge_reads_the_scope_from_the_query_like_every_other_route():
    """`POST /graph/edges?project_id=proj_A` read the scope from the BODY only,
    so the query was ignored, the edge became PLATFORM-scoped, and the
    deny-by-default platform gate answered 404 "Project not found" on a project
    the caller owns. Every other context route reads `?project_id=`."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
        # STORY 49-6 AC5: the door's writer is the AUTHORITY, so that is what is
        # patched. `projection` is the legacy row the authority wrote in the
        # same transaction -- the payload this route serialises.
        patch(
            "core.context_relationships.create_relationship",
            return_value={
                "id": "crel_new",
                "project_id": "proj_A",
                "projection_edge_id": "edge_new",
                "projection": {"id": "edge_new", "project_id": "proj_A"},
            },
        ) as create_mock,
    ):
        resp = client.post(
            "/api/context/graph/edges?project_id=proj_A",
            json={
                "from_type": "topic",
                "from_id": "top_01",
                "to_type": "topic",
                "to_id": "top_02",
                "edge_type": "relates_to",
            },
        )

    assert resp.status_code == 201
    assert create_mock.call_args.kwargs["project_id"] == "proj_A"


def test_create_graph_edge_refuses_a_body_scope_that_contradicts_the_query():
    """The body may REPEAT the scope; it may not redirect it. Same rule and
    same non-disclosing 404 as `_body_project_scope` on topics/procedures."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
        patch("core.context_relationships.create_relationship") as create_mock,
        patch("core.context_api.write_audit_row") as mock_audit,
    ):
        resp = client.post(
            "/api/context/graph/edges?project_id=proj_A",
            json={
                "project_id": "proj_B",
                "from_type": "topic",
                "from_id": "top_01",
                "to_type": "topic",
                "to_id": "top_02",
                "edge_type": "relates_to",
            },
        )

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"
    create_mock.assert_not_called()
    mock_audit.assert_called_once()


def test_create_graph_edge_keeps_the_platform_scope_when_no_project_is_named():
    """An ABSENT project_id still means the PLATFORM scope here -- unlike
    topics, where it is a 422. Harmonizing WHERE the scope is read must not
    silently make it required and delete the platform-edge path."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()

    with (
        patch("core.admin_api._check_auth", return_value=(True, "platform@example.com")),
        patch("core.context_api.check_platform_write_authorized", return_value=True),
        patch("core.db.get_connection", return_value=conn),
        patch(
            "core.context_relationships.create_relationship",
            return_value={
                "id": "crel_p",
                "project_id": None,
                "projection_edge_id": "edge_p",
                "projection": {"id": "edge_p", "project_id": None},
            },
        ) as create_mock,
    ):
        resp = client.post(
            "/api/context/graph/edges",
            json={
                "from_type": "topic",
                "from_id": "top_p01",
                "to_type": "procedure",
                "to_id": "proc_p01",
                "edge_type": "relates_to",
            },
        )

    assert resp.status_code == 201
    assert create_mock.call_args.kwargs["project_id"] is None
