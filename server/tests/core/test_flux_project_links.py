"""Tests for Story 21.4 -- flux org-scoped + linked to MANY projects (M:N).

Offline: input validation (handlers reject before touching the DB).
Live-Postgres (skipped when TEST_POSTGRES_DSN is unset): the M:N link + the org
invariant -- a flux only ever feeds projects of its OWN org -- proven both at the
app layer (409 code "cross_org") AND structurally (the composite FK to
projects(org_id, id) rejects a cross-org link even if app code is bypassed), on
the real schema (migration 038).
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

_AUTH = ("core.admin_api._check_auth", (True, "tester@example.com"))


def _post(flux_id: str, body: dict, **path) -> MagicMock:
    req = MagicMock()
    req.path_params = {"flux_id": flux_id, **path}
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    return req


def _get(flux_id: str, **path) -> MagicMock:
    req = MagicMock()
    req.path_params = {"flux_id": flux_id, **path}
    return req


# --- live fixture helpers (direct SQL; avoids the heavy create-project flow) ----


def _setup(suffix: str) -> dict:
    """Create org A + org B, projects PA1/PA2 (org A), PB1 (org B), and flux F (org A).

    F is a datastream in org A bound (compat) to PA1 but WITHOUT any project_flux
    row -- the tests create the links explicitly. datastreams NOT NULL columns are
    id, project_id, name, module_name (023_datastreams.sql) + org_id set to org A.
    Teardown via _teardown (FK-safe order).
    """
    from core.db import get_connection

    org_a = f"org_a_{suffix}"
    org_b = f"org_b_{suffix}"
    pa1 = f"proj_a1_{suffix}"
    pa2 = f"proj_a2_{suffix}"
    pb1 = f"proj_b1_{suffix}"
    flux = f"flux_{suffix}"
    with get_connection() as conn:
        with conn.cursor() as cur:
            for oid, nm in ((org_a, "OrgA"), (org_b, "OrgB")):
                cur.execute(
                    "INSERT INTO app.organizations (id, name, slug, created_by) "
                    "VALUES (%s, %s, %s, 'system')",
                    (oid, nm, f"{oid}"),
                )
            for pid, org in ((pa1, org_a), (pa2, org_a), (pb1, org_b)):
                cur.execute(
                    "INSERT INTO app.projects (id, name, slug, created_by, org_id) "
                    "VALUES (%s, %s, %s, 'system', %s)",
                    (pid, pid, f"{pid}", org),
                )
            cur.execute(
                "INSERT INTO app.datastreams "
                "(id, project_id, name, module_name, org_id) "
                "VALUES (%s, %s, %s, %s, %s)",
                (flux, pa1, f"F-{suffix}", "google-analytics", org_a),
            )
        conn.commit()
    return {
        "org_a": org_a,
        "org_b": org_b,
        "pa1": pa1,
        "pa2": pa2,
        "pb1": pb1,
        "flux": flux,
    }


def _teardown(ids: dict) -> None:
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            # datastreams / projects delete cascades their project_flux links.
            cur.execute("DELETE FROM app.datastreams WHERE id = %s", (ids["flux"],))
            cur.execute(
                "DELETE FROM app.projects WHERE id IN (%s, %s, %s)",
                (ids["pa1"], ids["pa2"], ids["pb1"]),
            )
            cur.execute(
                "DELETE FROM app.organizations WHERE id IN (%s, %s)",
                (ids["org_a"], ids["org_b"]),
            )
        conn.commit()


# ---------------------------------------------------------------------------
# Offline validation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_link_requires_project_id_422():
    from core.admin_api import _link_flux_to_project

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _link_flux_to_project(_post("flux_x", {}))
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_list_flux_projects_requires_auth_401():
    from core.admin_api import _list_flux_projects

    with patch(_AUTH[0], return_value=(False, "")):
        resp = await _list_flux_projects(_get("flux_x"))
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Live Postgres -- M:N links + org invariant
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_link_two_projects_same_org_and_list():
    from core.admin_api import _link_flux_to_project, _list_flux_projects

    suffix = uuid.uuid4().hex[:8]
    ids = _setup(suffix)
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            for pid in (ids["pa1"], ids["pa2"]):
                r = await _link_flux_to_project(
                    _post(ids["flux"], {"project_id": pid})
                )
                assert r.status_code == 201
            projects = json.loads(
                (await _list_flux_projects(_get(ids["flux"]))).body
            )["projects"]
        assert len(projects) == 2
        assert {p["project_id"] for p in projects} == {ids["pa1"], ids["pa2"]}
    finally:
        _teardown(ids)


@pg_available
@pytest.mark.anyio
async def test_link_cross_org_refused_409():
    from core.admin_api import _link_flux_to_project

    suffix = uuid.uuid4().hex[:8]
    ids = _setup(suffix)
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            resp = await _link_flux_to_project(
                _post(ids["flux"], {"project_id": ids["pb1"]})
            )
        assert resp.status_code == 409
        assert json.loads(resp.body)["code"] == "cross_org"
    finally:
        _teardown(ids)


@pg_available
@pytest.mark.anyio
async def test_link_nonexistent_flux_404():
    from core.admin_api import _link_flux_to_project

    suffix = uuid.uuid4().hex[:8]
    ids = _setup(suffix)
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            resp = await _link_flux_to_project(
                _post(f"flux_missing_{suffix}", {"project_id": ids["pa1"]})
            )
        assert resp.status_code == 404
    finally:
        _teardown(ids)


@pg_available
@pytest.mark.anyio
async def test_link_nonexistent_project_404():
    from core.admin_api import _link_flux_to_project

    suffix = uuid.uuid4().hex[:8]
    ids = _setup(suffix)
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            resp = await _link_flux_to_project(
                _post(ids["flux"], {"project_id": f"proj_missing_{suffix}"})
            )
        assert resp.status_code == 404
    finally:
        _teardown(ids)


@pg_available
@pytest.mark.anyio
async def test_duplicate_link_409():
    from core.admin_api import _link_flux_to_project

    suffix = uuid.uuid4().hex[:8]
    ids = _setup(suffix)
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            ok = await _link_flux_to_project(
                _post(ids["flux"], {"project_id": ids["pa1"]})
            )
            assert ok.status_code == 201
            dup = await _link_flux_to_project(
                _post(ids["flux"], {"project_id": ids["pa1"]})
            )
        assert dup.status_code == 409
    finally:
        _teardown(ids)


@pg_available
@pytest.mark.anyio
async def test_unlink_then_gone_and_second_unlink_404():
    from core.admin_api import (
        _link_flux_to_project,
        _list_flux_projects,
        _unlink_flux_from_project,
    )

    suffix = uuid.uuid4().hex[:8]
    ids = _setup(suffix)
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            await _link_flux_to_project(_post(ids["flux"], {"project_id": ids["pa1"]}))
            rv = await _unlink_flux_from_project(
                _get(ids["flux"], project_id=ids["pa1"])
            )
            assert rv.status_code == 200
            projects = json.loads(
                (await _list_flux_projects(_get(ids["flux"]))).body
            )["projects"]
            assert all(p["project_id"] != ids["pa1"] for p in projects)
            # Second unlink -> 404 (nothing left).
            rv2 = await _unlink_flux_from_project(
                _get(ids["flux"], project_id=ids["pa1"])
            )
            assert rv2.status_code == 404
    finally:
        _teardown(ids)


@pg_available
def test_project_flux_fk_rejects_cross_org_link():
    """AC3: the composite FK forbids a cross-org link even if app code is bypassed.

    A direct INSERT with org_id = org A but project_id = PB1 (org B) fails: the
    tuple (org_a, PB1) is NOT a row in projects(org_id, id), so the composite FK
    to projects rejects it. This proves project.org_id == flux.org_id is enforced
    at the DB, not only in the handler.
    """
    import psycopg
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    ids = _setup(suffix)
    try:
        with get_connection() as conn:
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO app.project_flux (project_id, flux_id, org_id) "
                        "VALUES (%s, %s, %s)",
                        (ids["pb1"], ids["flux"], ids["org_a"]),
                    )
            conn.rollback()
    finally:
        _teardown(ids)


@pg_available
def test_hard_delete_flux_sweeps_dangling_flux_grants():
    """Story 21.5 follow-up (review): hard-deleting a flux sweeps its
    scope_type='flux' resource_grants (polymorphic scope_id has NO FK), while a
    scope_type='project' grant is left intact -- closing the stale-grant lockout.
    """
    from core.datastreams import delete_datastream
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    ids = _setup(suffix)
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.resource_grants "
                    "(id, org_id, identity, scope_type, scope_id, granted_by) VALUES "
                    "(%s, %s, %s, 'flux', %s, 'system'), "
                    "(%s, %s, %s, 'project', %s, 'system')",
                    (
                        f"rgrant_f_{suffix}", ids["org_a"], "u@x", ids["flux"],
                        f"rgrant_p_{suffix}", ids["org_a"], "u@x", ids["pa1"],
                    ),
                )
            conn.commit()
            # No pull_jobs reference this flux -> hard-delete path -> sweep runs.
            assert delete_datastream(ids["flux"], ids["pa1"], conn, "tester") is True
            conn.commit()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM app.resource_grants WHERE id = %s",
                    (f"rgrant_f_{suffix}",),
                )
                assert cur.fetchone()[0] == 0  # flux grant swept
                cur.execute(
                    "SELECT count(*) FROM app.resource_grants WHERE id = %s",
                    (f"rgrant_p_{suffix}",),
                )
                assert cur.fetchone()[0] == 1  # project grant kept
    finally:
        _teardown(ids)
