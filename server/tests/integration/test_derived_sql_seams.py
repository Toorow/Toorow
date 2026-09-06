"""AI-45 -- build_asgi_app()/live_postgres seam tests for derived-SQL endpoint families.

Lesson (Epic 8 retro G-14/G-15): 37 single-day ledger tests all passed while the
real multi-day SQL was completely broken. Two root causes:
  (a) mock-only tests cannot catch SQL param-count mismatches;
  (b) single-day pull windows trivially satisfy both the correct OVERLAP condition
      AND the buggy CONTAINMENT condition, hiding the defect.

This file closes the seam for every derived-SQL endpoint family by exercising the
FULL ASGI stack (build_asgi_app() + TestClient + real Postgres) with MULTI-DAY,
MULTI-RANGE data -- exactly the shape that exposed the original failure.

Three endpoint families under test:
  (a) Extract LEDGER  GET /api/datastreams/{id}/ledger
        Seeded with contiguous pull windows, a gap, and an overlapping re-pull.
        Asserts on per-day statuses derived from the SQL, NOT just 200.
  (b) Report CHAIN    GET /api/reports/{module}/{report_id}/chain
        Seeded with target_fields + two datastreams with mappings to different metrics.
        Asserts ok_count, no_stream count, and per-metric status values.
  (c) DQ ISSUES       GET /api/dq/issues
        Seeded with multiple dq_* firings across several days + one ack.
        Asserts counts, filter behaviour, ack state derivation.

Prerequisites:
  - TEST_POSTGRES_DSN (or PLATFORM_DB_URL) must be set.
  - All migrations (001 through 026) applied to the test database.
  - Tests auto-skip when no live DB is present.

Auth is bypassed by patching core.admin_api._check_auth to return (True, "test").
All seeded rows are cleaned up in finally blocks (rollback-safe).

ASCII-only log strings (AI-03). No private framework attributes (AI-02).
AD-5 project_id scoping enforced via project_id query param.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import purge_fixture_project

# Detect live DB at module import time -- shared skip guard for the whole file.
_DSN = os.environ.get("TEST_POSTGRES_DSN") or os.environ.get("PLATFORM_DB_URL")

pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="TEST_POSTGRES_DSN/PLATFORM_DB_URL not set -- derived-SQL seam tests skipped",
)

# Guard daemon threads from starting during import / test collection.
os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _uid(prefix: str = "") -> str:
    """Generate a short random id (prefixed, safe as Postgres text PK)."""
    return f"{prefix}{uuid.uuid4().hex[:12]}"


@contextmanager
def _live_conn():
    """Yield a live psycopg connection; always rollback+close on exit."""
    import psycopg

    conn = psycopg.connect(_DSN)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _build_client():
    """Build a TestClient wrapping the real build_asgi_app() ASGI application.

    Auth is patched globally (core.admin_api._check_auth) to return (True, "test@test")
    so all tests in this file skip real token validation. Per-test patches for
    project_access are applied in each test context.
    """
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    app = build_asgi_app()
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# (a) Extract LEDGER -- GET /api/datastreams/{id}/ledger
#
# Seed shape:
#   pull_A: 2026-06-01 .. 2026-06-05  (state=done, verdict=ok)     -> contiguous block
#   pull_B: 2026-06-06 .. 2026-06-10  (state=done, verdict=ok)     -> contiguous continuation
#   GAP:    2026-06-11 .. 2026-06-14                               -> no pull (never_fetched)
#   pull_C: 2026-06-12 .. 2026-06-16  (state=done, verdict=partial)-> overlaps the gap + after
#
# Expected ledger over 2026-06-01 .. 2026-06-16:
#   Jun 01-05: ok  (pull_A)
#   Jun 06-10: ok  (pull_B)
#   Jun 11:    never_fetched (gap; pull_C starts 12)
#   Jun 12-16: partial (pull_C)
# ---------------------------------------------------------------------------


def _seed_ledger_project(conn):
    """Seed one project + connection + datastream + 3 pull_jobs for ledger tests.

    Returns (project_id, conn_ref_id, ds_id, pull_ids) for cleanup.
    """
    project_id = _uid("proj_ldg_")
    conn_ref_id = _uid("conn_")
    ds_id = _uid("ds_")
    pull_a_id = _uid("pull_")
    pull_b_id = _uid("pull_")
    pull_c_id = _uid("pull_")
    job_a_id = _uid("job_")
    job_b_id = _uid("job_")
    job_c_id = _uid("job_")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, created_by, org_id) "
            "VALUES (%s,%s,%s,'test', 'org_test_fixture') ON CONFLICT DO NOTHING",
            (project_id, project_id, project_id),
        )
        cur.execute(
            "INSERT INTO app.connection_ref "
            "(id, nango_connection_id, provider, project_id, status, enabled, owner_org_id, "
            "owner_identity) "
            "VALUES (%s,%s,'google-analytics',%s,'active',TRUE, 'org_test_fixture', "
            "'tester@example.com')",
            (conn_ref_id, f"nango-{conn_ref_id[-8:]}", project_id),
        )
        cur.execute(
            "INSERT INTO app.datastreams "
            "(id, project_id, name, module_name, connection_ref_id, "
            " report_profile_id, enabled, org_id) "
            "VALUES (%s,%s,%s,'google-analytics',%s,'standard_daily',TRUE, 'org_test_fixture')",
            (ds_id, project_id, f"stream-{ds_id[-6:]}", conn_ref_id),
        )
        # pull_A: 2026-06-01..2026-06-05, done/ok
        cur.execute(
            "INSERT INTO app.pull_jobs "
            "(id, pull_id, datastream_id, connection_ref_id, date_from, date_to, "
            " state, requested_by) "
            "VALUES (%s,%s,%s,%s,'2026-06-01','2026-06-05','done','test')",
            (job_a_id, pull_a_id, ds_id, conn_ref_id),
        )
        # pull_B: 2026-06-06..2026-06-10, done/ok
        cur.execute(
            "INSERT INTO app.pull_jobs "
            "(id, pull_id, datastream_id, connection_ref_id, date_from, date_to, "
            " state, requested_by) "
            "VALUES (%s,%s,%s,%s,'2026-06-06','2026-06-10','done','test')",
            (job_b_id, pull_b_id, ds_id, conn_ref_id),
        )
        # pull_C: 2026-06-12..2026-06-16, done/partial (overlaps the gap)
        cur.execute(
            "INSERT INTO app.pull_jobs "
            "(id, pull_id, datastream_id, connection_ref_id, date_from, date_to, "
            " state, requested_by) "
            "VALUES (%s,%s,%s,%s,'2026-06-12','2026-06-16','done','test')",
            (job_c_id, pull_c_id, ds_id, conn_ref_id),
        )
        # Verifications
        for pull_id, verdict in [
            (pull_a_id, "ok"),
            (pull_b_id, "ok"),
            (pull_c_id, "partial"),
        ]:
            ver_id = _uid("ver_")
            # connection_ref_id is NOT NULL on pull_verifications (the AI-37 lesson:
            # live constraints catch what mocked cursors cannot).
            cur.execute(
                "INSERT INTO app.pull_verifications "
                "(id, pull_id, connection_ref_id, verdict, actual_rows, expected_rows, "
                " completeness_ratio, verified_at) "
                "VALUES (%s,%s,%s,%s,100,150,0.67,%s)",
                (ver_id, pull_id, conn_ref_id, verdict, datetime.now(tz=timezone.utc)),
            )
    conn.commit()
    return project_id, conn_ref_id, ds_id, [job_a_id, job_b_id, job_c_id]


def _cleanup_ledger(conn, project_id, conn_ref_id, ds_id):
    """Delete seeded rows in FK-safe order."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM app.pull_verifications WHERE pull_id IN "
            "(SELECT pull_id FROM app.pull_jobs WHERE datastream_id=%s)",
            (ds_id,),
        )
        cur.execute("DELETE FROM app.pull_jobs WHERE datastream_id=%s", (ds_id,))
        cur.execute("DELETE FROM app.datastreams WHERE id=%s", (ds_id,))
        cur.execute("DELETE FROM app.connection_ref WHERE id=%s", (conn_ref_id,))
        # AI-291: le graphe prend le relais si une table gouvernee
        # ajoutee depuis retient le projet en ON DELETE RESTRICT.
        purge_fixture_project(cur.connection, project_id)
    conn.commit()


