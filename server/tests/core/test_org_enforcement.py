"""Tests for Story 21.5 -- ORG access resolution flip (default-open-until-enrolled).

Three layers, mirroring the 21.1-21.4 test style:
  - Offline (no DB): the disabled-auth "anonymous" short-circuit in resolve_org_role
    returns 'owner' WITHOUT touching the DB (the mock conn's cursor must NOT be
    called) -- proving the dev/legacy path never queries.
  - Live-Postgres (skipped when TEST_POSTGRES_DSN is unset): the resolver semantics
    (default-open, enrolled-closed, anonymous-owner, project allow-list, legacy
    fallback) + the resource_grants scope_type CHECK, on the REAL schema (mig 039).
  - Endpoint enforcement (live-Postgres, patched _check_auth): auto-enroll of the
    creator lets it manage; a stranger is refused 403 once the org is enrolled.

DEFAULT-OPEN-UNTIL-ENROLLED (human decision, Epic 21 decisions 4 & 6): an org with
ZERO members is OPEN (caller acts as owner); >= 1 member -> CLOSED (enrolled only).
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

_AUTH = "core.admin_api._check_auth"


# ---------------------------------------------------------------------------
# Offline: the anonymous/disabled short-circuit must NOT query the DB.
# ---------------------------------------------------------------------------


def test_resolve_org_role_anonymous_disabled_shortcircuits_no_db(monkeypatch):
    """disabled-auth + 'anonymous' -> 'owner' BEFORE any cursor call (mock asserts)."""
    from core.project_access import resolve_org_role

    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    conn = MagicMock()
    # If the resolver were to query, it would call conn.cursor(); assert it does NOT.
    assert resolve_org_role("org_x", "anonymous", conn) == "owner"
    conn.cursor.assert_not_called()


def test_resolve_org_role_non_disabled_anonymous_does_query(monkeypatch):
    """Outside disabled auth, 'anonymous' is NOT special: the DB is consulted."""
    from core.project_access import resolve_org_role

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = ("active", False, None)  # zero members -> open
    conn = MagicMock()
    conn.cursor.return_value = cur
    assert resolve_org_role("org_x", "anonymous", conn) == "owner"
    conn.cursor.assert_called()


# ---------------------------------------------------------------------------
# Live-Postgres helpers (direct SQL; mirrors the 21.1-21.4 fixtures).
# ---------------------------------------------------------------------------


def _mk_org(cur, org_id: str, suffix: str) -> None:
    cur.execute(
        "INSERT INTO app.organizations (id, name, slug, created_by) "
        "VALUES (%s, %s, %s, 'system')",
        (org_id, org_id, f"{org_id}-{suffix}"),
    )


def _mk_member(cur, org_id: str, identity: str, role: str, suffix: str) -> None:
    cur.execute(
        "INSERT INTO app.org_members (id, org_id, identity, role, status, joined_at) "
        "VALUES (%s, %s, %s, %s, 'active', NOW())",
        (f"omem_{role}_{uuid.uuid4().hex[:8]}", org_id, identity, role),
    )


def _mk_member_status(cur, org_id: str, identity: str, role: str, status: str) -> None:
    cur.execute(
        "INSERT INTO app.org_members (id, org_id, identity, role, status, joined_at) "
        "VALUES (%s, %s, %s, %s, %s, NOW())",
        (f"omem_{role}_{uuid.uuid4().hex[:8]}", org_id, identity, role, status),
    )


def _mk_project(cur, proj_id: str, org_id, suffix: str) -> None:
    cur.execute(
        "INSERT INTO app.projects (id, name, slug, created_by, org_id) "
        "VALUES (%s, %s, %s, 'system', %s)",
        (proj_id, proj_id, f"{proj_id}-{suffix}", org_id),
    )


def _drop_org(org_id: str) -> None:
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            # org delete cascades org_members + resource_grants.
            cur.execute("DELETE FROM app.organizations WHERE id = %s", (org_id,))
        conn.commit()


def _drop_project(proj_id: str) -> None:
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.projects WHERE id = %s", (proj_id,))
        conn.commit()


# ---------------------------------------------------------------------------
# Live-Postgres: resolver semantics.
# ---------------------------------------------------------------------------


@pg_available
def test_suspended_owner_loses_manage_rights(monkeypatch):
    """review-21.5 F-HIGH: a SUSPENDED owner/admin must NOT resolve to a role.

    Also: an active admin alongside the suspended owner keeps the org CLOSED and
    can manage -- proving 'has_active_members' counts only active rows.
    """
    from core.db import get_connection
    from core.project_access import identity_can_manage_org, resolve_org_role

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    suffix = uuid.uuid4().hex[:8]
    org = f"org_susp_{suffix}"
    suspended = "suspended-owner@acme"
    active_admin = "active-admin@acme"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _mk_org(cur, org, suffix)
                _mk_member_status(cur, org, suspended, "owner", "suspended")
                _mk_member_status(cur, org, active_admin, "admin", "active")
            conn.commit()
            # Suspended owner -> no role, no manage.
            assert resolve_org_role(org, suspended, conn) is None
            assert identity_can_manage_org(org, suspended, conn) is False
            # Active admin -> manages; org stays CLOSED to outsiders.
            assert identity_can_manage_org(org, active_admin, conn) is True
            assert resolve_org_role(org, "outsider@y", conn) is None
    finally:
        _drop_org(org)


@pg_available
def test_archived_org_closed_to_everyone(monkeypatch):
    """An archived org resolves to None even for a would-be member (default-open
    cannot resurrect it)."""
    from core.db import get_connection
    from core.project_access import resolve_org_role

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    suffix = uuid.uuid4().hex[:8]
    org = f"org_arch_{suffix}"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _mk_org(cur, org, suffix)
                _mk_member(cur, org, "owner@acme", "owner", suffix)
                cur.execute(
                    "UPDATE app.organizations SET status = 'archived' WHERE id = %s",
                    (org,),
                )
            conn.commit()
            assert resolve_org_role(org, "owner@acme", conn) is None
    finally:
        _drop_org(org)


@pg_available
def test_default_open_zero_members_resolves_owner(monkeypatch):
    from core.db import get_connection
    from core.project_access import identity_can_manage_org, resolve_org_role

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    suffix = uuid.uuid4().hex[:8]
    org = f"org_open_{suffix}"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _mk_org(cur, org, suffix)
            conn.commit()
            # ZERO members -> OPEN: any identity resolves to owner + can manage.
            assert resolve_org_role(org, "someone@x", conn) == "owner"
            assert identity_can_manage_org(org, "someone@x", conn) is True
    finally:
        _drop_org(org)


@pg_available
def test_enrolled_closed_role_and_manage(monkeypatch):
    from core.db import get_connection
    from core.project_access import identity_can_manage_org, resolve_org_role

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    suffix = uuid.uuid4().hex[:8]
    org = f"org_closed_{suffix}"
    x = "member-x@acme"
    z = "admin-z@acme"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _mk_org(cur, org, suffix)
                _mk_member(cur, org, x, "member", suffix)
            conn.commit()
            # Enrolled -> CLOSED: an outsider is denied, the member gets 'member'.
            assert resolve_org_role(org, "outsider@y", conn) is None
            assert resolve_org_role(org, x, conn) == "member"
            # member is NOT a manager.
            assert identity_can_manage_org(org, x, conn) is False
            with conn.cursor() as cur:
                _mk_member(cur, org, z, "admin", suffix)
            conn.commit()
            assert identity_can_manage_org(org, z, conn) is True
    finally:
        _drop_org(org)


@pg_available
def test_anonymous_is_owner_even_on_enrolled_org(monkeypatch):
    from core.db import get_connection
    from core.project_access import resolve_org_role

    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    suffix = uuid.uuid4().hex[:8]
    org = f"org_anon_{suffix}"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _mk_org(cur, org, suffix)
                _mk_member(cur, org, "real@acme", "member", suffix)
            conn.commit()
            # disabled auth -> 'anonymous' is always owner even on a CLOSED org.
            assert resolve_org_role(org, "anonymous", conn) == "owner"
    finally:
        _drop_org(org)


@pg_available
def test_member_project_allow_list(monkeypatch):
    from core.db import get_connection
    from core.project_access import identity_can_access_project_in_org

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    suffix = uuid.uuid4().hex[:8]
    org = f"org_al_{suffix}"
    p1 = f"proj_al1_{suffix}"
    p2 = f"proj_al2_{suffix}"
    x = "carole@acme"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _mk_org(cur, org, suffix)
                _mk_member(cur, org, x, "member", suffix)
                _mk_project(cur, p1, org, suffix)
                _mk_project(cur, p2, org, suffix)
            conn.commit()
            # NO grants -> member sees ALL org projects (default-open within org).
            assert identity_can_access_project_in_org(p1, x, conn) is True
            assert identity_can_access_project_in_org(p2, x, conn) is True
            # First project grant engages the allow-list: only P1 remains visible.
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.resource_grants "
                    "(id, org_id, identity, scope_type, scope_id, granted_by) "
                    "VALUES (%s, %s, %s, 'project', %s, 'system')",
                    (f"rgrant_{uuid.uuid4().hex[:10]}", org, x, p1),
                )
            conn.commit()
            assert identity_can_access_project_in_org(p1, x, conn) is True
            assert identity_can_access_project_in_org(p2, x, conn) is False
    finally:
        _drop_project(p1)
        _drop_project(p2)
        _drop_org(org)


@pg_available
def test_legacy_no_org_project_falls_back_to_project_default_open(monkeypatch):
    from core.db import get_connection
    from core.project_access import identity_can_access_project_in_org

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    suffix = uuid.uuid4().hex[:8]
    proj = f"proj_legacy_{suffix}"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # org_id NULL = legacy no-org project (pre-Epic-21).
                _mk_project(cur, proj, None, suffix)
            conn.commit()
            # No project_members -> Story 7.4 default-open -> anyone reaches it.
            assert identity_can_access_project_in_org(proj, "whoever@x", conn) is True
    finally:
        _drop_project(proj)


@pg_available
def test_unknown_project_denied(monkeypatch):
    from core.db import get_connection
    from core.project_access import identity_can_access_project_in_org

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    with get_connection() as conn:
        assert (
            identity_can_access_project_in_org("proj_nope_x", "whoever@x", conn) is False
        )


# ---------------------------------------------------------------------------
# Live-Postgres: resource_grants schema CHECK.
# ---------------------------------------------------------------------------


@pg_available
def test_resource_grants_scope_type_check_rejects_invalid():
    """The CHECK on resource_grants.scope_type rejects a value outside the enum.

    Self-contained: creates a throwaway org in the SAME transaction; the
    CheckViolation aborts it and rolls back the org insert too -> no cleanup.
    """
    import psycopg
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_id = f"org_sc_{suffix}"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, created_by) "
                "VALUES (%s, %s, %s, 'system')",
                (org_id, "ScopeCheck", f"sc-{suffix}"),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.resource_grants "
                    "(id, org_id, identity, scope_type, scope_id, granted_by) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        f"rgrant_sc_{suffix}",
                        org_id,
                        "x@e.com",
                        "workspace",  # NOT in ('project','flux')
                        "scope_1",
                        "system",
                    ),
                )
        conn.rollback()  # rolls back BOTH inserts


# ---------------------------------------------------------------------------
# Endpoint enforcement (live-Postgres, patched _check_auth).
# ---------------------------------------------------------------------------


def _post_request(body: dict) -> MagicMock:
    req = MagicMock()
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    req.path_params = {}
    return req


def _patch_request(org_id: str, body: dict) -> MagicMock:
    req = MagicMock()
    req.path_params = {"org_id": org_id}
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    return req


def _member_request(org_id: str, body: dict) -> MagicMock:
    req = MagicMock()
    req.path_params = {"org_id": org_id}
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    return req


@pg_available
@pytest.mark.anyio
async def test_create_org_auto_enrolls_creator_who_can_then_manage(monkeypatch):
    """The API-created org enrolls its creator as owner -> creator can PATCH; a
    stranger is refused 403 (the org is now CLOSED)."""
    from core.admin_api import _create_org, _patch_org

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    slug = f"ae-{uuid.uuid4().hex[:8]}"
    oid = None
    try:
        with patch(_AUTH, return_value=(True, "owner@x")):
            r = await _create_org(_post_request({"name": "AE", "slug": slug}))
            assert r.status_code == 201
            oid = json.loads(r.body)["id"]
            # Creator (auto-enrolled owner) CAN manage.
            ok = await _patch_org(_patch_request(oid, {"name": "AE2"}))
            assert ok.status_code == 200
        # A stranger CANNOT manage the now-enrolled org -> 403.
        with patch(_AUTH, return_value=(True, "stranger@x")):
            denied = await _patch_org(_patch_request(oid, {"name": "Nope"}))
        assert denied.status_code == 403
        assert json.loads(denied.body)["code"] == "forbidden"
    finally:
        if oid:
            _drop_org(oid)


@pg_available
@pytest.mark.anyio
async def test_add_member_denied_for_non_manager(monkeypatch):
    """A non-member of an ENROLLED org is refused 403 when adding a member."""
    from core.admin_api import _add_org_member
    from core.db import get_connection

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    suffix = uuid.uuid4().hex[:8]
    org = f"org_am_{suffix}"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _mk_org(cur, org, suffix)
                _mk_member(cur, org, "real@acme", "member", suffix)
            conn.commit()
        with patch(_AUTH, return_value=(True, "stranger@x")):
            resp = await _add_org_member(
                _member_request(org, {"identity": "new@acme", "role": "member"})
            )
        assert resp.status_code == 403
        assert json.loads(resp.body)["code"] == "forbidden"
    finally:
        _drop_org(org)


def _member_mgmt_request(org_id: str, target: str, body: dict | None = None) -> MagicMock:
    req = MagicMock()
    req.path_params = {"org_id": org_id, "identity": target}
    if body is not None:
        req.body = AsyncMock(return_value=json.dumps(body).encode())
    return req


def _org_id_request(org_id: str) -> MagicMock:
    req = MagicMock()
    req.path_params = {"org_id": org_id}
    return req


@pg_available
@pytest.mark.anyio
async def test_get_org_denied_for_non_member_404(monkeypatch):
    """Reads scoping: a non-member of an enrolled org gets 404 (existence hidden);
    the member (auto-enrolled creator) gets 200."""
    from core.admin_api import _create_org, _get_org

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    slug = f"rs-{uuid.uuid4().hex[:8]}"
    oid = None
    try:
        with patch(_AUTH, return_value=(True, "owner@rs")):
            oid = json.loads(
                (await _create_org(_post_request({"name": "RS", "slug": slug}))).body
            )["id"]
            assert (await _get_org(_org_id_request(oid))).status_code == 200
        with patch(_AUTH, return_value=(True, "stranger@rs")):
            assert (await _get_org(_org_id_request(oid))).status_code == 404
    finally:
        if oid:
            _drop_org(oid)


@pg_available
@pytest.mark.anyio
async def test_list_orgs_excludes_other_tenants(monkeypatch):
    """Reads scoping: an enrolled org appears in its member's list but not in a
    stranger's."""
    from core.admin_api import _create_org, _list_orgs

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    slug = f"ls-{uuid.uuid4().hex[:8]}"
    oid = None
    try:
        with patch(_AUTH, return_value=(True, "owner@ls")):
            oid = json.loads(
                (await _create_org(_post_request({"name": "LS", "slug": slug}))).body
            )["id"]
            mine = json.loads((await _list_orgs(_post_request({}))).body)["organizations"]
            assert any(o["id"] == oid for o in mine)
        with patch(_AUTH, return_value=(True, "stranger@ls")):
            theirs = json.loads((await _list_orgs(_post_request({}))).body)["organizations"]
            assert all(o["id"] != oid for o in theirs)
    finally:
        if oid:
            _drop_org(oid)


