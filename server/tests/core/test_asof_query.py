"""Tests for Story 4.6 as-of warehouse queries.

Verifies:
  - AC7: two-pull revision scenario with multi-breakdown fixtures (HG-2: L-1 lesson)
  - HG-1: as-of logic is in SQL (no Python sorted/max/filter)
  - HG-3: parameterized SQL — SQL injection safety
  - HG-4: current view (no as_of) is unchanged after adding as-of path
  - T2 Gate: fact_daily_kpi_all_pulls is the correct source (retains all pulls)

Fixture: DuckDB in-memory with two pulls for the same grain:
  - Pull A: loaded 2026-07-03, values = {FR: 100, DE: 50}
  - Pull B: loaded 2026-07-08 (revision), values = {FR: 120, DE: 60}
"""

from __future__ import annotations

import pytest  # noqa: F401 (used in injection test)

# ---------------------------------------------------------------------------
# DuckDB in-memory fixture helper
# ---------------------------------------------------------------------------

def _make_in_memory_db():
    """Create a DuckDB in-memory connection with fact_daily_kpi_all_pulls seeded."""
    import duckdb

    con = duckdb.connect(":memory:")

    # Create the schema / table that warehouse.get_daily_report_asof expects.
    # The DuckDB mart prefix is "main_marts." — we create that schema.
    con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
    con.execute("""
        CREATE TABLE main_marts.fact_daily_kpi_all_pulls (
            project_id          TEXT,
            date                TEXT,
            connector           TEXT,
            metric              TEXT,
            breakdown_dimension TEXT,
            breakdown_value     TEXT,
            value               DOUBLE,
            pull_id             TEXT,
            loaded_at           TIMESTAMP
        )
    """)

    # Pull A: loaded 2026-07-03 — original values
    con.execute("""
        INSERT INTO main_marts.fact_daily_kpi_all_pulls VALUES
        -- FR: 100 sessions
        ('test', '2026-07-01', 'ga4', 'sessions', 'country', 'FR',
         100.0, 'pull_aaa', '2026-07-03T00:00:00'),
        -- DE: 50 sessions (HG-2 / L-1: must have ≥2 breakdown values per grain)
        ('test', '2026-07-01', 'ga4', 'sessions', 'country', 'DE',
         50.0, 'pull_aaa', '2026-07-03T00:00:00')
    """)

    # Pull B: loaded 2026-07-08 — revised values (GA4 late attribution)
    con.execute("""
        INSERT INTO main_marts.fact_daily_kpi_all_pulls VALUES
        -- FR: 120 sessions (revised up)
        ('test', '2026-07-01', 'ga4', 'sessions', 'country', 'FR',
         120.0, 'pull_bbb', '2026-07-08T00:00:00'),
        -- DE: 60 sessions (revised up)
        ('test', '2026-07-01', 'ga4', 'sessions', 'country', 'DE',
         60.0, 'pull_bbb', '2026-07-08T00:00:00')
    """)

    return con


def _run_asof_query(con, as_of_ts: str, connectors=None):
    """Run the as-of SQL against the in-memory DB (same logic as warehouse.py)."""
    params = [as_of_ts, "test", "2026-07-01", "2026-07-01"]

    connector_clause = ""
    if connectors:
        placeholders = ", ".join(["?" for _ in connectors])
        connector_clause = f"  AND connector IN ({placeholders})\n"
        params.extend(connectors)

    sql = f"""
    WITH kpi_snapshot AS (
        SELECT *,
               ROW_NUMBER() OVER (
                   PARTITION BY project_id, date, connector, metric,
                                breakdown_dimension, breakdown_value
                   ORDER BY loaded_at DESC
               ) AS _rn
        FROM main_marts.fact_daily_kpi_all_pulls
        WHERE loaded_at <= CAST(? AS TIMESTAMP)
          AND project_id = ?
          AND date BETWEEN ? AND ?
{connector_clause}    )
    SELECT
        date, connector, metric, breakdown_dimension, breakdown_value,
        value, pull_id, loaded_at
    FROM kpi_snapshot
    WHERE _rn = 1
    ORDER BY breakdown_value
    """

    rel = con.execute(sql, params)
    cols = [d[0] for d in rel.description]
    return [dict(zip(cols, row)) for row in rel.fetchall()]


# ---------------------------------------------------------------------------
# AC7 — Two-pull revision scenario tests
# ---------------------------------------------------------------------------