def test_ledger_multi_day_multi_range_correct_statuses_through_build_asgi_app():
    """AI-45(a): ledger endpoint returns correct day-grain statuses for multi-day,
    multi-range, overlapping + gap pull data through the full ASGI stack.

    This test replicates the exact shape that hid the Epic 8 CRITICAL regression:
    contiguous pulls + a real gap + an overlapping re-pull covering partial of the gap.
    Single-day window tests would pass regardless of the bug.
    """
    with _live_conn() as pg:
        project_id, conn_ref_id, ds_id, _jobs = _seed_ledger_project(pg)
        try:
            with (
                patch(
                    "core.admin_api._check_auth",
                    new=AsyncMock(return_value=(True, "test@test")),
                ),
                patch("core.project_access.identity_can_read_project", return_value=True),
            ):
                client = _build_client()
                resp = client.get(
                    f"/api/datastreams/{ds_id}/ledger"
                    f"?project_id={project_id}&from=2026-06-01&to=2026-06-16",
                )

            assert resp.status_code == 200, f"Unexpected status {resp.status_code}: {resp.text}"
            body = resp.json()
            assert "ledger" in body

            by_date = {row["date"]: row["status"] for row in body["ledger"]}

            # 16-day window must be fully present.
            assert len(body["ledger"]) == 16, f"Expected 16 ledger rows, got {len(body['ledger'])}"

            # pull_A days (2026-06-01..2026-06-05) -- verdict=ok -> status='ok'
            for d in ("2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"):
                assert by_date[d] == "ok", f"{d}: expected 'ok', got {by_date[d]!r}"

            # pull_B days (2026-06-06..2026-06-10) -- verdict=ok -> status='ok'
            for d in ("2026-06-06", "2026-06-07", "2026-06-08", "2026-06-09", "2026-06-10"):
                assert by_date[d] == "ok", f"{d}: expected 'ok', got {by_date[d]!r}"

            # Gap day (2026-06-11) -- no pull -> never_fetched
            assert by_date["2026-06-11"] == "never_fetched", (
                f"2026-06-11: expected 'never_fetched', got {by_date['2026-06-11']!r}"
            )

            # pull_C days (2026-06-12..2026-06-16) -- verdict=partial -> status='partial'
            for d in ("2026-06-12", "2026-06-13", "2026-06-14", "2026-06-15", "2026-06-16"):
                assert by_date[d] == "partial", f"{d}: expected 'partial', got {by_date[d]!r}"
        finally:
            _cleanup_ledger(pg, project_id, conn_ref_id, ds_id)


