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
import pytest
from core import candidate_fate
from core.main import build_asgi_app
from starlette.testclient import TestClient


@pytest.fixture(autouse=True)
def _strict_context_access_allowed_by_default():
    with patch("core.admin_api._strict_project_capability_allowed", return_value=True):
        yield


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
        None,
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
        ("owner",),
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
            "/api/context/topics?project_id=proj_A",
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
                None,
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
                None,
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
                None,
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
            ("owner",),
            ("created_by",),
            ("created_at",),
            ("updated_at",),
        ]

        resp1 = client.patch(
            "/api/context/topics/top_123?project_id=proj_A",
            json={"title": "Update 1", "expected_version": 1},
        )
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
                None,
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
                None,
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
                None,
                "user_test",
                "2026-07-20T10:00:00Z",
                "2026-07-20T10:10:00Z",
            ),  # UPDATE RETURNING
        ]

        resp2 = client.patch(
            "/api/context/topics/top_123?project_id=proj_A",
            json={"title": "Update 2", "expected_version": 2},
        )
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
        None,
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
        ("owner",),
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
        mock_audit.assert_not_called()


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
            None,
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
            None,
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
        ("owner",),
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
            "/api/context/procedures?project_id=proj_A",
            json={
                "project_id": "proj_A",
                "frontmatter_yaml": "description: missing name key",
                "body_md": "proc body",
            },
        )
        assert resp.status_code == 422
        data = resp.json()
        assert data["code"] == "invalid_frontmatter"
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
            "/api/context/procedures?project_id=proj_A",
            json={
                "project_id": "proj_A",
                "frontmatter_yaml": "name: existing_proc\ndescription: desc",
                "body_md": "proc body",
            },
        )
        assert resp.status_code == 409
        data = resp.json()
        assert data["code"] == "duplicate_name"
        assert "already exists" in data["message"]


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
            None,
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
            None,
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
            None,
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
        ("owner",),
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
            "/api/context/topics/top_arc/archive?project_id=proj_A",
            json={"expected_version": 1},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "archived"
    assert data["version_number"] == 2


# ---------------------------------------------------------------------------
# The way back (2026-08-18): POST /{id}/restore, same guards as /archive.
# ---------------------------------------------------------------------------

_TOPIC_COLS = [
    ("id",),
    ("project_id",),
    ("title",),
    ("body_md",),
    ("status",),
    ("owner",),
    ("created_by",),
    ("created_at",),
    ("updated_at",),
]


def _seam_topic_row(status: str, *, with_version: bool = False):
    row = (
        "top_arc",
        "proj_A",
        "Archived Me",
        "body",
        status,
        None,
        "user_a",
        "2026-07-20T10:00:00Z",
        "2026-07-20T10:15:00Z",
    )
    return (*row, 2) if with_version else row


def test_restore_topic_200_active_status_next_version():
    """Restore returns 200, status='active', and the NEXT version number."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    cur.fetchone.side_effect = [
        _seam_topic_row("archived", with_version=True),  # get_topic in the handler
        _seam_topic_row("archived"),  # FOR UPDATE in update_topic
        (2,),  # max version_number
        _seam_topic_row("active"),  # UPDATE RETURNING
    ]
    cur.description = _TOPIC_COLS

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.post(
            "/api/context/topics/top_arc/restore?project_id=proj_A",
            json={"expected_version": 2},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "active"
    assert data["version_number"] == 3


def test_restore_topic_refuses_an_active_topic_with_a_typed_message():
    """A gesture with nothing to undo is refused, never answered "done"."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    cur.fetchone.side_effect = [
        _seam_topic_row("active", with_version=True),  # get_topic in the handler
        _seam_topic_row("active"),  # FOR UPDATE
        (2,),  # max version_number
    ]
    cur.description = _TOPIC_COLS

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.post("/api/context/topics/top_arc/restore?project_id=proj_A", json={})

    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "not_archived"
    assert "nothing to restore" in body["message"]


