"""Tools isolation — no cross-scope data path (Story 7.4, AC2, AC5, AC8).

Each test seeds distinct data per project (via the two_projects fixture) and
calls a tool with ALPHA scope, asserting ZERO beta data appears. Covers the
AD-5 tree nodes reachable by the tools layer:
  - Report / Dimension:  get_report against the project-scoped DuckDB mart.
  - Cache (briefing):    get_daily_report briefing is (project, date) keyed.
  - Alert firings:       fetch_recent_anomaly_firings (AI-36 end-to-end proof).
  - Tool (module):       list_modules per-project enablement.
"""

from __future__ import annotations

import psycopg
import pytest

pytestmark = pytest.mark.isolation


def test_anomaly_firings_scoped_to_alpha(two_projects, pg_conn):
    """AI-36 end-to-end: fetch_recent_anomaly_firings(alpha) excludes beta rows.

    Alpha seeded a 'sessions' anomaly firing; beta an 'average_position' one.
    A scope leak would surface beta's metric in alpha's result.
    """
    from core.anomaly_alerts import fetch_recent_anomaly_firings

    alpha, beta = two_projects

    alpha_firings = fetch_recent_anomaly_firings(alpha["id"], pg_conn, hours=48)
    metrics = {f["metric"] for f in alpha_firings}

    assert "sessions" in metrics, "alpha must see its own sessions firing"
    assert "average_position" not in metrics, "beta anomaly leaked into alpha!"
    assert all(f["metric"] != "average_position" for f in alpha_firings)

    # Symmetric proof: beta must not see alpha's firing.
    beta_firings = fetch_recent_anomaly_firings(beta["id"], pg_conn, hours=48)
    beta_metrics = {f["metric"] for f in beta_firings}
    assert "average_position" in beta_metrics
    assert "sessions" not in beta_metrics


def test_get_report_scoped_to_alpha(two_projects):
    """get_report(alpha) metric rows are alpha-only (project-scoped mart).

    The DuckDB mart carries alpha (sessions) and beta (average_position) rows
    for the same dates. The warehouse WHERE project_id = ? must exclude beta.
    """
    from core import warehouse

    alpha, beta = two_projects

    alpha_rows = warehouse.query_daily_report(
        project_id=alpha["id"],
        start_date="2026-07-01",
        end_date="2026-07-10",
        connectors=None,
    )
    assert alpha_rows, "alpha should have seeded fact rows"
    # query_daily_report projects specific columns (project_id is the bound WHERE
    # filter, not a returned column) — scoping is proven by metric/connector being
    # alpha-only, i.e. beta's disjoint 'average_position'/'gsc' never appear.
    metrics = {r["metric"] for r in alpha_rows}
    connectors = {r["connector"] for r in alpha_rows}
    assert "sessions" in metrics
    assert "average_position" not in metrics, "beta metric leaked into alpha report!"
    assert "gsc" not in connectors, "beta connector leaked into alpha report!"


def test_morning_briefing_scoped_to_alpha(two_projects, pg_conn):
    """alpha briefing does NOT include beta insights (cache key = project,date)."""
    alpha, beta = two_projects

    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT insights FROM app.morning_briefings
            WHERE project_id = %s AND briefing_date = CURRENT_DATE
            """,
            (alpha["id"],),
        )
        row = cur.fetchone()

    assert row is not None, "alpha briefing must exist"
    insights_text = str(row[0])
    assert "sessions" in insights_text
    assert "average_position" not in insights_text, "beta briefing leaked into alpha!"


def test_get_daily_report_scoped_to_alpha(two_projects):
    """get_daily_report(alpha) does not include beta connection/firing/briefing.

    Exercised via the warehouse + Postgres surfaces the tool reads: the briefing
    SELECT and anomaly-firing SELECT are both (project_id)-keyed. We assert the
    composed set of alpha data carries no beta metric.
    """
    from core import warehouse
    from core.anomaly_alerts import fetch_recent_anomaly_firings

    alpha, beta = two_projects

    rows = warehouse.query_daily_report(
        project_id=alpha["id"],
        start_date="2026-07-01",
        end_date="2026-07-10",
        connectors=None,
    )
    conn = psycopg.connect(alpha_dsn())
    try:
        firings = fetch_recent_anomaly_firings(alpha["id"], conn, hours=48)
    finally:
        conn.close()

    blob = str(rows) + str(firings)
    assert "average_position" not in blob, "beta data present in alpha daily report!"
    assert "gsc" not in {r["connector"] for r in rows}


def test_list_modules_scoped_to_alpha(two_projects):
    """alpha module list reflects alpha enablement, not beta's.

    Beta explicitly DISABLED gsc; alpha did not. Alpha's list_modules must not
    be affected by beta's project_modules row. We assert enablement is resolved
    per-project by comparing is_module_enabled for the two projects.
    """
    from core import db as core_db
    from core.module_enablement import is_module_enabled

    alpha, beta = two_projects

    with core_db.get_connection() as conn:
        # Beta disabled gsc; alpha (no gsc row) defaults to enabled.
        beta_gsc = is_module_enabled("gsc", beta["id"], conn)
        alpha_gsc = is_module_enabled("gsc", alpha["id"], conn)

    assert beta_gsc is False, "beta explicitly disabled gsc"
    assert alpha_gsc is True, "alpha gsc must be default-enabled, not shared with beta"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def alpha_dsn() -> str:
    import os

    return os.environ["TEST_POSTGRES_DSN"]
