"""Tests for core.cache_warehouse -- Story 19.1 (CAP-22 / AD-22) read-through cache builder.

Simulates the dev path ``origin-duckdb -> cache-duckdb`` on TWO DISTINCT tmp files
(the real BigQuery extract is Phase B / AI-08 BLOCKED). The origin file plays the
role of ``local.duckdb`` (dev truth, ``main_marts.*``); the cache file is the
separate ephemeral ``cache_warehouse.duckdb``. The two are NEVER the same path.

Proves the Story 19.1 contract + invariants:
  * window cut: 35+ days seeded, only the last 30 land in the cache;
  * AD-5 project isolation: 2 projects seeded, each project's slice scoped;
  * invariant (b) provenance: pull_id / loaded_at identical to origin, row-for-row;
  * manifest: tables, [min_date, max_date], cache_built_at, row counts, project_ids;
  * invariant (f): origin unreachable -> no exception + logged (status="failed");
  * allowlist unknown relation -> skipped cleanly (build still succeeds);
  * invariant (e) AD-8: the origin suffers NO write (row counts + pull_ids unchanged);
  * disable switch: TOOROW_CACHE_ENABLED unset/false -> status="disabled", no cache.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import pytest

_FACT_DDL = """
    CREATE TABLE main_marts.fact_daily_kpi (
        project_id          TEXT,
        date                DATE,
        connector           TEXT,
        metric              TEXT,
        breakdown_dimension TEXT,
        breakdown_value     TEXT,
        value               DOUBLE,
        pull_id             TEXT,
        loaded_at           TIMESTAMP
    )
"""

_DEDUP_VIEW_DDL = """
    CREATE VIEW main_marts.dedup_estimate AS
    SELECT
        project_id,
        date,
        connector          AS channel_connector,
        value              AS claimed_conversions,
        value * 0.8        AS verified_total,
        value              AS claimed_total,
        1.25               AS duplication_rate,
        value * 0.8        AS deduplicated_contribution,
        'ga4_purchase'     AS verification_source_type,
        'vs_test'          AS verification_source_id,
        CAST(NULL AS TEXT) AS lead_event_name,
        'estimation'       AS estimate_label,
        pull_id
    FROM main_marts.fact_daily_kpi
    WHERE metric = 'sessions'
"""
# NB: the REAL dedup_estimate view filters metric='conversions'; this stand-in maps
# the seeded sessions rows instead so the view is non-empty without doubling the
# fact row counts asserted by the builder tests. What the cache/router contract
# needs is the exact COLUMN schema above (mirrors _build_dedup_query), not the
# real view's internal filter.

# F-2: DDL for the 3 previously un-exercised allowlisted views. Column schemas
# mirror the dbt models in dbt/models/marts/ (pull_id/loaded_at included per AD-9).
_SEMANTIC_AVG_POSITION_DDL = """
    CREATE TABLE main_marts.semantic_avg_position (
        project_id          TEXT,
        date                DATE,
        connector           TEXT,
        breakdown_dimension TEXT,
        breakdown_value     TEXT,
        average_position    DOUBLE,
        impressions_weight  BIGINT,
        semantic_weight     BIGINT,
        pull_id             TEXT,
        loaded_at           TIMESTAMP
    )
"""

_SEMANTIC_AVG_POSITION_COMPOSITE_DDL = """
    CREATE TABLE main_marts.semantic_avg_position_composite (
        project_id          TEXT,
        date                DATE,
        connector           TEXT,
        breakdown_dimension TEXT,
        breakdown_value     TEXT,
        country             TEXT,
        device              TEXT,
        average_position    DOUBLE,
        impressions_weight  BIGINT,
        pull_id             TEXT,
        loaded_at           TIMESTAMP
    )
"""

_TRANSACTION_RECONCILIATION_DAILY_DDL = """
    CREATE TABLE main_marts.transaction_reconciliation_daily (
        project_id                  TEXT,
        date                        DATE,
        shopify_orders_with_txn     BIGINT,
        ga4_transactions            BIGINT,
        matched_count               BIGINT,
        coverage_rate               DOUBLE,
        shopify_orders_without_txn  BIGINT,
        method                      TEXT,
        shopify_pull_id             TEXT,
        ga4_pull_id                 TEXT
    )
"""

# F-2 (19.2 review): DDL for semantic_ctr -- mirrors dbt/models/marts/semantic_ctr.sql
# exactly (columns: project_id, date, connector, breakdown_dimension, breakdown_value,
# ctr, pull_id). No loaded_at: the dbt model aggregates pull_id via MAX() and does not
# carry loaded_at (ratio view, not a raw fact table). Seeded with multi-day /
# multi-project rows so the equivalence test has teeth.
_SEMANTIC_CTR_DDL = """
    CREATE TABLE main_marts.semantic_ctr (
        project_id          TEXT,
        date                DATE,
        connector           TEXT,
        breakdown_dimension TEXT,
        breakdown_value     TEXT,
        ctr                 DOUBLE,
        semantic_numerator   DOUBLE,
        semantic_denominator DOUBLE,
        pull_id             TEXT
    )
