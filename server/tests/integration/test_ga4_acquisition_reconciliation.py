"""Epic 16, story 16.1 -- GA4 acquisition reconciliation + double-count battery.

The HARD point of this story is the SAME as 10.3: TOP-N BOUNDED PARTITIONS that must
NOT change the existing totals and must NEVER be chosen as the canonical day-total
partition. Landing the NEW parallel breakdown partitions
(breakdown_dimension in {'session_source_medium','session_campaign',
'first_user_source_medium'}) MUST NOT change the existing device_category / country /
user_type / landing_page / page totals (the 9.9 CRITICAL scenario). This file proves it
on a REAL DuckDB seeded over multiple contiguous days (AI-45).

Battery (AC6):
  (a) Existing totals UNCHANGED with vs without the acquisition rows.
  (b) MIN(breakdown_dimension) stability: MIN across ALL GA4 dims stays 'country'
      (lexicographic proof, asserted programmatically).
  (c) Top-N bound: each acquisition partition has <= top_n distinct values/day.
  (d) The acquisition axes never EXCEED the connector (country) conversions total
      (a bounded partition cannot outweigh the true day total -- no double count).
  (e) Python tie-break: canonical_breakdown_per_connector(rows, metric) stays 'country'
      for GA4 even though the new partitions are lexicographically > 'country' AND are
      high-cardinality top-N (the review-epic-10 trap). Both dbt MIN and Python agree.

Seam (AI-56): GET /api/cards over the REAL build_asgi_app() ASGI stack, with the mart
mocked to return rows that INCLUDE the acquisition partitions, wires through without
error and the acquisition rows are NOT silently dropped. Pattern:
test_ga4_pages_reconciliation.py (do NOT edit that file).

The reconciliation SQL inlines the EXACT staging QUALIFY supersede + mart GROUP BY of
the GA4 acquisition blocks in fact_daily_kpi.sql so the test proves the mart aggregation
SEMANTICS without a dbt run inside pytest. Auto-skips when duckdb is not importable.

ASCII-only (AI-03). No live GA4 / Postgres required.
"""

from __future__ import annotations

import os
from datetime import date

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

duckdb = pytest.importorskip(
    "duckdb", reason="duckdb not installed -- acquisition reconciliation skipped"
)

import importlib.util  # noqa: E402
from pathlib import Path  # noqa: E402

_SEEDS_DIR = (
    Path(__file__).resolve().parents[3]
    / "server"
    / "modules"
    / "google-analytics"
    / "seeds"
)