def test_ledger_running_and_failed_pull_statuses_through_build_asgi_app():
    """AI-45(a) supplementary: ledger correctly surfaces 'running' and 'failed' states
    for in-progress and dead-letter pulls in a multi-day window.

    'running' covers 2026-07-01..2026-07-05, 'failed' covers 2026-07-03..2026-07-07
    (overlapping range). Latest pull per day wins (enqueued_at DESC).
    We seed failed pull FIRST (earlier enqueued_at), then running pull SECOND
    so running is the latest for 2026-07-03..2026-07-05 (running wins there).
    """
    with _live_conn() as pg:
        project_id = _uid("proj_ldg2_")
        conn_ref_id = _uid("conn_")
        ds_id = _uid("ds_")

        with pg.cursor() as cur:
            cur.execute(
                "INSERT INTO app.projects (id, name, slug, created_by, org_id) "
                "VALUES (%s,%s,%s,'test', 'org_test_fixture') ON CONFLICT DO NOTHING",
                (project_id, project_id, project_id),
            )
            cur.execute(
                "INSERT INTO app.connection_ref "
                "(id, nango_connection_id, provider, project_id, status, enabled, owner_org_id, "
                "owner_identity) "
                "VALUES (%s,%s,'google-analytics',%s,'active',TRUE, 'org_test_fixture', "
                "'tester@example.com')",
                (conn_ref_id, f"nango-{conn_ref_id[-8:]}", project_id),
            )
            cur.execute(
                "INSERT INTO app.datastreams "
                "(id, project_id, name, module_name, connection_ref_id, "
                " report_profile_id, enabled, org_id) "
                "VALUES (%s,%s,%s,'google-analytics',%s,'standard_daily',TRUE, 'org_test_fixture')",
                (ds_id, project_id, f"stream-{ds_id[-6:]}", conn_ref_id),
            )
            # failed pull seeded first (earlier enqueued_at via explicit NOW()-10min)
            job_f_id = _uid("job_")
            pull_f_id = _uid("pull_")
            cur.execute(
                "INSERT INTO app.pull_jobs "
                "(id, pull_id, datastream_id, connection_ref_id, date_from, date_to, "
                " state, requested_by, enqueued_at) "
                "VALUES (%s,%s,%s,%s,'2026-07-03','2026-07-07','failed','test', "
                " NOW()-'10 min'::interval)",
                (job_f_id, pull_f_id, ds_id, conn_ref_id),
            )
            # running pull seeded later (more recent enqueued_at = default NOW())
            job_r_id = _uid("job_")
            pull_r_id = _uid("pull_")
            cur.execute(
                "INSERT INTO app.pull_jobs "
                "(id, pull_id, datastream_id, connection_ref_id, date_from, date_to, "
                " state, requested_by) "
                "VALUES (%s,%s,%s,%s,'2026-07-01','2026-07-05','running','test')",
                (job_r_id, pull_r_id, ds_id, conn_ref_id),
            )
        pg.commit()

        try:
            with (
                patch(
                    "core.admin_api._check_auth",
                    new=AsyncMock(return_value=(True, "test@test")),
                ),
                patch("core.project_access.identity_can_read_project", return_value=True),
            ):
                client = _build_client()
                resp = client.get(
                    f"/api/datastreams/{ds_id}/ledger"
                    f"?project_id={project_id}&from=2026-07-01&to=2026-07-07",
                )

            assert resp.status_code == 200
            by_date = {row["date"]: row["status"] for row in resp.json()["ledger"]}

            # running pull is latest for 01-05 -> running
            for d in ("2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04", "2026-07-05"):
                assert by_date[d] == "running", f"{d}: expected 'running', got {by_date[d]!r}"
            # failed pull is the ONLY pull covering 06-07 -> failed
            for d in ("2026-07-06", "2026-07-07"):
                assert by_date[d] == "failed", f"{d}: expected 'failed', got {by_date[d]!r}"
        finally:
            with pg.cursor() as cur:
                cur.execute("DELETE FROM app.pull_jobs WHERE datastream_id=%s", (ds_id,))
                cur.execute("DELETE FROM app.datastreams WHERE id=%s", (ds_id,))
                cur.execute("DELETE FROM app.connection_ref WHERE id=%s", (conn_ref_id,))
                # AI-291: le graphe prend le relais si une table gouvernee
                # ajoutee depuis retient le projet en ON DELETE RESTRICT.
                purge_fixture_project(cur.connection, project_id)
            pg.commit()


