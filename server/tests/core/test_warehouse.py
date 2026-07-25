"""Tests for core.warehouse — Story 1.5, T2.6.

Covers:
  * Happy path: rows returned from mocked DuckDB.
  * Empty result when DuckDB path does not exist (warehouse_not_ready).
  * BigQuery error path returns empty list with structured warning.
  * Connector filter is applied in SQL.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_rows() -> list[dict]:
    return [
        {
            "date": "2026-04-01",
            "connector": "my-connector",
            "metric": "sessions",
            "breakdown_dimension": "device_category",
            "breakdown_value": "desktop",
            "value": 1234.0,
            "pull_id": "pull_01TEST",
            "loaded_at": "2026-06-30T12:00:00",
        }
    ]


# ---------------------------------------------------------------------------
# DuckDB path — happy path via mock
# ---------------------------------------------------------------------------

def test_query_duckdb_returns_rows(tmp_path, monkeypatch):
    """Happy path: DuckDB returns rows (T2.5)."""
    # Create a stub .duckdb file so the existence check passes
    db_file = tmp_path / "test.duckdb"
    db_file.touch()

    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(db_file))

    mock_rows = _fake_rows()

    mock_con = MagicMock()
    mock_rel = MagicMock()
    mock_rel.description = [
        ("date",), ("connector",), ("metric",), ("breakdown_dimension",),
        ("breakdown_value",), ("value",), ("pull_id",), ("loaded_at",),
    ]
    mock_rel.fetchall.return_value = [
        (r["date"], r["connector"], r["metric"], r["breakdown_dimension"],
         r["breakdown_value"], r["value"], r["pull_id"], r["loaded_at"])
        for r in mock_rows
    ]
    mock_con.execute.return_value = mock_rel
    mock_con.__enter__ = MagicMock(return_value=mock_con)
    mock_con.__exit__ = MagicMock(return_value=False)

    with patch("duckdb.connect", return_value=mock_con):
        # Reload to pick up env changes
        import importlib

        from core import warehouse
        importlib.reload(warehouse)
        from core.warehouse import query_daily_report
        importlib.reload(warehouse)

        result = query_daily_report(
            project_id="default",
            start_date="2026-04-01",
            end_date="2026-06-30",
            connectors=None,
        )

    assert len(result) == 1
    assert result[0]["connector"] == "my-connector"
    assert result[0]["metric"] == "sessions"


def test_query_duckdb_empty_when_no_db(tmp_path, monkeypatch, caplog):
    """Empty list returned when DuckDB file does not exist (T2.4)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "nonexistent.duckdb"))

    import importlib

    from core import warehouse
    importlib.reload(warehouse)
    import logging

    from core.warehouse import query_daily_report
    with caplog.at_level(logging.WARNING):
        result = query_daily_report("default", "2026-01-01", "2026-03-31", None)

    assert result == []
    assert "warehouse_not_ready" in caplog.text


def test_query_connector_filter_applied(tmp_path, monkeypatch):
    """Connector filter IN clause appears in the SQL executed (T2.2)."""
    db_file = tmp_path / "test.duckdb"
    db_file.touch()

    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(db_file))

    executed_sqls: list[str] = []

    mock_con = MagicMock()
    mock_rel = MagicMock()
    mock_rel.description = [
        ("date",), ("connector",), ("metric",), ("breakdown_dimension",),
        ("breakdown_value",), ("value",), ("pull_id",), ("loaded_at",),
    ]
    mock_rel.fetchall.return_value = []

    def capture_execute(sql, params=None):
        executed_sqls.append((sql, params))
        return mock_rel

    mock_con.execute.side_effect = capture_execute

    with patch("duckdb.connect", return_value=mock_con):
        import importlib

        from core import warehouse
        importlib.reload(warehouse)
        from core.warehouse import query_daily_report

        query_daily_report("default", "2026-01-01", "2026-03-31", ["my-connector"])

    assert executed_sqls, "DuckDB execute was not called"
    sql, params = executed_sqls[0]
    # review-1-5 F-01: values are BOUND, never interpolated into the SQL text
    assert "my-connector" not in sql
    assert "my-connector" in params
    assert "IN" in sql


# ---------------------------------------------------------------------------
# Story 22.3 review F-4 — query_campaign_spend must NOT degrade to a silent []
# ---------------------------------------------------------------------------

def test_query_campaign_spend_raises_when_duckdb_file_absent(tmp_path, monkeypatch):
    """review F-4: a missing .duckdb file -> WarehouseUnavailable, never a silent [].

    The [Unmapped Actuals] perimeter must surface an honest error when the marts
    are not seeded -- an empty result would falsely claim "100 % du réel est mappé".
    """
    import importlib

    import pytest

    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "nonexistent.duckdb"))

    from core import warehouse
    importlib.reload(warehouse)

    with pytest.raises(warehouse.WarehouseUnavailable):
        warehouse.query_campaign_spend("default", "2026-03-01", "2026-03-31")


def test_query_campaign_spend_raises_when_fact_relation_absent(tmp_path, monkeypatch):
    """review F-4: file present but fact_daily_kpi relation absent -> WarehouseUnavailable.

    A genuine empty-but-present fact table is an honest "no spend" ([]), but a MISSING
    relation means the marts are not built -> honest error, not a fake empty perimeter.
    """
    import importlib

    import pytest

    db_file = tmp_path / "empty.duckdb"
    # Create a real, empty DuckDB file (no fact_daily_kpi relation).
    import duckdb
    con = duckdb.connect(str(db_file))
    con.close()

    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(db_file))

    from core import warehouse
    importlib.reload(warehouse)

    with pytest.raises(warehouse.WarehouseUnavailable):
        warehouse.query_campaign_spend("default", "2026-03-01", "2026-03-31")