def test_restore_topic_refused_for_an_identity_without_edit():
    """Exactly what archive answers: the non-disclosing not-found."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, _cur = _make_mock_conn()

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.admin_api._strict_project_capability_allowed", return_value=False),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.post("/api/context/topics/top_arc/restore?project_id=proj_A", json={})

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_restore_topic_unauthenticated_is_401():
    app = build_asgi_app()
    client = TestClient(app)

    with patch("core.admin_api._check_auth", return_value=(False, "")):
        resp = client.post("/api/context/topics/top_arc/restore?project_id=proj_A", json={})
        assert resp.status_code == 401
        assert resp.json()["code"] == "unauthorized"


def test_restore_topic_version_conflict_is_409():
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    cur.fetchone.side_effect = [
        _seam_topic_row("archived", with_version=True),
        _seam_topic_row("archived"),
        (7,),  # the row moved since the caller read it
    ]
    cur.description = _TOPIC_COLS

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.post(
            "/api/context/topics/top_arc/restore?project_id=proj_A",
            json={"expected_version": 2},
        )

    assert resp.status_code == 409
    assert resp.json()["code"] == "version_conflict"


def test_restore_procedure_200_active_status():
    """The store is symmetric, so the route is -- the Skills console follows later."""
    app = build_asgi_app()
    client = TestClient(app)

    proc_cols = [
        ("id",),
        ("project_id",),
        ("name",),
        ("description",),
        ("frontmatter_yaml",),
        ("body_md",),
        ("status",),
        ("owner",),
        ("created_by",),
        ("created_at",),
        ("updated_at",),
    ]

    def proc_row(status: str, *, with_version: bool = False):
        row = (
            "proc_arc",
            "proj_A",
            "proc_arc",
            "desc",
            "name: proc_arc\ndescription: desc",
            "",
            status,
            None,
            "user_a",
            "2026-07-20T12:00:00Z",
            "2026-07-20T12:05:00Z",
        )
        return (*row, 2) if with_version else row

    conn, cur = _make_mock_conn()
    cur.fetchone.side_effect = [
        proc_row("archived", with_version=True),  # get_procedure in the handler
        proc_row("archived"),  # FOR UPDATE
        None,  # no ACTIVE row holds this name
        (2,),  # max version_number
        proc_row("active"),  # UPDATE RETURNING
    ]
    cur.description = proc_cols

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.post(
            "/api/context/procedures/proc_arc/restore?project_id=proj_A",
            json={"expected_version": 2},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "active"
    assert data["version_number"] == 3


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
        None,
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
        ("owner",),
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
            "/api/context/procedures?project_id=proj_A",
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
        patch("core.admin_api._strict_project_capability_allowed", return_value=False),
        patch("core.db.get_connection", return_value=conn),
        patch("core.context_api.write_audit_row"),
    ):
        resp = client.post(
            "/api/context/topics?project_id=proj_private",
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

    # Platform creation is not exposed by this project-scoped surface.
    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_param"


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
        None,
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
        ("owner",),
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
            "/api/context/topics?project_id=proj_A",
            json={"project_id": "proj_A", "title": "Project Topic", "body_md": ""},
        )

    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# Fix 5: List-without-project_id policy (platform rows readable by any authenticated)
# ---------------------------------------------------------------------------


def test_list_topics_without_project_id_is_rejected():
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
            None,
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
        ("owner",),
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

    assert resp.status_code == 422
    cur.execute.assert_not_called()


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
        None,
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
        ("owner",),
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
            "/api/context/procedures?project_id=proj_A",
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


# ---------------------------------------------------------------------------
# Story 44.11: owner PATCH + Request review
# ---------------------------------------------------------------------------


def test_patch_topic_owner_round_trips():
    """PATCH /api/context/topics/{id} with an `owner` key updates the row and
    the response reflects it (Story 44.11)."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    cur.fetchone.side_effect = [
        (
            "top_own",
            "proj_A",
            "Owned Topic",
            "body",
            "active",
            None,
            "user_a",
            "2026-07-20T10:00:00Z",
            "2026-07-20T10:00:00Z",
        ),  # get_topic check
        (
            "top_own",
            "proj_A",
            "Owned Topic",
            "body",
            "active",
            None,
            "user_a",
            "2026-07-20T10:00:00Z",
            "2026-07-20T10:00:00Z",
        ),  # update_topic FOR UPDATE
        (1,),  # max version
        (
            "top_own",
            "proj_A",
            "Owned Topic",
            "body",
            "active",
            "grace@toorow.com",
            "user_a",
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
        ("owner",),
        ("created_by",),
        ("created_at",),
        ("updated_at",),
    ]

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.patch(
            "/api/context/topics/top_own?project_id=proj_A",
            json={"owner": "grace@toorow.com", "expected_version": 1},
        )

    assert resp.status_code == 200
    assert resp.json()["owner"] == "grace@toorow.com"