@pg_available
@pytest.mark.anyio
async def test_remove_last_active_owner_refused_409(monkeypatch):
    """Removing the sole active owner would silently reopen the tenant -> 409."""
    from core.admin_api import _create_org, _remove_org_member

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    slug = f"lo-{uuid.uuid4().hex[:8]}"
    oid = None
    try:
        with patch(_AUTH, return_value=(True, "mgr@x")):
            oid = json.loads(
                (await _create_org(_post_request({"name": "LO", "slug": slug}))).body
            )["id"]
            # mgr@x is the sole auto-enrolled owner -> cannot remove itself.
            resp = await _remove_org_member(_member_mgmt_request(oid, "mgr@x"))
        assert resp.status_code == 409
    finally:
        if oid:
            _drop_org(oid)


@pg_available
@pytest.mark.anyio
async def test_remove_one_of_two_owners_ok(monkeypatch):
    from core.admin_api import _add_org_member, _create_org, _remove_org_member

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    slug = f"two-{uuid.uuid4().hex[:8]}"
    oid = None
    try:
        with patch(_AUTH, return_value=(True, "mgr@x")):
            oid = json.loads(
                (await _create_org(_post_request({"name": "TWO", "slug": slug}))).body
            )["id"]
            await _add_org_member(
                _member_request(oid, {"identity": "o2@x", "role": "owner"})
            )
            resp = await _remove_org_member(_member_mgmt_request(oid, "o2@x"))
        assert resp.status_code == 200
    finally:
        if oid:
            _drop_org(oid)


