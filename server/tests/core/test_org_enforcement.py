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

from tests.conftest import purge_fixture_project

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


def _role_conn(row):
    """A cursor answering the membership query: ``(role,)`` or None."""
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = row
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn


def test_anonymous_gets_no_short_circuit_and_no_role(monkeypatch):
    """Story 46.4 removed the disabled-auth 'anonymous -> owner' short-circuit.

    The resolver asks the database like it does for anybody else, and an
    identity with no ACTIVE membership row gets no role — in every auth mode.
    """
    from core.project_access import resolve_org_role

    for mode in ("disabled", "static", "oauth"):
        monkeypatch.setenv("TOOROW_AUTH_MODE", mode)
        conn = _role_conn(None)
        assert resolve_org_role("org_x", "anonymous", conn) is None
        conn.cursor.assert_called()


def test_an_active_membership_row_is_the_only_source_of_a_role(monkeypatch):
    from core.project_access import resolve_org_role

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    assert resolve_org_role("org_x", "member@example.com", _role_conn(("admin",))) == "admin"


def test_production_never_opens_an_unclaimed_organization(monkeypatch):
    """Zero membership used to read as 'unclaimed, therefore open'. It never does now.

    There is no runtime switch left to turn this on or off: the query itself
    joins ``app.org_members`` with ``status='active'``, so an organization with
    no member simply returns nothing to an outsider.
    """
    from core.project_access import resolve_org_role

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    assert resolve_org_role("org_unclaimed", "outsider@example.com", _role_conn(None)) is None

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


def _enrol(org_id: str, identity: str, role: str = "member") -> None:
    """Put a person in an organization, the only way that still exists.

    `POST /api/organizations/{id}/members` STOPPED ENROLLING ANYONE on 2026-08-24
    (`org_members_api._add_org_member`): it answers `409 invitation_required` to
    every caller, whatever their role, because direct enrolment wrote a
    caller-supplied string into `app.org_members.identity` where canonical
    identity requires a `person_<ULID>` minted from a verified pair. Four tests
    below used that route as a FIXTURE -- "add a second owner, then remove one",
    "add an extra member, then list" -- so the closed door did not fail them on
    the assertion they were making; it failed them on the decor, and the 409 read
    like the subject under test had regressed.

    Seeding the row is what the invitation path leaves behind, and it is what
    those tests were ever really asking for. The route's own refusal is asserted
    by `test_the_enrolment_door_is_closed_to_everyone` below, so nothing that
    used to be covered stops being covered.
    """
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        with conn.cursor() as cur:
            _mk_member(cur, org_id, identity, role, uuid.uuid4().hex[:8])
        conn.commit()


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
    """Erase the org through the product's own purge, not by hand.

    The comment that stood here -- "org delete cascades org_members +
    resource_grants" -- named two children out of 175 and was the reason 20 of
    this file's tests failed on teardown against a real Postgres:
    `mdm_business_domains_org_id_fkey` and `project_capabilities_project_id_fkey`
    are RESTRICT, so a bare DELETE on the org is refused and every later test in
    the module inherits the poisoned transaction.

    `purge_org_tree` is the ONE function that knows the whole tree, and it is the
    function `DELETE /api/organizations` runs. A teardown that reimplements it
    tests a deletion path no user ever takes -- and, worse, goes green while the
    real one is broken. That is exactly what happened: the purge raised on EVERY
    organization for months and this file could not see it.
    """
    from core.db import get_connection

    from tests.conftest import purge_fixture_org

    with get_connection() as conn:
        purge_fixture_org(conn, org_id)
        conn.commit()


