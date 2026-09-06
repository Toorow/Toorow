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
    are not seeded -- an empty result would falsely claim "100 % du actual est mappé".
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


# ---------------------------------------------------------------------------
# AI-267 — l'argent ne se somme pas en flottant, meme hors des marts de pacing
# ---------------------------------------------------------------------------

def _seed_cost_fact(db_file, rows):
    """A minimal `fact_daily_kpi` carrying nothing but the cost rows under test."""
    import duckdb

    con = duckdb.connect(str(db_file))
    # `main_marts` is the schema the routed reader prefixes with; a table created
    # in `main` is invisible to it and the test would prove the degrade path
    # instead of the statement.
    con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
    con.execute(
        """
        CREATE TABLE main_marts.fact_daily_kpi (
            project_id VARCHAR, date DATE, connector VARCHAR, metric VARCHAR,
            breakdown_dimension VARCHAR, breakdown_value VARCHAR, value DOUBLE
        )
        """
    )
    con.executemany(
        "INSERT INTO main_marts.fact_daily_kpi VALUES (?, ?, ?, 'cost', 'campaign_id', ?, ?)",
        rows,
    )
    con.close()


def _warehouse_on(db_file, monkeypatch):
    import importlib

    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(db_file))
    from core import warehouse
    return importlib.reload(warehouse)


def test_window_spend_is_summed_in_exact_micros_not_in_doubles(tmp_path, monkeypatch):
    """Ten days at 0.10 are 1.00, and `SUM(CAST(value AS DOUBLE))` said otherwise.

    THIS IS THE DEFECT, not an illustration of it: 0.10 and 0.20 have no exact
    binary representation, so `SUM(CAST(value AS DOUBLE))` of the two lands on
    0.30000000000000004. The reader fed that number to the pacing comparison,
    where it is held against a media plan expressed to the cent -- a plan of 0.30
    read as overspent by a hair, on every campaign whose daily cost is not a
    binary fraction. Which is most of them.

    TWO ROWS, AND THAT IS DELIBERATE. Over ten rows the drifted total depends on
    how DuckDB parallelises the aggregation -- measured here: 0.9999999999999999
    in one process, 1.0 in another. A test that pinned THAT number would be
    pinning the query plan. Two terms are one addition, and one addition has no
    order.

    The repair is the one `dbt/macros/fee_tax_to_micros.sql` already states:
    normalise PER ROW to exact integer micros, then SUM BIGINTs. So the assertion
    is on `spend_micros`, and it is an EQUALITY on an integer -- not an
    `approx`, because there is nothing left to approximate.
    """
    db_file = tmp_path / "cost.duckdb"
    _seed_cost_fact(
        db_file,
        [
            ("default", "2026-03-01", "google-ads", "camp_1", 0.10),
            ("default", "2026-03-02", "google-ads", "camp_1", 0.20),
        ],
    )
    # WHAT WAS BEING SERVED, measured on these exact rows BEFORE the reader runs.
    # After it, the read-through cache of story 19.2 has touched the file and a
    # second connection no longer sees the same plan -- so the witness is taken
    # first, and it is the exact statement this reader ran until today.
    import duckdb

    con = duckdb.connect(str(db_file))
    drifted = con.execute(
        "SELECT SUM(CAST(value AS DOUBLE)) FROM main_marts.fact_daily_kpi"
    ).fetchone()[0]
    con.close()
    assert drifted == 0.30000000000000004

    warehouse = _warehouse_on(db_file, monkeypatch)

    rows = warehouse.query_campaign_spend("default", "2026-03-01", "2026-03-31")

    assert len(rows) == 1
    assert rows[0]["spend_micros"] == 300_000
    # The display value the callers have always read, derived ONCE from the exact
    # integer instead of accumulated.
    assert rows[0]["spend"] == 0.30
    assert rows[0]["spend"] != drifted


def test_daily_spend_carries_the_same_exact_micros(tmp_path, monkeypatch):
    """The per-day reader is the same statement one GROUP BY wider -- same rule.

    A drift repaired on the window total and left on the daily breakdown would
    make the two disagree, and the pacing screen shows them side by side.
    """
    db_file = tmp_path / "cost_daily.duckdb"
    _seed_cost_fact(
        db_file,
        [
            ("default", "2026-03-01", "google-ads", "camp_1", 0.07),
            ("default", "2026-03-01", "google-ads", "camp_1", 0.07),
            ("default", "2026-03-02", "google-ads", "camp_1", 0.29),
        ],
    )
    warehouse = _warehouse_on(db_file, monkeypatch)

    rows = warehouse.query_campaign_spend_daily("default", "2026-03-01", "2026-03-31")

    by_day = {row["day"]: row["spend_micros"] for row in rows}
    assert by_day == {"2026-03-01": 140_000, "2026-03-02": 290_000}


# ---------------------------------------------------------------------------
# Story 66.3 (AC 9) — what a warehouse job COST.
#
# `total_bytes_billed` lives on the `QueryJob`, never on the `RowIterator`, and
# `_query_bigquery` threw the job away the moment it called `.result()`. No
# caller could reach the figure, on any engine, whatever it asked.
# ---------------------------------------------------------------------------