# ---------------------------------------------------------------------------
# (b) Report CHAIN -- GET /api/reports/{module}/{report_id}/chain
#
# Seed shape:
#   - 2 target_fields in app.target_fields: 'sessions' + 'clicks'
#     (already seeded by migration 023; ON CONFLICT DO NOTHING is safe)
#   - project with 2 enabled datastreams:
#       ds_sessions: maps 'sessions' -> sessions (OK path)
#       ds_clicks:   maps 'clicks'   -> clicks   (OK path)
#   - report doc (mocked via flows layer) declares metrics: ['sessions', 'clicks', 'impressions']
#     'impressions' has no mapping in this project -> no_stream warning
#
# Assertions:
#   - ok_count == 2 (sessions, clicks)
#   - validation.warnings has 1 entry (impressions no_stream)
#   - per-metric status: sessions=ok, clicks=ok, impressions=no_stream
# ---------------------------------------------------------------------------


def _seed_chain_project(conn):
    """Seed project + 2 datastreams with mappings for chain tests."""
    project_id = _uid("proj_chain_")
    conn_ref_id = _uid("conn_")
    ds_sess_id = _uid("ds_")
    ds_clk_id = _uid("ds_")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, created_by, org_id) "
            "VALUES (%s,%s,%s,'test', 'org_test_fixture') ON CONFLICT DO NOTHING",
            (project_id, project_id, project_id),
        )
        cur.execute(
            "INSERT INTO app.connection_ref "
            "(id, nango_connection_id, provider, project_id, status, enabled, owner_org_id, "
            "owner_identity) "
            "VALUES (%s,%s,'google-analytics',%s,'active',TRUE, 'org_test_fixture', "
            "'tester@example.com')",
            (conn_ref_id, f"nango-{conn_ref_id[-8:]}", project_id),
        )
        # ds_sessions: maps sessions -> sessions
        cur.execute(
            "INSERT INTO app.datastreams "
            "(id, project_id, name, module_name, connection_ref_id, "
            " report_profile_id, enabled, org_id) "
            "VALUES (%s,%s,'ds-sessions','google-analytics',%s,'standard_daily',TRUE, "
            "'org_test_fixture')",
            (ds_sess_id, project_id, conn_ref_id),
        )
        cur.execute(
            "INSERT INTO app.datastream_mappings "
            "(datastream_id, source_field, target_field) "
            "VALUES (%s,'sessions','sessions') ON CONFLICT DO NOTHING",
            (ds_sess_id,),
        )
        # ds_clicks: maps clicks -> clicks
        cur.execute(
            "INSERT INTO app.datastreams "
            "(id, project_id, name, module_name, connection_ref_id, "
            " report_profile_id, enabled, org_id) "
            "VALUES (%s,%s,'ds-clicks','google-analytics',%s,'standard_daily',TRUE, "
            "'org_test_fixture')",
            (ds_clk_id, project_id, conn_ref_id),
        )
        cur.execute(
            "INSERT INTO app.datastream_mappings "
            "(datastream_id, source_field, target_field) "
            "VALUES (%s,'clicks','clicks') ON CONFLICT DO NOTHING",
            (ds_clk_id,),
        )
    conn.commit()
    return project_id, conn_ref_id, [ds_sess_id, ds_clk_id]