def _drop_project(proj_id: str) -> None:
    """Drop a project -- but ONLY one that hangs off no organization.

    Deleting a project is not a product operation: it is ARCHIVED. The schema
    says so out loud -- 63 of the foreign keys pointing at `app.projects` are
    RESTRICT or NO ACTION and exactly zero cascade, measured with:

        SELECT count(*) FROM pg_constraint
         WHERE contype='f' AND confrelid='app.projects'::regclass
           AND confdeltype <> 'c';   -> 63

    So `DELETE FROM app.projects` could only ever succeed on a project with no
    content, and it broke the moment one of these tests gave a project a
    capability row. Every call site but one pairs this with `_drop_org`, and
    `purge_org_tree` already erases projects with the rest of the tenant tree --
    which makes the call redundant as well as refused. It is skipped there, and
    the org purge does the work.

    The one project created with `org_id=None` has no tree above it to erase it,
    so it is deleted here -- the only case a bare DELETE is the right statement.
    """
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (proj_id,))
            row = cur.fetchone()
            if row is None:
                return
            if row[0] is not None:
                return  # the org purge owns it
            # AI-291: le graphe prend le relais si une table gouvernee
            # ajoutee depuis retient le projet en ON DELETE RESTRICT.
            purge_fixture_project(cur.connection, proj_id)
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
def test_zero_members_opens_nothing(monkeypatch):
    """An organization with no member is NOT open. Renamed and inverted.

    This asserted `resolve_org_role(...) == "owner"` for any passer-by on a
    memberless org -- the *default-open-until-enrolled* rule of Epic 21. Story
    46.4 removed it, and this file's own offline layer already says so in
    `test_production_never_opens_an_unclaimed_organization`. Two tests in one
    module asserted opposite contracts, and only the live one could fail, so the
    stale one read as fixture noise on a real database.
    """
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
            # ZERO members -> CLOSED. Nothing about emptiness grants authority.
            assert resolve_org_role(org, "someone@x", conn) is None
            assert identity_can_manage_org(org, "someone@x", conn) is False
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
def test_anonymous_is_owner_of_nothing(monkeypatch):
    """`anonymous` gets no role, in every auth mode. Renamed and inverted.

    The disabled-auth short-circuit that made `anonymous` an owner was removed by
    Story 46.4 -- see `test_anonymous_gets_no_short_circuit_and_no_role` in this
    same file, which has asserted the opposite of this test since.
    """
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
            # No short-circuit left: `anonymous` holds no active membership row,
            # so it resolves to nothing here exactly as it does in oauth.
            assert resolve_org_role(org, "anonymous", conn) is None
    finally:
        _drop_org(org)


@pg_available
def test_project_access_needs_a_grant_not_a_membership(monkeypatch):
    """Renamed and half-inverted: org membership alone no longer opens a Project.

    The first half asserted *default-open within the org* -- a member with no
    grant sees every Project of its organization. `identity_can_access_project_in_org`
    is now a compatibility name over `identity_can_read_project`, the STRICT
    decision, so that half is stale for the same reason the org-level default-open
    is. The second half -- a grant admits its Project and only its Project -- was
    always the target and is kept verbatim.
    """
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
            # NO grants -> nothing is visible. Being a member of the organization
            # is not, by itself, permission to read one of its Projects.
            assert identity_can_access_project_in_org(p1, x, conn) is False
            assert identity_can_access_project_in_org(p2, x, conn) is False
            # The grant admits its Project, and only its Project.
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
def test_a_project_without_an_organization_can_no_longer_exist(monkeypatch):
    """The legacy no-org Project is gone from the SCHEMA, not merely from policy.

    This test used to create a project with `org_id=NULL` -- the pre-Epic-21
    "legacy" shape -- and assert that Story 7.4 default-open let anyone reach it.
    Both halves are obsolete, and the first one is why: `app.projects.org_id` is
    NOT NULL, so the row the test needs cannot be inserted at all. It failed with
    a `NotNullViolation` that named the column and was read as fixture noise.

    Kept rather than deleted, and inverted rather than weakened: an untenanted
    Project is the one shape that would sit outside every org-scoped guard in this
    file, so the fact that the database refuses it is worth a test of its own.
    """
    import psycopg
    from core.db import get_connection

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    suffix = uuid.uuid4().hex[:8]
    proj = f"proj_legacy_{suffix}"
    with get_connection() as conn:
        with pytest.raises(psycopg.errors.NotNullViolation):
            with conn.cursor() as cur:
                _mk_project(cur, proj, None, suffix)
        conn.rollback()


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


