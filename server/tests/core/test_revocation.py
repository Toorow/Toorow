"""Tests for connection revocation and key lifecycle endpoints (Story 7.3, AC8).

Tests:
  - test_revoke_connection_marks_revoked
  - test_revoke_connection_clears_health_cache
  - test_revoke_connection_writes_audit_row
  - test_revoke_connection_wrong_project_returns_404
  - test_key_rotation_writes_tka_row
  - test_project_creation_provisions_key
  - test_project_archive_deletes_key

All DB-facing tests use live Postgres and are skipped when PLATFORM_DB_URL is unset.
Nango API calls are mocked (never hitting real Nango in unit tests).
"""

from __future__ import annotations

import json
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


# ---------------------------------------------------------------------------
# Skip helpers
# ---------------------------------------------------------------------------


def _pg_reachable() -> bool:
    """Probe the opt-in live database without hanging test collection."""
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False
    try:
        import psycopg

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pg_available = pytest.mark.skipif(not _pg_reachable(), reason="platform Postgres not reachable")

_AUTH = ("core.admin_api._check_auth", (True, "tester@example.com"))


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _insert_project(project_id: str, conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.projects (id, name, slug, status, currency, timezone, created_by)
            VALUES (%s, %s, %s, 'active', 'EUR', 'Europe/Paris', 'test')
            ON CONFLICT DO NOTHING
            """,
            (project_id, f"Test {project_id}", project_id),
        )


def _insert_connection(
    conn_id: str, project_id: str, nango_conn_id: str, provider: str, conn
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.connection_ref (id, provider, nango_connection_id, project_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (conn_id, provider, nango_conn_id, project_id),
        )


def _cleanup(project_id: str, conn) -> None:
    """Best-effort cleanup after tests.

    audit_log is append-only (migration 003 trigger blocks DELETE) and has a NOT NULL
    FK to connection_ref(id). When a real audit row was written (e.g. revocation test),
    we cannot delete the referenced connection_ref row or the project.
    Use SAVEPOINTs to attempt each delete and rollback only the failing step.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM app.tenant_key_audit WHERE project_id = %s", (project_id,))
        # Try to delete connection_ref rows (may fail if audit_log has references).
        try:
            cur.execute("SAVEPOINT sp_conn")
            cur.execute("DELETE FROM app.connection_ref WHERE project_id = %s", (project_id,))
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT sp_conn")
            # Leave orphaned connection_ref rows; project delete will also fail below.
            return
        finally:
            try:
                cur.execute("RELEASE SAVEPOINT sp_conn")
            except Exception:
                pass

        try:
            cur.execute("SAVEPOINT sp_proj")
            cur.execute("DELETE FROM app.projects WHERE id = %s", (project_id,))
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT sp_proj")
        finally:
            try:
                cur.execute("RELEASE SAVEPOINT sp_proj")
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Request mock helpers
# ---------------------------------------------------------------------------


def _post_request(path_params: dict) -> MagicMock:
    req = MagicMock()
    req.path_params = path_params
    req.body = AsyncMock(return_value=b"{}")
    return req


def _create_project_request(body: dict) -> MagicMock:
    req = MagicMock()
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    return req


def _delete_request(project_id: str) -> MagicMock:
    req = MagicMock()
    req.path_params = {"project_id": project_id}
    return req


# ---------------------------------------------------------------------------
# AC8: test_revoke_connection_marks_revoked
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_revoke_connection_marks_revoked(tmp_path):
    """POST /revoke marks connection_ref.status = 'revoked'."""
    from core.admin_api import _revoke_connection
    from core.db import get_connection

    proj_id = f"proj_{uuid.uuid4().hex[:10]}"
    conn_id = f"conn_{uuid.uuid4().hex[:10]}"
    nango_id = f"nango_{uuid.uuid4().hex[:8]}"

    with get_connection() as db:
        _insert_project(proj_id, db)
        _insert_connection(conn_id, proj_id, nango_id, "google-analytics", db)
        db.commit()

    try:
        with (
            patch(_AUTH[0], return_value=_AUTH[1]),
            patch("core.nango_client.delete_connection", return_value=True),
            patch("core.health_poller.purge_connection_cache"),
        ):
            resp = await _revoke_connection(
                _post_request({"project_id": proj_id, "connection_id": conn_id})
            )

        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["status"] == "revoked"

        # Verify DB row
        with get_connection() as db:
            with db.cursor() as cur:
                cur.execute("SELECT status FROM app.connection_ref WHERE id = %s", (conn_id,))
                row = cur.fetchone()
        assert row is not None
        assert row[0] == "revoked"
    finally:
        with get_connection() as db:
            _cleanup(proj_id, db)
            db.commit()


# ---------------------------------------------------------------------------
# AC8: test_revoke_connection_clears_health_cache
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_revoke_connection_clears_health_cache(tmp_path):
    """POST /revoke calls purge_connection_cache for the connection."""
    from core.admin_api import _revoke_connection
    from core.db import get_connection

    proj_id = f"proj_{uuid.uuid4().hex[:10]}"
    conn_id = f"conn_{uuid.uuid4().hex[:10]}"
    nango_id = f"nango_{uuid.uuid4().hex[:8]}"

    with get_connection() as db:
        _insert_project(proj_id, db)
        _insert_connection(conn_id, proj_id, nango_id, "google-analytics", db)
        db.commit()

    try:
        purge_calls = []

        def _fake_purge(cid):
            purge_calls.append(cid)

        with (
            patch(_AUTH[0], return_value=_AUTH[1]),
            patch("core.nango_client.delete_connection", return_value=True),
            patch("core.health_poller.purge_connection_cache", side_effect=_fake_purge),
        ):
            resp = await _revoke_connection(
                _post_request({"project_id": proj_id, "connection_id": conn_id})
            )

        assert resp.status_code == 200
        assert conn_id in purge_calls, "purge_connection_cache must be called with connection_id"
    finally:
        with get_connection() as db:
            _cleanup(proj_id, db)
            db.commit()


# ---------------------------------------------------------------------------
# AC8: test_revoke_connection_writes_audit_row
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_revoke_connection_writes_audit_row(tmp_path):
    """POST /revoke writes an audit row with action='connection.revoked'."""
    from core.admin_api import _revoke_connection
    from core.db import get_connection

    proj_id = f"proj_{uuid.uuid4().hex[:10]}"
    conn_id = f"conn_{uuid.uuid4().hex[:10]}"
    nango_id = f"nango_{uuid.uuid4().hex[:8]}"

    with get_connection() as db:
        _insert_project(proj_id, db)
        _insert_connection(conn_id, proj_id, nango_id, "google-analytics", db)
        db.commit()

    try:
        with (
            patch(_AUTH[0], return_value=_AUTH[1]),
            patch("core.nango_client.delete_connection", return_value=True),
            patch("core.health_poller.purge_connection_cache"),
        ):
            resp = await _revoke_connection(
                _post_request({"project_id": proj_id, "connection_id": conn_id})
            )

        assert resp.status_code == 200

        # Check audit row was written
        with get_connection() as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT action, connection_ref
                    FROM app.audit_log
                    WHERE connection_ref = %s AND action = 'connection.revoked'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (conn_id,),
                )
                audit_row = cur.fetchone()

        assert audit_row is not None, "Audit row must be written for revocation"
        assert audit_row[0] == "connection.revoked"
        assert audit_row[1] == conn_id
    finally:
        with get_connection() as db:
            _cleanup(proj_id, db)
            db.commit()