def _fake_bigquery_job(rows, *, billed, cache_hit=False):
    schema = [MagicMock(name=name) for name in ("day",)]
    schema[0].name = "day"
    result = MagicMock()
    result.schema = schema
    result.__iter__ = lambda _self: iter(rows)
    job = MagicMock()
    job.result.return_value = result
    job.total_bytes_billed = billed
    job.cache_hit = cache_hit
    return job


def test_a_bigquery_job_reports_the_bytes_it_billed():
    from core import warehouse

    job = _fake_bigquery_job([("2026-08-01",)], billed=1_048_576)
    client = MagicMock()
    client.query.return_value = job
    with patch("google.cloud.bigquery.Client", return_value=client), patch.object(
        warehouse, "_gcp_project", return_value="toorow"
    ):
        rows, cost = warehouse.query_bigquery_measured("SELECT day FROM t", [])

    assert rows == [{"day": "2026-08-01"}]
    assert cost.engine == "bigquery"
    assert cost.billed_bytes == 1_048_576
    assert cost.billed_bytes_state == "exact"
    assert cost.cache_hit is False
    assert isinstance(cost.elapsed_ms, int)


def test_a_cache_hit_reports_zero_and_zero_is_not_a_measurement():
    """THE FIXTURE IS WHAT BIGQUERY RETURNS, and the first one was not.

    This test used to build `billed=None, cache_hit=True` and assert
    `unavailable` -- a combination the client does not produce. After
    `.result()` the statistics are present, so `total_bytes_billed` is
    `int("0")`, not `None`: the mock fabricated the premise and the assertion
    confirmed it, while the real cache hit fell into the `exact` branch and
    announced "this profile cost you zero bytes, measured".

    `0` from a cache hit is `unavailable`: the query was answered without
    touching a byte of the tables, so it says nothing about what the same
    question costs.
    """
    from core import warehouse

    job = _fake_bigquery_job([], billed=0, cache_hit=True)
    client = MagicMock()
    client.query.return_value = job
    with patch("google.cloud.bigquery.Client", return_value=client), patch.object(
        warehouse, "_gcp_project", return_value="toorow"
    ):
        _rows, cost = warehouse.query_bigquery_measured("SELECT day FROM t", [])

    assert cost.billed_bytes is None
    assert cost.billed_bytes_state == "unavailable"
    assert cost.cache_hit is True


def test_a_job_that_never_completed_is_unavailable_too():
    """`None` means the field was ABSENT -- a job that did not finish.

    Kept apart from the cache hit above because the two arrive by different
    roads and only one of them is a normal day.
    """
    from core import warehouse

    job = _fake_bigquery_job([], billed=None, cache_hit=None)
    client = MagicMock()
    client.query.return_value = job
    with patch("google.cloud.bigquery.Client", return_value=client), patch.object(
        warehouse, "_gcp_project", return_value="toorow"
    ):
        _rows, cost = warehouse.query_bigquery_measured("SELECT day FROM t", [])

    assert cost.billed_bytes is None
    assert cost.billed_bytes_state == "unavailable"


def test_a_real_scan_that_billed_zero_is_impossible_so_zero_alone_is_never_exact():
    """A non-cached job reporting 0 is BigQuery's free tier or a metadata-only
    read -- still a real answer about this query, so it stays `exact`.

    The discriminator is `cache_hit`, never the number: reading the number alone
    is what put a free job and an unmeasured job on the same row.
    """
    from core import warehouse

    job = _fake_bigquery_job([], billed=0, cache_hit=False)
    client = MagicMock()
    client.query.return_value = job
    with patch("google.cloud.bigquery.Client", return_value=client), patch.object(
        warehouse, "_gcp_project", return_value="toorow"
    ):
        _rows, cost = warehouse.query_bigquery_measured("SELECT day FROM t", [])

    assert cost.billed_bytes == 0
    assert cost.billed_bytes_state == "exact"


def test_the_unmeasured_bigquery_reader_still_returns_only_rows():
    """Every existing caller wants the rows alone; none of them changed."""
    from core import warehouse

    job = _fake_bigquery_job([("2026-08-01",)], billed=17)
    client = MagicMock()
    client.query.return_value = job
    with patch("google.cloud.bigquery.Client", return_value=client), patch.object(
        warehouse, "_gcp_project", return_value="toorow"
    ):
        assert warehouse._query_bigquery("SELECT day FROM t", []) == [{"day": "2026-08-01"}]


def test_duckdb_bills_no_bytes_and_says_so_rather_than_zero():
    from core import warehouse

    with patch.object(warehouse, "_query_duckdb", return_value=[{"day": "2026-08-01"}]):
        rows, cost = warehouse.query_duckdb_measured("SELECT day FROM t", [])

    assert rows == [{"day": "2026-08-01"}]
    assert cost.engine == "duckdb"
    assert cost.billed_bytes is None
    assert cost.billed_bytes_state == "not_applicable"
    assert isinstance(cost.elapsed_ms, int)