@pg_available
@pytest.mark.anyio
async def test_downgrade_sole_owner_refused_409(monkeypatch):
    from core.admin_api import _create_org, _update_org_member

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    slug = f"dg-{uuid.uuid4().hex[:8]}"
    oid = None
    try:
        with patch(_AUTH, return_value=(True, "mgr@x")):
            oid = json.loads(
                (await _create_org(_post_request({"name": "DG", "slug": slug}))).body
            )["id"]
            resp = await _update_org_member(
                _member_mgmt_request(oid, "mgr@x", {"role": "member"})
            )
        assert resp.status_code == 409
    finally:
        if oid:
            _drop_org(oid)


@pg_available
@pytest.mark.anyio
async def test_remove_nonmember_404(monkeypatch):
    from core.admin_api import _create_org, _remove_org_member

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    slug = f"nm-{uuid.uuid4().hex[:8]}"
    oid = None
    try:
        with patch(_AUTH, return_value=(True, "mgr@x")):
            oid = json.loads(
                (await _create_org(_post_request({"name": "NM", "slug": slug}))).body
            )["id"]
            resp = await _remove_org_member(_member_mgmt_request(oid, "ghost@x"))
        assert resp.status_code == 404
    finally:
        if oid:
            _drop_org(oid)


# ---------------------------------------------------------------------------
# Story 21.8 AC4: GET /api/organizations/{org_id}/members
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_list_org_members_authorized_member_gets_list(monkeypatch):
    """(a) An active member of the org gets 200 with a 'members' list.

    The auto-enrolled creator is itself in the list (role='owner', status='active').
    An extra member added via _add_org_member also appears.
    """
    from core.admin_api import _add_org_member, _create_org, _list_org_members

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    slug = f"lm-{uuid.uuid4().hex[:8]}"
    oid = None
    try:
        with patch(_AUTH, return_value=(True, "owner@lm")):
            oid = json.loads(
                (await _create_org(_post_request({"name": "LM", "slug": slug}))).body
            )["id"]
            await _add_org_member(
                _member_request(oid, {"identity": "extra@lm", "role": "member"})
            )
            resp = await _list_org_members(_org_id_request(oid))
        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert "members" in body
        identities = {m["identity"] for m in body["members"]}
        assert "owner@lm" in identities
        assert "extra@lm" in identities
        # Each member row must contain the expected fields.
        for m in body["members"]:
            assert "id" in m
            assert "org_id" in m
            assert "role" in m
            assert "status" in m
            assert "joined_at" in m
            assert "created_at" in m
    finally:
        if oid:
            _drop_org(oid)


