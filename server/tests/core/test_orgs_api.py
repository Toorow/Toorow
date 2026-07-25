"""Tests for /api/organizations CRUD + membership (Story 21.1, AC4, AC6, AC7).

Two layers:
  - Offline validation tests (no DB): the handlers reject invalid input BEFORE
    touching Postgres, so name/slug/role/status validation is provable locally.
  - Live-Postgres tests (skipped when TEST_POSTGRES_DSN is unset/unreachable):
    the slug UNIQUE constraint, the org_members role/status CHECKs, the
    UNIQUE(org_id, identity), and the projects.org_id FK are verified against
    the REAL schema created by migration 035 (AI-37: schema-constraint paths
    need a real DB, not a mock cursor).

FOUNDATION ONLY (Story 21.1): these endpoints add the org layer without changing
any access resolution -- the org-level default-closed flip is Story 21.5.
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
    req.path_params = {}
    return req


def _member_request(org_id: str, body: dict) -> MagicMock:
    req = MagicMock()
    req.path_params = {"org_id": org_id}
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    return req


def _get_request(org_id: str) -> MagicMock:
    req = MagicMock()
    req.path_params = {"org_id": org_id}
    return req


def _patch_request(org_id: str, body: dict) -> MagicMock:
    req = MagicMock()
    req.path_params = {"org_id": org_id}
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    return req


def _drop_org_by_slug(slug: str) -> None:
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            # org_members has ON DELETE CASCADE from organizations.
            cur.execute("DELETE FROM app.organizations WHERE slug = %s", (slug,))
        conn.commit()


def _drop_org(org_id: str) -> None:
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.organizations WHERE id = %s", (org_id,))
        conn.commit()


# ---------------------------------------------------------------------------
# Offline validation (no DB): handlers return before get_connection.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_org_requires_name():
    from core.admin_api import _create_org

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _create_org(_post_request({"name": "   "}))
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_create_org_invalid_slug():
    from core.admin_api import _create_org

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _create_org(_post_request({"name": "Acme", "slug": "Not A Slug!"}))
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_patch_org_no_updatable_fields_422():
    from core.admin_api import _patch_org

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _patch_org(_patch_request("org_whatever", {"unknown": "x"}))
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_add_member_requires_identity():
    from core.admin_api import _add_org_member

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _add_org_member(_member_request("org_x", {"identity": ""}))
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_add_member_invalid_role():
    from core.admin_api import _add_org_member

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _add_org_member(
            _member_request("org_x", {"identity": "u@e.com", "role": "superuser"})
        )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_add_member_invalid_status():
    from core.admin_api import _add_org_member

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _add_org_member(
            _member_request("org_x", {"identity": "u@e.com", "status": "banned"})
        )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_create_org_requires_auth():
    from core.admin_api import _create_org

    with patch(_AUTH[0], return_value=(False, "")):
        resp = await _create_org(_post_request({"name": "Acme"}))
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Live Postgres: real schema (migration 035).
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_create_get_list_org():
    from core.admin_api import _create_org, _get_org, _list_orgs

    slug = f"acme-{uuid.uuid4().hex[:8]}"
    _drop_org_by_slug(slug)
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            resp = await _create_org(_post_request({"name": "Acme", "slug": slug}))
            assert resp.status_code == 201
            org = json.loads(resp.body)
            assert org["id"].startswith("org_")
            assert org["slug"] == slug
            assert org["status"] == "active"

            got = await _get_org(_get_request(org["id"]))
            assert got.status_code == 200
            assert json.loads(got.body)["id"] == org["id"]

            listed = await _list_orgs(_post_request({}))
            assert listed.status_code == 200
            ids = {o["id"] for o in json.loads(listed.body)["organizations"]}
            assert org["id"] in ids
    finally:
        _drop_org_by_slug(slug)


@pg_available
@pytest.mark.anyio
async def test_create_org_duplicate_slug_conflict():
    from core.admin_api import _create_org

    slug = f"dupe-{uuid.uuid4().hex[:8]}"
    _drop_org_by_slug(slug)
    created = []
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r1 = await _create_org(_post_request({"name": "First", "slug": slug}))
            assert r1.status_code == 201
            created.append(json.loads(r1.body)["id"])
            r2 = await _create_org(_post_request({"name": "Second", "slug": slug}))
        assert r2.status_code == 409
    finally:
        for oid in created:
            _drop_org(oid)
        _drop_org_by_slug(slug)


@pg_available
@pytest.mark.anyio
async def test_patch_org_updates_name_slug_untouched():
    """Story 24.1: name stays editable; the slug NEVER changes (it names the
    org's warehouse datasets -- epic 24 decision 6)."""
    from core.admin_api import _create_org, _patch_org

    slug = f"patch-{uuid.uuid4().hex[:8]}"
    _drop_org_by_slug(slug)
    oid = None
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r = await _create_org(_post_request({"name": "Before", "slug": slug}))
            oid = json.loads(r.body)["id"]
            resp = await _patch_org(_patch_request(oid, {"name": "After"}))
        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["name"] == "After"
        assert body["slug"] == slug
    finally:
        if oid:
            _drop_org(oid)
        _drop_org_by_slug(slug)


@pg_available
@pytest.mark.anyio
async def test_patch_org_not_found_404():
    from core.admin_api import _patch_org

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _patch_org(_patch_request("org_does_not_exist", {"name": "X"}))
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_patch_org_slug_immutable_422():
    """Story 24.1: any PATCH containing 'slug' is rejected BEFORE the DB with
    an explicit 422 slug_immutable (the slug names the warehouse datasets)."""
    from core.admin_api import _patch_org

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _patch_org(_patch_request("org_any", {"slug": "new-slug"}))
    assert resp.status_code == 422
    assert json.loads(resp.body)["code"] == "slug_immutable"


@pg_available
@pytest.mark.anyio
async def test_create_org_sanitised_slug_collision_409():
    """Story 24.1 (review fix): the REPLACE guard is defensive depth, not the
    primary barrier -- the API slug charset ([a-z0-9-]) cannot produce '_', so
    two API-created slugs are always distinct once sanitised. The REACHABLE
    scenario is an out-of-band slug (direct SQL, legacy import) containing '_':
    creating its kebab twin via the API must 409, or both orgs would resolve to
    the same warehouse datasets (org_<wslug>_*)."""
    from core.admin_api import _create_org
    from core.db import get_connection

    base = f"col-{uuid.uuid4().hex[:8]}"
    twin = base.replace("-", "_")  # not creatable via the API (422 charset)
    for s in (base, twin):
        _drop_org_by_slug(s)
    try:
        # Seed the twin OUT-OF-BAND: bypasses _SLUG_RE like a legacy row would.
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.organizations (id, name, slug, created_by) "
                    "VALUES (%s, %s, %s, %s)",
                    (f"org_test_{uuid.uuid4().hex[:8]}", "Twin", twin, "system"),
                )
            conn.commit()
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r = await _create_org(_post_request({"name": "First", "slug": base}))
        assert r.status_code == 409
        assert json.loads(r.body)["code"] == "conflict"
    finally:
        for s in (base, twin):
            _drop_org_by_slug(s)


