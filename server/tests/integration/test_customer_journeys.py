"""Epic 16, story 16.2 -- customer_journeys v1 mart value tests.

customer_journeys is a READ-ONLY view over fact_daily_kpi that exposes TWO attribution
perspectives side by side (last click via session_source_medium / session_campaign; first
click via first_user_source_medium). This file proves the mart's VALUE SEMANTICS on a REAL
DuckDB seeded over multiple contiguous days (AI-45), WITHOUT running dbt inside pytest:

  * last_click channel rows carry BOTH conversions and sessions (session axis).
  * last_click campaign rows carry conversions (campaign axis; NULL channel).
  * first_click channel rows carry conversions ONLY -- sessions is NULL and campaign is
    NULL (16.1 landed no first-user campaign/sessions axis). We do NOT fabricate them.
  * share_of_conversions is a "Part" of the TOP-N subtotal per
    (project, date, model, breakdown_kind); shares sum to ~100 per group (not pooled
    across channel+campaign -- that would double-count).
  * reconciliation: mart per-axis conversions == fact partition totals (no rows created
    or lost); last != first (the two axes are independent marginals).

It builds fact_daily_kpi's GA4 acquisition blocks then customer_journeys.sql's projection
as TEMP VIEWs from the SAME staging QUALIFY supersede as story 16.1, mirroring
customer_journeys.sql exactly. Auto-skips when duckdb is not importable.

ASCII-only (AI-03). No live GA4 / Postgres required.
"""

from __future__ import annotations

import importlib.util
import os
import random
from datetime import date
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

duckdb = pytest.importorskip(
    "duckdb", reason="duckdb not installed -- customer_journeys tests skipped"
)

_SEEDS_DIR = (
    Path(__file__).resolve().parents[3]
    / "server"
    / "modules"
    / "google-analytics"
    / "seeds"
)