def test_patch_topic_owner_invalid_type_422():
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    cur.fetchone.return_value = (
        "top_own2",
        "proj_A",
        "Owned Topic",
        "body",
        "active",
        None,
        "user_a",
        "2026-07-20T10:00:00Z",
        "2026-07-20T10:00:00Z",
    )
    cur.description = [
        ("id",),
        ("project_id",),
        ("title",),
        ("body_md",),
        ("status",),
        ("owner",),
        ("created_by",),
        ("created_at",),
        ("updated_at",),
    ]

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.patch(
            "/api/context/topics/top_own2?project_id=proj_A",
            json={"owner": 42, "expected_version": 1},
        )

    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_param"


def test_procedure_detail_carries_what_the_skill_designates_of_the_data_model():
    """AI-157 : `mdm_references` ne vivait que dans l'outil MCP `get_procedure`.

    La console rendait `SkillStepList` avec une prop `mdmReferences` que RIEN ne
    remplissait : un agent savait de quoi parlait une Skill, la personne qui
    l'ecrit ne le savait pas. Ce test tient le MEME calcul
    (`core/mdm_references.resolve`) sur la route REST.

    Les DEUX cotes voyagent -- une reference qui ne resout pas est le retour sur
    la Skill, et ne rendre que `resolved` laisserait croire qu'une Skill ne cite
    que des champs existants.
    """
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    procedure_description = [
        ("id",), ("project_id",), ("name",), ("description",),
        ("frontmatter_yaml",), ("body_md",), ("status",), ("owner",),
        ("created_by",), ("created_at",), ("updated_at",), ("version_number",),
    ]
    cur.description = procedure_description
    cur.fetchone.return_value = (
        "proc_mdm", "proj_A", "cost review", "desc",
        'name: cost review\nmdm_tags: ["media_cost"]\n',
        "Compute {{media_cost}} per {{country_gone}}.",
        "active", None, "user_a",
        "2026-07-20T10:00:00Z", "2026-07-20T10:00:00Z", 1,
    )
    # Le catalogue connait `media_cost` et ignore `country_gone`. Depuis 49.3 le
    # catalogue gouverne lit ses lignes PAR NOM DE COLONNE (governed_field_catalogue
    # `_fetch` zippe `cur.description`) : la description doit donc etre celle du
    # statement en cours, pas celle de la procedure -- un seul curseur, trois
    # projections (rouge a HEAD le 2026-08-30 tant que la fixture l ignorait).
    concept_description = [
        ("name",), ("project_id",), ("kind",), ("label",), ("value_type",),
        ("definition",), ("aggregation",), ("additivity_class",),
    ]
    dictionary_description = [
        ("name",), ("display_name",), ("data_type",), ("field_kind",),
        ("measure",), ("description",), ("status",),
    ]
    dictionary_rows = [
        ("media_cost", "Media cost", "numeric", "metric", "sum",
         "Net media spend", "approved"),
    ]

    def _execute(sql, params=None):
        flat = " ".join(str(sql).split()).lower()
        if "from app.semantic_concepts" in flat:
            cur.description = concept_description
            cur.fetchall.return_value = []
        elif "from app.target_fields" in flat:
            cur.description = dictionary_description
            cur.fetchall.return_value = dictionary_rows
        else:
            cur.description = procedure_description

    cur.execute.side_effect = _execute

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.get("/api/context/procedures/proc_mdm?project_id=proj_A")

    assert resp.status_code == 200
    refs = resp.json()["mdm_references"]
    assert [f["name"] for f in refs["inline"]["resolved"]] == ["media_cost"]
    assert refs["inline"]["resolved"][0]["status"] == "approved"
    assert refs["inline"]["unresolved"] == ["country_gone"]
    # `mdm_tags` designe aussi le catalogue -- et par le meme chemin.
    assert [f["name"] for f in refs["tags"]["resolved"]] == ["media_cost"]


