"""Two-project isolation fixtures (Story 7.4, AC1, FR12, AD-5).

Provisions two fully isolated projects (alpha, beta) with DISTINCT data seeded
across every layer of the AD-5 addressing tree, plus a DuckDB mart carrying
project-scoped ``fact_daily_kpi`` rows. The isolation tests then exercise tools,
admin endpoints, and caches with one project's scope and assert ZERO rows from
the other project ever appear.

Requires ``TEST_POSTGRES_DSN`` (a database with migrations 001-021 applied) and
skips the whole suite otherwise. Postgres seeding runs in the fixture; the
DuckDB warehouse is materialised in a temp file per module and pointed at via
``TOOROW_DUCKDB_PATH`` / ``PLATFORM_DB_URL`` for the duration of the module.

Seed strategy (Dev Notes "Two-Project Fixture"):
  - Alpha: 10 GA4 fact rows (metric=sessions), a sessions anomaly firing, a
    sessions morning briefing, a google-analytics notebook, an active GA4
    connection, google-analytics module explicitly enabled.
  - Beta:  10 GSC fact rows (metric=average_position), an average_position
    anomaly firing, an average_position briefing, a gsc notebook, an active GSC
    connection, gsc module explicitly DISABLED.
  The metrics are DISJOINT ('sessions' vs 'average_position') so a leak is
  detectable by a single substring assertion.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone

import pytest

# Keep background threads off during isolation-suite imports (mirrors the rest
# of the server test suite).
os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

_DSN = os.environ.get("TEST_POSTGRES_DSN")

# Every module in this package requires TEST_POSTGRES_DSN.
pytestmark = pytest.mark.skipif(
    not _DSN, reason="Requires TEST_POSTGRES_DSN"
)

ALPHA_ID = "proj_alpha"
BETA_ID = "proj_beta"
TODAY = date.today()


# ---------------------------------------------------------------------------
# DuckDB mart: project-scoped fact_daily_kpi rows.
# ---------------------------------------------------------------------------


def _seed_duckdb_mart(path: str) -> None:
    """Create main_marts.fact_daily_kpi with distinct rows per project.

    Alpha rows carry connector='google-analytics', metric='sessions'.
    Beta rows carry connector='gsc', metric='average_position'.
    """
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(path)
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS main_marts.fact_daily_kpi (
                project_id           TEXT,
                date                 DATE,
                connector            TEXT,
                metric               TEXT,
                breakdown_dimension  TEXT,
                breakdown_value      TEXT,
                value                DOUBLE,
                pull_id              TEXT,
                loaded_at            TIMESTAMP
            )
            """
        )
        con.execute("DELETE FROM main_marts.fact_daily_kpi WHERE project_id IN (?, ?)",
                    [ALPHA_ID, BETA_ID])
        rows = []
        for d in range(1, 11):
            day = date(2026, 7, d)
            rows.append((ALPHA_ID, day, "google-analytics", "sessions",
                         "device", "desktop", 1000.0 + d, "pull_alpha", datetime(2026, 7, 11)))
            rows.append((BETA_ID, day, "gsc", "average_position",
                         "query", "brand", 3.0 + d, "pull_beta", datetime(2026, 7, 11)))
        con.executemany(
            "INSERT INTO main_marts.fact_daily_kpi VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Postgres seeding helpers.
# ---------------------------------------------------------------------------


def _seed_project(cur, pid: str, slug: str, name: str) -> None:
    cur.execute(
        """
        INSERT INTO app.projects (id, name, slug, status, currency, timezone, created_by)
        VALUES (%s, %s, %s, 'active', 'EUR', 'Europe/Paris', 'isolation-test')
        ON CONFLICT (id) DO NOTHING
        """,
        (pid, name, slug),
    )


def _seed_connection(cur, conn_id: str, pid: str, provider: str) -> None:
    cur.execute(
        """
        INSERT INTO app.connection_ref
            (id, provider, nango_connection_id, project_id, status, enabled)
        VALUES (%s, %s, %s, %s, 'active', TRUE)
        ON CONFLICT (id) DO NOTHING
        """,
        (conn_id, provider, f"nango_{conn_id}", pid),
    )


def _seed_anomaly_firing(cur, fire_id: str, pid: str, metric: str) -> None:
    cur.execute(
        """
        INSERT INTO app.alert_firings
            (id, definition_id, type, fired_at, observed_value, threshold,
             pull_ids, window_date, severity, project_id, metric)
        VALUES (%s, NULL, 'anomaly', NOW(), %s, %s, %s, %s, 'warning', %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (fire_id, 999.0, 100.0, [], TODAY, pid, metric),
    )


def _seed_briefing(cur, brief_id: str, pid: str, metric: str) -> None:
    import json  # noqa: PLC0415

    insights = json.dumps(
        {"top_insights": [{"metric": metric, "text": f"{metric} insight for {pid}"}]}
    )
    # briefing_date uses Postgres CURRENT_DATE (not Python date.today()) so the
    # tests' WHERE briefing_date = CURRENT_DATE comparisons are timezone-proof:
    # around midnight, the local Python date can be a day ahead of the UTC
    # Postgres session date (observed 2026-07-13 ~00h CEST).
    cur.execute(
        """
        INSERT INTO app.morning_briefings
            (id, project_id, briefing_date, insights, built_at)
        VALUES (%s, %s, CURRENT_DATE, %s::jsonb, NOW())
        ON CONFLICT (project_id, briefing_date) DO NOTHING
        """,
        (brief_id, pid, insights),
    )


def _seed_notebook(cur, nb_id: str, pid: str, title: str, report_ref: str) -> None:
    cur.execute(
        """
        INSERT INTO app.notebooks
            (id, project_id, title, report_ref, window_rule, created_by,
             created_at, updated_at)
        VALUES (%s, %s, %s, %s, 'last_30d', 'isolation-test', NOW(), NOW())
        ON CONFLICT (id) DO NOTHING
        """,
        (nb_id, pid, title, report_ref),
    )


def _seed_notebook_run(cur, run_id: str, nb_id: str) -> None:
    cur.execute(
        """
        INSERT INTO app.notebook_runs
            (id, notebook_id, executed_at, summary_text, pull_ids, status)
        VALUES (%s, %s, NOW(), %s, %s, 'success')
        ON CONFLICT (id) DO NOTHING
        """,
        (run_id, nb_id, "isolation run summary", []),
    )


def _seed_project_module(cur, pmod_id: str, pid: str, module: str, enabled: bool) -> None:
    cur.execute(
        """
        INSERT INTO app.project_modules
            (id, project_id, module_name, enabled, updated_by)
        VALUES (%s, %s, %s, %s, 'isolation-test')
        ON CONFLICT (project_id, module_name) DO UPDATE SET enabled = EXCLUDED.enabled
        """,
        (pmod_id, pid, module, enabled),
    )


def _seed_project_report(cur, rpt_id: str, pid: str, module: str, report_id: str) -> None:
    cur.execute(
        """
        INSERT INTO app.project_reports
            (id, project_id, module_name, report_id, enabled, display_order)
        VALUES (%s, %s, %s, %s, TRUE, 0)
        ON CONFLICT (project_id, module_name, report_id) DO NOTHING
        """,
        (rpt_id, pid, module, report_id),
    )


def _purge_projects(conn, ids: tuple[str, ...]) -> None:
    """Delete two test projects and their RESTRICT-scoped children.

    connection_ref, alert_firings, project_reports (and other business tables)
    carry ON DELETE RESTRICT FKs to app.projects, so they must go first. Audit
    rows written during the test reference these connections; delete them too so
    the connection_ref delete is not itself blocked by the audit_log FK.
    """
    with conn.cursor() as cur:
        # audit rows referencing this project's connections (FK: audit_log ->
        # connection_ref). Remove via the append-only-safe path is impossible
        # (UPDATE/DELETE blocked by trigger), so instead NULL nothing — the audit
        # rows are connection-agnostic (connection_ref IS NULL for our
        # access_denied rows) and do NOT block connection_ref deletion. Only rows
        # that actually reference conn_alpha/conn_beta would; our fixture writes
        # none. So we go straight to the RESTRICT children.
        for table in ("app.alert_firings", "app.project_reports", "app.connection_ref"):
            cur.execute(
                f"DELETE FROM {table} WHERE project_id = ANY(%s)",  # noqa: S608
                (list(ids),),
            )
        cur.execute("DELETE FROM app.projects WHERE id = ANY(%s)", (list(ids),))
    conn.commit()


@pytest.fixture(scope="module")
def two_projects(tmp_path_factory):
    """Provision two isolated projects (alpha, beta) with distinct data.

    Yields ``(alpha, beta)`` dicts, each with keys:
        id, name, slug, connection_id, notebook_id, notebook_run_id,
        firing_id, briefing_id, duckdb_path.

    Teardown removes both projects (CASCADE clears the child rows this fixture
    inserted). The DuckDB temp file is discarded with the tmp dir.
    """
    if not _DSN:
        pytest.skip("Requires TEST_POSTGRES_DSN")

    import psycopg  # noqa: PLC0415

    duckdb_path = str(tmp_path_factory.mktemp("mart") / "isolation.duckdb")
    _seed_duckdb_mart(duckdb_path)

    alpha = {
        "id": ALPHA_ID, "name": "Alpha", "slug": "alpha",
        "connection_id": "conn_alpha", "notebook_id": "nb_alpha",
        "notebook_run_id": "nbrun_alpha", "firing_id": "fire_alpha",
        "briefing_id": "brief_alpha", "duckdb_path": duckdb_path,
        "metric": "sessions", "module": "google-analytics",
    }
    beta = {
        "id": BETA_ID, "name": "Beta", "slug": "beta",
        "connection_id": "conn_beta", "notebook_id": "nb_beta",
        "notebook_run_id": "nbrun_beta", "firing_id": "fire_beta",
        "briefing_id": "brief_beta", "duckdb_path": duckdb_path,
        "metric": "average_position", "module": "gsc",
    }

    conn = psycopg.connect(_DSN)
    try:
        # Clean any leftovers from a previous failed run. Delete RESTRICT-scoped
        # child rows first (connection_ref, alert_firings, project_reports use
        # ON DELETE RESTRICT), then the projects (notebooks / morning_briefings /
        # project_modules cascade automatically).
        _purge_projects(conn, (ALPHA_ID, BETA_ID))

        with conn.cursor() as cur:
            _seed_project(cur, ALPHA_ID, "alpha", "Alpha")
            _seed_project(cur, BETA_ID, "beta", "Beta")

            _seed_connection(cur, "conn_alpha", ALPHA_ID, "google-analytics")
            _seed_connection(cur, "conn_beta", BETA_ID, "gsc")

            _seed_anomaly_firing(cur, "fire_alpha", ALPHA_ID, "sessions")
            _seed_anomaly_firing(cur, "fire_beta", BETA_ID, "average_position")

            _seed_briefing(cur, "brief_alpha", ALPHA_ID, "sessions")
            _seed_briefing(cur, "brief_beta", BETA_ID, "average_position")

            _seed_notebook(cur, "nb_alpha", ALPHA_ID, "Alpha NB", "google-analytics/overview_daily")
            _seed_notebook(cur, "nb_beta", BETA_ID, "Beta NB", "gsc/position_movements")
            _seed_notebook_run(cur, "nbrun_alpha", "nb_alpha")
            _seed_notebook_run(cur, "nbrun_beta", "nb_beta")

            # Alpha enables GA; Beta explicitly DISABLES gsc (distinct enablement).
            _seed_project_module(cur, "pmod_alpha", ALPHA_ID, "google-analytics", True)
            _seed_project_module(cur, "pmod_beta", BETA_ID, "gsc", False)

            _seed_project_report(cur, "rpt_alpha", ALPHA_ID, "google-analytics", "overview_daily")
            _seed_project_report(cur, "rpt_beta", BETA_ID, "gsc", "position_movements")
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise

    # Point the warehouse + platform DB at the seeded fixtures for the module.
    prev_duckdb = os.environ.get("TOOROW_DUCKDB_PATH")
    prev_platform = os.environ.get("PLATFORM_DB_URL")
    os.environ["TOOROW_DUCKDB_PATH"] = duckdb_path
    os.environ["PLATFORM_DB_URL"] = _DSN

    try:
        yield alpha, beta
    finally:
        _purge_projects(conn, (ALPHA_ID, BETA_ID))
        conn.close()
        if prev_duckdb is None:
            os.environ.pop("TOOROW_DUCKDB_PATH", None)
        else:
            os.environ["TOOROW_DUCKDB_PATH"] = prev_duckdb
        if prev_platform is None:
            os.environ.pop("PLATFORM_DB_URL", None)
        else:
            os.environ["PLATFORM_DB_URL"] = prev_platform


@pytest.fixture()
def pg_conn():
    """A fresh psycopg connection to the test Postgres (autocommit off)."""
    if not _DSN:
        pytest.skip("Requires TEST_POSTGRES_DSN")
    import psycopg  # noqa: PLC0415

    conn = psycopg.connect(_DSN)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)