async def _new_org(name: str, slug: str, owner: str) -> str:
    """Seed an enrolled organization for a test that is about ENFORCEMENT.

    These tests used to build their org by calling ``_create_org`` and reading
    ``json.loads(resp.body)["id"]`` with no look at the status. Two things were
    wrong with that, and the second hid the first for months.

    **The status was never checked**, so any refusal surfaced as
    ``KeyError: 'id'`` -- a message naming neither code nor reason. Twenty-eight
    failures arrived as twenty-eight identical mysteries, and the answer had been
    sitting in the response body the whole time.

    **And the refusal was correct.** ``_create_org`` refuses whenever
    ``TOOROW_AUTH_MODE != "disabled"`` (409 ``entry_scope_required``,
    ``server/core/organizations_api.py#_create_org``): creating the first
    organization went to the hosted
    ENTRY scope command. Every test here calls
    ``monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")`` **on purpose**, because
    oauth is the mode whose enforcement they exist to test. So they were asking
    the product to do something it is designed to refuse in exactly the mode they
    had selected. The tests were stale, not the guard.

    None of them is about *how* an organization comes into being -- they are about
    what happens to roles, members and grants once one exists. So the org is
    seeded directly, the way this file's own live-Postgres layer already does
    (``_mk_org`` / ``_mk_member``), and the creator is enrolled as the active
    owner because that is the state ``_create_org`` used to leave behind.

    The one test that IS about creation asserts the current contract instead --
    see ``test_direct_org_creation_is_refused_outside_disabled_auth``.
    """
    from core.db import get_connection  # noqa: PLC0415

    org_id = f"org_{slug.replace('-', '_')}"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, created_by) "
                "VALUES (%s, %s, %s, %s)",
                (org_id, name, slug, owner),
            )
            cur.execute(
                "INSERT INTO app.org_members (id, org_id, identity, role, status, joined_at) "
                "VALUES (%s, %s, %s, 'owner', 'active', NOW())",
                (f"omem_{uuid.uuid4().hex[:12]}", org_id, owner),
            )
        conn.commit()
    return org_id


@pg_available
@pytest.mark.anyio
async def test_direct_org_creation_is_refused_outside_disabled_auth(monkeypatch):
    """`POST /api/organizations` is NOT how an organization comes into being.

    This test used to assert 201 on that route and then read its `id`. It was
    written before `server/core/organizations_api.py#_create_org`, which refuses
    direct creation whenever
    `TOOROW_AUTH_MODE != "disabled"` and points at the hosted ENTRY scope
    command instead. Since every test in this section selects `oauth` on
    purpose -- oauth being the mode whose enforcement they exist to test -- they
    were all asking the product to do the one thing it is designed to refuse
    there, and reading `["id"]` off the refusal.

    So the assertion is inverted rather than deleted: the refusal IS the
    contract, and a test that stops naming it would let the guard disappear
    unnoticed. Where the organization actually comes from is
    `_create_hosted_entry_scope` (`POST /api/entry/scope`).
    """
    from core.organizations_api import _create_org  # noqa: PLC0415

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    slug = f"ae-{uuid.uuid4().hex[:8]}"
    # `_check_auth` must be patched even here: in oauth mode the real one demands
    # TOOROW_JWT_PUBLIC_KEY or TOOROW_JWKS_URI and raises before the guard runs.
    with patch(_AUTH, return_value=(True, "owner@x")):
        r = await _create_org(_post_request({"name": "AE", "slug": slug}))
    assert r.status_code == 409
    body = json.loads(r.body)
    assert body["code"] == "entry_scope_required"
    assert "ENTRY scope" in body["message"]


