"""Integration seam tests for Context Layer REST API (Story 11.1, AI-56).

Exercises /api/context/* endpoints through build_asgi_app() + TestClient.
"""

from __future__ import annotations

import os

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from unittest.mock import MagicMock, patch

import psycopg
from core.main import build_asgi_app
from starlette.testclient import TestClient


def _make_mock_conn():
    conn = MagicMock()
    conn.__enter__.return_value = conn
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


def test_create_topic_authorized_seam():
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    cur.fetchone.return_value = (
        "top_01HX1234567890ABCDEF",
        "proj_A",
        "Analytics Guidelines",
        "# Guidelines",
        "active",
        "user_test",
        "2026-07-20T10:00:00Z",
        "2026-07-20T10:00:00Z",
    )
    cur.description = [
        ("id",),
        ("project_id",),
        ("title",),
        ("body_md",),
        ("status",),
        ("created_by",),
        ("created_at",),
        ("updated_at",),
    ]

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_test")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.post(
            "/api/context/topics",
            json={
                "project_id": "proj_A",
                "title": "Analytics Guidelines",
                "body_md": "# Guidelines",
            },
        )

        assert resp.status_code == 201
        data = resp.json()
        assert data["id"] == "top_01HX1234567890ABCDEF"
        assert data["title"] == "Analytics Guidelines"
        assert data["version_number"] == 1


def test_unauthenticated_request_seam():
    app = build_asgi_app()
    client = TestClient(app)

    with patch("core.admin_api._check_auth", return_value=(False, "")):
        resp = client.get("/api/context/topics?project_id=proj_A")
        assert resp.status_code == 401
        assert resp.json()["code"] == "unauthorized"


def test_topic_updates_version_counter_value_3():
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_test")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
    ):
        # First update (patch title) -> version 2
        cur.fetchone.side_effect = [
            (
                "top_123",
                "proj_A",
                "Original",
                "body",
                "active",
                "user_test",
                "2026-07-20T10:00:00Z",
                "2026-07-20T10:00:00Z",
            ),  # get_topic check
            (
                "top_123",
                "proj_A",
                "Original",
                "body",
                "active",
                "user_test",
                "2026-07-20T10:00:00Z",
                "2026-07-20T10:00:00Z",
            ),  # update_topic FOR UPDATE
            (1,),  # max version
            (
                "top_123",
                "proj_A",
                "Update 1",
                "body",
                "active",
                "user_test",
                "2026-07-20T10:00:00Z",
                "2026-07-20T10:05:00Z",
            ),  # UPDATE RETURNING
        ]
        cur.description = [
            ("id",),
            ("project_id",),
            ("title",),
            ("body_md",),
            ("status",),
            ("created_by",),
            ("created_at",),
            ("updated_at",),
        ]

        resp1 = client.patch("/api/context/topics/top_123", json={"title": "Update 1"})
        assert resp1.status_code == 200
        assert resp1.json()["version_number"] == 2

        # Second update -> version 3
        cur.fetchone.side_effect = [
            (
                "top_123",
                "proj_A",
                "Update 1",
                "body",
                "active",
                "user_test",
                "2026-07-20T10:00:00Z",
                "2026-07-20T10:05:00Z",
            ),  # get_topic check
            (
                "top_123",
                "proj_A",
                "Update 1",
                "body",
                "active",
                "user_test",
                "2026-07-20T10:00:00Z",
                "2026-07-20T10:05:00Z",
            ),  # update_topic FOR UPDATE
            (2,),  # max version
            (
                "top_123",
                "proj_A",
                "Update 2",
                "body",
                "active",
                "user_test",
                "2026-07-20T10:00:00Z",
                "2026-07-20T10:10:00Z",
            ),  # UPDATE RETURNING
        ]

        resp2 = client.patch("/api/context/topics/top_123", json={"title": "Update 2"})
        assert resp2.status_code == 200
        assert resp2.json()["version_number"] == 3


def test_cross_project_get_404_and_audit():
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    cur.fetchone.return_value = (
        "top_proj_b",
        "proj_B",
        "Private B Topic",
        "body",
        "active",
        "user_b",
        "2026-07-20T10:00:00Z",
        "2026-07-20T10:00:00Z",
        1,
    )
    cur.description = [
        ("id",),
        ("project_id",),
        ("title",),
        ("body_md",),
        ("status",),
        ("created_by",),
        ("created_at",),
        ("updated_at",),
        ("version_number",),
    ]

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch(
            "core.project_access.identity_has_project_role",
            side_effect=lambda pid, ident, r, conn: pid == "proj_A",
        ),
        patch("core.db.get_connection", return_value=conn),
        patch("core.context_api.write_audit_row") as mock_audit,
    ):
        resp = client.get("/api/context/topics/top_proj_b?project_id=proj_A")
        assert resp.status_code == 404
        mock_audit.assert_called_once()
        assert mock_audit.call_args.kwargs["action"] == "access_denied"