@pg_available
@pytest.mark.anyio
async def test_add_member_success_and_duplicate_conflict():
    from core.admin_api import _add_org_member, _create_org

    slug = f"mem-{uuid.uuid4().hex[:8]}"
    _drop_org_by_slug(slug)
    oid = None
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r = await _create_org(_post_request({"name": "MemOrg", "slug": slug}))
            oid = json.loads(r.body)["id"]

            m1 = await _add_org_member(
                _member_request(oid, {"identity": "carole@acme", "role": "member"})
            )
            assert m1.status_code == 201
            member = json.loads(m1.body)
            assert member["id"].startswith("omem_")
            assert member["role"] == "member"
            assert member["status"] == "active"
            assert member["joined_at"] is not None

            m2 = await _add_org_member(
                _member_request(oid, {"identity": "carole@acme", "role": "viewer"})
            )
            assert m2.status_code == 409
    finally:
        if oid:
            _drop_org(oid)  # cascades org_members
        _drop_org_by_slug(slug)


@pg_available
@pytest.mark.anyio
async def test_add_member_org_not_found_404():
    from core.admin_api import _add_org_member

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _add_org_member(
            _member_request("org_does_not_exist", {"identity": "x@e.com"})
        )
    assert resp.status_code == 404


@pg_available
def test_org_members_role_check_rejects_invalid():
    """AC6: the CHECK on org_members.role rejects a value outside the enum.

    Self-contained: creates a throwaway org in the SAME transaction so the test
    does not depend on a backfilled 'org_default' existing. The CheckViolation
    aborts the transaction, rolling back the org insert too -> no cleanup needed.
    """
    import psycopg
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_id = f"org_rc_{suffix}"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, created_by) "
                "VALUES (%s, %s, %s, %s)",
                (org_id, "RoleCheck", f"rc-{suffix}", "system"),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.org_members (id, org_id, identity, role) "
                    "VALUES (%s, %s, %s, %s)",
                    (f"omem_rc_{suffix}", org_id, "x@e.com", "superadmin"),
                )
        conn.rollback()  # rolls back BOTH inserts