@pg_available
@pytest.mark.anyio
async def test_enrolled_owner_can_manage_and_a_stranger_cannot(monkeypatch):
    """The enrolled owner CAN patch; a stranger is refused 403 (the org is CLOSED)."""
    from core.organizations_api import _patch_org  # noqa: PLC0415

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    slug = f"ae-{uuid.uuid4().hex[:8]}"
    oid = None
    try:
        with patch(_AUTH, return_value=(True, "owner@x")):
            oid = await _new_org("AE", slug, "owner@x")
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
async def test_the_enrolment_door_is_closed_to_everyone(monkeypatch):
    """A STRANGER cannot add a member -- and neither can the owner.

    This test asked for `403 forbidden` on a non-manager. The answer is `409
    invitation_required`, and it is not a regression: direct enrolment was
    withdrawn on 2026-08-24 (`org_members_api._add_org_member`, whose docstring
    carries the reason -- a caller-supplied string cannot be a canonical
    `person_<ULID>` minted from a verified pair, so the route could only insert a
    key that authorizes nobody or collides with a real person).

    Asserting the refusal for the OWNER as well as the stranger is what makes
    this stronger than what it replaces: "a non-manager is refused" is implied by
    "everyone is refused", and a future reopening of the route for managers only
    -- the shape that would bring the defect back -- turns this red.
    """
    from core.db import get_connection
    from core.org_members_api import _add_org_member  # noqa: PLC0415

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    suffix = uuid.uuid4().hex[:8]
    org = f"org_am_{suffix}"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _mk_org(cur, org, suffix)
                _mk_member(cur, org, "real@acme", "owner", suffix)
            conn.commit()
        for caller in ("stranger@x", "real@acme"):
            with patch(_AUTH, return_value=(True, caller)):
                resp = await _add_org_member(
                    _member_request(org, {"identity": "new@acme", "role": "member"})
                )
            assert resp.status_code == 409, caller
            body = json.loads(resp.body)
            assert body["code"] == "invitation_required", caller
            # The refusal names the gesture that replaces it.
            assert "invitation" in body["message"].lower(), caller

        # And it enrolled nobody, which is the half a status code cannot say.
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM app.org_members WHERE org_id = %s", (org,)
                )
                assert cur.fetchone()[0] == 1
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
    from core.organizations_api import _get_org  # noqa: PLC0415

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    slug = f"rs-{uuid.uuid4().hex[:8]}"
    oid = None
    try:
        with patch(_AUTH, return_value=(True, "owner@rs")):
            oid = await _new_org("RS", slug, "owner@rs")
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
    from core.organizations_api import _list_orgs  # noqa: PLC0415

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    slug = f"ls-{uuid.uuid4().hex[:8]}"
    oid = None
    try:
        with patch(_AUTH, return_value=(True, "owner@ls")):
            oid = await _new_org("LS", slug, "owner@ls")
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
    from core.org_members_api import _remove_org_member  # noqa: PLC0415

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    slug = f"lo-{uuid.uuid4().hex[:8]}"
    oid = None
    try:
        with patch(_AUTH, return_value=(True, "mgr@x")):
            oid = await _new_org("LO", slug, "mgr@x")
            # mgr@x is the sole auto-enrolled owner -> cannot remove itself.
            resp = await _remove_org_member(_member_mgmt_request(oid, "mgr@x"))
        assert resp.status_code == 409
    finally:
        if oid:
            _drop_org(oid)


@pg_available
@pytest.mark.anyio
async def test_remove_one_of_two_owners_ok(monkeypatch):
    from core.org_members_api import _remove_org_member  # noqa: PLC0415

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    slug = f"two-{uuid.uuid4().hex[:8]}"
    oid = None
    try:
        with patch(_AUTH, return_value=(True, "mgr@x")):
            oid = await _new_org("TWO", slug, "mgr@x")
            _enrol(oid, "o2@x", "owner")  # the invitation path's residue, seeded
            resp = await _remove_org_member(_member_mgmt_request(oid, "o2@x"))
        assert resp.status_code == 200
    finally:
        if oid:
            _drop_org(oid)


@pg_available
@pytest.mark.anyio
async def test_downgrade_sole_owner_refused_409(monkeypatch):
    from core.org_members_api import _update_org_member  # noqa: PLC0415

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    slug = f"dg-{uuid.uuid4().hex[:8]}"
    oid = None
    try:
        with patch(_AUTH, return_value=(True, "mgr@x")):
            oid = await _new_org("DG", slug, "mgr@x")
            resp = await _update_org_member(
                _member_mgmt_request(oid, "mgr@x", {"role": "member"})
            )
        assert resp.status_code == 409
    finally:
        if oid:
            _drop_org(oid)


