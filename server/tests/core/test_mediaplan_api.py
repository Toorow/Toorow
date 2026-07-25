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


def _seed_project(members=None) -> str:
    """Create a project and enrol membership rows.

    resolve_project_role (project_access.py) has NO default-open-when-empty
    fallback for named identities: a project with zero members grants a named
    subject role None (denied). So the happy-path tests MUST enrol the acting
    identity. When ``members`` is None we enrol the default tester as owner.
    """
    if members is None:
        members = [(_TESTER, "owner")]
    project_id = f"proj-mpapi-{uuid.uuid4().hex[:8]}"
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.projects (id, name, slug, status, currency, timezone, created_by)
                VALUES (%s, %s, %s, 'active', 'EUR', 'Europe/Paris', 'system')
                """,
                (project_id, "MP API", project_id),
            )
            for identity, role in members or []:
                cur.execute(
                    """
                    INSERT INTO app.project_members (id, project_id, identity, role)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (f"pmem_{uuid.uuid4().hex}", project_id, identity, role),
                )
        conn.commit()
    finally:
        conn.close()
    return project_id


def _drop_project(project_id: str) -> None:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE app.plan_allocation_daily DISABLE TRIGGER USER")
            cur.execute("ALTER TABLE app.media_plan_versions DISABLE TRIGGER USER")
            try:
                cur.execute(
                    """
                    DELETE FROM app.plan_allocation_daily WHERE version_id IN (
                        SELECT v.id FROM app.media_plan_versions v
                        JOIN app.media_plans p ON p.id = v.plan_id WHERE p.project_id = %s
                    )
                    """,
                    (project_id,),
                )
                cur.execute(
                    """
                    DELETE FROM app.media_plan_lines WHERE version_id IN (
                        SELECT v.id FROM app.media_plan_versions v
                        JOIN app.media_plans p ON p.id = v.plan_id WHERE p.project_id = %s
                    )
                    """,
                    (project_id,),
                )
                cur.execute(
                    """
                    DELETE FROM app.media_plan_versions WHERE plan_id IN (
                        SELECT id FROM app.media_plans WHERE project_id = %s
                    )
                    """,
                    (project_id,),
                )
                cur.execute("DELETE FROM app.media_plans WHERE project_id = %s", (project_id,))
                cur.execute("DELETE FROM app.project_members WHERE project_id = %s", (project_id,))
                cur.execute("DELETE FROM app.projects WHERE id = %s", (project_id,))
            finally:
                cur.execute("ALTER TABLE app.plan_allocation_daily ENABLE TRIGGER USER")
                cur.execute("ALTER TABLE app.media_plan_versions ENABLE TRIGGER USER")
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