@pg_available
def test_projects_org_fk_rejects_orphan():
    """AC6: projects.org_id FK rejects a project pointing at a missing org."""
    import psycopg
    from core.db import get_connection

    with get_connection() as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.projects (id, name, slug, created_by, org_id) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (
                        "proj_orphan_test",
                        "Orphan",
                        f"orphan-{uuid.uuid4().hex[:8]}",
                        "system",
                        "org_missing_parent",
                    ),
                )
        conn.rollback()


# The migration's backfill statements (kept in sync with 035_organizations.sql).
# Exercised twice by test_backfill_idempotent_double_apply to PROVE AC6's
# headline claim (a re-run is a no-op) against the real schema.
_BACKFILL_SQL = (
    "INSERT INTO app.organizations (id, name, slug, status, created_by) "
    "SELECT 'org_' || p.id, p.name, p.slug, 'active', 'system' "
    "FROM app.projects p ON CONFLICT (id) DO NOTHING",
    "UPDATE app.projects p SET org_id = 'org_' || p.id WHERE p.org_id IS NULL",
    "INSERT INTO app.org_members (id, org_id, identity, role, status, joined_at) "
    "SELECT 'omem_' || pm.id, 'org_' || pm.project_id, pm.identity, 'owner', 'active', NOW() "
    "FROM app.project_members pm ON CONFLICT DO NOTHING",
)


@pg_available
def test_backfill_idempotent_double_apply():
    """AC6 (headline): running the backfill TWICE is a no-op -- no dup, no error.

    Seeds a legacy project (org_id NULL) + a project_member, applies the backfill
    block twice, and asserts exactly one org and one membership exist after each
    run. Proves the derived-id + ON CONFLICT DO NOTHING scheme is idempotent
    against BOTH unique axes (PK and composite).
    """
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    proj_id = f"proj_bf_{suffix}"
    pmem_id = f"pmem_bf_{suffix}"
    org_id = f"org_{proj_id}"
    ident = f"bf-{suffix}@e.com"

    def _counts(cur) -> tuple[int, int]:
        cur.execute("SELECT count(*) FROM app.organizations WHERE id = %s", (org_id,))
        orgs = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM app.org_members WHERE org_id = %s AND identity = %s",
            (org_id, ident),
        )
        return orgs, cur.fetchone()[0]

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.projects (id, name, slug, created_by, org_id) "
                    "VALUES (%s, %s, %s, 'system', NULL)",
                    (proj_id, "Backfill", f"bf-{suffix}"),
                )
                cur.execute(
                    "INSERT INTO app.project_members (id, project_id, identity, role) "
                    "VALUES (%s, %s, %s, 'owner')",
                    (pmem_id, proj_id, ident),
                )
            conn.commit()

            with conn.cursor() as cur:
                for sql in _BACKFILL_SQL:
                    cur.execute(sql)
            conn.commit()
            with conn.cursor() as cur:
                first = _counts(cur)

            # Second apply MUST NOT raise and MUST NOT duplicate.
            with conn.cursor() as cur:
                for sql in _BACKFILL_SQL:
                    cur.execute(sql)
            conn.commit()
            with conn.cursor() as cur:
                second = _counts(cur)

        assert first == (1, 1)
        assert second == (1, 1)
    finally:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # projects delete cascades project_members; org delete cascades org_members.
                cur.execute("DELETE FROM app.projects WHERE id = %s", (proj_id,))
                cur.execute("DELETE FROM app.organizations WHERE id = %s", (org_id,))
            conn.commit()