def test_list_topics_multi_project_scope_filtering():
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    # DB returns proj_A topic and platform topic (proj_B topic excluded by query)
    cur.fetchall.return_value = [
        (
            "top_a",
            "proj_A",
            "Topic A",
            "body",
            "active",
            "user_a",
            "2026-07-20T10:00:00Z",
            "2026-07-20T10:00:00Z",
            1,
        ),
        (
            "top_platform",
            None,
            "Platform Topic",
            "body",
            "active",
            "admin",
            "2026-07-20T09:00:00Z",
            "2026-07-20T09:00:00Z",
            1,
        ),
    ]
    cur.description = [
        ("id",),
        ("project_id",),
        ("title",),
        ("body_md",),
        ("status",),
        ("created_by",),
        ("created_at",),
        ("updated_at",),
        ("version_number",),
    ]

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.get("/api/context/topics?project_id=proj_A")
        assert resp.status_code == 200
        topics = resp.json()["topics"]
        assert len(topics) == 2
        assert {t["id"] for t in topics} == {"top_a", "top_platform"}


def test_create_procedure_invalid_frontmatter_422():
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
    ):
        # Missing name in frontmatter
        resp = client.post(
            "/api/context/procedures",
            json={
                "project_id": "proj_A",
                "frontmatter_yaml": "description: missing name key",
                "body_md": "proc body",
            },
        )
        assert resp.status_code == 422
        data = resp.json()
        assert data["code"] == "frontmatter_invalide"
        assert "name" in data["message"]


def test_create_procedure_duplicate_name_409():
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    # No manual pre-check anymore: create_procedure relies on the DB unique index,
    # so simulate the UniqueViolation the INSERT would raise on a duplicate name.
    cur.execute.side_effect = psycopg.errors.UniqueViolation("duplicate key: procedures name")

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.post(
            "/api/context/procedures",
            json={
                "project_id": "proj_A",
                "frontmatter_yaml": "name: existing_proc\ndescription: desc",
                "body_md": "proc body",
            },
        )
        assert resp.status_code == 409
        data = resp.json()
        assert data["code"] == "nom_deja_utilise"
        assert "déjà" in data["message"]


# ---------------------------------------------------------------------------
# Fix 7: Missing seam gap tests
# ---------------------------------------------------------------------------


def test_archive_topic_200_archived_status_version_2():
    """Archive topic returns 200, status='archived', version_number==2 (Fix 7)."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    # get_topic call (before archive)
    cur.fetchone.side_effect = [
        (
            "top_arc",
            "proj_A",
            "Archive Me",
            "body",
            "active",
            "user_a",
            "2026-07-20T10:00:00Z",
            "2026-07-20T10:00:00Z",
            1,
        ),  # get_topic check in handler
        # archive_topic calls update_topic which calls get_topic internally via cursor
        (
            "top_arc",
            "proj_A",
            "Archive Me",
            "body",
            "active",
            "user_a",
            "2026-07-20T10:00:00Z",
            "2026-07-20T10:00:00Z",
        ),  # FOR UPDATE in update_topic
        (1,),  # max version_number
        (
            "top_arc",
            "proj_A",
            "Archive Me",
            "body",
            "archived",
            "user_a",
            "2026-07-20T10:00:00Z",
            "2026-07-20T10:15:00Z",
        ),  # UPDATE RETURNING
    ]
    cur.description = [
        ("id",),
        ("project_id",),
        ("title",),
        ("body_md",),
        ("status",),
        ("created_by",),
        ("created_at",),
        ("updated_at",),
    ]

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.post("/api/context/topics/top_arc/archive")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "archived"
    assert data["version_number"] == 2


def test_create_procedure_valid_returns_201_proc_id():
    """Valid procedure create returns 201 with proc_<ULID> id (Fix 7)."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    cur.fetchone.return_value = (
        "proc_01ABCDEF12345678901234",
        "proj_A",
        "my_proc",
        "A test procedure",
        "name: my_proc\ndescription: A test procedure",
        "body text",
        "active",
        "user_a",
        "2026-07-20T10:00:00Z",
        "2026-07-20T10:00:00Z",
    )
    cur.description = [
        ("id",),
        ("project_id",),
        ("name",),
        ("description",),
        ("frontmatter_yaml",),
        ("body_md",),
        ("status",),
        ("created_by",),
        ("created_at",),
        ("updated_at",),
    ]

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.post(
            "/api/context/procedures",
            json={
                "project_id": "proj_A",
                "frontmatter_yaml": "name: my_proc\ndescription: A test procedure",
                "body_md": "body text",
            },
        )

    assert resp.status_code == 201
    data = resp.json()
    assert data["id"].startswith("proc_")
    assert data["version_number"] == 1