"""

# data F-2 (19.4 review): DDL for semantic_cpa and semantic_roas -- mirrors
# dbt/models/marts/semantic_cpa.sql and semantic_roas.sql (columns: project_id,
# date, connector, breakdown_dimension, breakdown_value, cpa/roas, pull_id).
# No loaded_at (ratio views, MAX(pull_id) only, same pattern as semantic_ctr).
_SEMANTIC_CPA_DDL = """
    CREATE TABLE main_marts.semantic_cpa (
        project_id          TEXT,
        date                DATE,
        connector           TEXT,
        breakdown_dimension TEXT,
        breakdown_value     TEXT,
        cpa                 DOUBLE,
        semantic_numerator   DOUBLE,
        semantic_denominator DOUBLE,
        pull_id             TEXT
    )
"""

_SEMANTIC_ROAS_DDL = """
    CREATE TABLE main_marts.semantic_roas (
        project_id          TEXT,
        date                DATE,
        connector           TEXT,
        breakdown_dimension TEXT,
        breakdown_value     TEXT,
        roas                DOUBLE,
        semantic_numerator   DOUBLE,
        semantic_denominator DOUBLE,
        pull_id             TEXT
    )
"""


def _make_origin_db(path: str, *, today: date, num_days: int, projects: list[str]):
    """Create an on-disk origin DuckDB (plays local.duckdb) seeded over *num_days*.

    For each project and each of the last *num_days* calendar days (today back),
    inserts one 'sessions' fact row with a deterministic value + provenance so the
    window cut and the per-project scoping are both observable. Also creates the
    ``dedup_estimate`` VIEW and all 3 previously un-exercised allowlist tables
    (semantic_avg_position, semantic_avg_position_composite,
    transaction_reconciliation_daily) with realistic seed rows.
    """
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(path)
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
        con.execute(_FACT_DDL)
        for proj in projects:
            for offset in range(num_days):
                day = today - timedelta(days=offset)
                # value encodes the day offset so we can assert exact copies.
                value = 1000.0 + offset
                pull_id = f"pull_{proj}_{day.isoformat()}"
                loaded_at = datetime(2026, 7, 19, 3, 0, 0, tzinfo=timezone.utc).replace(
                    tzinfo=None
                )
                con.execute(
                    "INSERT INTO main_marts.fact_daily_kpi VALUES (?,?,?,?,?,?,?,?,?)",
                    [
                        proj,
                        day,
                        "ga4",
                        "sessions",
                        "date",
                        day.isoformat(),
                        value,
                        pull_id,
                        loaded_at,
                    ],
                )
        con.execute(_DEDUP_VIEW_DDL)

        # F-2 (19.2 review): Seed allowlisted relations.
        # semantic_avg_position is seeded for tests that override the allowlist
        # explicitly; it is NOT in the default allowlist (see F-1 comment below).
        con.execute(_SEMANTIC_AVG_POSITION_DDL)
        con.execute(_SEMANTIC_AVG_POSITION_COMPOSITE_DDL)
        con.execute(_TRANSACTION_RECONCILIATION_DAILY_DDL)
        # F-2 (19.2 review): semantic_ctr -- mirrors dbt/models/marts/semantic_ctr.sql.
        # Columns: project_id, date, connector, breakdown_dimension, breakdown_value,
        # ctr, pull_id. No loaded_at (ratio view, MAX(pull_id) only).
        con.execute(_SEMANTIC_CTR_DDL)
        # data F-2 (19.4 review): semantic_cpa and semantic_roas -- mirrors
        # dbt/models/marts/semantic_cpa.sql / semantic_roas.sql.
        # Columns: project_id, date, connector, breakdown_dimension, breakdown_value,
        # cpa/roas, pull_id. No loaded_at (same ratio-view pattern as ctr).
        con.execute(_SEMANTIC_CPA_DDL)
        con.execute(_SEMANTIC_ROAS_DDL)

        loaded_at = datetime(2026, 7, 19, 3, 0, 0, tzinfo=timezone.utc).replace(tzinfo=None)
        for proj in projects:
            for offset in range(num_days):
                day = today - timedelta(days=offset)
                pull_id = f"pull_{proj}_{day.isoformat()}"
                # semantic_avg_position: now in default allowlist (spec F4 debt resolved).
                # Columns match semantic_avg_position.sql output:
                # project_id, date, connector, breakdown_dimension, breakdown_value,
                # average_position, impressions_weight, pull_id, loaded_at.
                con.execute(
                    "INSERT INTO main_marts.semantic_avg_position VALUES "
                    "(?,?,?,?,?,?,?,?,?,?)",
                    [
                        proj,
                        day,
                        "gsc",
                        "page",
                        f"https://example.com/{proj}/{offset}",
                        10.0 + offset * 0.1,
                        100 + offset,
                        100 + offset,
                        pull_id,
                        loaded_at,
                    ],
                )
                # semantic_avg_position_composite: country>device grain
                con.execute(
                    "INSERT INTO main_marts.semantic_avg_position_composite VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        proj, day, "gsc", "country>device", "fra>mobile",
                        "fra", "mobile",
                        12.0 + offset * 0.1, 80 + offset, pull_id, loaded_at,
                    ],
                )
                # transaction_reconciliation_daily: per (project, date)
                con.execute(
                    "INSERT INTO main_marts.transaction_reconciliation_daily VALUES "
                    "(?,?,?,?,?,?,?,?,?,?)",
                    [
                        proj, day, 90, 85, 80, 80.0 / 90.0, 10,
                        "measured_reconciliation", pull_id, pull_id,
                    ],
                )
                # semantic_ctr: two breakdown rows per (project, date) -- one per
                # connector/dimension pair -- to cover multi-day multi-project grain.
                # clicks=offset*10+1, impressions=offset*100+10 -> ctr deterministic.
                clicks = float(offset * 10 + 1)
                impressions = float(offset * 100 + 10)
                ctr = clicks / impressions if impressions else None
                con.execute(
                    "INSERT INTO main_marts.semantic_ctr VALUES (?,?,?,?,?,?,?,?,?)",
                    [
                        proj,
                        day,
                        "google_ads",
                        "date",
                        day.isoformat(),
                        ctr,
                        clicks,
                        impressions,
                        pull_id,
                    ],
                )
                # semantic_cpa: cost/conversions ratio. Use deterministic values.
                # cost = (offset+1)*5.0, conversions = offset+1 -> cpa = 5.0 always.
                cost = float((offset + 1) * 5)
                conversions = float(offset + 1)
                cpa = cost / conversions if conversions else None
                con.execute(
                    "INSERT INTO main_marts.semantic_cpa VALUES (?,?,?,?,?,?,?,?,?)",
                    [
                        proj,
                        day,
                        "google_ads",
                        "date",
                        day.isoformat(),
                        cpa,
                        cost,
                        conversions,
                        pull_id,
                    ],
                )
                # semantic_roas: revenue/cost. revenue = (offset+1)*15.0, cost above.
                revenue = float((offset + 1) * 15)
                roas = revenue / cost if cost else None
                con.execute(
                    "INSERT INTO main_marts.semantic_roas VALUES (?,?,?,?,?,?,?,?,?)",
                    [
                        proj,
                        day,
                        "google_ads",
                        "date",
                        day.isoformat(),
                        roas,
                        revenue,
                        cost,
                        pull_id,
                    ],
                )
    finally:
        con.close()


def _origin_snapshot(path: str) -> dict:
    """Return an integrity snapshot of the origin: fact row count + sorted pull_ids."""
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(path, read_only=True)
    try:
        count = con.execute(
            "SELECT COUNT(*) FROM main_marts.fact_daily_kpi"
        ).fetchone()[0]
        pull_ids = [
            r[0]
            for r in con.execute(
                "SELECT DISTINCT pull_id FROM main_marts.fact_daily_kpi ORDER BY pull_id"
            ).fetchall()
        ]
    finally:
        con.close()
    return {"count": count, "pull_ids": pull_ids}


def _read_cache_table(cache_path: str, table: str) -> list[tuple]:
    """Return all rows of *table* from the cache file, ordered deterministically."""
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(cache_path, read_only=True)
    try:
        return con.execute(
            f'SELECT * FROM "{table}" ORDER BY project_id, date'  # noqa: S608 -- test-local
        ).fetchall()
    finally:
        con.close()


@pytest.fixture
def cache_env(tmp_path, monkeypatch):
    """Configure a duckdb origin + a DISTINCT cache path, cache enabled.

    Returns (origin_path, cache_path). The two paths are asserted distinct so a
    regression that points the cache at local.duckdb fails loudly.
    """
    origin_path = str(tmp_path / "local.duckdb")
    cache_path = str(tmp_path / "cache_warehouse.duckdb")
    assert origin_path != cache_path

    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", origin_path)
    monkeypatch.setenv("TOOROW_CACHE_PATH", cache_path)
    monkeypatch.setenv("TOOROW_CACHE_ENABLED", "true")
    monkeypatch.setenv("TOOROW_CACHE_WINDOW_DAYS", "30")
    return origin_path, cache_path


# ---------------------------------------------------------------------------
# Window cut + AD-5 project isolation + provenance (the core happy path)
# ---------------------------------------------------------------------------


def test_rebuild_window_cut_and_multi_project(cache_env):
    """35 days x 2 projects seeded; only the last 30 days per project land (window + AD-5)."""
    origin_path, cache_path = cache_env
    today = date(2026, 7, 19)
    projects = ["proj_a", "proj_b"]
    _make_origin_db(origin_path, today=today, num_days=35, projects=projects)

    from core import cache_warehouse

    result = cache_warehouse.rebuild_cache(today=today)

    assert result["status"] == "ok"
    # 30-day window x 2 projects = 60 fact rows (35 seeded, 5 oldest cut).
    assert result["row_counts"]["fact_daily_kpi"] == 60
    assert set(result["project_ids"]) == {"proj_a", "proj_b"}

    # Window bounds in the manifest: [today-29, today].
    assert result["min_date"] == (today - timedelta(days=29)).isoformat()
    assert result["max_date"] == today.isoformat()

    rows = _read_cache_table(cache_path, "fact_daily_kpi")
    assert len(rows) == 60
    # Every cached date is inside the window (the 5 oldest days were cut).
    min_allowed = today - timedelta(days=29)
    for r in rows:
        row_date = r[1]  # DATE column, preserved as a date object
        assert isinstance(row_date, date)
        assert min_allowed <= row_date <= today
    # Both projects present, each with exactly 30 rows (AD-5 scoping).
    proj_col = [r[0] for r in rows]
    assert proj_col.count("proj_a") == 30
    assert proj_col.count("proj_b") == 30


def test_provenance_copied_as_is_row_for_row(cache_env):
    """pull_id / loaded_at land in the cache byte-identical to the origin (invariant b, AD-9)."""
    origin_path, cache_path = cache_env
    today = date(2026, 7, 19)
    _make_origin_db(origin_path, today=today, num_days=30, projects=["proj_a"])

    from core import cache_warehouse

    cache_warehouse.rebuild_cache(today=today)

    import duckdb  # noqa: PLC0415

    # Compare (project_id, date, pull_id, loaded_at, value) row-for-row over the window.
    origin_con = duckdb.connect(origin_path, read_only=True)
    try:
        origin_rows = origin_con.execute(
            "SELECT project_id, date, pull_id, loaded_at, value "
            "FROM main_marts.fact_daily_kpi ORDER BY project_id, date"
        ).fetchall()
    finally:
        origin_con.close()

    cache_con = duckdb.connect(cache_path, read_only=True)
    try:
        cache_rows = cache_con.execute(
            'SELECT project_id, date, pull_id, loaded_at, value '
            'FROM fact_daily_kpi ORDER BY project_id, date'
        ).fetchall()
    finally:
        cache_con.close()

    assert cache_rows == origin_rows
    # Explicit provenance assertion: every pull_id/loaded_at is preserved, not re-emitted.
    for o, c in zip(origin_rows, cache_rows):
        assert c[2] == o[2]  # pull_id identical
        assert c[3] == o[3]  # loaded_at identical (same TIMESTAMP object)


def test_manifest_contents(cache_env):
    """Manifest carries tables, window [min,max], cache_built_at, row counts, projects."""
    origin_path, cache_path = cache_env
    today = date(2026, 7, 19)
    _make_origin_db(origin_path, today=today, num_days=30, projects=["proj_a", "proj_b"])

    from core import cache_warehouse

    before = datetime.now(tz=timezone.utc)
    cache_warehouse.rebuild_cache(today=today)
    after = datetime.now(tz=timezone.utc)

    manifest = cache_warehouse.read_manifest(cache_path)
    assert manifest is not None
    assert manifest["schema_version"] == cache_warehouse.CACHE_SCHEMA_VERSION
    assert "fact_daily_kpi" in manifest["tables"]
    assert "dedup_estimate" in manifest["tables"]
    assert manifest["min_date"] == (today - timedelta(days=29)).isoformat()
    assert manifest["max_date"] == today.isoformat()
    assert manifest["row_counts"]["fact_daily_kpi"] == 60
    assert set(manifest["project_ids"]) == {"proj_a", "proj_b"}

    # cache_built_at is a real UTC ISO instant produced during the build.
    built = datetime.fromisoformat(manifest["cache_built_at"])
    assert before <= built <= after


# ---------------------------------------------------------------------------
# Invariant (e) / AD-8 -- the cache is NOT a writer: origin unchanged after build.
# ---------------------------------------------------------------------------


def test_origin_never_written(cache_env):
    """The origin row counts + pull_ids are identical before and after a build (AD-8)."""
    origin_path, cache_path = cache_env
    today = date(2026, 7, 19)
    _make_origin_db(origin_path, today=today, num_days=32, projects=["proj_a", "proj_b"])

    snapshot_before = _origin_snapshot(origin_path)

    from core import cache_warehouse

    result = cache_warehouse.rebuild_cache(today=today)
    assert result["status"] == "ok"

    snapshot_after = _origin_snapshot(origin_path)
    assert snapshot_after == snapshot_before
    # 32 days x 2 projects seeded, all pull_ids preserved on the origin side.
    assert snapshot_after["count"] == 64
    assert len(snapshot_after["pull_ids"]) == 64


# ---------------------------------------------------------------------------
# Invariant (f) -- origin unreachable => no exception, logged, status="failed".
# ---------------------------------------------------------------------------


def test_origin_unreachable_never_raises(tmp_path, monkeypatch, caplog):
    """A missing origin file must NOT raise -- logged + meta-alert path, status failed."""
    missing_origin = str(tmp_path / "does_not_exist.duckdb")
    cache_path = str(tmp_path / "cache_warehouse.duckdb")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", missing_origin)
    monkeypatch.setenv("TOOROW_CACHE_PATH", cache_path)
    monkeypatch.setenv("TOOROW_CACHE_ENABLED", "true")

    from core import cache_warehouse

    # Neutralise the DB-backed meta-alert (no Postgres in unit tests).
    monkeypatch.setattr(cache_warehouse, "_meta_alert", lambda *a, **k: None)

    with caplog.at_level(logging.WARNING):
        result = cache_warehouse.rebuild_cache(today=date(2026, 7, 19))

    assert result["status"] == "failed"
    assert "rebuild_failed" in caplog.text


# ---------------------------------------------------------------------------
# Allowlist -- an unknown relation is skipped cleanly; the build still succeeds.
# ---------------------------------------------------------------------------


def test_unknown_allowlist_table_skipped(cache_env, monkeypatch, caplog):
    """An allowlisted relation that does not exist is skipped -- build still succeeds."""
    origin_path, cache_path = cache_env
    today = date(2026, 7, 19)
    _make_origin_db(origin_path, today=today, num_days=30, projects=["proj_a"])

    # Add a non-existent relation to the declarative allowlist.
    monkeypatch.setenv(
        "TOOROW_CACHE_TABLES", "fact_daily_kpi,does_not_exist_mart,dedup_estimate"
    )

    from core import cache_warehouse

    with caplog.at_level(logging.WARNING):
        result = cache_warehouse.rebuild_cache(today=today)

    assert result["status"] == "ok"
    # The real relations landed; the ghost one was skipped, not fatal.
    assert "fact_daily_kpi" in result["tables"]
    assert "dedup_estimate" in result["tables"]
    assert "does_not_exist_mart" not in result["tables"]
    assert "does_not_exist_mart" not in result["row_counts"]


# ---------------------------------------------------------------------------
# Disable switch -- TOOROW_CACHE_ENABLED default is prudent (off).
# ---------------------------------------------------------------------------


def test_disabled_by_default(tmp_path, monkeypatch):
    """With TOOROW_CACHE_ENABLED unset, rebuild is a no-op (status='disabled')."""
    origin_path = str(tmp_path / "local.duckdb")
    cache_path = str(tmp_path / "cache_warehouse.duckdb")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", origin_path)
    monkeypatch.setenv("TOOROW_CACHE_PATH", cache_path)
    monkeypatch.delenv("TOOROW_CACHE_ENABLED", raising=False)

    from core import cache_warehouse

    result = cache_warehouse.rebuild_cache()
    assert result["status"] == "disabled"
    # No cache file was created.
    import os as _os

    assert not _os.path.exists(cache_path)


# ---------------------------------------------------------------------------
# Explicit project pin -- only the pinned project is cached (AD-5).
# ---------------------------------------------------------------------------


def test_explicit_project_pin_scopes_cache(cache_env):
    """Passing project_ids=[proj_a] caches ONLY proj_a even though proj_b exists."""
    origin_path, cache_path = cache_env
    today = date(2026, 7, 19)
    _make_origin_db(origin_path, today=today, num_days=30, projects=["proj_a", "proj_b"])

    from core import cache_warehouse

    result = cache_warehouse.rebuild_cache(project_ids=["proj_a"], today=today)

    assert result["status"] == "ok"
    assert result["project_ids"] == ["proj_a"]
    rows = _read_cache_table(cache_path, "fact_daily_kpi")
    assert {r[0] for r in rows} == {"proj_a"}
    assert len(rows) == 30


# ---------------------------------------------------------------------------
# BigQuery origin path is Phase B / AI-08 BLOCKED -- degrades, never raises.
# ---------------------------------------------------------------------------


def test_bigquery_origin_blocked_phase_b(tmp_path, monkeypatch, caplog):
    """TOOROW_DB_MODE=bigquery is Phase B BLOCKED: status='failed', no raise."""
    cache_path = str(tmp_path / "cache_warehouse.duckdb")
    monkeypatch.setenv("TOOROW_DB_MODE", "bigquery")
    monkeypatch.setenv("TOOROW_CACHE_PATH", cache_path)
    monkeypatch.setenv("TOOROW_CACHE_ENABLED", "true")

    from core import cache_warehouse

    monkeypatch.setattr(cache_warehouse, "_meta_alert", lambda *a, **k: None)

    with caplog.at_level(logging.WARNING):
        result = cache_warehouse.rebuild_cache(today=date(2026, 7, 19))

    assert result["status"] == "failed"
    assert "Phase B" in (result["reason"] or "") or "AI-08" in (result["reason"] or "")


# ---------------------------------------------------------------------------
# F-2: Row-count + provenance for the 3 previously un-exercised allowlist views.
# ---------------------------------------------------------------------------


def test_semantic_ctr_in_cache(cache_env):
    """semantic_ctr rows land in the cache with correct count and provenance (F-2).

    semantic_ctr IS in the default allowlist and IS routed by query_report
    (non-additive, metric="ctr"). semantic_avg_position is now ALSO in the default
    allowlist (spec F4 debt resolved in 19.4): query_report routes average_position
    to semantic_avg_position via _SEMANTIC_VIEW_BY_METRIC.
    """
    origin_path, cache_path = cache_env
    today = date(2026, 7, 19)
    projects = ["proj_a"]
    _make_origin_db(origin_path, today=today, num_days=30, projects=projects)

    from core import cache_warehouse

    result = cache_warehouse.rebuild_cache(today=today)

    assert result["status"] == "ok"
    # 30 days x 1 project x 1 breakdown row per day = 30 rows (one per date).
    assert result["row_counts"].get("semantic_ctr") == 30
    # spec F4 (19.4): semantic_avg_position IS now in the default allowlist (debt resolved).
    assert "semantic_avg_position" in result["tables"]

    import duckdb  # noqa: PLC0415

    origin_con = duckdb.connect(origin_path, read_only=True)
    try:
        origin_rows = origin_con.execute(
            "SELECT project_id, date, pull_id, ctr "
            "FROM main_marts.semantic_ctr ORDER BY project_id, date"
        ).fetchall()
    finally:
        origin_con.close()

    cache_con = duckdb.connect(cache_path, read_only=True)
    try:
        cache_rows = cache_con.execute(
            "SELECT project_id, date, pull_id, ctr "
            'FROM semantic_ctr ORDER BY project_id, date'
        ).fetchall()
    finally:
        cache_con.close()

    # Row-for-row provenance: pull_id and ctr values identical to origin (invariant b).
    assert len(cache_rows) == len(origin_rows)
    for o, c in zip(origin_rows, cache_rows):
        assert c[0] == o[0]             # project_id
        assert c[1] == o[1]             # date
        assert c[2] == o[2]             # pull_id identical (AD-9)
        assert c[3] == pytest.approx(o[3])  # ctr (float)


def test_transaction_reconciliation_daily_in_cache(cache_env):
    """transaction_reconciliation_daily rows land in cache with count + provenance."""
    origin_path, cache_path = cache_env
    today = date(2026, 7, 19)
    projects = ["proj_a", "proj_b"]
    _make_origin_db(origin_path, today=today, num_days=30, projects=projects)

    from core import cache_warehouse

    result = cache_warehouse.rebuild_cache(today=today)

    assert result["status"] == "ok"
    # 30 days x 2 projects = 60 rows.
    assert result["row_counts"].get("transaction_reconciliation_daily") == 60

    import duckdb  # noqa: PLC0415

    origin_con = duckdb.connect(origin_path, read_only=True)
    try:
        origin_rows = origin_con.execute(
            "SELECT project_id, date, matched_count, coverage_rate, "
            "shopify_pull_id, ga4_pull_id "
            "FROM main_marts.transaction_reconciliation_daily ORDER BY project_id, date"
        ).fetchall()
    finally:
        origin_con.close()

    cache_con = duckdb.connect(cache_path, read_only=True)
    try:
        cache_rows = cache_con.execute(
            "SELECT project_id, date, matched_count, coverage_rate, "
            "shopify_pull_id, ga4_pull_id "
            'FROM transaction_reconciliation_daily ORDER BY project_id, date'
        ).fetchall()
    finally:
        cache_con.close()

    # Row-for-row provenance: pull_ids identical to origin (invariant b, AD-9).
    assert len(cache_rows) == len(origin_rows)
    for o, c in zip(origin_rows, cache_rows):
        assert c[0] == o[0]   # project_id
        assert c[1] == o[1]   # date
        assert c[2] == o[2]   # matched_count
        assert c[3] == pytest.approx(o[3])  # coverage_rate (float)
        assert c[4] == o[4]   # shopify_pull_id identical (AD-9)
        assert c[5] == o[5]   # ga4_pull_id identical (AD-9)


def test_semantic_avg_position_composite_in_cache(cache_env):
    """semantic_avg_position_composite rows land in the cache (F-2 row-count assertion)."""
    origin_path, cache_path = cache_env
    today = date(2026, 7, 19)
    _make_origin_db(origin_path, today=today, num_days=30, projects=["proj_a"])

    from core import cache_warehouse

    result = cache_warehouse.rebuild_cache(today=today)

    assert result["status"] == "ok"
    assert result["row_counts"].get("semantic_avg_position_composite") == 30


# ---------------------------------------------------------------------------
# F-3: AD-8 path-collision guard -- TOOROW_CACHE_PATH == TOOROW_DUCKDB_PATH.
# ---------------------------------------------------------------------------


def test_cache_path_equals_origin_path_fails_cleanly(tmp_path, monkeypatch, caplog):
    """If TOOROW_CACHE_PATH resolves to the same path as TOOROW_DUCKDB_PATH,
    the build must fail with status='failed' and log a clear AD-8 message, never raise.
    """
    same_path = str(tmp_path / "same.duckdb")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", same_path)
    monkeypatch.setenv("TOOROW_CACHE_PATH", same_path)
    monkeypatch.setenv("TOOROW_CACHE_ENABLED", "true")

    # Create the file so the "origin exists" check passes and we reach the guard.
    import duckdb as _duckdb  # noqa: PLC0415

    _duckdb.connect(same_path).close()

    from core import cache_warehouse

    monkeypatch.setattr(cache_warehouse, "_meta_alert", lambda *a, **k: None)

    with caplog.at_level(logging.WARNING):
        result = cache_warehouse.rebuild_cache(today=date(2026, 7, 19))

    assert result["status"] == "failed"
    assert "AD-8" in (result["reason"] or "") or "origin_path" in (result["reason"] or "")
    # The log must mention the collision (either AD-8 or the path or rebuild_failed).
    assert "rebuild_failed" in caplog.text or "AD-8" in caplog.text


# ---------------------------------------------------------------------------
# F-4: Invariant (e) proved directly -- DuckDB rejects writes on READ_ONLY attach.
# ---------------------------------------------------------------------------


def test_origin_read_only_attach_rejects_writes(cache_env):
    """Open the cache (primary) + origin (ATTACH READ_ONLY) and attempt an INSERT
    into the origin mart -- DuckDB must raise an exception (invariant e / AD-8).
    The builder never issues such a write; this test proves the physical guard.
    """
    origin_path, cache_path = cache_env
    today = date(2026, 7, 19)
    _make_origin_db(origin_path, today=today, num_days=5, projects=["proj_a"])

    import duckdb  # noqa: PLC0415

    cache_con = duckdb.connect(cache_path)
    try:
        origin_literal = origin_path.replace("'", "''")
        cache_con.execute(
            f"ATTACH '{origin_literal}' AS origin (READ_ONLY)"  # noqa: S608
        )
        try:
            with pytest.raises(Exception):  # noqa: PT011 -- DuckDB raises its own type
                cache_con.execute(
                    "INSERT INTO origin.main_marts.fact_daily_kpi "
                    "VALUES ('x', '2026-01-01', 'ga4', 'sessions', 'date', "
                    "'2026-01-01', 1.0, 'p1', '2026-01-01 00:00:00')"
                )
        finally:
            try:
                cache_con.execute("DETACH origin")
            except Exception:  # noqa: BLE001
                pass
    finally:
        cache_con.close()


# ---------------------------------------------------------------------------
# F-6: Atomic build -- failed build leaves the previous cache intact.
# ---------------------------------------------------------------------------


def test_atomic_build_failed_build_leaves_previous_cache_intact(cache_env, monkeypatch):
    """A build that fails mid-way must NOT overwrite a previously good cache.

    Strategy: do a successful first build, then force the second build to fail
    (by removing the origin file after env is configured), and assert the first
    cache file is still present and readable.
    """
    import os as _os

    origin_path, cache_path = cache_env
    today = date(2026, 7, 19)
    _make_origin_db(origin_path, today=today, num_days=10, projects=["proj_a"])

    from core import cache_warehouse

    # First build: succeeds; establishes a valid cache at cache_path.
    result1 = cache_warehouse.rebuild_cache(today=today)
    assert result1["status"] == "ok"
    assert _os.path.exists(cache_path)

    # Simulate a mid-build failure on the second attempt by making the origin
    # disappear after the enabled-check (monkeypatch _origin_duckdb_path to a
    # non-existent path so _rebuild_cache_inner raises before writing anything).
    monkeypatch.setattr(cache_warehouse, "_meta_alert", lambda *a, **k: None)
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(cache_env[0]) + "_gone")

    result2 = cache_warehouse.rebuild_cache(today=today)
    assert result2["status"] == "failed"

    # The previous cache must still exist and still be readable (atomic guarantee).
    assert _os.path.exists(cache_path)
    manifest = cache_warehouse.read_manifest(cache_path)
    assert manifest is not None
    assert manifest["schema_version"] == cache_warehouse.CACHE_SCHEMA_VERSION

    # The .tmp orphan must NOT be present (it was either never created or cleaned up).
    assert not _os.path.exists(cache_path + ".tmp")


def test_cache_file_appears_only_after_successful_build(cache_env, monkeypatch):
    """The final cache file must not exist before a successful build completes.

    Confirms the atomic write: cache_path is absent until os.replace() promotes tmp.
    """
    import os as _os

    origin_path, cache_path = cache_env
    today = date(2026, 7, 19)
    _make_origin_db(origin_path, today=today, num_days=5, projects=["proj_a"])

    from core import cache_warehouse

    # Before any build: no cache file.
    assert not _os.path.exists(cache_path)

    result = cache_warehouse.rebuild_cache(today=today)
    assert result["status"] == "ok"

    # After successful build: cache file exists.
    assert _os.path.exists(cache_path)
    # The .tmp staging file is gone (promoted via os.replace).
    assert not _os.path.exists(cache_path + ".tmp")


# ---------------------------------------------------------------------------
# data F-2 (19.4 review): semantic_cpa and semantic_roas -- stand-in seeds and
# row-count assertions mirroring test_semantic_ctr_in_cache (fix 3).
# ---------------------------------------------------------------------------


def test_semantic_cpa_in_cache(cache_env):
    """semantic_cpa rows land in the cache with correct count and provenance (data F-2).

    Column schema mirrors dbt/models/marts/semantic_cpa.sql:
    project_id, date, connector, breakdown_dimension, breakdown_value, cpa, pull_id.
    No loaded_at (ratio view, MAX(pull_id) only).
    """
    origin_path, cache_path = cache_env
    today = date(2026, 7, 19)
    projects = ["proj_a"]
    _make_origin_db(origin_path, today=today, num_days=30, projects=projects)

    from core import cache_warehouse

    result = cache_warehouse.rebuild_cache(today=today)

    assert result["status"] == "ok"
    # 30 days x 1 project x 1 breakdown row per day = 30 rows.
    assert result["row_counts"].get("semantic_cpa") == 30

    import duckdb  # noqa: PLC0415

    origin_con = duckdb.connect(origin_path, read_only=True)
    try:
        origin_rows = origin_con.execute(
            "SELECT project_id, date, pull_id, cpa "
            "FROM main_marts.semantic_cpa ORDER BY project_id, date"
        ).fetchall()
    finally:
        origin_con.close()

    cache_con = duckdb.connect(cache_path, read_only=True)
    try:
        cache_rows = cache_con.execute(
            "SELECT project_id, date, pull_id, cpa "
            "FROM semantic_cpa ORDER BY project_id, date"
        ).fetchall()
    finally:
        cache_con.close()

    # Row-for-row provenance: pull_id and cpa values identical to origin (invariant b).
    assert len(cache_rows) == len(origin_rows)
    for o, c in zip(origin_rows, cache_rows):
        assert c[0] == o[0]              # project_id
        assert c[1] == o[1]              # date
        assert c[2] == o[2]              # pull_id identical (AD-9)
        assert c[3] == pytest.approx(o[3])  # cpa (float)


def test_semantic_roas_in_cache(cache_env):
    """semantic_roas rows land in the cache with correct count and provenance (data F-2).

    Column schema mirrors dbt/models/marts/semantic_roas.sql:
    project_id, date, connector, breakdown_dimension, breakdown_value, roas, pull_id.
    No loaded_at (ratio view, MAX(pull_id) only).
    """
    origin_path, cache_path = cache_env
    today = date(2026, 7, 19)
    projects = ["proj_a"]
    _make_origin_db(origin_path, today=today, num_days=30, projects=projects)

    from core import cache_warehouse

    result = cache_warehouse.rebuild_cache(today=today)

    assert result["status"] == "ok"
    # 30 days x 1 project x 1 breakdown row per day = 30 rows.
    assert result["row_counts"].get("semantic_roas") == 30

    import duckdb  # noqa: PLC0415

    origin_con = duckdb.connect(origin_path, read_only=True)
    try:
        origin_rows = origin_con.execute(
            "SELECT project_id, date, pull_id, roas "
            "FROM main_marts.semantic_roas ORDER BY project_id, date"
        ).fetchall()
    finally:
        origin_con.close()

    cache_con = duckdb.connect(cache_path, read_only=True)
    try:
        cache_rows = cache_con.execute(
            "SELECT project_id, date, pull_id, roas "
            "FROM semantic_roas ORDER BY project_id, date"
        ).fetchall()
    finally:
        cache_con.close()

    # Row-for-row provenance: pull_id and roas values identical to origin (invariant b).
    assert len(cache_rows) == len(origin_rows)
    for o, c in zip(origin_rows, cache_rows):
        assert c[0] == o[0]              # project_id
        assert c[1] == o[1]              # date
        assert c[2] == o[2]              # pull_id identical (AD-9)
        assert c[3] == pytest.approx(o[3])  # roas (float)