# ---------------------------------------------------------------------------
# Story 24.2 -- pg-gated warehouse provisioning lifecycle tests (T9)
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_create_org_provisions_schemas_resolve_ok():
    """T9: creating an org means resolve_org_schemas returns the correct names.

    DuckDB actual file creation is not checked here (TOOROW_DUCKDB_PATH may not
    be set in CI) -- we verify only that the Postgres side is correct and that
    resolve_org_schemas returns a valid OrgSchemas with the expected naming
    (org_<wslug>_raw / org_<wslug>_marts).
    """
    from core import warehouse_tenancy as wt
    from core.admin_api import _create_org

    slug = f"prov-{uuid.uuid4().hex[:8]}"
    _drop_org_by_slug(slug)
    oid = None
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            resp = await _create_org(_post_request({"name": "ProvOrg", "slug": slug}))
        assert resp.status_code == 201
        oid = json.loads(resp.body)["id"]

        wt._reset_cache()
        schemas = wt.resolve_org_schemas(org_id=oid)
        assert schemas is not None
        wslug = slug.replace("-", "_")
        assert schemas.raw == f"org_{wslug}_raw"
        assert schemas.marts == f"org_{wslug}_marts"
    finally:
        if oid:
            _drop_org(oid)
        _drop_org_by_slug(slug)
        wt._reset_cache()


@pg_available
@pytest.mark.anyio
async def test_backfill_endpoint_idempotent():
    """T9: calling the backfill endpoint twice returns 0 errors both times."""
    from core.admin_api import _backfill_warehouse_schemas, _create_org

    slug = f"bfill-{uuid.uuid4().hex[:8]}"
    _drop_org_by_slug(slug)
    oid = None
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r = await _create_org(_post_request({"name": "BfillOrg", "slug": slug}))
        assert r.status_code == 201
        oid = json.loads(r.body)["id"]

        def _backfill_req():
            req = MagicMock()
            req.path_params = {}
            req.body = AsyncMock(return_value=b"{}")
            req.headers = {}
            return req

        with patch(_AUTH[0], return_value=_AUTH[1]):
            r1 = await _backfill_warehouse_schemas(_backfill_req())
            r2 = await _backfill_warehouse_schemas(_backfill_req())

        b1 = json.loads(r1.body)
        b2 = json.loads(r2.body)
        assert b1["errors"] == []
        assert b2["errors"] == []
    finally:
        if oid:
            _drop_org(oid)
        _drop_org_by_slug(slug)


@pg_available
@pytest.mark.anyio
async def test_delete_org_requires_confirmation_pg():
    """T9 (pg-gated): DELETE without header -> 422, org NOT deleted."""
    from core.admin_api import _create_org, _delete_org
    from core.db import get_connection

    slug = f"del-noconf-{uuid.uuid4().hex[:8]}"
    _drop_org_by_slug(slug)
    oid = None
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r = await _create_org(_post_request({"name": "DelNoConf", "slug": slug}))
        assert r.status_code == 201
        oid = json.loads(r.body)["id"]

        req = MagicMock()
        req.path_params = {"org_id": oid}
        req.body = AsyncMock(return_value=b"")
        req.headers = {}
        with patch(_AUTH[0], return_value=_AUTH[1]):
            resp = await _delete_org(req)
        assert resp.status_code == 422
        assert json.loads(resp.body)["code"] == "confirmation_required"

        # Org must still exist.
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM app.organizations WHERE id = %s", (oid,))
                assert cur.fetchone() is not None
    finally:
        if oid:
            _drop_org(oid)
        _drop_org_by_slug(slug)


@pg_available
@pytest.mark.anyio
async def test_delete_org_full_lifecycle_pg():
    """T9 (pg-gated): create org + confirm delete -> row disappears from DB."""
    from core.admin_api import _create_org, _delete_org
    from core.db import get_connection

    slug = f"del-full-{uuid.uuid4().hex[:8]}"
    _drop_org_by_slug(slug)
    oid = None
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r = await _create_org(_post_request({"name": "DelFull", "slug": slug}))
        assert r.status_code == 201
        oid = json.loads(r.body)["id"]

        req = MagicMock()
        req.path_params = {"org_id": oid}
        req.body = AsyncMock(return_value=b"")
        req.headers = {"X-Confirm-Delete": "drop-warehouse-data"}

        with patch(_AUTH[0], return_value=_AUTH[1]):
            resp = await _delete_org(req)

        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["deleted"] is True
        assert body["org_id"] == oid

        # Org must be gone from Postgres.
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM app.organizations WHERE id = %s", (oid,))
                assert cur.fetchone() is None

        oid = None  # already deleted, no cleanup needed
    finally:
        if oid:
            _drop_org(oid)
        _drop_org_by_slug(slug)