#: Les colonnes que `context_review` rend, dans son ordre. Ecrites ici plutot
#: qu'importees : si le module en ajoute une sans que la route la rende, ce test
#: doit s'en apercevoir.
_REVIEW_ROW = (
    "crr_01",           # id
    "org_1",            # org_id
    "proj_A",           # project_id
    "topic",            # node_type
    "top_review",       # node_id
    3,                  # node_version
    "Please double-check the FX rate.",  # note
    "requester_user",   # requested_by
    "human",            # origin
    "open",             # status
    None,               # resolved_by
    None,               # resolved_at
    "2026-08-04T10:00:00Z",  # created_at
    None,               # proposed_change
    None,               # applied_ref
)

_TOPIC_ROW = (
    "top_review",
    "proj_A",
    "Reviewed Topic",
    "body",
    "active",
    None,
    "user_a",
    "2026-07-20T10:00:00Z",
    "2026-07-20T10:00:00Z",
    3,
)
_TOPIC_DESCRIPTION = [
    ("id",),
    ("project_id",),
    ("title",),
    ("body_md",),
    ("status",),
    ("owner",),
    ("created_by",),
    ("created_at",),
    ("updated_at",),
    ("version_number",),
]


def test_request_review_queues_the_remark_at_the_node_version():
    """AI-157 : la remarque part en FILE, pas seulement dans le journal d'audit.

    Jusqu'au 2026-08-04 ce handler ecrivait UNE ligne `context.review_requested`
    et rien d'autre : un journal append-only et scelle, dont aucune remarque ne
    ressort. `core/context_review.py` portait la file, testee, et aucune route ne
    l'appelait.

    Ce que ce test tient, et qui est le fond : la VERSION du noeud voyage avec la
    remarque. « Cette contrainte n'existe plus » ne veut rien dire si l'on ignore
    de quelle version on parle.
    """
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    cur.description = _TOPIC_DESCRIPTION
    cur.fetchone.side_effect = [
        _TOPIC_ROW,      # get_topic
        ("org_1",),      # _project_org_id
        _REVIEW_ROW,     # INSERT ... RETURNING
    ]

    with (
        patch("core.admin_api._check_auth", return_value=(True, "requester_user")),
        patch("core.db.get_connection", return_value=conn),
        patch("core.context_api.insert_audit_row") as mock_insert_audit,
    ):
        resp = client.post(
            "/api/context/nodes/top_review/request-review?project_id=proj_A",
            json={"node_type": "topic", "note": "Please double-check the FX rate."},
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "requested"
    # Une file dont on ne peut pas nommer l'entree est une file qu'on ne peut
    # pas clore : l'id revient dans la reponse.
    assert body["request"]["id"] == "crr_01"
    assert body["request"]["node_version"] == 3
    assert body["request"]["status"] == "open"

    # Le SELECT du noeud a bien lu la version, et l'INSERT l'a portee.
    insert_sql = next(
        call for call in cur.execute.call_args_list
        if "context_review_requests" in call.args[0]
    )
    assert insert_sql.args[1][5] == 3

    # L'audit RESTE ecrit -- il n'est pas remplace, il est complete, et il POINTE
    # desormais sur la ligne de file.
    mock_insert_audit.assert_called_once()
    kwargs = mock_insert_audit.call_args.kwargs
    assert kwargs["identity"] == "requester_user"
    assert kwargs["action"] == "context.review_requested"
    assert kwargs["metadata"]["node_id"] == "top_review"
    assert kwargs["metadata"]["request_id"] == "crr_01"
    assert kwargs["metadata"]["node_version"] == 3


def test_request_review_without_a_note_is_refused():
    """Une demande de relecture qui ne dit rien n'est pas une remarque, c'est un
    clic -- et la table la refuse (`length(btrim(note)) >= 1`). Le handler le
    disait implicitement en ecrivant `null` dans l'audit ; il le dit maintenant."""
    app = build_asgi_app()
    client = TestClient(app)

    with patch("core.admin_api._check_auth", return_value=(True, "user_a")):
        resp = client.post(
            "/api/context/nodes/top_x/request-review?project_id=proj_A",
            json={"node_type": "topic"},
        )

    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_param"
    assert "note is required" in resp.json()["message"]


def test_request_review_without_a_project_is_refused():
    """`project_id` est la SEULE source de l'organisation que la file exige -- et
    un noeud de plateforme n'en porte aucune."""
    app = build_asgi_app()
    client = TestClient(app)

    with patch("core.admin_api._check_auth", return_value=(True, "user_a")):
        resp = client.post(
            "/api/context/nodes/top_x/request-review",
            json={"node_type": "topic", "note": "look at this"},
        )

    assert resp.status_code == 422
    assert resp.json()["message"] == "project_id is required"


def test_request_review_invalid_node_type_422():
    app = build_asgi_app()
    client = TestClient(app)

    with patch("core.admin_api._check_auth", return_value=(True, "user_a")):
        resp = client.post(
            "/api/context/nodes/top_x/request-review?project_id=proj_A",
            json={"node_type": "schema_doc", "note": "n"},
        )

    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_param"


def test_request_review_unknown_node_404_non_disclosing():
    """A node the caller cannot see (missing, or out of scope) comes back as a
    plain 404 -- never distinguishing 'does not exist' from 'not yours'."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    cur.fetchone.return_value = None

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.db.get_connection", return_value=conn),
        patch("core.context_api.write_audit_row") as mock_cross_scope_audit,
    ):
        resp = client.post(
            "/api/context/nodes/top_missing/request-review?project_id=proj_A",
            json={"node_type": "topic", "note": "look at this"},
        )

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"
    mock_cross_scope_audit.assert_called_once()


def test_request_review_cross_project_404_non_disclosing():
    """Le noeud existe, mais dans un AUTRE projet que celui de l'URL -> 404, meme
    forme que le noeud absent (non divulguant). La garde vient de
    `get_topic(caller_project_id=...)`, pas d'un test ecrit a cote."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    cur.fetchone.return_value = (
        "proc_private",
        "proj_B",
        "proc_name",
        "desc",
        "name: proc_name\ndescription: desc",
        "",
        "active",
        None,
        "user_b",
        "2026-07-20T10:00:00Z",
        "2026-07-20T10:00:00Z",
        1,
    )
    cur.description = [
        ("id",),
        ("project_id",),
        ("name",),
        ("description",),
        ("frontmatter_yaml",),
        ("body_md",),
        ("status",),
        ("owner",),
        ("created_by",),
        ("created_at",),
        ("updated_at",),
        ("version_number",),
    ]

    with (
        patch("core.admin_api._check_auth", return_value=(True, "outsider")),
        patch("core.db.get_connection", return_value=conn),
        patch("core.context_api.write_audit_row") as mock_cross_scope_audit,
    ):
        resp = client.post(
            "/api/context/nodes/proc_private/request-review?project_id=proj_A",
            json={"node_type": "procedure", "note": "look at this"},
        )

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"
    mock_cross_scope_audit.assert_called_once()