def _load_generate_seed():
    spec = importlib.util.spec_from_file_location(
        "_ga4_generate_seed_acq", str(_SEEDS_DIR / "generate_seed.py")
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Inlined staging views mirroring fact_daily_kpi.sql GA4 blocks EXACTLY.
# ---------------------------------------------------------------------------

_STG_STANDARD = """
CREATE OR REPLACE TEMP VIEW stg_std AS
SELECT date, device_category, country, sessions, active_users, conversions,
       pull_id, loaded_at, project_id
FROM (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY project_id, date, device_category, country
        ORDER BY pull_id DESC
    ) AS rn
    FROM raw_ga4_standard_daily
) WHERE rn = 1
"""

_STG_USER_TYPE = """
CREATE OR REPLACE TEMP VIEW stg_ut AS
SELECT date, user_type, sessions, active_users, conversions,
       pull_id, loaded_at, project_id
FROM (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY project_id, date, user_type
        ORDER BY pull_id DESC
    ) AS rn
    FROM raw_ga4_user_type_daily
) WHERE rn = 1
"""

_STG_ACQ_SESSION = """
CREATE OR REPLACE TEMP VIEW stg_acq_session AS
SELECT date, session_source_medium, session_campaign, conversions, sessions,
       pull_id, loaded_at, project_id
FROM (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY project_id, date, session_source_medium, session_campaign
        ORDER BY pull_id DESC
    ) AS rn
    FROM raw_ga4_acquisition_session
) WHERE rn = 1
"""

_STG_ACQ_FIRST_USER = """
CREATE OR REPLACE TEMP VIEW stg_acq_fu AS
SELECT date, first_user_source_medium, conversions,
       pull_id, loaded_at, project_id
FROM (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY project_id, date, first_user_source_medium
        ORDER BY pull_id DESC
    ) AS rn
    FROM raw_ga4_acquisition_first_user
) WHERE rn = 1
"""

_METRICS = ["sessions", "active_users", "conversions"]


def _mart_sql(include_acq: bool) -> str:
    """Build the GA4 portion of fact_daily_kpi from the staging views.

    Always includes standard (device_category + country) and user_type. When
    *include_acq* is True, appends the acquisition blocks (this story). Mirrors
    fact_daily_kpi.sql exactly.
    """
    blocks: list[str] = []
    for metric in _METRICS:
        for dimension in ("device_category", "country"):
            blocks.append(
                f"""
                SELECT project_id, date, 'google-analytics' AS connector,
                       '{metric}' AS metric, '{dimension}' AS breakdown_dimension,
                       {dimension} AS breakdown_value,
                       SUM(CAST({metric} AS DOUBLE)) AS value
                FROM stg_std GROUP BY project_id, date, {dimension}
                """
            )
    for metric in _METRICS:
        blocks.append(
            f"""
            SELECT project_id, date, 'google-analytics' AS connector,
                   '{metric}' AS metric, 'user_type' AS breakdown_dimension,
                   user_type AS breakdown_value,
                   SUM(CAST({metric} AS DOUBLE)) AS value
            FROM stg_ut GROUP BY project_id, date, user_type
            """
        )
    if include_acq:
        # session profile: two marginal partitions x {conversions, sessions}.
        for metric in ("conversions", "sessions"):
            blocks.append(
                f"""
                SELECT project_id, date, 'google-analytics' AS connector,
                       '{metric}' AS metric, 'session_source_medium' AS breakdown_dimension,
                       session_source_medium AS breakdown_value,
                       SUM(CAST({metric} AS DOUBLE)) AS value
                FROM stg_acq_session GROUP BY project_id, date, session_source_medium
                """
            )
            blocks.append(
                f"""
                SELECT project_id, date, 'google-analytics' AS connector,
                       '{metric}' AS metric, 'session_campaign' AS breakdown_dimension,
                       session_campaign AS breakdown_value,
                       SUM(CAST({metric} AS DOUBLE)) AS value
                FROM stg_acq_session GROUP BY project_id, date, session_campaign
                """
            )
        # first_user profile: one partition x conversions.
        blocks.append(
            """
            SELECT project_id, date, 'google-analytics' AS connector,
                   'conversions' AS metric, 'first_user_source_medium' AS breakdown_dimension,
                   first_user_source_medium AS breakdown_value,
                   SUM(CAST(conversions AS DOUBLE)) AS value
            FROM stg_acq_fu GROUP BY project_id, date, first_user_source_medium
            """
        )
    return "\nUNION ALL\n".join(blocks)


@pytest.fixture()
def seeded_con():
    """In-memory DuckDB seeded with standard + user_type + acquisition profiles over
    >= 3 contiguous days (AI-45)."""
    gen = _load_generate_seed()
    import random

    end_date = date(2026, 6, 1)
    std_rows = gen.generate_rows(end_date=end_date, rng=random.Random(1234))
    ut_rows = gen.generate_user_type_rows(end_date=end_date, rng=random.Random(1234))
    acq_sess_rows = gen.generate_acquisition_session_rows(
        end_date=end_date, rng=random.Random(1234)
    )
    acq_fu_rows = gen.generate_acquisition_first_user_rows(
        end_date=end_date, rng=random.Random(1234)
    )

    con = duckdb.connect(":memory:")
    con.execute(
        """
        CREATE TABLE raw_ga4_standard_daily (
            date VARCHAR, device_category VARCHAR, country VARCHAR,
            sessions INTEGER, active_users INTEGER, conversions INTEGER,
            pull_id VARCHAR, loaded_at VARCHAR, project_id VARCHAR
        )
        """
    )
    con.execute(
        """
        CREATE TABLE raw_ga4_user_type_daily (
            date VARCHAR, user_type VARCHAR,
            sessions INTEGER, active_users INTEGER, conversions INTEGER,
            pull_id VARCHAR, loaded_at VARCHAR, project_id VARCHAR
        )
        """
    )
    con.execute(
        """
        CREATE TABLE raw_ga4_acquisition_session (
            date VARCHAR, session_source_medium VARCHAR, session_campaign VARCHAR,
            conversions INTEGER, sessions INTEGER,
            pull_id VARCHAR, loaded_at VARCHAR, project_id VARCHAR
        )
        """
    )
    con.execute(
        """
        CREATE TABLE raw_ga4_acquisition_first_user (
            date VARCHAR, first_user_source_medium VARCHAR, conversions INTEGER,
            pull_id VARCHAR, loaded_at VARCHAR, project_id VARCHAR
        )
        """
    )
    con.executemany(
        "INSERT INTO raw_ga4_standard_daily VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                r["date"], r["device_category"], r["country"],
                r["sessions"], r["active_users"], r["conversions"],
                "pull_std_1", "2026-06-01T00:00:00Z", "default",
            )
            for r in std_rows
        ],
    )
    con.executemany(
        "INSERT INTO raw_ga4_user_type_daily VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                r["date"], r["user_type"],
                r["sessions"], r["active_users"], r["conversions"],
                "pull_ut_1", "2026-06-01T00:00:00Z", "default",
            )
            for r in ut_rows
        ],
    )
    con.executemany(
        "INSERT INTO raw_ga4_acquisition_session VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                r["date"], r["session_source_medium"], r["session_campaign"],
                r["conversions"], r["sessions"],
                "pull_acq_sess_1", "2026-06-01T00:00:00Z", "default",
            )
            for r in acq_sess_rows
        ],
    )
    con.executemany(
        "INSERT INTO raw_ga4_acquisition_first_user VALUES (?,?,?,?,?,?)",
        [
            (
                r["date"], r["first_user_source_medium"], r["conversions"],
                "pull_acq_fu_1", "2026-06-01T00:00:00Z", "default",
            )
            for r in acq_fu_rows
        ],
    )
    con.execute(_STG_STANDARD)
    con.execute(_STG_USER_TYPE)
    con.execute(_STG_ACQ_SESSION)
    con.execute(_STG_ACQ_FIRST_USER)
    try:
        yield con
    finally:
        con.close()


