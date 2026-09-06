"""toorow -- Route-handler tests for mediaplan_api.py (Story 22.1).

Same seam as the other admin-API handler tests: call the async handler directly
with a mock Request and patch core.admin_api._check_auth. Live-Postgres gated
(handlers hit the real DB via core.db.get_connection). Covers create/list/get/
versions/publish/diff, 422 validation, 403->404 cross-project (audited), deny
without membership (deny-by-default), and Viewer-cannot-write.
"""

from __future__ import annotations

import json
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import enrol_fixture_identity, purge_fixture_project

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

# Handlers reach the DB via core.db.get_connection (PLATFORM_DB_URL). Point it at
# the opt-in test DSN so the handler path uses the same database the probe checks.
if os.environ.get("TEST_POSTGRES_DSN") and not os.environ.get("PLATFORM_DB_URL"):
    os.environ["PLATFORM_DB_URL"] = os.environ["TEST_POSTGRES_DSN"]


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

_AUTH = ("core.admin_api._check_auth", (True, "tester@example.com"))


@pytest.fixture()
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _production_auth_mode(monkeypatch):
    """These routes take a PRODUCTION access decision, so the mode must allow one.

    `resolve_strict_resource_access` answers `production_identity_required` --
    denied -- whenever `TOOROW_AUTH_MODE` is `disabled`, which is the suite's
    default. Every route in this file then returned 404 for a reason that had
    nothing to do with what it was testing, and the file read as seven broken
    routes instead of one missing environment variable.

    Set once for the module rather than in seven signatures: the requirement
    belongs to the surface, not to each test.
    """
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")


def _req(*, path_params=None, body=None, query=None) -> MagicMock:
    req = MagicMock()
    req.path_params = path_params or {}
    req.query_params = query or {}
    req.json = AsyncMock(return_value=body if body is not None else {})
    return req


def _connect():
    import psycopg

    return psycopg.connect(os.environ["TEST_POSTGRES_DSN"])


# The identity _check_auth is patched to return in the happy-path tests.
_TESTER = "tester@example.com"


#: What a `project_members.role` meant, in the vocabulary that replaced it.
#: Migration 132 wrote this same mapping to migrate the live rows, so the
#: fixture and the migration agree by construction rather than by memory.
_ROLE_TO_CAPABILITY = {"owner": "manage", "member": "edit"}


def _seed_project(members=None) -> str:
    """Create a project and enrol membership rows.

    `app.project_members` IS GONE, dropped by migration 132, and this fixture
    kept writing to it -- so every test in this file raised `UndefinedTable` as
    soon as a Postgres DSN was present, and skipped silently without one.
    Measured 2026-08-16: 15 failed / 21 passed in this file alone.

    The vocabulary that replaced it is TWO rows, and 132 says which: membership
    of the ORG (`app.org_members`) plus a scoped grant on the project
    (`app.resource_grants`, `scope_type='project'`). `project_access` reads
    exactly those two, so a fixture writing them is enrolling the way the
    product does.

    A project with no grant denies a named subject -- there is no
    default-open-when-empty fallback -- so the happy-path tests MUST enrol the
    acting identity. When ``members`` is None we enrol the default tester as
    owner, which is `manage`.
    """
    if members is None:
        members = [(_TESTER, "owner")]
    project_id = f"proj-mpapi-{uuid.uuid4().hex[:8]}"
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.projects (id, name, slug, status, created_by, org_id)
                VALUES (%s, %s, %s, 'active', 'system', 'org_test_fixture')
                """,
                (project_id, "MP API", project_id),
            )
            for identity, role in members or []:
                enrol_fixture_identity(
                    cur,
                    identity,
                    project_id=project_id,
                    org_role="owner" if role == "owner" else "member",
                    capability=_ROLE_TO_CAPABILITY.get(role, "view"),
                )
        conn.commit()
    finally:
        conn.close()
    return project_id


def _drop_project(project_id: str) -> None:
    """Remove the Project and everything hanging off it.

    THIS USED TO DISABLE TRIGGERS BY HAND. `ALTER TABLE ... DISABLE TRIGGER USER`
    requires OWNING the table, and these fixtures connect as `connector`, which
    owns nothing -- so every teardown raised `InsufficientPrivilege` and left its
    rows behind. It also went one table at a time, which is the list that loses
    the race (AI-291).

    `purge_fixture_project` flags the transaction as an erasure, which is exactly
    what those immutability triggers yield to (migration 099, extended by 264).
    So the append-only children go without anyone disabling anything, and a
    governed table added tomorrow goes with them.
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            # The grants are the Project's; the org membership belongs to the
            # shared fixture org and is NOT this Project's to remove.
            cur.execute(
                "DELETE FROM app.resource_grants "
                "WHERE scope_type = 'project' AND scope_id = %s",
                (project_id,),
            )
        purge_fixture_project(conn, project_id)
        conn.commit()
    finally:
        conn.close()


_LINES = [
    {
        "line_key": "digital/meta",
        "label": "Meta",
        "channel": "Meta",
        "start_date": "2026-03-15",
        "end_date": "2026-04-28",
        "budget": "10000.00",
    }
]