def test_request_review_unauthenticated_401():
    app = build_asgi_app()
    client = TestClient(app)

    with patch("core.admin_api._check_auth", return_value=(False, "")):
        resp = client.post(
            "/api/context/nodes/top_x/request-review?project_id=proj_A",
            json={"node_type": "topic", "note": "n"},
        )

    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


def test_review_queue_is_readable():
    """AI-157 : la file se LIT. Sans cette route, `list_open` restait du code
    prouve et inatteignable -- exactement le defaut que AI-157 nomme."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    cur.fetchone.return_value = ("org_1",)
    cur.fetchall.return_value = [_REVIEW_ROW]

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.get("/api/context/review-requests?project_id=proj_A")

    assert resp.status_code == 200
    body = resp.json()
    assert [r["id"] for r in body["requests"]] == ["crr_01"]
    assert body["requests"][0]["note"] == "Please double-check the FX rate."
    assert body["can_resolve"] is True


def test_review_queue_requires_a_project():
    app = build_asgi_app()
    client = TestClient(app)

    with patch("core.admin_api._check_auth", return_value=(True, "user_a")):
        resp = client.get("/api/context/review-requests")

    assert resp.status_code == 422
    assert resp.json()["message"] == "project_id is required"


def test_review_request_resolves_and_reports_what_it_produced():
    """Clore, c'est `accepted` ou `declined` -- jamais un silence. Et la reponse
    porte `applied_ref` : accepter et appliquer ne sont pas le meme fait."""
    app = build_asgi_app()
    client = TestClient(app)

    resolved_row = (
        *_REVIEW_ROW[:9], "accepted", "owner_user", "2026-08-04T11:00:00Z",
        _REVIEW_ROW[12], None, None,
    )
    conn, cur = _make_mock_conn()
    cur.fetchone.side_effect = [
        ("org_1",),     # _project_org_id
        resolved_row,   # UPDATE ... RETURNING
    ]

    with (
        patch("core.admin_api._check_auth", return_value=(True, "owner_user")),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.post(
            "/api/context/review-requests/crr_01/resolve?project_id=proj_A",
            json={"status": "accepted"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["resolved_by"] == "owner_user"
    # Sans charge, rien n'est applique -- et c'est le cas courant.
    assert body["applied_ref"] is None
    conn.commit.assert_called_once()


def test_review_request_resolve_refuses_an_unknown_status():
    """Ni `open`, ni un mot invente : une file qui accepte n'importe quel etat
    n'est plus une file."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    cur.fetchone.return_value = ("org_1",)

    with (
        patch("core.admin_api._check_auth", return_value=(True, "owner_user")),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.post(
            "/api/context/review-requests/crr_01/resolve?project_id=proj_A",
            json={"status": "maybe"},
        )

    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_param"


def test_review_request_already_closed_is_409_not_a_second_verdict():
    """Une remarque deja close a un id qui EXISTE : le second verdict est un
    conflit nomme (409 already_resolved), pas le 404 non-divulgateur d'un id
    inconnu -- sinon deux acceptations contradictoires seraient
    indiscernables d'une coquille."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    cur.fetchone.side_effect = [
        ("org_1",),     # _project_org_id
        None,           # UPDATE ... RETURNING : plus rien n'est 'open' sous cet id
        ("accepted",),  # ... mais l'id existe, deja clos
    ]

    with (
        patch("core.admin_api._check_auth", return_value=(True, "owner_user")),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.post(
            "/api/context/review-requests/crr_01/resolve?project_id=proj_A",
            json={"status": "declined"},
        )

    assert resp.status_code == 409
    assert resp.json()["code"] == "already_resolved"


def test_review_request_resolve_unknown_id_is_404_non_disclosing():
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    cur.fetchone.side_effect = [
        ("org_1",),  # _project_org_id
        None,        # UPDATE ... RETURNING : rien
        None,        # l'id n'existe pas non plus
    ]

    with (
        patch("core.admin_api._check_auth", return_value=(True, "owner_user")),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.post(
            "/api/context/review-requests/crr_gone/resolve?project_id=proj_A",
            json={"status": "declined"},
        )

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_review_request_resolve_requires_edit_not_view():
    """Deposer une remarque est un droit de lecteur ; la trancher est un acte
    d'ecriture -- une acceptation peut poser un lien en base."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, _cur = _make_mock_conn()

    with (
        patch("core.admin_api._check_auth", return_value=(True, "viewer_user")),
        patch("core.admin_api._strict_project_capability_allowed", return_value=False),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.post(
            "/api/context/review-requests/crr_01/resolve?project_id=proj_A",
            json={"status": "accepted"},
        )

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"

# ---------------------------------------------------------------------------
# Epic 43.22b -- immutable history route seam
# ---------------------------------------------------------------------------


def test_topic_versions_route_requires_scoped_view_and_returns_history():
    app = build_asgi_app()
    client = TestClient(app)
    conn, _ = _make_mock_conn()
    topic = {
        "id": "top_history",
        "project_id": "proj_A",
        "title": "History",
        "body_md": "",
        "status": "active",
        "owner": None,
        "created_by": "user_a",
        "created_at": "2026-07-26T00:00:00Z",
        "updated_at": "2026-07-26T00:00:00Z",
        "version_number": 2,
    }
    versions = [{"topic_id": "top_history", "version_number": 2}]

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.db.get_connection", return_value=conn),
        patch("core.context_store.get_topic", return_value=topic),
        patch("core.context_store.list_topic_versions", return_value=versions),
    ):
        response = client.get(
            "/api/context/topics/top_history/versions?project_id=proj_A"
        )

    assert response.status_code == 200
    assert response.json()["versions"] == versions


def test_legacy_knowledge_route_is_removed():
    client = TestClient(build_asgi_app())

    response = client.get("/api/knowledge?project_id=proj_A")

    assert response.status_code == 404


def test_skill_tool_catalog_is_live_project_scoped_and_bounded(monkeypatch):
    from types import SimpleNamespace

    async def fake_provider():
        return [
            SimpleNamespace(
                name="get_daily_report",
                description="Read the governed daily KPI report.",
                meta={},
                tags=set(),
            ),
            SimpleNamespace(
                name="publish_mapping",
                description="Publish a reviewed mapping.",
                meta={
                    "profile": "governance",
                    "effect": "confirmed_write",
                    "data_class": "sensitive",
                    "confirmation_mode": "human",
                },
                tags=set(),
            ),
        ]

    monkeypatch.setattr("core.skill_tool_catalog._provider", fake_provider)
    app = build_asgi_app()
    client = TestClient(app)
    conn, _ = _make_mock_conn()

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_test")),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.get("/api/context/skill-tools?project_id=proj_A")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["catalog_version"]) == 64
    assert body["tools"] == [
        {
            "name": "get_daily_report",
            "description": "Read the governed daily KPI report.",
            "profile": "insights",
            "effect": "read",
            "data_class": "operational",
            "confirmation_mode": "none",
        },
        {
            "name": "publish_mapping",
            "description": "Publish a reviewed mapping.",
            "profile": "governance",
            "effect": "confirmed_write",
            "data_class": "sensitive",
            "confirmation_mode": "human",
        },
    ]


def test_skill_tool_catalog_hides_cross_project_scope(monkeypatch):
    provider = MagicMock()
    monkeypatch.setattr("core.skill_tool_catalog._provider", provider)
    app = build_asgi_app()
    client = TestClient(app)
    conn, _ = _make_mock_conn()

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_test")),
        patch("core.admin_api._strict_project_capability_allowed", return_value=False),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.get("/api/context/skill-tools?project_id=other_project")

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"
    provider.assert_not_called()


# --- le repli : ce que la recherche ecarte a repetition ---------------------


def test_recurrent_fates_are_readable_with_their_threshold():
    """AI-210 -- le signal etait agrege et n'avait AUCUN lecteur.

    `record_fates` conserve le sort de chaque candidat ecarte depuis le
    2026-08-04 et `recurrent_fates` sait dire lesquels reviennent. Aucun
    appelant : le repli avait donc une arrivee (un lien `derived` confirme) et
    aucun DEPART. Cette route est le depart.
    """
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    cur.fetchall.return_value = [
        ("proc_7", "procedure", candidate_fate.REASON_OUT_OF_SCOPE, 9, "attribution window",
         "2026-08-01T09:00:00Z", "2026-08-05T09:00:00Z"),
    ]

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.get("/api/context/recurrent-fates?project_id=proj_A&minimum=5")

    assert resp.status_code == 200
    body = resp.json()
    assert body["minimum"] == 5
    assert [row["candidate_id"] for row in body["fates"]] == ["proc_7"]
    assert body["fates"][0]["reason"] == candidate_fate.REASON_OUT_OF_SCOPE
    assert body["fates"][0]["times"] == 9
    # Le seuil demande est celui qui part en base : une valeur affichee qui n'est
    # pas celle appliquee ferait lire un chiffre pour un autre.
    assert cur.execute.call_args[0][1] == ("proj_A", 5)


def test_recurrent_fates_refuse_a_threshold_that_is_not_a_count():
    app = build_asgi_app()
    client = TestClient(app)

    with patch("core.admin_api._check_auth", return_value=(True, "user_a")):
        zero = client.get("/api/context/recurrent-fates?project_id=proj_A&minimum=0")
        text = client.get("/api/context/recurrent-fates?project_id=proj_A&minimum=often")

    assert zero.status_code == 422
    assert text.status_code == 422


def test_recurrent_fates_require_a_project():
    app = build_asgi_app()
    client = TestClient(app)

    with patch("core.admin_api._check_auth", return_value=(True, "user_a")):
        resp = client.get("/api/context/recurrent-fates")

    assert resp.status_code == 422
    assert resp.json()["message"] == "project_id is required"


def test_list_review_requests_unknown_project_is_404_not_500():
    """Constat live A2 (2026-08-05) : quand la seam d'acces court-circuite (mode
    auth desactivee), l'existence du projet n'est pas verifiee par le gate ;
    `_project_org_id` levait alors ValueError et le handler repondait 500
    `db_error`. Un projet inconnu est un 404 non-divulgateur, pas une erreur
    serveur."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    cur.fetchone.return_value = None  # app.projects ne connait pas ce projet

    with (
        patch("core.admin_api._check_auth", return_value=(True, "anonymous")),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = client.get("/api/context/review-requests?project_id=proj_GONE")

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


# ---------------------------------------------------------------------------
# Live finding F1 (2026-08-05): strict status filter on topics/procedures
# ---------------------------------------------------------------------------


def test_topics_status_filter_is_strict_and_validated():
    """`status=archived` returned active + archived (include_archived semantics)
    and `status=all` excluded archived -- the exact opposite of the contract.
    The filter is now strict, and an unknown value is a 422."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.db.get_connection", return_value=conn),
        patch("core.context_store.list_topics", return_value=[]) as list_mock,
    ):
        archived = client.get("/api/context/topics?project_id=proj_A&status=archived")
        all_status = client.get("/api/context/topics?project_id=proj_A&status=all")
        bogus = client.get("/api/context/topics?project_id=proj_A&status=bogus")

    assert archived.status_code == 200
    assert list_mock.call_args_list[0].kwargs["status"] == "archived"
    assert all_status.status_code == 200
    assert list_mock.call_args_list[1].kwargs["status"] == "all"
    assert bogus.status_code == 422
    assert bogus.json()["code"] == "invalid_param"