@pg_available
@pytest.mark.anyio
async def test_list_org_members_non_member_gets_404(monkeypatch):
    """(b) A non-member of an enrolled org gets 404 (existence not disclosed)."""
    from core.admin_api import _create_org, _list_org_members

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    slug = f"lm2-{uuid.uuid4().hex[:8]}"
    oid = None
    try:
        with patch(_AUTH, return_value=(True, "owner@lm2")):
            oid = json.loads(
                (await _create_org(_post_request({"name": "LM2", "slug": slug}))).body
            )["id"]
        # A stranger cannot see the enrolled org's member list.
        with patch(_AUTH, return_value=(True, "stranger@lm2")):
            resp = await _list_org_members(_org_id_request(oid))
        assert resp.status_code == 404
        assert json.loads(resp.body)["code"] == "not_found"
    finally:
        if oid:
            _drop_org(oid)


@pg_available
@pytest.mark.anyio
async def test_list_org_members_includes_suspended_member(monkeypatch):
    """(c) A suspended member row is visible in the list for an authorized caller.

    The *caller's* own active membership gates the route; the suspended member
    appears as a row with status='suspended'.
    """
    from core.admin_api import _add_org_member, _create_org, _list_org_members, _update_org_member

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    slug = f"lm3-{uuid.uuid4().hex[:8]}"
    oid = None
    try:
        with patch(_AUTH, return_value=(True, "owner@lm3")):
            oid = json.loads(
                (await _create_org(_post_request({"name": "LM3", "slug": slug}))).body
            )["id"]
            # Add a second owner so we can suspend the extra member without
            # triggering the last-owner guard on the original owner.
            await _add_org_member(
                _member_request(oid, {"identity": "extra@lm3", "role": "member"})
            )
            # Suspend the extra member.
            await _update_org_member(
                _member_mgmt_request(oid, "extra@lm3", {"status": "suspended"})
            )
            resp = await _list_org_members(_org_id_request(oid))
        assert resp.status_code == 200
        members = json.loads(resp.body)["members"]
        suspended = [m for m in members if m["identity"] == "extra@lm3"]
        assert len(suspended) == 1
        assert suspended[0]["status"] == "suspended"
    finally:
        if oid:
            _drop_org(oid)