@pg_available
@pytest.mark.anyio
async def test_demote_last_owner_with_admin_remaining_refused_409(monkeypatch):
    """AI-348, ratified 2026-09-02 (organization-settings.md).

    The manager floor is satisfied -- an admin remains -- and the demotion is
    still a dead end: FIX 3 forbids that admin from ever assigning `owner`
    again. The refusal names the transfer gesture instead of letting the 200
    through.
    """
    from core.org_members_api import _update_org_member  # noqa: PLC0415

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    slug = f"lot-{uuid.uuid4().hex[:8]}"
    oid = None
    try:
        with patch(_AUTH, return_value=(True, "mgr@x")):
            oid = await _new_org("LOT", slug, "mgr@x")
            _enrol(oid, "adm@x", "admin")
            resp = await _update_org_member(
                _member_mgmt_request(oid, "mgr@x", {"role": "admin"})
            )
        assert resp.status_code == 409
        assert json.loads(resp.body)["code"] == "last_owner_transfer_required"
    finally:
        if oid:
            _drop_org(oid)


@pg_available
@pytest.mark.anyio
async def test_remove_last_owner_with_admin_remaining_refused_409(monkeypatch):
    """The remove door composes into the same dead end and gets the same 409."""
    from core.org_members_api import _remove_org_member  # noqa: PLC0415

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    slug = f"rot-{uuid.uuid4().hex[:8]}"
    oid = None
    try:
        with patch(_AUTH, return_value=(True, "mgr@x")):
            oid = await _new_org("ROT", slug, "mgr@x")
            _enrol(oid, "adm@x", "admin")
            resp = await _remove_org_member(_member_mgmt_request(oid, "mgr@x"))
        assert resp.status_code == 409
        assert json.loads(resp.body)["code"] == "last_owner_transfer_required"
    finally:
        if oid:
            _drop_org(oid)


@pg_available
@pytest.mark.anyio
async def test_demote_an_owner_when_a_second_owner_exists_ok(monkeypatch):
    """With ownership transferred -- a second active owner -- the demotion is
    ordinary. This is exactly the gesture the 409 above names."""
    from core.org_members_api import _update_org_member  # noqa: PLC0415

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    slug = f"ok2-{uuid.uuid4().hex[:8]}"
    oid = None
    try:
        with patch(_AUTH, return_value=(True, "mgr@x")):
            oid = await _new_org("OK2", slug, "mgr@x")
            _enrol(oid, "o2@x", "owner")
            resp = await _update_org_member(
                _member_mgmt_request(oid, "mgr@x", {"role": "admin"})
            )
        assert resp.status_code == 200
        assert json.loads(resp.body)["role"] == "admin"
    finally:
        if oid:
            _drop_org(oid)


