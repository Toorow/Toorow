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

# ONE ORGANIZATION PER PERSON, enforced on CREATION (decision Jean, 2026-07-25,
# `organizations_api._create_org`). Every live-Postgres test below creates an org, and
# they all used to do it as the SAME `tester@example.com`: the first creation in a
# run succeeded and every one after it answered
#     409 {"code":"organization_limit_reached", ...}
# -- and once one run had left a membership behind, even the first one did. That
# is what the 19 failures + 12 errors of 2026-08-05 were; NOT the hosted ENTRY
# scope, which never fires here because these tests run with
# TOOROW_AUTH_MODE unset (`deployment_mode()` -> hosted, auth_mode -> disabled).
#
# The cap is product behaviour and is tested on purpose elsewhere
# (`test_epic36_entry_invitation_without_org.py`). It is not the subject here, so
# each test gets a caller who has never created anything. `_AUTH` stays
# INDEXABLE so the 22 `patch(_AUTH[0], return_value=_AUTH[1])` call sites read
# exactly as they did.
_CALLER = {"identity": "tester@example.com"}


class _Auth:
    """``_AUTH[0]`` is the patch target, ``_AUTH[1]`` the CURRENT caller."""

    target = "core.admin_api._check_auth"

    def __getitem__(self, index: int):
        return self.target if index == 0 else (True, _CALLER["identity"])


_AUTH = _Auth()


@pytest.fixture(autouse=True)
def _a_caller_who_owns_nothing():
    """A never-before-used identity per test, and no membership left behind.

    Cleaning by SLUG alone was never enough: `_drop_org_by_slug` removes the org
    (and cascades its members), but a test that fails midway leaves the membership
    that then blocks the NEXT run of the whole file.
    """
    _CALLER["identity"] = f"tester-{uuid.uuid4().hex[:12]}@example.com"
    yield
    if not _pg_reachable():
        return
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.org_members WHERE identity = %s", (_CALLER["identity"],)
            )
        conn.commit()


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


def _drop_org(org_id: str) -> None:
    """Erase an org THE WAY PRODUCTION DOES -- `org_purge`, not a bare DELETE.

    "org_members has ON DELETE CASCADE" was true and beside the point: creating an
    org PROVISIONS a tree (mdm_business_domains, warehouse tenancy rows, ...), and
    those FKs do not cascade. So the bare DELETE raised

        ForeignKeyViolation: ... viole la contrainte
        « mdm_business_domains_org_id_fkey » de la table « mdm_business_domains »

    IN THE CLEANUP -- after the assertions had already passed. Every such test
    reported a failure it had not actually suffered, and left the org behind for
    the next run. `purge_org_tree` is the same walker `DELETE /api/organizations`
    uses, append-only escape hatch included, so this harness erases exactly what
    the product erases.
    """
    from core.db import get_connection

    from tests.conftest import purge_fixture_org

    with get_connection() as conn:
        try:
            purge_fixture_org(conn, org_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _drop_org_by_slug(slug: str) -> None:
    from core.db import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM app.organizations WHERE slug = %s", (slug,))
        row = cur.fetchone()
    if row is not None:
        _drop_org(row[0])


# ---------------------------------------------------------------------------
# Offline validation (no DB): handlers return before get_connection.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_org_requires_name():
    from core.organizations_api import _create_org  # noqa: PLC0415

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _create_org(_post_request({"name": "   "}))
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_create_org_invalid_slug():
    from core.organizations_api import _create_org  # noqa: PLC0415

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _create_org(_post_request({"name": "Acme", "slug": "Not A Slug!"}))
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_patch_org_no_updatable_fields_422():
    from core.organizations_api import _patch_org  # noqa: PLC0415

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _patch_org(_patch_request("org_whatever", {"unknown": "x"}))
    assert resp.status_code == 422


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        {"identity": ""},
        {"identity": "u@e.com", "role": "superuser"},
        {"identity": "u@e.com", "status": "banned"},
        {"identity": "u@e.com", "role": "member", "status": "active"},
    ],
)
async def test_add_member_refuses_every_body_and_names_the_gesture(body):
    """REPLACES the three `test_add_member_invalid_*` body-validation tests.

    Those tests validated the enrolment BODY -- identity length, role enum,
    status enum -- because the route wrote that body into
    `app.org_members.identity`. It no longer writes anything (2026-08-24,
    67-17): membership is created by accepting an invitation, which binds the
    person a token proves instead of a string a caller typed.

    A well-formed body is in the table on purpose. If a future change re-opens
    the write path, THAT row is the one that stops being a 409.
    """
    from core.org_members_api import _add_org_member  # noqa: PLC0415

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _add_org_member(_member_request("org_x", body))
    assert resp.status_code == 409
    payload = json.loads(resp.body)
    assert payload["code"] == "invitation_required"
    assert "invitation" in payload["message"].lower()