def test_no_membership_returns_404_on_post():
    """No membership (role check returns False) → 404 on a POST write (Fix 7)."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_outsider")),
        patch("core.project_access.identity_has_project_role", return_value=False),
        patch("core.db.get_connection", return_value=conn),
        patch("core.context_api.write_audit_row"),
    ):
        resp = client.post(
            "/api/context/topics",
            json={
                "project_id": "proj_private",
                "title": "Should be denied",
                "body_md": "",
            },
        )

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


# ---------------------------------------------------------------------------
# Fix 3: Platform-write deny-by-default seam tests
# ---------------------------------------------------------------------------


def test_platform_write_denied_for_project_member_without_allowlist(monkeypatch):
    """A project Member NOT in CONTEXT_PLATFORM_WRITERS → 403/404 on
    platform-scope write (Fix 3)."""
    monkeypatch.setenv("CONTEXT_PLATFORM_WRITERS", "")
    monkeypatch.setenv("TOOROW_AUTH_MODE", "jwt")  # non-disabled so anon bypass doesn't apply

    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()

    with (
        patch("core.admin_api._check_auth", return_value=(True, "member_user")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
        patch("core.context_api.write_audit_row"),
    ):
        # project_id=None (platform scope) → should be denied
        resp = client.post(
            "/api/context/topics",
            json={"project_id": None, "title": "Platform Topic", "body_md": ""},
        )

    # Denied: not in allowlist and not disabled-auth mode
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_project_member_can_write_project_scope_rows(monkeypatch):
    """A project Member NOT platform-authorized CAN still write project-scope rows (Fix 3)."""
    monkeypatch.setenv("CONTEXT_PLATFORM_WRITERS", "")
    monkeypatch.setenv("TOOROW_AUTH_MODE", "jwt")

    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    cur.fetchone.return_value = (
        "top_proj_ok",
        "proj_A",
        "Project Topic",
        "",
        "active",
        "member_user",
        "2026-07-20T10:00:00Z",
        "2026-07-20T10:00:00Z",
    )
    cur.description = [
        ("id",),
        ("project_id",),
        ("title",),
        ("body_md",),
        ("status",),
        ("created_by",),
        ("created_at",),
        ("updated_at",),
    ]

    with (
        patch("core.admin_api._check_auth", return_value=(True, "member_user")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.post(
            "/api/context/topics",
            json={"project_id": "proj_A", "title": "Project Topic", "body_md": ""},
        )

    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# Fix 5: List-without-project_id policy (platform rows readable by any authenticated)
# ---------------------------------------------------------------------------


def test_list_topics_without_project_id_returns_platform_rows():
    """GET /api/context/topics with no project_id returns platform rows
    for any authenticated (Fix 5)."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    cur.fetchall.return_value = [
        (
            "top_platform_only",
            None,
            "Global Platform Topic",
            "body",
            "active",
            "admin",
            "2026-07-20T09:00:00Z",
            "2026-07-20T09:00:00Z",
            1,
        ),
    ]
    cur.description = [
        ("id",),
        ("project_id",),
        ("title",),
        ("body_md",),
        ("status",),
        ("created_by",),
        ("created_at",),
        ("updated_at",),
        ("version_number",),
    ]

    with (
        patch("core.admin_api._check_auth", return_value=(True, "any_authenticated_user")),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.get("/api/context/topics")

    assert resp.status_code == 200
    topics = resp.json()["topics"]
    assert any(t["id"] == "top_platform_only" for t in topics)


def test_list_topics_unauthenticated_returns_401():
    """GET /api/context/topics without auth → 401 (Fix 5, explicit auth gate)."""
    app = build_asgi_app()
    client = TestClient(app)

    with patch("core.admin_api._check_auth", return_value=(False, "")):
        resp = client.get("/api/context/topics")

    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


# ---------------------------------------------------------------------------
# Fix 2: Archived-name reuse seam test
# ---------------------------------------------------------------------------


def test_archived_procedure_name_can_be_reused():
    """Archive a procedure, create a new one with same name in same scope → 201 (not 409) (Fix 2).

    Since we dropped the manual pre-check in create_procedure, the DB partial index
    (WHERE status != 'archived') allows this. The mock returns None (no UniqueViolation),
    simulating the DB correctly allowing the insert.
    """
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    # DB returns the new procedure row (no uniqueness violation for archived name)
    cur.fetchone.return_value = (
        "proc_new_after_archive",
        "proj_A",
        "reused_name",
        "Fresh start",
        "name: reused_name\ndescription: Fresh start",
        "",
        "active",
        "user_a",
        "2026-07-20T12:00:00Z",
        "2026-07-20T12:00:00Z",
    )
    cur.description = [
        ("id",),
        ("project_id",),
        ("name",),
        ("description",),
        ("frontmatter_yaml",),
        ("body_md",),
        ("status",),
        ("created_by",),
        ("created_at",),
        ("updated_at",),
    ]

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.post(
            "/api/context/procedures",
            json={
                "project_id": "proj_A",
                "frontmatter_yaml": "name: reused_name\ndescription: Fresh start",
                "body_md": "",
            },
        )

    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "reused_name"
    assert data["status"] == "active"