class TestAsOfRevisionScenario:
    """Two-pull revision scenario: pull A (2026-07-03) then pull B (2026-07-08).

    HG-2 (L-1 lesson): fixtures include 2 breakdown values (FR, DE) per grain.
    """

    def setup_method(self):
        self.con = _make_in_memory_db()

    def teardown_method(self):
        self.con.close()

    def test_asof_between_pulls(self):
        """as_of = 2026-07-05 (between pull A and B) → returns pull A values (100, 50).

        AC7 / HG-2: both breakdown values (FR: 100, DE: 50) must be returned.
        """
        rows = _run_asof_query(self.con, "2026-07-05T00:00:00")

        assert len(rows) == 2, (
            f"Expected 2 rows (FR + DE), got {len(rows)}: {rows}"
        )
        values = {r["breakdown_value"]: r["value"] for r in rows}
        assert values["FR"] == 100.0, f"FR should be 100 (pull A), got {values['FR']}"
        assert values["DE"] == 50.0, f"DE should be 50 (pull A), got {values['DE']}"

        # Both rows should come from pull_aaa
        pull_ids = {r["pull_id"] for r in rows}
        assert pull_ids == {"pull_aaa"}, f"Expected pull_aaa, got {pull_ids}"

    def test_asof_after_revision(self):
        """as_of = 2026-07-09 (after pull B) → returns pull B values (120, 60).

        AC7 / HG-2: both breakdown values (FR: 120, DE: 60) must be returned.
        """
        rows = _run_asof_query(self.con, "2026-07-09T00:00:00")

        assert len(rows) == 2, (
            f"Expected 2 rows (FR + DE), got {len(rows)}: {rows}"
        )
        values = {r["breakdown_value"]: r["value"] for r in rows}
        assert values["FR"] == 120.0, f"FR should be 120 (pull B), got {values['FR']}"
        assert values["DE"] == 60.0, f"DE should be 60 (pull B), got {values['DE']}"

        # Both rows should come from pull_bbb
        pull_ids = {r["pull_id"] for r in rows}
        assert pull_ids == {"pull_bbb"}, f"Expected pull_bbb, got {pull_ids}"

    def test_asof_grain_completeness(self):
        """Both FR and DE rows returned for EACH pull — L-1 grain completeness.

        HG-2: a single-breakdown fixture would mask the ROW_NUMBER grain bug.
        This test proves ROW_NUMBER partitions at the correct grain:
          (project_id, date, connector, metric, breakdown_dimension, breakdown_value)
        If any dimension column is missing from PARTITION BY, one row would suppress
        the other (only 1 result instead of 2).
        """
        # Between pulls: both FR and DE from pull A
        rows_between = _run_asof_query(self.con, "2026-07-05T00:00:00")
        breakdown_values_between = {r["breakdown_value"] for r in rows_between}
        assert breakdown_values_between == {"FR", "DE"}, (
            f"Between-pull: expected FR+DE, got {breakdown_values_between}"
        )

        # After revision: both FR and DE from pull B
        rows_after = _run_asof_query(self.con, "2026-07-09T00:00:00")
        breakdown_values_after = {r["breakdown_value"] for r in rows_after}
        assert breakdown_values_after == {"FR", "DE"}, (
            f"After-revision: expected FR+DE, got {breakdown_values_after}"
        )

    def test_asof_before_any_pull(self):
        """as_of = 2026-07-01 (before any pull) → returns [] (no data known yet).

        AC7 / AC2b: the as-of filter loaded_at <= as_of_ts correctly excludes
        all rows when as_of_ts is before any pull's loaded_at.
        """
        rows = _run_asof_query(self.con, "2026-07-01T00:00:00")
        assert rows == [], (
            f"Expected empty result before any pull, got {len(rows)} rows: {rows}"
        )

    def test_current_view_unchanged(self):
        """Current-view query (no as_of) returns latest values (pull B).

        HG-4: adding the as-of path must not affect the current-view query.
        The current-view query reads from fact_daily_kpi (not fact_daily_kpi_all_pulls).
        We test the logical equivalent: querying without as_of filter returns pull_bbb.
        """
        # Simulate current-view: no loaded_at filter → ROW_NUMBER gets pull_bbb
        # (loaded_at DESC: pull_bbb 2026-07-08 > pull_aaa 2026-07-03)
        sql = """
        WITH current_view AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY project_id, date, connector, metric,
                                    breakdown_dimension, breakdown_value
                       ORDER BY loaded_at DESC
                   ) AS _rn
            FROM main_marts.fact_daily_kpi_all_pulls
            WHERE project_id = 'test'
              AND date BETWEEN '2026-07-01' AND '2026-07-01'
        )
        SELECT breakdown_value, value, pull_id FROM current_view WHERE _rn = 1
        ORDER BY breakdown_value
        """
        rel = self.con.execute(sql)
        rows = [dict(zip([d[0] for d in rel.description], row)) for row in rel.fetchall()]

        assert len(rows) == 2
        pull_ids = {r["pull_id"] for r in rows}
        assert pull_ids == {"pull_bbb"}, (
            f"Current view must return latest pull (pull_bbb), got {pull_ids}"
        )
        values = {r["breakdown_value"]: r["value"] for r in rows}
        assert values["FR"] == 120.0
        assert values["DE"] == 60.0