# ---------------------------------------------------------------------------
# Epic 21 security regression tests (FIX 1a, 1b, 3, 4, 5).
# All tests are under the pg_available marker and skip offline.
# ---------------------------------------------------------------------------


# -- Seed helpers for credential tests (NULL-owner and normal owner variants) --


def _mk_credential(cur, cred_id: str, proj_id: str, owner_org_id: str | None) -> None:
    """Insert a connection_ref row with optional owner_org_id (None = legacy/NULL)."""
    cur.execute(
        "INSERT INTO app.connection_ref "
        "(id, provider, nango_connection_id, project_id, owner_org_id) "
        "VALUES (%s, %s, %s, %s, %s)",
        (cred_id, "google-analytics", f"nango-{cred_id}", proj_id, owner_org_id),
    )


def _drop_credential(cred_id: str) -> None:
    """Delete a connection_ref row (cascades credential_accounts + grants)."""
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.connection_ref WHERE id = %s", (cred_id,))
        conn.commit()


def _cred_post_req(credential_id: str, body: dict) -> MagicMock:
    req = MagicMock()
    req.path_params = {"credential_id": credential_id}
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    return req


def _cred_get_req(credential_id: str, **extra_params) -> MagicMock:
    req = MagicMock()
    req.path_params = {"credential_id": credential_id, **extra_params}
    return req


# ---------------------------------------------------------------------------
# FIX 1(b): last-active-MANAGER floor widened to owner|admin.
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_remove_sole_active_admin_no_owner_refused_409(monkeypatch):
    """FIX 1(b): when the sole active manager is an ADMIN (no active owner), removing
    it must return 409 -- the org would be left with zero active managers.

    Seed: org with one active ADMIN + two non-manager members (viewer + member).
    The guard must fire even though the org has no owner at all.
    """
    from core.admin_api import _remove_org_member
    from core.db import get_connection

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    suffix = uuid.uuid4().hex[:8]
    org = f"org_fix1b_rm_{suffix}"
    admin_id = "sole-admin@fix1b"
    viewer_id = "viewer@fix1b"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _mk_org(cur, org, suffix)
                _mk_member_status(cur, org, admin_id, "admin", "active")
                _mk_member_status(cur, org, viewer_id, "viewer", "active")
            conn.commit()
        # The admin is the actor AND the target -- it is the sole active manager.
        with patch(_AUTH, return_value=(True, admin_id)):
            resp = await _remove_org_member(_member_mgmt_request(org, admin_id))
        assert resp.status_code == 409
        assert json.loads(resp.body)["code"] == "conflict"
    finally:
        _drop_org(org)


@pg_available
@pytest.mark.anyio
async def test_demote_sole_active_admin_to_member_refused_409(monkeypatch):
    """FIX 1(b): demoting the sole active admin to 'member' returns 409."""
    from core.admin_api import _update_org_member
    from core.db import get_connection

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    suffix = uuid.uuid4().hex[:8]
    org = f"org_fix1b_dm_{suffix}"
    admin_id = "sole-admin2@fix1b"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _mk_org(cur, org, suffix)
                _mk_member_status(cur, org, admin_id, "admin", "active")
            conn.commit()
        with patch(_AUTH, return_value=(True, admin_id)):
            resp = await _update_org_member(
                _member_mgmt_request(org, admin_id, {"role": "member"})
            )
        assert resp.status_code == 409
        assert json.loads(resp.body)["code"] == "conflict"
    finally:
        _drop_org(org)