def test_procedures_status_filter_is_strict_and_validated():
    """Same contract as the topics list -- one semantics for every lifecycle
    filter of the Context surfaces."""
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.db.get_connection", return_value=conn),
        patch("core.context_store.list_procedures", return_value=[]) as list_mock,
    ):
        archived = client.get("/api/context/procedures?project_id=proj_A&status=archived")
        bogus = client.get("/api/context/procedures?project_id=proj_A&status=bogus")

    assert archived.status_code == 200
    assert list_mock.call_args_list[0].kwargs["status"] == "archived"
    assert bogus.status_code == 422
    assert bogus.json()["code"] == "invalid_param"


def test_procedure_analysis_picker_is_server_bounded_and_compact():
    app = build_asgi_app()
    client = TestClient(app)
    conn, _cur = _make_mock_conn()
    procedures = [
        {
            "id": f"proc_{index}",
            "name": f"Skill {index}",
            "version_number": 1,
            "body_md": "must not cross the picker wire",
        }
        for index in range(201)
    ]
    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.db.get_connection", return_value=conn),
        patch("core.context_store.list_procedure_picker", return_value=procedures) as list_mock,
    ):
        response = client.get(
            "/api/context/procedures?project_id=proj_A&projection=analysis-picker"
        )

    assert response.status_code == 200
    assert len(response.json()["procedures"]) == 200
    assert response.json()["truncated"] is True
    assert "body_md" not in response.text
    list_mock.assert_called_once_with(
        conn,
        project_id="proj_A",
        status="active",
        limit=201,
    )