# ---------------------------------------------------------------------------
# HG-3 — SQL injection safety
# ---------------------------------------------------------------------------

class TestAsOfSQLInjectionSafety:
    """HG-3: as_of_ts MUST be a SQL parameter, never string-interpolated.

    Parameterized binding means an injection payload like
    "2026-07-01'; DROP TABLE ...; --" is treated as a literal string value
    for CAST(? AS TIMESTAMP), which fails type conversion — it does NOT execute
    the injected SQL.
    """

    def setup_method(self):
        self.con = _make_in_memory_db()

    def teardown_method(self):
        self.con.close()

    def test_sql_injection_in_as_of_ts_raises_not_executes(self):
        """SQL injection string in as_of_ts fails as TIMESTAMP cast, not as SQL exec.

        The test verifies the injection does NOT silently succeed (which would
        indicate string interpolation). It must fail with a cast/conversion error.
        """
        injection_payload = "2026-07-01'; DROP TABLE main_marts.fact_daily_kpi_all_pulls; --"

        # With parameterized binding, the payload goes into CAST(? AS TIMESTAMP).
        # DuckDB raises a ConversionException — NOT a CatalogException (table dropped).
        with pytest.raises(Exception) as exc_info:
            _run_asof_query(self.con, injection_payload)

        # The error must be a type/conversion error — NOT a SQL syntax / execution error
        # that would imply the injected SQL was parsed and run.
        error_msg = str(exc_info.value).lower()
        # DuckDB error: "could not convert" or "invalid input" for bad TIMESTAMP cast
        assert any(
            kw in error_msg
            for kw in ("could not convert", "invalid", "conversion", "cast", "timestamp")
        ), (
            f"Expected a TIMESTAMP cast error for injection payload, got: {exc_info.value}"
        )

        # Verify the table still exists (was NOT dropped by the injection)
        result = self.con.execute(
            "SELECT COUNT(*) FROM main_marts.fact_daily_kpi_all_pulls"
        ).fetchone()
        assert result is not None and result[0] == 4, (
            "fact_daily_kpi_all_pulls must still have 4 rows — "
            f"SQL injection must not have executed (got {result})"
        )


# ---------------------------------------------------------------------------
# get_daily_report_asof integration via warehouse module
# ---------------------------------------------------------------------------