def _load_generate_seed():
    spec = importlib.util.spec_from_file_location(
        "_ga4_generate_seed_cj", str(_SEEDS_DIR / "generate_seed.py")
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Staging views (QUALIFY supersede) mirroring stg_ga4_acquisition_* EXACTLY.
# ---------------------------------------------------------------------------

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

# fact_daily_kpi GA4 acquisition blocks (session two marginals + first_user), the ONLY
# partitions customer_journeys reads. Mirrors fact_daily_kpi.sql.
_FACT_ACQ_SQL = """
CREATE OR REPLACE TEMP VIEW fact_acq AS
SELECT project_id, date, 'google-analytics' AS connector, 'conversions' AS metric,
       'session_source_medium' AS breakdown_dimension, session_source_medium AS breakdown_value,
       SUM(CAST(conversions AS DOUBLE)) AS value, MAX(pull_id) AS pull_id,
       MAX(loaded_at) AS loaded_at
FROM stg_acq_session GROUP BY project_id, date, session_source_medium
UNION ALL
SELECT project_id, date, 'google-analytics', 'sessions',
       'session_source_medium', session_source_medium,
       SUM(CAST(sessions AS DOUBLE)), MAX(pull_id), MAX(loaded_at)
FROM stg_acq_session GROUP BY project_id, date, session_source_medium
UNION ALL
SELECT project_id, date, 'google-analytics', 'conversions',
       'session_campaign', session_campaign,
       SUM(CAST(conversions AS DOUBLE)), MAX(pull_id), MAX(loaded_at)
FROM stg_acq_session GROUP BY project_id, date, session_campaign
UNION ALL
SELECT project_id, date, 'google-analytics', 'sessions',
       'session_campaign', session_campaign,
       SUM(CAST(sessions AS DOUBLE)), MAX(pull_id), MAX(loaded_at)
FROM stg_acq_session GROUP BY project_id, date, session_campaign
UNION ALL
SELECT project_id, date, 'google-analytics', 'conversions',
       'first_user_source_medium', first_user_source_medium,
       SUM(CAST(conversions AS DOUBLE)), MAX(pull_id), MAX(loaded_at)
FROM stg_acq_fu GROUP BY project_id, date, first_user_source_medium
"""

# customer_journeys.sql projection over fact_acq. Mirrors the mart SQL exactly.
_CUSTOMER_JOURNEYS_SQL = """
CREATE OR REPLACE TEMP VIEW customer_journeys AS
WITH acq AS (
    SELECT project_id, date, breakdown_dimension, breakdown_value, metric, value,
           pull_id, loaded_at
    FROM fact_acq
    WHERE connector = 'google-analytics'
      AND breakdown_dimension IN
          ('session_source_medium', 'session_campaign', 'first_user_source_medium')
),
axis_rows AS (
    SELECT project_id, date, 'last_click' AS attribution_model, 'channel' AS breakdown_kind,
           breakdown_value AS channel, CAST(NULL AS VARCHAR) AS campaign,
           SUM(CASE WHEN metric='conversions' THEN value END) AS conversions,
           SUM(CASE WHEN metric='sessions'    THEN value END) AS sessions,
           MAX(pull_id) AS pull_id, MAX(loaded_at) AS loaded_at
    FROM acq WHERE breakdown_dimension='session_source_medium'
    GROUP BY project_id, date, breakdown_value
    UNION ALL
    SELECT project_id, date, 'last_click', 'campaign',
           CAST(NULL AS VARCHAR), breakdown_value,
           SUM(CASE WHEN metric='conversions' THEN value END),
           SUM(CASE WHEN metric='sessions'    THEN value END),
           MAX(pull_id), MAX(loaded_at)
    FROM acq WHERE breakdown_dimension='session_campaign'
    GROUP BY project_id, date, breakdown_value
    UNION ALL
    SELECT project_id, date, 'first_click', 'channel',
           breakdown_value, CAST(NULL AS VARCHAR),
           SUM(CASE WHEN metric='conversions' THEN value END),
           CAST(NULL AS DOUBLE),
           MAX(pull_id), MAX(loaded_at)
    FROM acq WHERE breakdown_dimension='first_user_source_medium'
    GROUP BY project_id, date, breakdown_value
)
SELECT project_id, date, attribution_model, breakdown_kind, channel, campaign,
       conversions, sessions,
       conversions / NULLIF(SUM(conversions) OVER (
           PARTITION BY project_id, date, attribution_model, breakdown_kind), 0) * 100.0
           AS share_of_conversions,
       pull_id, loaded_at
FROM axis_rows
"""


@pytest.fixture()
def cj_con():
    """In-memory DuckDB with the acquisition seeds -> fact_acq -> customer_journeys, over
    >= 3 contiguous days (AI-45)."""
    gen = _load_generate_seed()
    end_date = date(2026, 6, 1)
    acq_sess_rows = gen.generate_acquisition_session_rows(
        end_date=end_date, rng=random.Random(1234)
    )
    acq_fu_rows = gen.generate_acquisition_first_user_rows(
        end_date=end_date, rng=random.Random(1234)
    )

    con = duckdb.connect(":memory:")
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
    con.execute(_STG_ACQ_SESSION)
    con.execute(_STG_ACQ_FIRST_USER)
    con.execute(_FACT_ACQ_SQL)
    con.execute(_CUSTOMER_JOURNEYS_SQL)
    try:
        yield con
    finally:
        con.close()


def _days(con) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM customer_journeys ORDER BY date"
    ).fetchall()]


# ---------------------------------------------------------------------------
# Grain + shape.
# ---------------------------------------------------------------------------


