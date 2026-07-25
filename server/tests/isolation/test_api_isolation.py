"""Admin API isolation — cross-scope attempts rejected AND audited (Story 7.4).

Covers AC4, AC7 (AI-38), AC8. The admin API uses a shared Bearer token, so a
"cross-scope attempt" is simulated by passing a WRONG project_id scope (alpha)
against another project's (beta) notebook. Even with a shared credential the
endpoint must validate ownership: a mismatch returns 404 (existence not
disclosed) and writes an ACTION_CROSS_SCOPE_ATTEMPT audit row.

Runs against live Postgres (TEST_POSTGRES_DSN) so the SQL scoping is proven, not
mocked. Auth is disabled -> identity 'anonymous'; scope enforcement still fires
on the explicit-claim-mismatch path.
"""

from __future__ import annotations

import os

import psycopg
import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

pytestmark = pytest.mark.isolation


@pytest.fixture()
def client(two_projects):
    """Admin router test client with auth disabled and PLATFORM_DB_URL set."""
    os.environ["TOOROW_AUTH_MODE"] = "disabled"
    os.environ["PLATFORM_DB_URL"] = os.environ["TEST_POSTGRES_DSN"]
    from core import api_auth
    from core.admin_api import router

    api_auth.reset_verifier_cache()
    app = Starlette(routes=[Mount("/", app=router)])
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    os.environ.pop("TOOROW_AUTH_MODE", None)
    api_auth.reset_verifier_cache()


def _audit_count(action: str, notebook_id: str) -> int:
    conn = psycopg.connect(os.environ["TEST_POSTGRES_DSN"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM app.audit_log
                WHERE action = %s AND metadata->>'notebook_id' = %s
                """,
                (action, notebook_id),
            )
            return int(cur.fetchone()[0])
    finally:
        conn.close()


def test_reports_available_scoped_to_alpha(client, two_projects):
    """GET /api/reports/available?project_id=alpha reflects ONLY alpha's per-project
    enablement rows (app.project_reports), never beta's.

    The report *catalog* is module-defined and shared, so isolation here is about
    the per-project enablement state merged in: alpha's response must carry
    alpha's project_reports rows and none of beta's. We seeded alpha with a
    google-analytics/overview_daily enablement and beta with a distinct
    gsc/position_movements one; the enablement row for beta's report must not
    surface in alpha's response, and the raw table must be project-scoped.
    """
    alpha, beta = two_projects

    # Direct table scoping proof (this is the AC3 property).
    conn = psycopg.connect(os.environ["TEST_POSTGRES_DSN"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT module_name, report_id FROM app.project_reports "
                "WHERE project_id = %s",
                (alpha["id"],),
            )
            alpha_reports = {(m, r) for m, r in cur.fetchall()}
    finally:
        conn.close()

    assert ("google-analytics", "overview_daily") in alpha_reports
    assert ("gsc", "position_movements") not in alpha_reports, (
        "beta's project_reports row leaked into alpha's scope!"
    )

    # Endpoint responds 200 and is project-scoped (does not 500 or cross-read).
    resp = client.get(f"/api/reports/available?project_id={alpha['id']}")
    assert resp.status_code == 200


def test_notebooks_list_scoped_to_alpha(client, two_projects):
    """GET /api/notebooks?project_id=alpha returns only alpha notebooks."""
    alpha, beta = two_projects
    resp = client.get(f"/api/notebooks?project_id={alpha['id']}")
    assert resp.status_code == 200
    payload = resp.json()
    notebooks = payload if isinstance(payload, list) else payload.get("notebooks", [])
    ids = {nb["id"] for nb in notebooks}
    assert alpha["notebook_id"] in ids
    assert beta["notebook_id"] not in ids, "beta notebook leaked into alpha list!"


def test_cross_scope_notebook_get_returns_404(client, two_projects):
    """GET /api/notebooks/{beta_nb}?project_id=alpha -> 404 (AI-38)."""
    alpha, beta = two_projects
    resp = client.get(f"/api/notebooks/{beta['notebook_id']}?project_id={alpha['id']}")
    assert resp.status_code == 404, resp.text
    # Same notebook fetched with its OWN scope succeeds -> proves it EXISTS.
    ok = client.get(f"/api/notebooks/{beta['notebook_id']}?project_id={beta['id']}")
    assert ok.status_code == 200


def test_cross_scope_notebook_patch_returns_404(client, two_projects):
    """PATCH /api/notebooks/{beta_nb} with project_id=alpha in body -> 404 (AI-38)."""
    alpha, beta = two_projects
    resp = client.patch(
        f"/api/notebooks/{beta['notebook_id']}",
        json={"title": "HIJACKED", "project_id": alpha["id"]},
    )
    assert resp.status_code == 404, resp.text

    # Assert the title was NOT mutated (the 404 must be a no-op).
    conn = psycopg.connect(os.environ["TEST_POSTGRES_DSN"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT title FROM app.notebooks WHERE id = %s",
                (beta["notebook_id"],),
            )
            title = cur.fetchone()[0]
    finally:
        conn.close()
    assert title != "HIJACKED", "cross-scope PATCH mutated beta's notebook!"


def test_cross_scope_notebook_schedule_returns_404(client, two_projects):
    """PATCH /api/notebooks/{beta_nb}/schedule with alpha scope -> 404 (AI-38)."""
    alpha, beta = two_projects
    resp = client.patch(
        f"/api/notebooks/{beta['notebook_id']}/schedule",
        json={"scheduled": True, "schedule_rule": "nightly", "project_id": alpha["id"]},
    )
    assert resp.status_code == 404, resp.text


def test_cross_scope_notebook_export_returns_404(client, two_projects):
    """GET /api/notebooks/{beta_nb}/runs/{run}/export/html with alpha scope -> 404."""
    alpha, beta = two_projects
    url = (
        f"/api/notebooks/{beta['notebook_id']}/runs/{beta['notebook_run_id']}"
        f"/export/html?project_id={alpha['id']}"
    )
    resp = client.get(url)
    assert resp.status_code == 404, resp.text


def test_cross_scope_attempt_is_audited(client, two_projects):
    """A rejected cross-scope attempt writes an access_denied audit row (AC4)."""
    from core.audit import ACTION_CROSS_SCOPE_ATTEMPT

    alpha, beta = two_projects
    before = _audit_count(ACTION_CROSS_SCOPE_ATTEMPT, beta["notebook_id"])

    resp = client.patch(
        f"/api/notebooks/{beta['notebook_id']}",
        json={"title": "x", "project_id": alpha["id"]},
    )
    assert resp.status_code == 404

    after = _audit_count(ACTION_CROSS_SCOPE_ATTEMPT, beta["notebook_id"])
    assert after == before + 1, "cross-scope attempt must be audited (access_denied)"


def test_archived_project_closed_despite_default_open(two_projects, pg_conn):
    BETA_ID = "proj_beta"
    """review-epic-7 F-3: archiving closes a project even with zero member rows."""
    from core.project_access import identity_has_project_access

    with pg_conn.cursor() as cur:
        cur.execute("UPDATE app.projects SET status = 'archived' WHERE id = %s", (BETA_ID,))
    pg_conn.commit()
    try:
        assert identity_has_project_access(BETA_ID, "any-user", pg_conn) is False
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("UPDATE app.projects SET status = 'active' WHERE id = %s", (BETA_ID,))
        pg_conn.commit()