def _cleanup_chain(conn, project_id, conn_ref_id, ds_ids):
    with conn.cursor() as cur:
        for ds_id in ds_ids:
            cur.execute("DELETE FROM app.datastream_mappings WHERE datastream_id=%s", (ds_id,))
            cur.execute("DELETE FROM app.datastreams WHERE id=%s", (ds_id,))
        cur.execute("DELETE FROM app.connection_ref WHERE id=%s", (conn_ref_id,))
        # AI-291: le graphe prend le relais si une table gouvernee
        # ajoutee depuis retient le projet en ON DELETE RESTRICT.
        purge_fixture_project(cur.connection, project_id)
    conn.commit()


def test_report_chain_multi_metric_multi_datastream_statuses_through_build_asgi_app():
    """AI-45(b): chain endpoint returns correct per-metric statuses when multiple
    datastreams map to multiple metrics, through the full ASGI stack.

    Report declares 3 metrics: sessions (ok), clicks (ok), impressions (no_stream).
    Verifies ok_count=2 and per-metric status, not just 200.
    """
    with _live_conn() as pg:
        project_id, conn_ref_id, ds_ids = _seed_chain_project(pg)
        try:
            # Mock the flows layer so we don't need real module files on disk.
            merged_doc = {
                "schema_version": "1",
                "kind": "report",
                "id": "google-analytics/overview_daily",
                "base_report_id": "google-analytics/overview_daily",
                "display_name": "Overview Daily (test)",
                "metrics": ["sessions", "clicks", "impressions"],
                "metric_definitions": None,
                "llm_commentary_guidelines": None,
            }

            with (
                patch(
                    "core.admin_api._check_auth",
                    new=AsyncMock(return_value=(True, "test@test")),
                ),
                patch("core.project_access.identity_can_read_project", return_value=True),
                patch("core.flows._base_report_doc", return_value=merged_doc),
                patch("core.flows._fetch_report_override", return_value=None),
                patch("core.flows._merge_report", return_value=merged_doc),
            ):
                client = _build_client()
                resp = client.get(
                    f"/api/reports/google-analytics/overview_daily/chain?project_id={project_id}",
                )

            assert resp.status_code == 200, f"Unexpected status {resp.status_code}: {resp.text}"
            body = resp.json()

            # Validation block
            assert "validation" in body
            assert body["validation"]["ok_count"] == 2, (
                f"Expected ok_count=2, got {body['validation']['ok_count']}"
            )
            assert len(body["validation"]["warnings"]) == 1, (
                f"Expected 1 warning (impressions no_stream), got {body['validation']['warnings']}"
            )

            # Per-metric statuses
            by_metric = {m["metric"]: m for m in body["metrics"]}
            assert by_metric["sessions"]["status"] == "ok", (
                f"sessions: expected 'ok', got {by_metric['sessions']['status']!r}"
            )
            assert by_metric["clicks"]["status"] == "ok", (
                f"clicks: expected 'ok', got {by_metric['clicks']['status']!r}"
            )
            # impressions has no mapping in this project
            assert by_metric["impressions"]["status"] in ("no_stream", "not_in_dictionary"), (
                f"impressions: expected 'no_stream' or 'not_in_dictionary', "
                f"got {by_metric['impressions']['status']!r}"
            )
        finally:
            _cleanup_chain(pg, project_id, conn_ref_id, ds_ids)