@pytest.mark.anyio
async def test_create_org_requires_auth():
    from core.organizations_api import _create_org  # noqa: PLC0415

    with patch(_AUTH[0], return_value=(False, "")):
        resp = await _create_org(_post_request({"name": "Acme"}))
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Live Postgres: real schema (migration 035).
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_create_get_list_org():
    from core.organizations_api import _create_org, _get_org, _list_orgs  # noqa: PLC0415

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
    from core.organizations_api import _create_org  # noqa: PLC0415

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
    from core.organizations_api import _create_org, _patch_org  # noqa: PLC0415

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
    from core.organizations_api import _patch_org  # noqa: PLC0415

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _patch_org(_patch_request("org_does_not_exist", {"name": "X"}))
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_patch_org_slug_immutable_422():
    """Story 24.1: any PATCH containing 'slug' is rejected BEFORE the DB with
    an explicit 422 slug_immutable (the slug names the warehouse datasets)."""
    from core.organizations_api import _patch_org  # noqa: PLC0415

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
    from core.db import get_connection
    from core.organizations_api import _create_org  # noqa: PLC0415

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


# `test_add_member_success_and_duplicate_conflict` was DELETED here on 2026-08-24
# (67-17), not ported. It proved the route inserting `carole@acme` into
# `app.org_members.identity` and answering 201, then 409 on the duplicate. Both
# halves are gone with the write path: the column holds a `person_<ULID>` that
# only `resolve_canonical_identity` mints, and there is no longer a first insert
# for a second one to conflict with. What replaced its subject is
# `test_add_member_refuses_every_body_and_names_the_gesture` above, and the
# membership that DOES get created is covered by the invitation-acceptance
# suites (`test_epic36_invitation_acceptance.py`).


@pg_available
@pytest.mark.anyio
async def test_add_member_does_not_disclose_whether_the_org_exists():
    """REPLACES `test_add_member_org_not_found_404` (2026-08-24, 67-17).

    That test asserted 404 for an unknown org, which proved the route LOOKED the
    org up. It no longer looks anything up: it refuses every caller with the same
    409 before touching the database. Asserting a 404 would now be asserting a
    disclosure -- an org id that answers differently from an unknown one tells an
    unauthorized caller which organizations exist.

    Kept pg-gated on purpose: this is the case where a database IS reachable, so
    a re-opened lookup would be observable here and nowhere else.
    """
    from core.org_members_api import _add_org_member  # noqa: PLC0415

    with patch(_AUTH[0], return_value=_AUTH[1]):
        unknown = await _add_org_member(
            _member_request("org_does_not_exist", {"identity": "x@e.com"})
        )
        known = await _add_org_member(_member_request("org_default", {"identity": "x@e.com"}))

    assert unknown.status_code == known.status_code == 409
    assert unknown.body == known.body


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


# `_BACKFILL_SQL` STOOD HERE AND IS GONE (2026-08-16). It carried migration 035's
# backfill statements, and its last executor was retired when migration 100 made
# `app.projects.org_id` NOT NULL -- the docstring below tells that story. What
# made it worth removing rather than leaving: its third statement read
# `FROM app.project_members`, a table migration 132 DROPPED, so the constant was
# SQL nobody could run against a table nobody has. A dead fixture naming a dead
# relation is how the next reader concludes the table still exists.


@pg_available
def test_the_state_the_backfill_repaired_can_no_longer_be_created():
    """AC6, held by the SCHEMA now -- an orphan project is refused, not repaired.

    This test used to seed a legacy project with ``org_id = NULL`` and apply
    migration 035's backfill statements twice to prove idempotence. Migration 100
    (``100_org_chain_not_null.sql``) closed that door: *"un projet orphelin -- sans
    entrepot ou atterrir, hors de tout cloisonnement -- est reste techniquement
    possible. C'est cette porte que la migration ferme."* Measured on the real
    schema:

        SELECT is_nullable FROM information_schema.columns
        WHERE table_schema='app' AND table_name='projects' AND column_name='org_id'
        -> NO

    So the seed raised ``NotNullViolation`` and the test failed while asserting
    nothing -- the sole reason it was red on 2026-08-05. Fabricating the row
    anyway (deferring or dropping the constraint) would have tested a state the
    product forbids.

    The guarantee is stronger stated this way round: the backfill's job is done by
    a constraint, and if anyone ever relaxes it back to nullable, THIS fails and
    the orphan door is open again. Idempotence of the derived-id + ON CONFLICT
    scheme itself is still covered, on reachable rows, by
    ``test_backfill_endpoint_idempotent`` above.
    """
    import psycopg
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    proj_id = f"proj_bf_{suffix}"

    with get_connection() as conn:
        try:
            with conn.cursor() as cur, pytest.raises(psycopg.errors.NotNullViolation):
                cur.execute(
                    "INSERT INTO app.projects (id, name, slug, created_by, org_id) "
                    "VALUES (%s, %s, %s, 'system', NULL)",
                    (proj_id, "Backfill", f"bf-{suffix}"),
                )
        finally:
            # The refused INSERT aborted the transaction; nothing to clean.
            conn.rollback()


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
    from core.organizations_api import _create_org  # noqa: PLC0415

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
    from core.organizations_api import _create_org  # noqa: PLC0415
    from core.platform_maintenance_api import _backfill_warehouse_schemas  # noqa: PLC0415

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
    from core.db import get_connection
    from core.organizations_api import _create_org, _delete_org  # noqa: PLC0415

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
    from core.db import get_connection
    from core.organizations_api import _create_org, _delete_org  # noqa: PLC0415

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