@pg_available
@pytest.mark.anyio
async def test_suspend_sole_active_admin_refused_409(monkeypatch):
    """FIX 1(b): suspending the sole active admin returns 409."""
    from core.admin_api import _update_org_member
    from core.db import get_connection

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    suffix = uuid.uuid4().hex[:8]
    org = f"org_fix1b_su_{suffix}"
    admin_id = "sole-admin3@fix1b"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _mk_org(cur, org, suffix)
                _mk_member_status(cur, org, admin_id, "admin", "active")
            conn.commit()
        with patch(_AUTH, return_value=(True, admin_id)):
            resp = await _update_org_member(
                _member_mgmt_request(org, admin_id, {"status": "suspended"})
            )
        assert resp.status_code == 409
        assert json.loads(resp.body)["code"] == "conflict"
    finally:
        _drop_org(org)


# ---------------------------------------------------------------------------
# FIX 1(a): the guard counts only ACTIVE managers -- suspended owners are
# NOT a backstop.
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_remove_sole_active_owner_with_suspended_owner_refused_409(monkeypatch):
    """FIX 1(a): 1 active owner + 1 SUSPENDED owner -> removing the active owner
    must still be refused with 409.

    The suspended owner does NOT count as a backstop; the guard uses
    'status = active' in its FOR UPDATE count. The TOCTOU race itself is covered
    by the FOR UPDATE lock in _would_orphan_last_manager and would require a
    concurrent-connection harness to exercise directly -- this test exercises the
    counting logic without concurrency.
    """
    from core.admin_api import _remove_org_member
    from core.db import get_connection

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    suffix = uuid.uuid4().hex[:8]
    org = f"org_fix1a_{suffix}"
    active_owner = "active-owner@fix1a"
    susp_owner = "susp-owner@fix1a"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _mk_org(cur, org, suffix)
                _mk_member_status(cur, org, active_owner, "owner", "active")
                _mk_member_status(cur, org, susp_owner, "owner", "suspended")
            conn.commit()
        with patch(_AUTH, return_value=(True, active_owner)):
            resp = await _remove_org_member(_member_mgmt_request(org, active_owner))
        assert resp.status_code == 409
        assert json.loads(resp.body)["code"] == "conflict"
    finally:
        _drop_org(org)


# ---------------------------------------------------------------------------
# FIX 3: role-hierarchy enforcement (admin cannot assign owner).
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_admin_cannot_add_member_with_role_owner_403(monkeypatch):
    """FIX 3(a): an ADMIN actor adding a new member with role='owner' -> 403."""
    from core.admin_api import _add_org_member
    from core.db import get_connection

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    suffix = uuid.uuid4().hex[:8]
    org = f"org_fix3_add_{suffix}"
    owner_id = "owner@fix3"
    admin_id = "admin@fix3"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _mk_org(cur, org, suffix)
                _mk_member_status(cur, org, owner_id, "owner", "active")
                _mk_member_status(cur, org, admin_id, "admin", "active")
            conn.commit()
        # Admin actor tries to add a new member with role='owner' -> must be denied.
        with patch(_AUTH, return_value=(True, admin_id)):
            resp = await _add_org_member(
                _member_request(org, {"identity": "new@fix3", "role": "owner"})
            )
        assert resp.status_code == 403
        assert json.loads(resp.body)["code"] == "forbidden"
    finally:
        _drop_org(org)


@pg_available
@pytest.mark.anyio
async def test_admin_cannot_patch_member_role_to_owner_403(monkeypatch):
    """FIX 3(b): an ADMIN actor PATCHing another member's role to 'owner' -> 403."""
    from core.admin_api import _update_org_member
    from core.db import get_connection

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    suffix = uuid.uuid4().hex[:8]
    org = f"org_fix3_patch_{suffix}"
    owner_id = "owner@fix3p"
    admin_id = "admin@fix3p"
    target_id = "member@fix3p"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _mk_org(cur, org, suffix)
                _mk_member_status(cur, org, owner_id, "owner", "active")
                _mk_member_status(cur, org, admin_id, "admin", "active")
                _mk_member_status(cur, org, target_id, "member", "active")
            conn.commit()
        with patch(_AUTH, return_value=(True, admin_id)):
            resp = await _update_org_member(
                _member_mgmt_request(org, target_id, {"role": "owner"})
            )
        assert resp.status_code == 403
        assert json.loads(resp.body)["code"] == "forbidden"
    finally:
        _drop_org(org)