def test_report_chain_all_not_in_dictionary_when_no_target_fields_match():
    """AI-45(b) supplementary: when report metrics have no matching target_fields rows,
    every metric is 'not_in_dictionary' and ok_count=0. Exercises the not_in_dictionary
    branch through the full ASGI stack.
    """
    with _live_conn() as pg:
        project_id, conn_ref_id, ds_ids = _seed_chain_project(pg)
        try:
            merged_doc = {
                "schema_version": "1",
                "kind": "report",
                "id": "test-mod/ghost_report",
                "base_report_id": "test-mod/ghost_report",
                "display_name": "Ghost Report",
                "metrics": ["ghost_metric_zzz_1", "ghost_metric_zzz_2"],
                "metric_definitions": None,
                "llm_commentary_guidelines": None,
            }

            with (
                patch(
                    "core.admin_api._check_auth",
                    new=AsyncMock(return_value=(True, "test@test")),
                ),
                patch("core.project_access.identity_can_read_project", return_value=True),
                patch("core.flows._base_report_doc", return_value=merged_doc),
                patch("core.flows._fetch_report_override", return_value=None),
                patch("core.flows._merge_report", return_value=merged_doc),
            ):
                client = _build_client()
                resp = client.get(
                    f"/api/reports/test-mod/ghost_report/chain?project_id={project_id}",
                )

            assert resp.status_code == 200
            body = resp.json()
            assert body["validation"]["ok_count"] == 0
            by_metric = {m["metric"]: m["status"] for m in body["metrics"]}
            assert by_metric["ghost_metric_zzz_1"] == "not_in_dictionary"
            assert by_metric["ghost_metric_zzz_2"] == "not_in_dictionary"
        finally:
            _cleanup_chain(pg, project_id, conn_ref_id, ds_ids)


# ---------------------------------------------------------------------------
# (c) DQ ISSUES -- retired with the routes they exercised (Story 49.4).
#
# Two tests lived here: one drove `GET /api/dq/issues` through the full ASGI
# stack across filters and cross-project isolation, the other drove
# `POST /api/dq/issues/{id}/acknowledge`. Both routes were unmounted by Story
# 49.4, which moved DQ issues off `app.alert_firings` (where an "issue" was a
# firing whose identity was parsed out of a MESSAGE STRING) onto governed
# `app.dq_issues` rows with an explicit workflow.
#
# The coverage did not disappear; it moved, and this comment exists so the move
# is findable rather than looking like a deletion:
#
#   * the issue list, including CLOSED issues so a recurrence does not read as
#     new -> `core.governance_read_model._dq_issue_list`, rendered by
#     `MonitorIssuesTab`, tested in `ui/admin/src/__tests__/ControlsQualityTabs`;
#   * acknowledgement -> `core.dq_governance.transition_issue`, which is now a
#     SUPPRESSION and refuses one without an end date, tested in
#     `tests/core/test_controls_quality.py`;
#   * cross-project isolation -> `resolve_strict_resource_access` on the
#     Governance surface, tested in `test_controls_quality_seam.py`.
#
# What replaces them here is one assertion: the doors are gone. A retired route
# that quietly came back would otherwise be invisible.
# ---------------------------------------------------------------------------


def test_the_retired_dq_routes_are_absent_from_the_router_the_app_forwards_to():
    from core.admin_api import router

    leftovers = sorted(
        path
        for path in (getattr(route, "path", "") for route in router.routes)
        if path.startswith("/api/dq/")
    )
    assert leftovers == [], leftovers