async def _create_plan_via_api(project_id, identity="tester@example.com"):
    from core.mediaplan_api import _create_plan

    with patch(_AUTH[0], return_value=(True, identity)):
        resp = await _create_plan(
            _req(path_params={"project_id": project_id}, body={"name": "Plan"})
        )
    return resp


@pg_available
@pytest.mark.anyio
async def test_create_list_get_flow():
    from core.mediaplan_api import _get_plan, _list_plans

    project_id = _seed_project()
    try:
        resp = await _create_plan_via_api(project_id)
        assert resp.status_code == 201
        plan = json.loads(resp.body)
        assert plan["name"] == "Plan"

        with patch(_AUTH[0], return_value=_AUTH[1]):
            lresp = await _list_plans(_req(path_params={"project_id": project_id}))
        assert lresp.status_code == 200
        assert any(p["id"] == plan["id"] for p in json.loads(lresp.body)["plans"])

        with patch(_AUTH[0], return_value=_AUTH[1]):
            gresp = await _get_plan(_req(path_params={"plan_id": plan["id"]}))
        assert gresp.status_code == 200
        body = json.loads(gresp.body)
        assert body["active_version"] is None
        assert body["lines"] == []
    finally:
        _drop_project(project_id)


@pg_available
@pytest.mark.anyio
async def test_version_publish_diff_flow():
    from core.mediaplan_api import _create_version, _diff_versions, _publish_version

    project_id = _seed_project()
    try:
        plan = json.loads((await _create_plan_via_api(project_id)).body)

        with patch(_AUTH[0], return_value=_AUTH[1]):
            v1resp = await _create_version(
                _req(path_params={"plan_id": plan["id"]}, body={"lines": _LINES})
            )
        assert v1resp.status_code == 201
        v1 = json.loads(v1resp.body)
        assert v1["status"] == "candidate"

        with patch(_AUTH[0], return_value=_AUTH[1]):
            presp = await _publish_version(_req(path_params={"version_id": v1["id"]}))
        assert presp.status_code == 200
        assert json.loads(presp.body)["is_active"] is True

        lines_v2 = [dict(_LINES[0], budget="12000.00")]
        with patch(_AUTH[0], return_value=_AUTH[1]):
            v2resp = await _create_version(
                _req(path_params={"plan_id": plan["id"]}, body={"lines": lines_v2})
            )
        v2 = json.loads(v2resp.body)

        with patch(_AUTH[0], return_value=_AUTH[1]):
            dresp = await _diff_versions(
                _req(path_params={"plan_id": plan["id"]}, query={"from": v1["id"], "to": v2["id"]})
            )
        assert dresp.status_code == 200
        diff = json.loads(dresp.body)
        assert {c["line_key"] for c in diff["changed"]} == {"digital/meta"}
    finally:
        _drop_project(project_id)


@pg_available
@pytest.mark.anyio
async def test_create_version_validation_422():
    from core.mediaplan_api import _create_version

    project_id = _seed_project()
    try:
        plan = json.loads((await _create_plan_via_api(project_id)).body)
        bad = [dict(_LINES[0], start_date="2026-05-01", end_date="2026-04-01")]
        with patch(_AUTH[0], return_value=_AUTH[1]):
            resp = await _create_version(
                _req(path_params={"plan_id": plan["id"]}, body={"lines": bad})
            )
        assert resp.status_code == 422
    finally:
        _drop_project(project_id)


@pg_available
@pytest.mark.anyio
async def test_diff_requires_from_to():
    from core.mediaplan_api import _diff_versions

    project_id = _seed_project()
    try:
        plan = json.loads((await _create_plan_via_api(project_id)).body)
        with patch(_AUTH[0], return_value=_AUTH[1]):
            resp = await _diff_versions(
                _req(path_params={"plan_id": plan["id"]}, query={"from": ""})
            )
        assert resp.status_code == 422
    finally:
        _drop_project(project_id)


@pg_available
@pytest.mark.anyio
async def test_deny_without_membership_returns_404():
    """A closed project (has members) denies a non-member -> 404 (existence hidden)."""
    from core.mediaplan_api import _create_plan, _list_plans

    project_id = _seed_project(members=[("owner@example.com", "owner")])
    try:
        with patch(_AUTH[0], return_value=(True, "intruder@example.com")):
            cresp = await _create_plan(
                _req(path_params={"project_id": project_id}, body={"name": "X"})
            )
        assert cresp.status_code == 404
        with patch(_AUTH[0], return_value=(True, "intruder@example.com")):
            lresp = await _list_plans(_req(path_params={"project_id": project_id}))
        assert lresp.status_code == 404
    finally:
        _drop_project(project_id)