@pg_available
@pytest.mark.anyio
async def test_admin_cannot_self_promote_to_owner_403(monkeypatch):
    """FIX 3(c): an ADMIN self-promoting to 'owner' -> 403 (self-escalation blocked)."""
    from core.admin_api import _update_org_member
    from core.db import get_connection

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    suffix = uuid.uuid4().hex[:8]
    org = f"org_fix3_self_{suffix}"
    owner_id = "owner@fix3s"
    admin_id = "admin@fix3s"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _mk_org(cur, org, suffix)
                _mk_member_status(cur, org, owner_id, "owner", "active")
                _mk_member_status(cur, org, admin_id, "admin", "active")
            conn.commit()
        with patch(_AUTH, return_value=(True, admin_id)):
            resp = await _update_org_member(
                _member_mgmt_request(org, admin_id, {"role": "owner"})
            )
        assert resp.status_code == 403
        assert json.loads(resp.body)["code"] == "forbidden"
    finally:
        _drop_org(org)


@pg_available
@pytest.mark.anyio
async def test_owner_can_assign_owner_role_positive_control(monkeypatch):
    """FIX 3 positive control: an OWNER actor CAN add a new member with role='owner'."""
    from core.admin_api import _add_org_member
    from core.db import get_connection

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    suffix = uuid.uuid4().hex[:8]
    org = f"org_fix3_ok_{suffix}"
    owner_id = "owner@fix3ok"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _mk_org(cur, org, suffix)
                _mk_member_status(cur, org, owner_id, "owner", "active")
            conn.commit()
        with patch(_AUTH, return_value=(True, owner_id)):
            resp = await _add_org_member(
                _member_request(org, {"identity": "new-owner@fix3ok", "role": "owner"})
            )
        assert resp.status_code == 201
        assert json.loads(resp.body)["role"] == "owner"
    finally:
        _drop_org(org)


# ---------------------------------------------------------------------------
# FIX 4: NULL-owner credential -> non-disclosing 404 on grant and revoke.
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_null_owner_credential_create_grant_returns_404(monkeypatch):
    """FIX 4: POST grant on a credential with owner_org_id IS NULL -> 404 (not 403).

    The handler emits a non-disclosing 404 so callers cannot distinguish a
    missing credential from a credential whose owner org is un-backfilled.
    """
    from core.admin_api import _create_account_grant
    from core.db import get_connection

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    suffix = uuid.uuid4().hex[:8]
    org = f"org_fix4_cg_{suffix}"
    proj = f"proj_fix4_cg_{suffix}"
    cred = f"conn_fix4_cg_{suffix}"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _mk_org(cur, org, suffix)
                _mk_project(cur, proj, org, suffix)
                # credential with owner_org_id deliberately left NULL (legacy).
                cur.execute(
                    "INSERT INTO app.connection_ref "
                    "(id, provider, nango_connection_id, project_id) "
                    "VALUES (%s, %s, %s, %s)",
                    (cred, "google-analytics", f"nango-{suffix}", proj),
                )
                # Register the account via direct SQL so the FK exists and the
                # handler reaches the owner_org_id check (not the account-missing 404).
                cur.execute(
                    "INSERT INTO app.credential_accounts "
                    "(credential_id, external_account_id) VALUES (%s, %s)",
                    (cred, "acct_null"),
                )
            conn.commit()
        req = MagicMock()
        req.path_params = {
            "credential_id": cred,
            "external_account_id": "acct_null",
        }
        req.body = AsyncMock(return_value=json.dumps({"grantee_org_id": org}).encode())
        with patch(_AUTH, return_value=(True, "anyone@fix4")):
            resp = await _create_account_grant(req)
        assert resp.status_code == 404
        assert json.loads(resp.body)["code"] == "not_found"
    finally:
        _drop_credential(cred)
        _drop_project(proj)
        _drop_org(org)


@pg_available
@pytest.mark.anyio
async def test_null_owner_credential_revoke_grant_returns_404(monkeypatch):
    """FIX 4: DELETE revoke on a credential with owner_org_id IS NULL -> 404."""
    from core.admin_api import _revoke_account_grant
    from core.db import get_connection

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    suffix = uuid.uuid4().hex[:8]
    org = f"org_fix4_rv_{suffix}"
    proj = f"proj_fix4_rv_{suffix}"
    cred = f"conn_fix4_rv_{suffix}"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _mk_org(cur, org, suffix)
                _mk_project(cur, proj, org, suffix)
                cur.execute(
                    "INSERT INTO app.connection_ref "
                    "(id, provider, nango_connection_id, project_id) "
                    "VALUES (%s, %s, %s, %s)",
                    (cred, "google-analytics", f"nango-{suffix}", proj),
                )
            conn.commit()
        req = MagicMock()
        req.path_params = {
            "credential_id": cred,
            "external_account_id": "acct_null",
            "grantee_org_id": org,
        }
        with patch(_AUTH, return_value=(True, "anyone@fix4rv")):
            resp = await _revoke_account_grant(req)
        assert resp.status_code == 404
        assert json.loads(resp.body)["code"] == "not_found"
    finally:
        _drop_credential(cred)
        _drop_project(proj)
        _drop_org(org)


