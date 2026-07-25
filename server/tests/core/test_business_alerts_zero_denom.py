"""AI-35 (Story 6.1, AC11) -- CPA zero-denominator guard test.

Seeds a real DuckDB mart with conversions=0 for a CPA alert definition and calls
evaluate_business_alerts(). Asserts:
  - no firing row inserted, no exception raised;
  - the NULLIF(conversions, 0) guard in semantic_cpa returns NULL, so the
    evaluator's ``if observed is None: continue`` branch is exercised (a NULL CPA
    can never breach a numeric threshold).

Pure test addition -- exercises the guard already present in
dbt/models/marts/semantic_cpa.sql. No production code change expected.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import business_alerts  # noqa: E402


def _make_cursor(rows=None, description=None):
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchall = MagicMock(return_value=rows or [])
    cur.fetchone = MagicMock(return_value=(rows[0] if rows else None))
    cur.description = description or []
    return cur


def _make_conn(cursor):
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cursor)
    conn.commit = MagicMock()
    return conn


@contextmanager
def _conn_ctx(conn):
    yield conn


def _build_zero_conversion_duckdb(db_path: Path) -> None:
    """Materialise a semantic_cpa view over fact rows with conversions=0."""
    import duckdb

    con = duckdb.connect(str(db_path))
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
        con.execute(
            """CREATE TABLE main_marts.fact_daily_kpi (
                project_id TEXT, date DATE, connector TEXT, metric TEXT,
                breakdown_dimension TEXT, breakdown_value TEXT, value DOUBLE,
                pull_id TEXT, loaded_at TIMESTAMP)"""
        )
        # cost=500, conversions=0 -> CPA = 500 / NULLIF(0,0) = NULL.
        con.execute(
            "INSERT INTO main_marts.fact_daily_kpi VALUES "
            "('proj1','2026-07-10','meta-ads','cost','campaign_id','c1',500,'pull_z','2026-07-10T00:00:00'),"
            "('proj1','2026-07-10','meta-ads','conversions','campaign_id','c1',0,'pull_z','2026-07-10T00:00:00')"
        )
        con.execute(
            """CREATE VIEW main_marts.semantic_cpa AS
               SELECT project_id, date, connector, breakdown_dimension, breakdown_value,
                      SUM(CASE WHEN metric='cost' THEN value ELSE 0 END)
                      / NULLIF(SUM(CASE WHEN metric='conversions' THEN value ELSE 0 END), 0) AS cpa,
                      MAX(pull_id) AS pull_id
               FROM main_marts.fact_daily_kpi
               GROUP BY project_id, date, connector, breakdown_dimension, breakdown_value"""
        )
    finally:
        con.close()


def test_cpa_zero_denominator_no_firing(tmp_path, monkeypatch):
    db_path = tmp_path / "zero.duckdb"
    _build_zero_conversion_duckdb(db_path)
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(db_path))
    monkeypatch.setenv("BUSINESS_ALERTS_ENABLED", "true")
    monkeypatch.setenv("ALERT_SEMANTIC_METRICS", "cpa,roas,ctr")

    # One enabled CPA alert definition: fire when cpa > 2.0.
    defn_row = ("alrt_CPA0", "proj1", "cpa", ">", 2.0, None)
    defn_desc = [
        ("id",), ("project_id",), ("metric",), ("operator",), ("threshold",), ("connector",),
    ]
    select_cur = _make_cursor(rows=[defn_row], description=defn_desc)
    conn = _make_conn(select_cur)

    # First cursor() call returns the SELECT cursor; any INSERT would use another.
    insert_cur = _make_cursor()
    conn.cursor = MagicMock(side_effect=[select_cur, insert_cur, insert_cur])

    with patch("core.db.get_connection", return_value=_conn_ctx(conn)):
        firings = business_alerts.evaluate_business_alerts(
            evaluation_date=date(2026, 7, 10)
        )

    # No firing produced (NULL CPA -> observed is None -> continue).
    assert firings == []
    # No INSERT into alert_firings was executed (only the SELECT ran).
    executed_sql = " ".join(
        str(c.args[0]) for c in select_cur.execute.call_args_list
    )
    assert "INSERT INTO app.alert_firings" not in executed_sql


def test_semantic_cpa_view_returns_null_on_zero(tmp_path):
    """Directly assert the NULLIF guard returns NULL for zero conversions."""
    db_path = tmp_path / "zero2.duckdb"
    _build_zero_conversion_duckdb(db_path)

    observed, pull_ids = business_alerts._query_semantic_metric(
        "proj1", "cpa", date(2026, 7, 10), None, str(db_path)
    )
    assert observed is None
    assert pull_ids == []