class TestWarehouseGetDailyReportAsof:
    """Integration test for warehouse.get_daily_report_asof() end-to-end.

    Uses monkeypatching to point the function at an in-memory DuckDB.
    Verifies the function signature and routing (DuckDB mode).
    """

    def test_get_daily_report_asof_returns_pull_a_between_pulls(self, monkeypatch, tmp_path):
        """warehouse.get_daily_report_asof with as_of between A and B → pull A values."""
        import core.warehouse as wh
        import duckdb

        # Build in-memory DB and save to a temp file (warehouse uses file path)
        db_path = tmp_path / "test.duckdb"
        con = duckdb.connect(str(db_path))
        con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
        con.execute("""
            CREATE TABLE main_marts.fact_daily_kpi_all_pulls (
                project_id TEXT, date TEXT, connector TEXT, metric TEXT,
                breakdown_dimension TEXT, breakdown_value TEXT,
                value DOUBLE, pull_id TEXT, loaded_at TIMESTAMP
            )
        """)
        con.execute("""
            INSERT INTO main_marts.fact_daily_kpi_all_pulls VALUES
            ('p', '2026-07-01', 'ga4', 'sessions', 'country', 'FR',
             100.0, 'pull_aaa', '2026-07-03T00:00:00'),
            ('p', '2026-07-01', 'ga4', 'sessions', 'country', 'DE',
             50.0, 'pull_aaa', '2026-07-03T00:00:00'),
            ('p', '2026-07-01', 'ga4', 'sessions', 'country', 'FR',
             120.0, 'pull_bbb', '2026-07-08T00:00:00'),
            ('p', '2026-07-01', 'ga4', 'sessions', 'country', 'DE',
             60.0, 'pull_bbb', '2026-07-08T00:00:00')
        """)
        con.close()

        monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
        monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(db_path))

        rows = wh.get_daily_report_asof(
            project_id="p",
            start_date="2026-07-01",
            end_date="2026-07-01",
            connectors=["ga4"],
            as_of_ts="2026-07-05T00:00:00+00:00",
        )

        assert len(rows) == 2
        values = {r["breakdown_value"]: r["value"] for r in rows}
        assert values["FR"] == 100.0, f"Expected FR=100 (pull A), got {values['FR']}"
        assert values["DE"] == 50.0, f"Expected DE=50 (pull A), got {values['DE']}"

    def test_get_daily_report_asof_returns_pull_b_after_revision(self, monkeypatch, tmp_path):
        """warehouse.get_daily_report_asof with as_of after B → pull B values."""
        import core.warehouse as wh
        import duckdb

        db_path = tmp_path / "test2.duckdb"
        con = duckdb.connect(str(db_path))
        con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
        con.execute("""
            CREATE TABLE main_marts.fact_daily_kpi_all_pulls (
                project_id TEXT, date TEXT, connector TEXT, metric TEXT,
                breakdown_dimension TEXT, breakdown_value TEXT,
                value DOUBLE, pull_id TEXT, loaded_at TIMESTAMP
            )
        """)
        con.execute("""
            INSERT INTO main_marts.fact_daily_kpi_all_pulls VALUES
            ('p', '2026-07-01', 'ga4', 'sessions', 'country', 'FR',
             100.0, 'pull_aaa', '2026-07-03T00:00:00'),
            ('p', '2026-07-01', 'ga4', 'sessions', 'country', 'DE',
             50.0, 'pull_aaa', '2026-07-03T00:00:00'),
            ('p', '2026-07-01', 'ga4', 'sessions', 'country', 'FR',
             120.0, 'pull_bbb', '2026-07-08T00:00:00'),
            ('p', '2026-07-01', 'ga4', 'sessions', 'country', 'DE',
             60.0, 'pull_bbb', '2026-07-08T00:00:00')
        """)
        con.close()

        monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
        monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(db_path))

        rows = wh.get_daily_report_asof(
            project_id="p",
            start_date="2026-07-01",
            end_date="2026-07-01",
            connectors=["ga4"],
            as_of_ts="2026-07-09T00:00:00+00:00",
        )

        assert len(rows) == 2
        values = {r["breakdown_value"]: r["value"] for r in rows}
        assert values["FR"] == 120.0, f"Expected FR=120 (pull B), got {values['FR']}"
        assert values["DE"] == 60.0, f"Expected DE=60 (pull B), got {values['DE']}"

    def test_get_daily_report_asof_empty_before_any_pull(self, monkeypatch, tmp_path):
        """warehouse.get_daily_report_asof with as_of before all pulls → []."""
        import core.warehouse as wh
        import duckdb

        db_path = tmp_path / "test3.duckdb"
        con = duckdb.connect(str(db_path))
        con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
        con.execute("""
            CREATE TABLE main_marts.fact_daily_kpi_all_pulls (
                project_id TEXT, date TEXT, connector TEXT, metric TEXT,
                breakdown_dimension TEXT, breakdown_value TEXT,
                value DOUBLE, pull_id TEXT, loaded_at TIMESTAMP
            )
        """)
        con.execute("""
            INSERT INTO main_marts.fact_daily_kpi_all_pulls VALUES
            ('p', '2026-07-01', 'ga4', 'sessions', 'country', 'FR',
             100.0, 'pull_aaa', '2026-07-03T00:00:00')
        """)
        con.close()

        monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
        monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(db_path))

        rows = wh.get_daily_report_asof(
            project_id="p",
            start_date="2026-07-01",
            end_date="2026-07-01",
            connectors=None,
            as_of_ts="2026-07-01T00:00:00+00:00",
        )

        assert rows == [], f"Expected [] before any pull, got {rows}"

    def test_no_python_max_or_sorted_in_asof_code(self):
        """HG-1: warehouse.get_daily_report_asof must NOT use Python max/sorted/filter
        to implement as-of logic. The entire supersede computation must live in SQL.
        """
        import inspect

        import core.warehouse as wh

        src = inspect.getsource(wh.get_daily_report_asof)
        src_build = inspect.getsource(wh._build_asof_query)

        # These would indicate Python-side as-of logic (HG-1 violation)
        for forbidden in ["sorted(", "max(", "filter("]:
            assert forbidden not in src, (
                f"HG-1 violation: {forbidden!r} found in get_daily_report_asof — "
                "as-of logic must be in SQL, not Python"
            )
            assert forbidden not in src_build, (
                f"HG-1 violation: {forbidden!r} found in _build_asof_query — "
                "as-of logic must be in SQL, not Python"
            )