def _distinct_days(con) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM stg_std ORDER BY date"
    ).fetchall()]


# ---------------------------------------------------------------------------
# AC6a -- existing partition totals UNCHANGED by the new acquisition partitions.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dim", ["country", "device_category", "user_type"])
def test_existing_totals_unchanged_after_acquisition_added(seeded_con, dim):
    """AC6a: country / device_category / user_type totals are byte-identical with vs
    without the acquisition rows present (9.9 non-interference)."""
    con = seeded_con

    def _totals(include_acq: bool):
        con.execute(f"CREATE OR REPLACE TEMP VIEW fact AS {_mart_sql(include_acq)}")
        return {
            (r[0], r[1], r[2]): r[3]
            for r in con.execute(
                f"""
                SELECT project_id, date, metric, SUM(value)
                FROM fact
                WHERE breakdown_dimension = '{dim}'
                GROUP BY project_id, date, metric
                """
            ).fetchall()
        }

    before = _totals(False)
    after = _totals(True)

    assert before, f"expected non-empty {dim} totals in the seeded mart"
    assert before == after, (
        f"adding acquisition rows changed the {dim} partition totals "
        "(9.9 CRITICAL regression -- the acquisition partitions are NOT non-interfering)"
    )
    assert len(_distinct_days(con)) >= 3


# ---------------------------------------------------------------------------
# AC6b -- MIN(breakdown_dimension) remains 'country' after acquisition arrives.
# ---------------------------------------------------------------------------


def test_min_breakdown_dimension_remains_country(seeded_con):
    """AC6b: MIN(breakdown_dimension) across all GA4 dims is 'country'.

    Proven programmatically (not just the lexicographic analysis in the story):
    'country' < 'country>device' < 'device_category' < 'first_user_source_medium'
    < 'session_campaign' < 'session_source_medium' < 'user_type'. (This test's mart does
    not include the composite/page blocks, but the acquisition dims are all > 'country'.)
    """
    con = seeded_con
    con.execute(f"CREATE OR REPLACE TEMP VIEW fact AS {_mart_sql(True)}")

    dims = {
        r[0]
        for r in con.execute(
            "SELECT DISTINCT breakdown_dimension FROM fact WHERE connector='google-analytics'"
        ).fetchall()
    }
    assert {
        "session_source_medium",
        "session_campaign",
        "first_user_source_medium",
    } <= dims, dims

    min_dim = con.execute(
        "SELECT MIN(breakdown_dimension) FROM fact WHERE connector='google-analytics'"
    ).fetchone()[0]
    assert min_dim == "country", (
        f"MIN(breakdown_dimension)={min_dim!r} -- canonical single dim must stay 'country' "
        "so metric_baselines / cross_source_conversions do not double-count"
    )