@pg_available
@pytest.mark.anyio
async def test_cross_project_get_audited_404():
    """A member of project B cannot GET project A's plan -> 404 + audit row."""
    from core.mediaplan_api import _get_plan

    proj_a = _seed_project(members=[("alice@example.com", "member")])
    proj_b = _seed_project(members=[("bob@example.com", "member")])
    try:
        plan_a = json.loads((await _create_plan_via_api(proj_a, "alice@example.com")).body)

        with patch(_AUTH[0], return_value=(True, "bob@example.com")):
            resp = await _get_plan(_req(path_params={"plan_id": plan_a["id"]}))
        assert resp.status_code == 404

        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM app.audit_log
                    WHERE action = 'access_denied' AND identity = 'bob@example.com'
                      AND metadata->>'resource_id' = %s
                    """,
                    (plan_a["id"],),
                )
                assert cur.fetchone()[0] >= 1
        finally:
            conn.close()
    finally:
        _drop_project(proj_a)
        _drop_project(proj_b)


@pg_available
@pytest.mark.anyio
async def test_viewer_cannot_write():
    """A Viewer of the project is denied plan creation (Member+ required)."""
    from core.mediaplan_api import _create_plan

    project_id = _seed_project(
        members=[("owner@example.com", "owner"), ("view@example.com", "viewer")]
    )
    try:
        with patch(_AUTH[0], return_value=(True, "view@example.com")):
            resp = await _create_plan(
                _req(path_params={"project_id": project_id}, body={"name": "Nope"})
            )
        assert resp.status_code == 404
    finally:
        _drop_project(project_id)


# ---------------------------------------------------------------------------
# The carrier Datastream on the REST surface -- ratified 2026-08-24.
#
# `analyze-and-test.md`, « the media plan is created and imported in the carrier
# Datastream's Workbench ». The console can only obey that if the creation route
# takes the carrier and the list route hands back its ADDRESS -- the second is
# what the Pacing empty state could not name for want of a decision.
# ---------------------------------------------------------------------------


def _seed_carrier_datastream(project_id: str, suffix: str) -> str:
    datastream_id = f"ds-mpapi-{suffix}-{uuid.uuid4().hex[:8]}"
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.datastreams
                    (id, project_id, name, source_kind, created_by, org_id)
                VALUES (%s, %s, %s, 'managed_feed', 'tester', 'org_test_fixture')
                """,
                (datastream_id, project_id, f"Plan file {suffix} {uuid.uuid4().hex[:6]}"),
            )
        conn.commit()
    finally:
        conn.close()
    return datastream_id


@pg_available
@pytest.mark.anyio
async def test_create_binds_the_plan_to_the_datastream_that_carries_it():
    from core.mediaplan_api import _create_plan

    project_id = _seed_project()
    try:
        carrier = _seed_carrier_datastream(project_id, "own")
        with patch(_AUTH[0], return_value=(True, _TESTER)):
            resp = await _create_plan(
                _req(
                    path_params={"project_id": project_id},
                    body={"name": "Q1 Brand", "carrier_datastream_id": carrier},
                )
            )
        assert resp.status_code == 201
        assert json.loads(resp.body)["carrier_datastream_id"] == carrier

        # THE SAME CARRIER TWICE IS A REFUSAL, AND A NAMED ONE: 409 with the
        # code the store mints, never an opaque 500 from a unique violation.
        with patch(_AUTH[0], return_value=(True, _TESTER)):
            again = await _create_plan(
                _req(
                    path_params={"project_id": project_id},
                    body={"name": "Q1 Retail", "carrier_datastream_id": carrier},
                )
            )
        assert again.status_code == 409
        assert json.loads(again.body)["code"] == "carrier_already_carries_a_plan"
    finally:
        _drop_project(project_id)


@pg_available
@pytest.mark.anyio
async def test_a_carrier_of_another_project_is_not_found_rather_than_a_constraint_error():
    from core.mediaplan_api import _create_plan

    mine = _seed_project()
    theirs = _seed_project()
    try:
        foreign = _seed_carrier_datastream(theirs, "foreign")
        with patch(_AUTH[0], return_value=(True, _TESTER)):
            resp = await _create_plan(
                _req(
                    path_params={"project_id": mine},
                    body={"name": "Q1 Brand", "carrier_datastream_id": foreign},
                )
            )
        # EXISTENCE IS NEVER DISCLOSED across a scope boundary (AD-5), and the
        # composite foreign key would have answered with a 500 nobody can read.
        assert resp.status_code == 404
    finally:
        _drop_project(mine)
        _drop_project(theirs)


@pg_available
@pytest.mark.anyio
async def test_the_plan_list_carries_the_address_of_the_gesture_that_fills_it():
    """The `carriers` key -- what the Pacing empty state opens.

    A Datastream with no plan-store template is NOT a carrier: the list is
    composed from the template's own `landing_target`, so a file source that
    lands warehouse rows must not appear as a door to a plan.
    """
    from core.mediaplan_api import _list_plans

    project_id = _seed_project()
    try:
        _seed_carrier_datastream(project_id, "warehouse")
        with patch(_AUTH[0], return_value=(True, _TESTER)):
            resp = await _list_plans(_req(path_params={"project_id": project_id}))
        body = json.loads(resp.body)
        assert body["plans"] == []
        # The key TRAVELS even when it is empty: a console that had to guess
        # whether the server knew about carriers would guess wrong once.
        assert body["carriers"] == []
    finally:
        _drop_project(project_id)
