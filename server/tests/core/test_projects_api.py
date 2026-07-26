"""Tests for /api/projects CRUD (Story 7.1, AC3, AC4, AC8).

Runs against the local platform Postgres (default DSN) so the slug UNIQUE
constraint, the FK to app.projects, the archive+revoke flow, and the audit row
are verified against the REAL schema (AI-37: schema-constraint paths need a real
DB, not a mock cursor). Skips when Postgres is unreachable.

Covers the AC8 cases:
  - test_create_project_success
  - test_create_project_duplicate_slug_conflict
  - test_create_project_invalid_timezone
  - test_list_projects_active_only
  - test_patch_project_updates_fields
  - test_delete_project_archives_and_revokes
  - test_delete_already_archived_returns_409
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


def _post_request(body: dict) -> MagicMock:
    req = MagicMock()
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    return req


def _get_request(path_params: dict | None = None) -> MagicMock:
    req = MagicMock()
    req.path_params = path_params or {}
    return req


def _patch_request(project_id: str, body: dict) -> MagicMock:
    req = MagicMock()
    req.path_params = {"project_id": project_id}
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    return req


def _delete_request(project_id: str) -> MagicMock:
    req = MagicMock()
    req.path_params = {"project_id": project_id}
    return req


def _drop_project(project_id: str) -> None:
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Story 7.3: tenant_key_audit has FK ON DELETE RESTRICT; clear it first.
            cur.execute("DELETE FROM app.tenant_key_audit WHERE project_id = %s", (project_id,))
            # AI-41: archival may emit a nango_revoke_failed firing referencing the project.
            cur.execute("DELETE FROM app.alert_firings WHERE project_id = %s", (project_id,))
            cur.execute("DELETE FROM app.connection_ref WHERE project_id = %s", (project_id,))
            cur.execute("DELETE FROM app.projects WHERE id = %s", (project_id,))
        conn.commit()


def _drop_by_slug(slug: str) -> None:
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Story 7.3: resolve project_id first so we can clear tenant_key_audit.
            cur.execute("SELECT id FROM app.projects WHERE slug = %s", (slug,))
            row = cur.fetchone()
            if row:
                proj_id = row[0]
                cur.execute("DELETE FROM app.tenant_key_audit WHERE project_id = %s", (proj_id,))
                # AI-41: archival may emit a nango_revoke_failed firing for the project.
                cur.execute("DELETE FROM app.alert_firings WHERE project_id = %s", (proj_id,))
                cur.execute("DELETE FROM app.connection_ref WHERE project_id = %s", (proj_id,))
            cur.execute("DELETE FROM app.projects WHERE slug = %s", (slug,))
        conn.commit()


@pytest.fixture(autouse=True)
def caller_org():
    """L'identité de test appartient à une organisation, comme un vrai utilisateur.

    Sans ce contexte, ces tests créaient des projets ORPHELINS : sans org_id, un
    projet n'a pas d'entrepôt où atterrir (warehouse_tenancy retombe sur le
    nommage legacy) et échappe au cloisonnement. La création de projet refuse
    désormais de fabriquer cet objet incohérent, et le harnais doit refléter le
    parcours réel — on crée son organisation, PUIS son premier projet.
    """
    if not _pg_reachable():
        yield None
        return

    from core.db import get_connection

    org_id = f"org_test_{uuid.uuid4().hex[:12]}"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, status, created_by) "
                "VALUES (%s, %s, %s, 'active', %s)",
                (org_id, f"Test {org_id}", org_id.replace("_", "-"), _AUTH[1][1]),
            )
            cur.execute(
                "INSERT INTO app.org_members (id, org_id, identity, role, status, joined_at) "
                "VALUES (%s, %s, %s, 'owner', 'active', NOW())",
                (f"omem_test_{uuid.uuid4().hex[:12]}", org_id, _AUTH[1][1]),
            )
        conn.commit()
    try:
        yield org_id
    finally:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM app.org_members WHERE org_id = %s", (org_id,))
                cur.execute("DELETE FROM app.organizations WHERE id = %s", (org_id,))
            conn.commit()


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_create_project_attaches_org_and_enrols_owner(caller_org):
    """Un projet naît rattaché à son org, avec son créateur propriétaire.

    Les deux moitiés comptent autant : sans org_id les datasets provisionnés à
    la création de l'organisation ne serviraient jamais ; sans ligne dans
    project_members, resolve_project_role ne trouve aucun rôle et le créateur
    perd l'accès à son propre projet dans la seconde qui suit (404 sur ses
    datastreams juste après un 201).
    """
    from core.admin_api import _create_project
    from core.db import get_connection

    slug = f"attach-{uuid.uuid4().hex[:8]}"
    _drop_by_slug(slug)
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            resp = await _create_project(_post_request({"name": "Attach", "slug": slug}))
        assert resp.status_code == 201
        project_id = json.loads(resp.body)["id"]

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
            assert cur.fetchone()[0] == caller_org
            cur.execute(
                "SELECT role FROM app.project_members WHERE project_id = %s AND identity = %s",
                (project_id, _AUTH[1][1]),
            )
            assert cur.fetchone()[0] == "owner"
    finally:
        _drop_by_slug(slug)


@pg_available
@pytest.mark.anyio
async def test_create_project_without_org_is_refused(caller_org):
    """Sans organisation, la création est REFUSÉE — pas dégradée en orphelin."""
    from core.admin_api import _create_project
    from core.db import get_connection

    # Retirer l'appartenance le temps de l'appel : l'identité n'a plus d'org.
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM app.org_members WHERE identity = %s", (_AUTH[1][1],))
        conn.commit()

    slug = f"noorg-{uuid.uuid4().hex[:8]}"
    _drop_by_slug(slug)
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            resp = await _create_project(_post_request({"name": "NoOrg", "slug": slug}))
        assert resp.status_code == 422
        assert json.loads(resp.body)["code"] == "org_required"

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM app.projects WHERE slug = %s", (slug,))
            assert cur.fetchone() is None, "un projet a été créé malgré le refus"
    finally:
        _drop_by_slug(slug)


@pg_available
@pytest.mark.anyio
async def test_create_project_success():
    from core.admin_api import _create_project

    slug = f"acme-{uuid.uuid4().hex[:8]}"
    _drop_by_slug(slug)
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            resp = await _create_project(_post_request({"name": "Acme", "slug": slug}))
        assert resp.status_code == 201
        body = json.loads(resp.body)
        assert body["id"].startswith("proj_")
        assert body["slug"] == slug
        assert body["status"] == "active"
        assert body["currency"] == "EUR"
    finally:
        _drop_by_slug(slug)


@pg_available
@pytest.mark.anyio
async def test_create_project_auto_slug_from_name():
    from core.admin_api import _create_project

    name = f"Acme Corp {uuid.uuid4().hex[:6]} (FR)"
    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _create_project(_post_request({"name": name}))
    assert resp.status_code == 201
    body = json.loads(resp.body)
    try:
        # lowercase, spaces->hyphens, special chars stripped
        assert body["slug"].startswith("acme-corp-")
        assert body["slug"].endswith("-fr")
    finally:
        _drop_project(body["id"])


@pg_available
@pytest.mark.anyio
async def test_create_project_duplicate_slug_conflict():
    from core.admin_api import _create_project

    slug = f"dupe-{uuid.uuid4().hex[:8]}"
    _drop_by_slug(slug)
    created_ids = []
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r1 = await _create_project(_post_request({"name": "First", "slug": slug}))
            assert r1.status_code == 201
            created_ids.append(json.loads(r1.body)["id"])
            r2 = await _create_project(_post_request({"name": "Second", "slug": slug}))
        assert r2.status_code == 409
    finally:
        for pid in created_ids:
            _drop_project(pid)
        _drop_by_slug(slug)


@pg_available
@pytest.mark.anyio
async def test_create_project_invalid_timezone():
    from core.admin_api import _create_project

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _create_project(_post_request({"name": "Bad TZ", "timezone": "Mars/Olympus"}))
    assert resp.status_code == 422


@pg_available
@pytest.mark.anyio
async def test_create_project_invalid_currency():
    from core.admin_api import _create_project

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _create_project(_post_request({"name": "Bad Cur", "currency": "XYZ"}))
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# List / Get
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_list_projects_active_only():
    from core.admin_api import _create_project, _delete_project, _list_projects

    active_slug = f"active-{uuid.uuid4().hex[:8]}"
    arch_slug = f"arch-{uuid.uuid4().hex[:8]}"
    created = []
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            ra = await _create_project(_post_request({"name": "ZZZ Active", "slug": active_slug}))
            rb = await _create_project(_post_request({"name": "ZZZ Arch", "slug": arch_slug}))
            active_id = json.loads(ra.body)["id"]
            arch_id = json.loads(rb.body)["id"]
            created = [active_id, arch_id]
            # Archive the second one.
            await _delete_project(_delete_request(arch_id))

            resp = await _list_projects(_get_request())
        assert resp.status_code == 200
        slugs = {p["slug"] for p in json.loads(resp.body)["projects"]}
        assert active_slug in slugs
        assert arch_slug not in slugs
    finally:
        for pid in created:
            _drop_project(pid)


# ---------------------------------------------------------------------------
# Patch
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_patch_project_updates_fields():
    from core.admin_api import _create_project, _patch_project

    slug = f"patch-{uuid.uuid4().hex[:8]}"
    _drop_by_slug(slug)
    pid = None
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r = await _create_project(_post_request({"name": "Before", "slug": slug}))
            pid = json.loads(r.body)["id"]
            resp = await _patch_project(_patch_request(pid, {"name": "After", "currency": "USD"}))
        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["name"] == "After"
        assert body["currency"] == "USD"
        # slug is immutable.
        assert body["slug"] == slug
    finally:
        if pid:
            _drop_project(pid)


# ---------------------------------------------------------------------------
# Delete (archive) + revoke connections + audit
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_delete_project_archives_and_revokes():
    from core.admin_api import _create_project, _delete_project
    from core.db import get_connection

    slug = f"del-{uuid.uuid4().hex[:8]}"
    _drop_by_slug(slug)
    pid = None
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r = await _create_project(_post_request({"name": "To Delete", "slug": slug}))
            pid = json.loads(r.body)["id"]

        # Attach a connection to the project.
        conn_id = f"conn_{uuid.uuid4().hex[:12]}"
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.connection_ref "
                    "(id, provider, nango_connection_id, project_id, owner_org_id, owner_identity) "
                    "VALUES (%s, 'google-analytics', %s, %s, 'org_test_fixture', "
                    "'tester@example.com')",
                    (conn_id, conn_id, pid),
                )
            conn.commit()

        with (
            patch(_AUTH[0], return_value=_AUTH[1]),
            patch("core.admin_api.write_audit_row") as mock_audit,
        ):
            resp = await _delete_project(_delete_request(pid))

        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["status"] == "archived"
        assert body["connections_revoked"] == 1

        # DB assertions: project archived, connection revoked.
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status, archived_at FROM app.projects WHERE id = %s", (pid,))
                prow = cur.fetchone()
                assert prow[0] == "archived"
                assert prow[1] is not None
                cur.execute(
                    "SELECT status, revoked_at FROM app.connection_ref WHERE id = %s",
                    (conn_id,),
                )
                crow = cur.fetchone()
                assert crow[0] == "revoked"
                assert crow[1] is not None

        # Audit rows: Story 7.3 adds a key_deleted audit row BEFORE project_archived.
        # The archive handler now calls write_audit_row twice:
        #   1. action='key_deleted'   (Story 7.3, T5.2)
        #   2. action='project_archived' (Story 7.1, AC4)
        assert mock_audit.call_count >= 1
        all_calls = mock_audit.call_args_list
        actions = [c.kwargs.get("action") for c in all_calls]
        assert "project_archived" in actions, (
            f"project_archived missing from audit calls: {actions}"
        )
        archive_call = next(c for c in all_calls if c.kwargs.get("action") == "project_archived")
        assert archive_call.kwargs["metadata"]["connections_revoked"] == 1
    finally:
        if pid:
            _drop_project(pid)


@pg_available
@pytest.mark.anyio
async def test_delete_already_archived_returns_409():
    from core.admin_api import _create_project, _delete_project

    slug = f"twice-{uuid.uuid4().hex[:8]}"
    _drop_by_slug(slug)
    pid = None
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r = await _create_project(_post_request({"name": "Twice", "slug": slug}))
            pid = json.loads(r.body)["id"]
            first = await _delete_project(_delete_request(pid))
            assert first.status_code == 200
            second = await _delete_project(_delete_request(pid))
        assert second.status_code == 409
    finally:
        if pid:
            _drop_project(pid)


@pg_available
@pytest.mark.anyio
async def test_delete_missing_project_returns_404():
    from core.admin_api import _delete_project

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _delete_project(_delete_request("proj_does_not_exist"))
    assert resp.status_code == 404