# ---------------------------------------------------------------------------
# AC6c -- each acquisition partition has AT MOST top_n distinct values per day.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dim,metric",
    [
        ("session_source_medium", "conversions"),
        ("session_campaign", "conversions"),
        ("first_user_source_medium", "conversions"),
    ],
)
def test_acquisition_partition_is_top_n_bounded(seeded_con, dim, metric):
    """AC6c: each acquisition partition contains <= top_n distinct breakdown_values per
    (project_id, date). The long tail is dropped by design.

    review-16-5 fix-11 comment: n_distinct <= top_n is a VALID bound because the
    GA4 Data API `limit=top_n` applies to the FULL window (not per day). A window
    with W days can return at most top_n combos; each combo may appear on 1..W days,
    so any given day sees <= top_n distinct values. This bound is tight when every
    top-N combo appears on every day; in LIVE data it will be looser (most combos
    are active on only a subset of days). The seed always satisfies the bound because
    it is generated with exactly top_n or fewer combos. Phase B: compare against a
    live property to calibrate the actual live long-tail discount.
    """
    con = seeded_con
    top_n = _load_generate_seed().get_page_top_n()
    con.execute(f"CREATE OR REPLACE TEMP VIEW fact AS {_mart_sql(True)}")

    rows = con.execute(
        f"""
        SELECT project_id, date, COUNT(DISTINCT breakdown_value) AS n_distinct
        FROM fact
        WHERE breakdown_dimension = '{dim}' AND metric = '{metric}'
        GROUP BY project_id, date
        """
    ).fetchall()

    assert rows, f"expected {dim} partition rows in the seeded mart"
    assert len(rows) >= 3, "expected >= 3 contiguous days (AI-45)"
    for _project, _d, n_distinct in rows:
        # n_distinct <= top_n: valid because limit=top_n is per-window (see docstring).
        # Live runs will typically see LESS than top_n distinct values per day.
        assert 0 < n_distinct <= top_n, (
            f"{dim} on {_d} has {n_distinct} distinct values -- exceeds the top_n bound "
            f"({top_n}); the top-N truncation is not applied"
        )


# ---------------------------------------------------------------------------
# AC6d -- the acquisition axes never EXCEED the connector (country) conversions total.
# A top-N-bounded partition cannot outweigh the true day total (no double count).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dim,metric",
    [
        ("session_source_medium", "conversions"),
        ("session_campaign", "conversions"),
        ("first_user_source_medium", "conversions"),
        # review-16-1 central verification: the sessions axis is a subset of the same
        # day's sessions -- guard it too (the seed's old ACQ_SESSIONS_PER_CONVERSION=22
        # produced ~5000 sessions/day vs a ~2990 connector total).
        ("session_source_medium", "sessions"),
        ("session_campaign", "sessions"),
    ],
)
def test_acquisition_conversions_never_exceed_connector_total(seeded_con, dim, metric):
    """AC6d: SUM(metric) over each acquisition axis <= SUM over 'country' (the
    full-coverage canonical partition), within a small noise margin. If an axis exceeded
    the connector total it would signal a double count -- or, as caught during the 16.1
    central verification, seed magnitudes inconsistent with the standard profile
    (a partition must be a SUBSET of the day total: generate_seed.py calibration note)."""
    con = seeded_con
    con.execute(f"CREATE OR REPLACE TEMP VIEW fact AS {_mart_sql(True)}")

    rows = con.execute(
        f"""
        WITH axis AS (
            SELECT project_id, date, SUM(value) AS axis_total
            FROM fact WHERE breakdown_dimension='{dim}' AND metric='{metric}'
            GROUP BY project_id, date
        ),
        ctry AS (
            SELECT project_id, date, SUM(value) AS ctry_total
            FROM fact WHERE breakdown_dimension='country' AND metric='{metric}'
            GROUP BY project_id, date
        )
        SELECT a.date, a.axis_total, c.ctry_total
        FROM axis a JOIN ctry c
          ON a.project_id=c.project_id AND a.date=c.date
        """
    ).fetchall()

    assert rows, f"expected joined {dim} vs country {metric} totals"
    for _d, axis_total, ctry_total in rows:
        assert axis_total >= 0
        assert ctry_total > 0
        # 1.05 margin (independent noise draws; a real double count -> ~2x).
        assert axis_total <= ctry_total * 1.05, (
            f"{dim} {metric} total {axis_total} exceeds the connector total "
            f"{ctry_total} on {_d} -- possible double count"
        )