def test_grain_is_unique(cj_con):
    """AI-05: one row per (project, date, model, kind, channel, campaign)."""
    con = cj_con
    total = con.execute("SELECT COUNT(*) FROM customer_journeys").fetchone()[0]
    distinct = con.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT DISTINCT project_id, date, attribution_model, breakdown_kind,
                            channel, campaign
            FROM customer_journeys
        )
        """
    ).fetchone()[0]
    assert total == distinct, (
        "grain not unique -- duplicate (project,date,model,kind,channel,campaign)"
    )
    assert len(_days(con)) >= 3, "expected >= 3 contiguous days (AI-45)"


def test_models_present_side_by_side(cj_con):
    """Both attribution perspectives are present -- the point of v1."""
    models = {
        r[0] for r in cj_con.execute(
            "SELECT DISTINCT attribution_model FROM customer_journeys"
        ).fetchall()
    }
    assert models == {"last_click", "first_click"}, models


def test_last_click_has_both_kinds_first_click_channel_only(cj_con):
    """last_click carries channel AND campaign axes; first_click carries channel only."""
    con = cj_con
    last_kinds = {
        r[0] for r in con.execute(
            "SELECT DISTINCT breakdown_kind FROM customer_journeys "
            "WHERE attribution_model='last_click'"
        ).fetchall()
    }
    first_kinds = {
        r[0] for r in con.execute(
            "SELECT DISTINCT breakdown_kind FROM customer_journeys "
            "WHERE attribution_model='first_click'"
        ).fetchall()
    }
    assert last_kinds == {"channel", "campaign"}, last_kinds
    assert first_kinds == {"channel"}, first_kinds


# ---------------------------------------------------------------------------
# NULL discipline: campaign NULL on channel rows + all first_click; sessions NULL
# on every first_click row.
# ---------------------------------------------------------------------------


def test_campaign_null_on_channel_and_first_click(cj_con):
    """campaign is NULL on all 'channel' rows and on ALL first_click rows (no first-user
    campaign axis in 16.1 -- we do NOT invent one)."""
    con = cj_con
    bad_channel = con.execute(
        "SELECT COUNT(*) FROM customer_journeys "
        "WHERE breakdown_kind='channel' AND campaign IS NOT NULL"
    ).fetchone()[0]
    bad_first = con.execute(
        "SELECT COUNT(*) FROM customer_journeys "
        "WHERE attribution_model='first_click' AND campaign IS NOT NULL"
    ).fetchone()[0]
    assert bad_channel == 0, "campaign must be NULL on channel rows"
    assert bad_first == 0, "first_click must never carry a campaign (16.1 landed none)"


def test_sessions_null_on_first_click_present_on_last_click(cj_con):
    """sessions is NULL on every first_click row (the first-user axis never carried
    sessions) and populated on last_click channel rows."""
    con = cj_con
    first_with_sessions = con.execute(
        "SELECT COUNT(*) FROM customer_journeys "
        "WHERE attribution_model='first_click' AND sessions IS NOT NULL"
    ).fetchone()[0]
    assert first_with_sessions == 0, "first_click sessions must be NULL (honest, not zero)"

    last_channel_sessions = con.execute(
        "SELECT COUNT(*) FROM customer_journeys "
        "WHERE attribution_model='last_click' AND breakdown_kind='channel' "
        "AND sessions IS NOT NULL AND sessions > 0"
    ).fetchone()[0]
    assert last_channel_sessions > 0, "last_click channel rows must carry sessions"


# ---------------------------------------------------------------------------
# Reconciliation: mart per-axis conversions == fact partition totals (no rows
# created or lost). AC (a).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model,kind,fact_dim",
    [
        ("last_click", "channel", "session_source_medium"),
        ("last_click", "campaign", "session_campaign"),
        ("first_click", "channel", "first_user_source_medium"),
    ],
)
def test_reconciliation_per_axis(cj_con, model, kind, fact_dim):
    """AC (a): SUM(conversions) per (project,date) for a mart axis equals SUM(value) of the
    matching fact partition -- the mart neither creates nor loses conversions."""
    con = cj_con
    rows = con.execute(
        f"""
        WITH mart AS (
            SELECT project_id, date, SUM(conversions) AS v
            FROM customer_journeys
            WHERE attribution_model='{model}' AND breakdown_kind='{kind}'
            GROUP BY project_id, date
        ),
        fact AS (
            SELECT project_id, date, SUM(value) AS v
            FROM fact_acq
            WHERE metric='conversions' AND breakdown_dimension='{fact_dim}'
            GROUP BY project_id, date
        )
        SELECT m.date, m.v AS mart_v, f.v AS fact_v
        FROM mart m JOIN fact f
          ON m.project_id=f.project_id AND m.date=f.date
        """
    ).fetchall()
    assert len(rows) >= 3, "expected >= 3 reconciled days"
    for d, mart_v, fact_v in rows:
        assert mart_v == pytest.approx(fact_v), (
            f"{model}/{kind} on {d}: mart {mart_v} != fact partition {fact_v}"
        )


def test_last_click_not_summed_with_first_click(cj_con):
    """The two perspectives are INDEPENDENT marginals -- never summed. On the CHANNEL axis
    both models exist for the same day and (for a seeded distribution) their totals differ,
    proving they are distinct perspectives, not the same number reused."""
    con = cj_con
    rows = con.execute(
        """
        WITH last_c AS (
            SELECT date, SUM(conversions) AS v FROM customer_journeys
            WHERE attribution_model='last_click' AND breakdown_kind='channel'
            GROUP BY date
        ),
        first_c AS (
            SELECT date, SUM(conversions) AS v FROM customer_journeys
            WHERE attribution_model='first_click' AND breakdown_kind='channel'
            GROUP BY date
        )
        SELECT l.date, l.v, f.v FROM last_c l JOIN first_c f ON l.date=f.date
        """
    ).fetchall()
    assert len(rows) >= 3
    # Both perspectives carry conversions; at least one day must differ (independent draws).
    assert any(lv != fv for _d, lv, fv in rows), (
        "last-click and first-click channel totals are identical every day -- the axes are "
        "not independent perspectives (or one is a copy of the other)"
    )
    for _d, lv, fv in rows:
        assert lv > 0 and fv > 0


# ---------------------------------------------------------------------------
# share_of_conversions: "Part" of the top-N subtotal, sums to ~100 per group,
# NOT pooled across channel + campaign. AC (c).
# ---------------------------------------------------------------------------


def test_share_sums_to_100_per_group(cj_con):
    """AC (c): share_of_conversions sums to ~100 per (project, date, model, breakdown_kind)."""
    con = cj_con
    rows = con.execute(
        """
        SELECT project_id, date, attribution_model, breakdown_kind,
               SUM(share_of_conversions) AS s, SUM(conversions) AS subtotal
        FROM customer_journeys
        GROUP BY project_id, date, attribution_model, breakdown_kind
        HAVING SUM(conversions) > 0
        """
    ).fetchall()
    assert rows, "expected non-empty share groups"
    for _p, d, model, kind, s, _sub in rows:
        assert s == pytest.approx(100.0, abs=0.5), (
            f"{model}/{kind} on {d}: shares sum to {s}, expected ~100"
        )


def test_share_computed_on_axis_subtotal_not_pooled(cj_con):
    """The last_click share denominator is the AXIS subtotal, not the pooled channel +
    campaign subtotal. If it were pooled, channel + campaign shares would each sum to ~50
    and the two together to ~100. We assert channel alone sums to ~100 (its own subtotal)."""
    con = cj_con
    rows = con.execute(
        """
        SELECT date, SUM(share_of_conversions) AS s
        FROM customer_journeys
        WHERE attribution_model='last_click' AND breakdown_kind='channel'
        GROUP BY date
        """
    ).fetchall()
    assert len(rows) >= 3
    for d, s in rows:
        assert s == pytest.approx(100.0, abs=0.5), (
            f"last_click channel shares on {d} sum to {s} -- expected ~100 (axis subtotal, "
            "not a pooled channel+campaign denominator)"
        )


def test_share_matches_manual_ratio_for_a_row(cj_con):
    """Spot-check: share = conversions / axis_subtotal * 100 for a concrete row."""
    con = cj_con
    row = con.execute(
        """
        WITH one AS (
            SELECT project_id, date, attribution_model, breakdown_kind, channel,
                   conversions, share_of_conversions
            FROM customer_journeys
            WHERE attribution_model='last_click' AND breakdown_kind='channel'
              AND conversions > 0
            ORDER BY date, channel LIMIT 1
        )
        SELECT o.conversions, o.share_of_conversions,
               (SELECT SUM(conversions) FROM customer_journeys c
                WHERE c.project_id=o.project_id AND c.date=o.date
                  AND c.attribution_model=o.attribution_model
                  AND c.breakdown_kind=o.breakdown_kind) AS subtotal
        FROM one o
        """
    ).fetchone()
    conv, share, subtotal = row
    assert subtotal > 0
    assert share == pytest.approx(conv / subtotal * 100.0)


# ---------------------------------------------------------------------------
# Provenance (AD-7): pull_id / loaded_at propagated.
# ---------------------------------------------------------------------------


def test_provenance_propagated(cj_con):
    """AD-7: every mart row carries a well-formed pull_id and a loaded_at."""
    con = cj_con
    bad = con.execute(
        "SELECT COUNT(*) FROM customer_journeys "
        "WHERE pull_id IS NULL OR pull_id NOT LIKE 'pull_%' OR loaded_at IS NULL"
    ).fetchone()[0]
    assert bad == 0, "pull_id/loaded_at must be propagated on every row (AD-7)"