# ---------------------------------------------------------------------------
# AC8: test_revoke_connection_wrong_project_returns_404
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_revoke_connection_wrong_project_returns_404(tmp_path):
    """Revoking a connection that belongs to project B via project A returns 404."""
    from core.admin_api import _revoke_connection
    from core.db import get_connection

    proj_a = f"proj_{uuid.uuid4().hex[:10]}"
    proj_b = f"proj_{uuid.uuid4().hex[:10]}"
    conn_id = f"conn_{uuid.uuid4().hex[:10]}"
    nango_id = f"nango_{uuid.uuid4().hex[:8]}"

    with get_connection() as db:
        _insert_project(proj_a, db)
        _insert_project(proj_b, db)
        # Connection belongs to project B
        _insert_connection(conn_id, proj_b, nango_id, "google-analytics", db)
        db.commit()

    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            resp = await _revoke_connection(
                # Caller asserts project A -- should get 404
                _post_request({"project_id": proj_a, "connection_id": conn_id})
            )

        assert resp.status_code == 404
        body = json.loads(resp.body)
        assert body["code"] == "not_found"
    finally:
        with get_connection() as db:
            _cleanup(proj_b, db)
            _cleanup(proj_a, db)
            db.commit()


# ---------------------------------------------------------------------------
# AC8: test_key_rotation_writes_tka_row
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_key_rotation_writes_tka_row(tmp_path):
    """POST /rotate-key writes a tka_ audit row with action='key_rotated'."""
    from core.admin_api import _rotate_project_key
    from core.db import get_connection

    proj_id = f"proj_{uuid.uuid4().hex[:10]}"
    with get_connection() as db:
        _insert_project(proj_id, db)
        db.commit()

    try:
        req = MagicMock()
        req.path_params = {"project_id": proj_id}

        with (
            patch(_AUTH[0], return_value=_AUTH[1]),
            patch.dict(os.environ, {"TENANT_KEY_DIR": str(tmp_path / "keys")}),
        ):
            resp = await _rotate_project_key(req)

        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["status"] == "rotated"
        assert "rotated_at" in body

        # Check tka_ row
        with get_connection() as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT action FROM app.tenant_key_audit
                    WHERE project_id = %s AND action = 'key_rotated'
                    ORDER BY performed_at DESC LIMIT 1
                    """,
                    (proj_id,),
                )
                tka_row = cur.fetchone()

        assert tka_row is not None, "tka_ audit row must be written on rotation"
        assert tka_row[0] == "key_rotated"
    finally:
        with get_connection() as db:
            with db.cursor() as cur:
                cur.execute("DELETE FROM app.tenant_key_audit WHERE project_id = %s", (proj_id,))
            _cleanup(proj_id, db)
            db.commit()


# ---------------------------------------------------------------------------
# AC8: test_project_creation_provisions_key
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_project_creation_provisions_key(tmp_path):
    """POST /api/projects provisions a key file and writes a tka_ audit row."""
    from core.admin_api import _create_project
    from core.db import get_connection

    key_dir = str(tmp_path / "keys")
    slug = f"test-{uuid.uuid4().hex[:8]}"

    with (
        patch(_AUTH[0], return_value=_AUTH[1]),
        patch.dict(os.environ, {"TENANT_KEY_DIR": key_dir, "TENANT_KEY_BACKEND": "local"}),
    ):
        req = MagicMock()
        body_bytes = json.dumps({"name": f"Test {slug}", "slug": slug}).encode()
        req.body = AsyncMock(return_value=body_bytes)
        resp = await _create_project(req)

    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.body}"
    body = json.loads(resp.body)
    proj_id = body["id"]

    try:
        # Key file must exist
        key_file = os.path.join(key_dir, f"{proj_id}.key")
        assert os.path.exists(key_file), f"Key file must be created at {key_file}"

        # tka_ audit row must exist
        with get_connection() as db:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT action FROM app.tenant_key_audit WHERE project_id = %s",
                    (proj_id,),
                )
                rows = cur.fetchall()
        actions = [r[0] for r in rows]
        assert "key_created" in actions, f"key_created tka_ row missing; got {actions}"
    finally:
        with get_connection() as db:
            with db.cursor() as cur:
                cur.execute("DELETE FROM app.tenant_key_audit WHERE project_id = %s", (proj_id,))
            _cleanup(proj_id, db)
            db.commit()


# ---------------------------------------------------------------------------
# AC8: test_project_archive_deletes_key
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_project_archive_deletes_key(tmp_path):
    """DELETE /api/projects/{id} deletes the key file and writes a tka_ row."""
    from core.admin_api import _create_project, _delete_project
    from core.db import get_connection

    key_dir = str(tmp_path / "keys")
    slug = f"arch-{uuid.uuid4().hex[:8]}"

    # Create the project (also provisions the key)
    with (
        patch(_AUTH[0], return_value=_AUTH[1]),
        patch.dict(os.environ, {"TENANT_KEY_DIR": key_dir, "TENANT_KEY_BACKEND": "local"}),
    ):
        req = MagicMock()
        body_bytes = json.dumps({"name": f"Archive {slug}", "slug": slug}).encode()
        req.body = AsyncMock(return_value=body_bytes)
        resp = await _create_project(req)

    assert resp.status_code == 201
    proj_id = json.loads(resp.body)["id"]
    key_file = os.path.join(key_dir, f"{proj_id}.key")
    assert os.path.exists(key_file), "Key file must exist before archive"

    try:
        # Archive (delete) the project
        with (
            patch(_AUTH[0], return_value=_AUTH[1]),
            patch("core.nango_client.delete_connection", return_value=True),
            patch.dict(os.environ, {"TENANT_KEY_DIR": key_dir, "TENANT_KEY_BACKEND": "local"}),
        ):
            del_req = MagicMock()
            del_req.path_params = {"project_id": proj_id}
            del_resp = await _delete_project(del_req)

        assert del_resp.status_code == 200

        # Key file must be gone
        assert not os.path.exists(key_file), "Key file must be deleted after project archive"

        # tka_ row for key_deleted must exist
        with get_connection() as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT action FROM app.tenant_key_audit
                    WHERE project_id = %s AND action = 'key_deleted'
                    """,
                    (proj_id,),
                )
                tka_row = cur.fetchone()

        assert tka_row is not None, "key_deleted tka_ row must exist after archive"
    finally:
        with get_connection() as db:
            with db.cursor() as cur:
                cur.execute("DELETE FROM app.tenant_key_audit WHERE project_id = %s", (proj_id,))
            _cleanup(proj_id, db)
            db.commit()