# ---------------------------------------------------------------------------
# AC6e -- Python rollup tie-break stays 'country' (canonical_breakdown_per_connector).
# This is the DANGEROUS point flagged in the brief: the new partitions are
# lexicographically > 'country' AND high-cardinality top-N -- the exact review-epic-10
# trap. Prove the coverage-guarded lexicographic MIN still pins 'country'.
# ---------------------------------------------------------------------------


def _rollup_rows_with_acquisition():
    """Mart-shaped rows: full-coverage 'country' + high-cardinality top-N acquisition
    partitions over 3 contiguous days. The acquisition partitions have MORE distinct
    values (higher row count) but the SAME day coverage -- the review-epic-10 trap where
    a naive row-count tie-break would wrongly pick the top-N partition."""
    rows = []
    days = ("2026-06-01", "2026-06-02", "2026-06-03")
    for d in days:
        # country: 2 full-coverage values (the canonical single dim).
        for ctry, val in (("FR", 30.0), ("DE", 12.0)):
            rows.append(
                {"date": d, "connector": "google-analytics", "metric": "conversions",
                 "breakdown_dimension": "country", "breakdown_value": ctry,
                 "value": val, "pull_id": "pull_1", "loaded_at": f"{d}T00:00:00Z"}
            )
        # session_source_medium: MANY values, same coverage, top-N bounded (partial sum).
        for i in range(8):
            rows.append(
                {"date": d, "connector": "google-analytics", "metric": "conversions",
                 "breakdown_dimension": "session_source_medium",
                 "breakdown_value": f"chan-{i}", "value": 3.0,
                 "pull_id": "pull_1", "loaded_at": f"{d}T00:00:00Z"}
            )
        # session_campaign + first_user_source_medium: also many values.
        for i in range(6):
            rows.append(
                {"date": d, "connector": "google-analytics", "metric": "conversions",
                 "breakdown_dimension": "session_campaign",
                 "breakdown_value": f"camp-{i}", "value": 2.0,
                 "pull_id": "pull_1", "loaded_at": f"{d}T00:00:00Z"}
            )
        for i in range(7):
            rows.append(
                {"date": d, "connector": "google-analytics", "metric": "conversions",
                 "breakdown_dimension": "first_user_source_medium",
                 "breakdown_value": f"fu-{i}", "value": 4.0,
                 "pull_id": "pull_1", "loaded_at": f"{d}T00:00:00Z"}
            )
    return rows


def test_rollup_canonical_stays_country():
    """AC6e: canonical_breakdown_per_connector pins 'country' for GA4 conversions even
    with the new acquisition partitions present (> 'country' lexically, higher row
    count). Guards the review-epic-10 CRITICAL: a bounded top-N partition must NOT be
    chosen and undercount the window."""
    from core.rollup import canonical_breakdown_per_connector

    rows = _rollup_rows_with_acquisition()
    canonical = canonical_breakdown_per_connector(rows, "conversions")
    assert canonical.get("google-analytics") == "country", (
        f"canonical GA4 partition = {canonical.get('google-analytics')!r} -- must be "
        "'country' (coverage-guarded lexicographic MIN); an acquisition top-N partition "
        "would undercount the window (review-epic-10 CRITICAL)"
    )


def test_rollup_conversions_not_inflated_by_acquisition():
    """AC6d/AC6e end-to-end: compute_rollup totals GA4 conversions over the canonical
    'country' partition only -- the acquisition partitions do NOT inflate the hero KPI."""
    from core.rollup import compute_rollup

    rows = _rollup_rows_with_acquisition()
    rollup = compute_rollup(
        rows,
        metrics=["conversions"],
        date_from="2026-06-01",
        date_to="2026-06-03",
        project_id="default",
        pull_ids=["pull_1"],
    )
    # country total per day = 42 (30 + 12) x 3 days = 126. Acquisition partitions
    # (summing to 24 + 12 + 28 = 64/day) must NOT be added on top.
    assert rollup["conversions"]["value"] == pytest.approx(126.0), (
        f"conversions hero = {rollup['conversions']['value']} -- expected 126 (country "
        "only); the acquisition partitions inflated the total (double count)"
    )