@pg_available
@pytest.mark.anyio
async def test_remove_nonmember_404(monkeypatch):
    from core.org_members_api import _remove_org_member  # noqa: PLC0415

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    slug = f"nm-{uuid.uuid4().hex[:8]}"
    oid = None
    try:
        with patch(_AUTH, return_value=(True, "mgr@x")):
            oid = await _new_org("NM", slug, "mgr@x")
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
    A second member, seeded the way an accepted invitation leaves one, also appears.
    """
    from core.org_members_api import _list_org_members  # noqa: PLC0415

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    slug = f"lm-{uuid.uuid4().hex[:8]}"
    oid = None
    try:
        with patch(_AUTH, return_value=(True, "owner@lm")):
            oid = await _new_org("LM", slug, "owner@lm")
            _enrol(oid, "extra@lm", "member")
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
    from core.org_members_api import _list_org_members  # noqa: PLC0415

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    slug = f"lm2-{uuid.uuid4().hex[:8]}"
    oid = None
    try:
        with patch(_AUTH, return_value=(True, "owner@lm2")):
            oid = await _new_org("LM2", slug, "owner@lm2")
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
    from core.org_members_api import (  # noqa: PLC0415
        _list_org_members,
        _update_org_member,
    )

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    slug = f"lm3-{uuid.uuid4().hex[:8]}"
    oid = None
    try:
        with patch(_AUTH, return_value=(True, "owner@lm3")):
            oid = await _new_org("LM3", slug, "owner@lm3")
            # A second member, so suspending it does not trip the last-owner
            # guard on the creator.
            _enrol(oid, "extra@lm3", "member")
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
        "(id, provider, nango_connection_id, project_id, owner_org_id, owner_identity) "
        "VALUES (%s, %s, %s, %s, %s, 'tester@example.com')",
        (cred_id, "google-analytics", f"nango-{cred_id}", proj_id, owner_org_id),
    )


def _drop_credential(cred_id: str) -> None:
    """Delete a connection_ref row (cascades credential_accounts + grants)."""
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.connection_ref WHERE id = %s", (cred_id,))
        conn.commit()


def _cred_post_req(credential_id: str, body: dict, **extra_params) -> MagicMock:
    """A credential POST with REAL headers.

    A bare `MagicMock()` answers `request.headers.get("Idempotency-Key")` with
    another MagicMock -- truthy, and `.strip()` on it returns a MagicMock too. So
    the guard that refuses a request without the header sees one, and the mock
    travels on into the durable-operation layer, where it fails as a 500. The
    test then reads "the route is broken" from a request no client would send.

    `_create_account_grant` is one of the 23 routes that REFUSE a missing
    `Idempotency-Key` (422 `missing_idempotency_key`) -- the same header whose
    omission made the console's Expose button answer 422 on every click (AI-188).
    These tests predate the guard; they now send what a client sends.
    """
    req = MagicMock()
    req.path_params = {"credential_id": credential_id, **extra_params}
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    req.headers = {"Idempotency-Key": f"test-{uuid.uuid4().hex[:12]}"}
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
    from core.db import get_connection
    from core.org_members_api import _remove_org_member  # noqa: PLC0415

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
    from core.db import get_connection
    from core.org_members_api import _update_org_member  # noqa: PLC0415

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
    from core.db import get_connection
    from core.org_members_api import _update_org_member  # noqa: PLC0415

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
    from core.db import get_connection
    from core.org_members_api import _remove_org_member  # noqa: PLC0415

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
        # Re-stated 2026-09-02: the sole ACTIVE owner is the last owner, so the
        # AI-348 refusal (organization-settings.md amendment) answers first and
        # names the transfer gesture -- the floor guard behind it still holds.
        assert json.loads(resp.body)["code"] == "last_owner_transfer_required"
    finally:
        _drop_org(org)


# ---------------------------------------------------------------------------
# FIX 3: role-hierarchy enforcement (admin cannot assign owner).
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_admin_cannot_add_member_with_role_owner_403(monkeypatch):
    """FIX 3(a): an ADMIN actor adding a new member with role='owner' -> 403."""
    from core.db import get_connection
    from core.org_members_api import _add_org_member  # noqa: PLC0415

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
        # Admin actor tries to add a new member with role='owner'. It is refused,
        # and since 2026-08-24 the refusal is the CLOSED DOOR rather than the role
        # hierarchy: `_add_org_member` enrolls nobody at all. The rule this test
        # names is not weakened -- an admin still cannot make an owner -- and the
        # half that matters, that no `owner` row appears, is now asserted instead
        # of inferred from a status code.
        with patch(_AUTH, return_value=(True, admin_id)):
            resp = await _add_org_member(
                _member_request(org, {"identity": "new@fix3", "role": "owner"})
            )
        assert resp.status_code == 409
        assert json.loads(resp.body)["code"] == "invitation_required"
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM app.org_members WHERE org_id = %s AND role = 'owner'",
                    (org,),
                )
                assert cur.fetchone()[0] == 1
    finally:
        _drop_org(org)


@pg_available
@pytest.mark.anyio
async def test_admin_cannot_patch_member_role_to_owner_403(monkeypatch):
    """FIX 3(b): an ADMIN actor PATCHing another member's role to 'owner' -> 403."""
    from core.db import get_connection
    from core.org_members_api import _update_org_member  # noqa: PLC0415

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
    from core.db import get_connection
    from core.org_members_api import _update_org_member  # noqa: PLC0415

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
    """FIX 3 positive control, ON THE DOOR THAT STILL OPENS.

    It used to be `_add_org_member` with role='owner' -> 201. That door answers
    409 to everyone since 2026-08-24, so the control proved nothing about the
    role hierarchy any more -- it only proved the door was shut, which the
    negative cases above already say. The hierarchy lives on `_update_org_member`
    now: `test_admin_cannot_patch_member_role_to_owner_403` is its negative half,
    and this is the positive one it was missing -- an OWNER really can promote an
    existing member. Without it, a rule that refused EVERYONE would read green.
    """
    from core.db import get_connection
    from core.org_members_api import _update_org_member  # noqa: PLC0415

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    suffix = uuid.uuid4().hex[:8]
    org = f"org_fix3_ok_{suffix}"
    owner_id = "owner@fix3ok"
    member_id = "member@fix3ok"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _mk_org(cur, org, suffix)
                _mk_member_status(cur, org, owner_id, "owner", "active")
                _mk_member_status(cur, org, member_id, "member", "active")
            conn.commit()
        with patch(_AUTH, return_value=(True, owner_id)):
            resp = await _update_org_member(
                _member_mgmt_request(org, member_id, {"role": "owner"})
            )
        assert resp.status_code == 200, resp.body
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT role FROM app.org_members WHERE org_id = %s AND identity = %s",
                    (org, member_id),
                )
                assert cur.fetchone()[0] == "owner"
    finally:
        _drop_org(org)