def test_object_detail_answers_whether_this_caller_may_write():
    """The WORKBENCH reads the detail route, and it must know before it offers.

    Until 2026-08-17 only the LIST routes carried `capabilities`. The detail
    route stayed silent, so the object workbench had no way to ask "may I write
    here?" -- which is why editing a Knowledge item or a Skill lived only on the
    collection screen, and why a remark read on the workbench could be closed
    and never answered (`context-hub.md`, "a remark can only be closed and not
    acted on").

    Same rule as the list: the project's `edit` capability decides, and the
    caller's inability to write is a `can_write: false`, never an absent key --
    a screen cannot tell "not allowed" from "not answered" otherwise.
    """
    app = build_asgi_app()
    client = TestClient(app)

    conn, cur = _make_mock_conn()
    cur.description = _TOPIC_DESCRIPTION
    cur.fetchone.return_value = _TOPIC_ROW

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.db.get_connection", return_value=conn),
    ):
        allowed = client.get("/api/context/topics/top_review?project_id=proj_A")

    assert allowed.status_code == 200
    assert allowed.json()["capabilities"]["can_write"] is True

    conn2, cur2 = _make_mock_conn()
    cur2.description = _TOPIC_DESCRIPTION
    cur2.fetchone.return_value = _TOPIC_ROW

    # `view` passes, `edit` does not: the object is readable and not writable,
    # and the payload says exactly that rather than dropping the key.
    def _view_only(_conn, *, identity, project_id, minimum_capability, **_kwargs):
        return minimum_capability == "view"

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.db.get_connection", return_value=conn2),
        patch("core.admin_api._strict_project_capability_allowed", _view_only),
    ):
        read_only = client.get("/api/context/topics/top_review?project_id=proj_A")

    assert read_only.status_code == 200
    assert read_only.json()["capabilities"]["can_write"] is False