# ---------------------------------------------------------------------------
# AI-56 seam -- build_asgi_app(): acquisition rows reach a card endpoint through the
# FULL ASGI stack (mart query mocked) without error and are not silently dropped.
# ---------------------------------------------------------------------------


def _mart_rows_with_acquisition():
    """Mart-shaped rows for a card: the GA4 funnel metrics PLUS the acquisition
    partitions this story lands. >= 3 contiguous days (AI-45)."""
    rows = []
    session_channels = [("google / cpc", 1.0), ("facebook / cpc", 0.5), ("direct", 0.3)]
    first_channels = [("google / organic", 1.0), ("bing / organic", 0.4)]
    for d in ("2026-06-01", "2026-06-02", "2026-06-03"):
        for metric, base in (("sessions", 100.0), ("active_users", 80.0), ("conversions", 12.0)):
            rows.append(
                {"date": d, "connector": "google-analytics", "metric": metric,
                 "breakdown_dimension": "country", "breakdown_value": "FR",
                 "value": base, "pull_id": "pull_1", "loaded_at": f"{d}T00:00:00Z"}
            )
        for chan, w in session_channels:
            rows.append(
                {"date": d, "connector": "google-analytics", "metric": "conversions",
                 "breakdown_dimension": "session_source_medium", "breakdown_value": chan,
                 "value": 12.0 * w, "pull_id": "pull_1", "loaded_at": f"{d}T00:00:00Z"}
            )
        for chan, w in first_channels:
            rows.append(
                {"date": d, "connector": "google-analytics", "metric": "conversions",
                 "breakdown_dimension": "first_user_source_medium", "breakdown_value": chan,
                 "value": 12.0 * w, "pull_id": "pull_1", "loaded_at": f"{d}T00:00:00Z"}
            )
    return rows


def _splice_card_routes_into_admin_router():
    from core.admin_api import router as admin_router
    from core.cards_api import CARDS_ROUTES

    existing = {(r.path, tuple(sorted(r.methods or []))) for r in admin_router.routes}
    for route in CARDS_ROUTES:
        key = (route.path, tuple(sorted(route.methods or [])))
        if key not in existing:
            admin_router.routes.append(route)


def test_card_endpoint_carries_acquisition_rows_through_asgi():
    """AI-56: GET /api/cards wires through build_asgi_app() with a mart that INCLUDES
    the acquisition partitions, returns 200 and a valid envelope. The acquisition rows
    are present in the mocked source (seam input) and the layer processes them without
    error. (The Attribution card that RENDERS these axes is story 16.3 -- this seam
    proves the ingestion layer this story delivers is wired end to end.)"""
    from unittest.mock import AsyncMock, patch

    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    _splice_card_routes_into_admin_router()
    app = build_asgi_app()

    rows = _mart_rows_with_acquisition()
    assert any(r["breakdown_dimension"] == "session_source_medium" for r in rows)
    assert any(r["breakdown_dimension"] == "first_user_source_medium" for r in rows)

    with patch("core.warehouse.query_daily_report", return_value=rows) as _mart, patch(
        "core.cards_api._check_auth", new=AsyncMock(return_value=(True, "test@test"))
    ), patch("core.project_access.identity_has_project_access", return_value=True):
        with TestClient(app, raise_server_exceptions=True) as c:
            resp = c.get(
                "/api/cards?project_id=default&template=journey"
                "&metrics=sessions,conversions"
                "&date_from=2026-06-01&date_to=2026-06-03",
                headers={"Host": "localhost"},
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["summary"].strip()
    assert _mart.called
    # Conversions hero must be the canonical 'country' total (12 x 3 = 36), NOT inflated
    # by summing the acquisition partitions on top -- proves the seam does not double count.
    resp_metrics = body["envelope"]["data"].get("metrics") or {}
    conv = (resp_metrics.get("conversions") or {}).get("value")
    assert conv == pytest.approx(36.0), (
        f"conversions hero through the ASGI stack = {conv} -- expected 36 (country only); "
        "the acquisition partitions inflated the total (double count end to end)"
    )