@pg_available
@pytest.mark.anyio
async def test_credential_with_owner_org_create_grant_positive_control(monkeypatch):
    """FIX 4 positive control: a credential WITH a valid owner_org_id lets an
    owner/admin of that org successfully create a grant (201)."""
    from core.admin_api import _create_account_grant, _register_credential_account
    from core.db import get_connection

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    suffix = uuid.uuid4().hex[:8]
    org_a = f"org_fix4_ok_a_{suffix}"
    org_b = f"org_fix4_ok_b_{suffix}"
    proj = f"proj_fix4_ok_{suffix}"
    cred = f"conn_fix4_ok_{suffix}"
    owner_id = "owner@fix4ok"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _mk_org(cur, org_a, suffix)
                _mk_org(cur, org_b, f"b{suffix}")
                _mk_member_status(cur, org_a, owner_id, "owner", "active")
                _mk_project(cur, proj, org_a, suffix)
                _mk_credential(cur, cred, proj, org_a)
            conn.commit()
        with patch(_AUTH, return_value=(True, owner_id)):
            # Register account via API (owner of org_a -> succeeds).
            r_reg = await _register_credential_account(
                _cred_post_req(cred, {"external_account_id": "acct_ok"})
            )
            assert r_reg.status_code == 201
            # Grant access to org_b.
            req = MagicMock()
            req.path_params = {"credential_id": cred, "external_account_id": "acct_ok"}
            req.body = AsyncMock(
                return_value=json.dumps({"grantee_org_id": org_b}).encode()
            )
            r_grant = await _create_account_grant(req)
        assert r_grant.status_code == 201
    finally:
        _drop_credential(cred)
        _drop_project(proj)
        _drop_org(org_a)
        _drop_org(org_b)


# ---------------------------------------------------------------------------
# FIX 5: _register_credential_account gating.
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_register_credential_account_non_manager_denied_403(monkeypatch):
    """FIX 5: a caller who is NOT a manager (owner/admin) of the credential's
    owner org is denied 403."""
    from core.admin_api import _register_credential_account
    from core.db import get_connection

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    suffix = uuid.uuid4().hex[:8]
    org = f"org_fix5_nm_{suffix}"
    proj = f"proj_fix5_nm_{suffix}"
    cred = f"conn_fix5_nm_{suffix}"
    owner_id = "owner@fix5"
    member_id = "member@fix5"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _mk_org(cur, org, suffix)
                _mk_member_status(cur, org, owner_id, "owner", "active")
                _mk_member_status(cur, org, member_id, "member", "active")
                _mk_project(cur, proj, org, suffix)
                _mk_credential(cur, cred, proj, org)
            conn.commit()
        # A plain member (not a manager) -> 403.
        with patch(_AUTH, return_value=(True, member_id)):
            resp = await _register_credential_account(
                _cred_post_req(cred, {"external_account_id": "acct_nm"})
            )
        assert resp.status_code == 403
        assert json.loads(resp.body)["code"] == "forbidden"
    finally:
        _drop_credential(cred)
        _drop_project(proj)
        _drop_org(org)


@pg_available
@pytest.mark.anyio
async def test_register_credential_account_null_owner_org_returns_404(monkeypatch):
    """FIX 5: a credential with owner_org_id IS NULL -> non-disclosing 404."""
    from core.admin_api import _register_credential_account
    from core.db import get_connection

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    suffix = uuid.uuid4().hex[:8]
    org = f"org_fix5_no_{suffix}"
    proj = f"proj_fix5_no_{suffix}"
    cred = f"conn_fix5_no_{suffix}"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _mk_org(cur, org, suffix)
                _mk_project(cur, proj, org, suffix)
                # owner_org_id LEFT NULL intentionally.
                cur.execute(
                    "INSERT INTO app.connection_ref "
                    "(id, provider, nango_connection_id, project_id) "
                    "VALUES (%s, %s, %s, %s)",
                    (cred, "google-analytics", f"nango-{suffix}", proj),
                )
            conn.commit()
        with patch(_AUTH, return_value=(True, "anyone@fix5no")):
            resp = await _register_credential_account(
                _cred_post_req(cred, {"external_account_id": "acct_no"})
            )
        assert resp.status_code == 404
        assert json.loads(resp.body)["code"] == "not_found"
    finally:
        _drop_credential(cred)
        _drop_project(proj)
        _drop_org(org)


@pg_available
@pytest.mark.anyio
async def test_register_credential_account_owner_org_manager_succeeds(monkeypatch):
    """FIX 5 positive control: an owner/admin of the credential's owner org -> 201."""
    from core.admin_api import _register_credential_account
    from core.db import get_connection

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    suffix = uuid.uuid4().hex[:8]
    org = f"org_fix5_ok_{suffix}"
    proj = f"proj_fix5_ok_{suffix}"
    cred = f"conn_fix5_ok_{suffix}"
    owner_id = "owner@fix5ok"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _mk_org(cur, org, suffix)
                _mk_member_status(cur, org, owner_id, "owner", "active")
                _mk_project(cur, proj, org, suffix)
                _mk_credential(cur, cred, proj, org)
            conn.commit()
        with patch(_AUTH, return_value=(True, owner_id)):
            resp = await _register_credential_account(
                _cred_post_req(cred, {"external_account_id": "acct_ok", "label": "OK"})
            )
        assert resp.status_code == 201
        body = json.loads(resp.body)
        assert body["external_account_id"] == "acct_ok"
    finally:
        _drop_credential(cred)
        _drop_project(proj)
        _drop_org(org)