# ---------------------------------------------------------------------------
# FIX 4: NULL-owner credential -> non-disclosing 404 on grant and revoke.
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_create_grant_refuses_a_non_manager_of_the_owner_org_403(monkeypatch):
    """FIX 4: POST grant on a credential with owner_org_id IS NULL -> 404 (not 403).

    The handler emits a non-disclosing 404 so callers cannot distinguish a
    missing credential from a credential whose owner org is un-backfilled.

    A CREDENTIAL WITHOUT AN OWNER ORGANIZATION CANNOT EXIST. `connection_ref.
    owner_org_id` is NOT NULL and carries `fk_connection_ref_owner_org` to
    `app.organizations` (verified on the live schema, 2026-08-04), so the "legacy
    NULL owner" this test was written for was abolished by the schema.

    The seed was already patched once, to `owner_org_id = 'org_test_fixture'` --
    a row that DOES exist. That silently changed the question: it stopped asking
    "what happens when the owner is unknown" and started asking "what happens
    when the owner is known and I do not manage it". The answer to the second is
    **403**, and it is correct; only the name and the assertion still carried the
    first. A test whose premise is edited out from under its name is worse than a
    red one, because it goes green while proving something else.

    Renamed and inverted to what it now measures.
    """
    from core.credential_accounts_api import _create_account_grant  # noqa: PLC0415
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
                    "(id, provider, nango_connection_id, project_id, owner_org_id, owner_identity) "
                    "VALUES (%s, %s, %s, %s, 'org_test_fixture', 'tester@example.com')",
                    (cred, "google-analytics", f"nango-{suffix}", proj),
                )
                # Register the account via direct SQL so the FK exists and the
                # handler reaches the owner_org_id check (not the account-missing 404).
                # `source_account_id` is NOT NULL since migration 133:14. This
                # fixture predates it and inserted two columns, so the row it was
                # meant to plant never existed and the handler was judged on a
                # path it never took.
                cur.execute(
                    "INSERT INTO app.credential_accounts "
                    "(source_account_id, credential_id, external_account_id) "
                    "VALUES (%s, %s, %s)",
                    (f"sacct_{uuid.uuid4().hex[:20]}", cred, "acct_null"),
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
        assert resp.status_code == 403
        assert json.loads(resp.body)["code"] == "forbidden"
    finally:
        _drop_credential(cred)
        _drop_project(proj)
        _drop_org(org)


@pg_available
@pytest.mark.anyio
async def test_revoke_grant_refuses_a_non_manager_of_the_owner_org_403(monkeypatch):
    """FIX 4: DELETE revoke on a credential with owner_org_id IS NULL -> 404.
    A CREDENTIAL WITHOUT AN OWNER ORGANIZATION CANNOT EXIST. `connection_ref.
    owner_org_id` is NOT NULL and carries `fk_connection_ref_owner_org` to
    `app.organizations` (verified on the live schema, 2026-08-04), so the "legacy
    NULL owner" this test was written for was abolished by the schema.

    The seed was already patched once, to `owner_org_id = 'org_test_fixture'` --
    a row that DOES exist. That silently changed the question: it stopped asking
    "what happens when the owner is unknown" and started asking "what happens
    when the owner is known and I do not manage it". The answer to the second is
    **403**, and it is correct; only the name and the assertion still carried the
    first. A test whose premise is edited out from under its name is worse than a
    red one, because it goes green while proving something else.

    Renamed and inverted to what it now measures.
    """
    from core.credential_accounts_api import _revoke_account_grant  # noqa: PLC0415
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
                    "(id, provider, nango_connection_id, project_id, owner_org_id, owner_identity) "
                    "VALUES (%s, %s, %s, %s, 'org_test_fixture', 'tester@example.com')",
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
        assert resp.status_code == 403
        assert json.loads(resp.body)["code"] == "forbidden"
    finally:
        _drop_credential(cred)
        _drop_project(proj)
        _drop_org(org)


@pg_available
@pytest.mark.anyio
async def test_credential_with_owner_org_create_grant_positive_control(monkeypatch):
    """FIX 4 positive control: a credential WITH a valid owner_org_id lets an
    owner/admin of that org successfully create a grant (201)."""
    from core.credential_accounts_api import (  # noqa: PLC0415
        _create_account_grant,
        _register_credential_account,
    )
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
            req = _cred_post_req(cred, {"grantee_org_id": org_b}, external_account_id="acct_ok")
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
    from core.credential_accounts_api import _register_credential_account  # noqa: PLC0415
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
async def test_register_account_refuses_a_non_manager_of_the_owner_org_403(monkeypatch):
    """FIX 5: a credential with owner_org_id IS NULL -> non-disclosing 404.
    A CREDENTIAL WITHOUT AN OWNER ORGANIZATION CANNOT EXIST. `connection_ref.
    owner_org_id` is NOT NULL and carries `fk_connection_ref_owner_org` to
    `app.organizations` (verified on the live schema, 2026-08-04), so the "legacy
    NULL owner" this test was written for was abolished by the schema.

    The seed was already patched once, to `owner_org_id = 'org_test_fixture'` --
    a row that DOES exist. That silently changed the question: it stopped asking
    "what happens when the owner is unknown" and started asking "what happens
    when the owner is known and I do not manage it". The answer to the second is
    **403**, and it is correct; only the name and the assertion still carried the
    first. A test whose premise is edited out from under its name is worse than a
    red one, because it goes green while proving something else.

    Renamed and inverted to what it now measures.
    """
    from core.credential_accounts_api import _register_credential_account  # noqa: PLC0415
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
                    "(id, provider, nango_connection_id, project_id, owner_org_id, owner_identity) "
                    "VALUES (%s, %s, %s, %s, 'org_test_fixture', 'tester@example.com')",
                    (cred, "google-analytics", f"nango-{suffix}", proj),
                )
            conn.commit()
        with patch(_AUTH, return_value=(True, "anyone@fix5no")):
            resp = await _register_credential_account(
                _cred_post_req(cred, {"external_account_id": "acct_no"})
            )
        assert resp.status_code == 403
        assert json.loads(resp.body)["code"] == "forbidden"
    finally:
        _drop_credential(cred)
        _drop_project(proj)
        _drop_org(org)


@pg_available
@pytest.mark.anyio
async def test_register_credential_account_owner_org_manager_succeeds(monkeypatch):
    """FIX 5 positive control: an owner/admin of the credential's owner org -> 201."""
    from core.credential_accounts_api import _register_credential_account  # noqa: PLC0415
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
