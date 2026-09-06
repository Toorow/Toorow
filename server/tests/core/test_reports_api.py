"""Tests for report management endpoints (Story 6.1, AC9, T7.4).

Runs against the local platform Postgres (default DSN) so the merge, opt-in
default, and INSERT ... ON CONFLICT upsert are verified against the REAL
app.project_reports schema (AI-37: schema-constraint paths need a real DB, not a
mock cursor). Skips when Postgres is unreachable.

Covers:
  - test_available_reports_project_scoped: two projects -> GET returns only own rows.
  - test_available_reports_disabled_by_default: report not in project_reports -> enabled=false.
  - test_patch_enables_report: PATCH -> project_reports upserted; next GET shows enabled=true.
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

#: THE IDENTITY THE HANDLER IS GIVEN, and it is `anonymous` on purpose.
#:
#: These tests measure the MERGE -- catalogue x `app.project_reports` -- not the
#: access gate. `_list_available_reports` gained a tenant-scope gate (R43-FR09,
#: closed 2026-07-27 on a live leak), and `admin_api._strict_project_capability_
#: allowed` admits `anonymous` under `TOOROW_AUTH_MODE=disabled` by asking
#: `project_exists` alone. A named identity would be sent to the strict resolver
#: instead and refused for holding no grant on a project this file creates for
#: itself -- so the merge would never be reached and the 404 would be read as a
#: merge failure. `_seed_project` creating the row is what the gate then checks.
_IDENTITY = "anonymous"

_CATALOG = [
    {
        "module_name": "google-analytics",
        "report_id": "overview_daily",
        "display_name": "Vue d'ensemble quotidienne",
    },
    {
        "module_name": "meta-ads",
        "report_id": "campaign_overview",
        "display_name": "Performance campagnes",
    },
]


def _make_get_request(project_id: str) -> MagicMock:
    from starlette.datastructures import QueryParams

    req = MagicMock()
    req.query_params = QueryParams({"project_id": project_id})
    req.body = AsyncMock(return_value=b"")
    return req


def _make_patch_request(project_id, connector_name, report_id, body: dict) -> MagicMock:
    """The path param is `connector_name`. It was `module_name` until 7c3db1fe.

    `Module` left the product vocabulary ("Connector is the canonical noun"), and
    `_patch_report` reads `path_params["connector_name"]`. A request still
    carrying the retired key reaches the handler with an EMPTY connector name and
    is answered `400 missing_id` -- which reads as a malformed test rather than as
    a stale one, so the key is named here once and nowhere else.
    """
    req = MagicMock()
    req.path_params = {
        "project_id": project_id,
        "connector_name": connector_name,
        "report_id": report_id,
    }
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    return req


def _seed_project(project_id: str) -> None:
    """The Project, plus a live connection for each connector in `_CATALOG`.

    Story 7.1: `app.project_reports` has an FK to `app.projects`, so the parent
    row comes first. The connections are the second half, and they are not
    decoration: since the 2026-07-27 decision `_list_available_reports` serves
    only the connectors a Project is ACTUALLY connected to (`app.connection_ref`,
    `enabled`), because a brand-new Project was being offered the whole
    deployment's catalogue. A fixture with no connection is now correctly served
    an EMPTY list, which measures the filter and not the merge these tests are
    about.
    """
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.projects (id, name, slug, created_by, org_id)
                VALUES (%s, %s, %s, 'test', 'org_test_fixture')
                ON CONFLICT DO NOTHING
                """,
                (project_id, project_id, project_id),
            )
            for entry in _CATALOG:
                provider = entry["module_name"]
                cur.execute(
                    """
                    INSERT INTO app.connection_ref
                        (id, provider, nango_connection_id, project_id, status, enabled,
                         owner_org_id, owner_identity)
                    VALUES (%s, %s, %s, %s, 'active', TRUE, 'org_test_fixture',
                            'owner@example.com')
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        f"conn_{project_id}_{provider}",
                        provider,
                        f"nango_{project_id}_{provider}",
                        project_id,
                    ),
                )
        conn.commit()


def _cleanup(project_id: str) -> None:
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.project_reports WHERE project_id = %s", (project_id,))
            # Remove the parent project row too (created by _seed_project).
            # AI-291: le graphe prend le relais si une table gouvernee
            # ajoutee depuis retient le projet en ON DELETE RESTRICT.
            purge_fixture_project(cur.connection, project_id)
        conn.commit()


@pg_available
@pytest.mark.anyio
async def test_available_reports_disabled_by_default():
    from core.catalog_api import _list_available_reports  # noqa: PLC0415

    project_id = f"proj_test_{uuid.uuid4().hex[:8]}"
    _cleanup(project_id)
    _seed_project(project_id)
    try:
        req = _make_get_request(project_id)
        with (
            patch("core.admin_api._check_auth", return_value=(True, _IDENTITY)),
            patch("core.catalog_api._connector_report_catalog", return_value=_CATALOG),
        ):
            resp = await _list_available_reports(req)
        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert len(body) == 2
        assert all(r["enabled"] is False for r in body)  # opt-in default
    finally:
        _cleanup(project_id)


@pg_available
@pytest.mark.anyio
async def test_patch_enables_report():
    from core.catalog_api import (
        _list_available_reports,  # noqa: PLC0415
        _patch_report,  # noqa: PLC0415
    )

    project_id = f"proj_test_{uuid.uuid4().hex[:8]}"
    _cleanup(project_id)
    _seed_project(project_id)
    try:
        patch_req = _make_patch_request(
            project_id, "google-analytics", "overview_daily", {"enabled": True}
        )
        with patch("core.admin_api._check_auth", return_value=(True, _IDENTITY)):
            presp = await _patch_report(patch_req)
        assert presp.status_code == 200
        pbody = json.loads(presp.body)
        assert pbody["enabled"] is True

        # Next GET shows enabled=true for that report only.
        get_req = _make_get_request(project_id)
        with (
            patch("core.admin_api._check_auth", return_value=(True, _IDENTITY)),
            patch("core.catalog_api._connector_report_catalog", return_value=_CATALOG),
        ):
            gresp = await _list_available_reports(get_req)
        gbody = json.loads(gresp.body)
        enabled_map = {(r["module_name"], r["report_id"]): r["enabled"] for r in gbody}
        assert enabled_map[("google-analytics", "overview_daily")] is True
        assert enabled_map[("meta-ads", "campaign_overview")] is False
    finally:
        _cleanup(project_id)


@pg_available
@pytest.mark.anyio
async def test_available_reports_project_scoped():
    from core.catalog_api import (
        _list_available_reports,  # noqa: PLC0415
        _patch_report,  # noqa: PLC0415
    )

    proj_a = f"proj_a_{uuid.uuid4().hex[:8]}"
    proj_b = f"proj_b_{uuid.uuid4().hex[:8]}"
    _cleanup(proj_a)
    _cleanup(proj_b)
    _seed_project(proj_a)
    _seed_project(proj_b)
    try:
        # Enable a report for project A only.
        req_a = _make_patch_request(proj_a, "google-analytics", "overview_daily", {"enabled": True})
        with patch("core.admin_api._check_auth", return_value=(True, _IDENTITY)):
            await _patch_report(req_a)

        # Project B GET must NOT see A's enablement.
        get_b = _make_get_request(proj_b)
        with (
            patch("core.admin_api._check_auth", return_value=(True, _IDENTITY)),
            patch("core.catalog_api._connector_report_catalog", return_value=_CATALOG),
        ):
            resp_b = await _list_available_reports(get_b)
        body_b = json.loads(resp_b.body)
        assert all(r["enabled"] is False for r in body_b)
    finally:
        _cleanup(proj_a)
        _cleanup(proj_b)


@pg_available
@pytest.mark.anyio
async def test_available_reports_missing_project_id():
    from core.catalog_api import _list_available_reports  # noqa: PLC0415

    req = _make_get_request("")
    with patch("core.admin_api._check_auth", return_value=(True, _IDENTITY)):
        resp = await _list_available_reports(req)
    assert resp.status_code == 400
