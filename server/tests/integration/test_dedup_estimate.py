"""Epic 17, story 17.2 -- dedup_estimate mart VALUE tests.

dedup_estimate is a READ-ONLY view over fact_daily_kpi + dim_project that estimates the
deduplication of régie-claimed conversions against a project-designated source of truth.
This file proves the mart's VALUE SEMANTICS on a REAL DuckDB seeded over multiple contiguous
days (AI-45), WITHOUT running dbt inside pytest, mirroring dedup_estimate.sql exactly:

  * duplication_rate = SUM(claimed) / verified, computed by hand on a concrete day.
  * deduplicated_contribution = claimed_channel * verified / SUM(claimed); contributions
    across channels sum to verified_total.
  * OPT-OUT: a project with verification_source_type NULL yields ZERO rows.
  * SHOPIFY as source of truth: verified_total reads orders_count 'day_total'.
  * verified = 0 -> duplication_rate NULL (honest degrade, never a disguised 0 / div error).
  * HONESTY 2: claimed is meta-ads' canonical 'ad_id' partition, never the sum of its
    parallel campaign_id / adset_id / ad_id partitions (which each total the day).

It builds the relevant fact_daily_kpi conversions/orders_count partitions and the
dedup_estimate.sql projection as TEMP VIEWs from hand-authored seed rows. Auto-skips when
duckdb is not importable.

ASCII-only (AI-03). No live GA4 / Shopify / Postgres required.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

duckdb = pytest.importorskip(
    "duckdb", reason="duckdb not installed -- dedup_estimate tests skipped"
)

# ---------------------------------------------------------------------------
# fact_daily_kpi conversions/orders_count partitions (only the rows dedup_estimate reads).
#   GA4 conversions: parallel partitions 'country' (canonical, MIN) and 'device_category'
#     -- each totals the day; MIN(breakdown_dimension)='country' is the verified GA4 dim.
#   meta-ads conversions: parallel 'campaign_id' / 'adset_id' / 'ad_id' -- each totals the
#     day; the claimant seed pins 'ad_id' as the canonical claimed partition.
#   shopify orders_count: single 'day_total' partition.
# ---------------------------------------------------------------------------

_FACT_DDL = """
CREATE TABLE fact_daily_kpi (
    project_id VARCHAR, date VARCHAR, connector VARCHAR, metric VARCHAR,
    breakdown_dimension VARCHAR, breakdown_value VARCHAR, value DOUBLE,
    pull_id VARCHAR, loaded_at VARCHAR
)
"""

_DIM_PROJECT_DDL = """
CREATE TABLE dim_project (
    project_id VARCHAR,
    canonical_currency VARCHAR,
    reporting_timezone VARCHAR,
    verification_source_type VARCHAR,
    verification_source_id VARCHAR,
    lead_event_name VARCHAR
)
"""

_CLAIMANTS_DDL = """
CREATE TABLE conversion_claimants (
    connector VARCHAR, canonical_breakdown_dimension VARCHAR
)
"""

# dedup_estimate.sql projection, mirrored EXACTLY (ref() -> local table/view names).
_DEDUP_ESTIMATE_SQL = """
CREATE OR REPLACE TEMP VIEW dedup_estimate AS
WITH prefs AS (
    SELECT project_id, verification_source_type, verification_source_id, lead_event_name
    FROM dim_project
    WHERE verification_source_type IS NOT NULL
),
conv AS (
    SELECT project_id, date, connector, breakdown_dimension, value, pull_id
    FROM fact_daily_kpi
    WHERE metric = 'conversions'
),
claimed AS (
    SELECT c.project_id, c.date, c.connector AS channel_connector,
           SUM(c.value) AS claimed_conversions, MAX(c.pull_id) AS pull_id
    FROM conv c
    JOIN conversion_claimants cl
        ON cl.connector = c.connector
       AND cl.canonical_breakdown_dimension = c.breakdown_dimension
    JOIN prefs p ON p.project_id = c.project_id
    GROUP BY c.project_id, c.date, c.connector
),
claimed_sum AS (
    SELECT project_id, date, SUM(claimed_conversions) AS claimed_total
    FROM claimed GROUP BY project_id, date
),
ga4_canonical_dim AS (
    SELECT project_id, date, connector, MIN(breakdown_dimension) AS dim
    FROM conv WHERE connector = 'google-analytics'
    GROUP BY project_id, date, connector
),
verified_ga4 AS (
    SELECT c.project_id, c.date, SUM(c.value) AS verified_total
    FROM conv c
    JOIN ga4_canonical_dim d
        ON d.project_id = c.project_id AND d.date = c.date
       AND d.connector = c.connector AND d.dim = c.breakdown_dimension
    WHERE c.connector = 'google-analytics'
    GROUP BY c.project_id, c.date
),
verified_shopify AS (
    SELECT project_id, date, SUM(value) AS verified_total
    FROM fact_daily_kpi
    WHERE connector = 'shopify' AND metric = 'orders_count'
      AND breakdown_dimension = 'day_total'
    GROUP BY project_id, date
),
verified AS (
    SELECT p.project_id, p.verification_source_type, p.verification_source_id,
           p.lead_event_name, cs.date,
           CASE p.verification_source_type
               WHEN 'ga4' THEN vg.verified_total
               WHEN 'shopify' THEN vs.verified_total
               ELSE NULL
           END AS verified_total
    FROM prefs p
    JOIN claimed_sum cs ON cs.project_id = p.project_id
    LEFT JOIN verified_ga4 vg ON vg.project_id = p.project_id AND vg.date = cs.date
    LEFT JOIN verified_shopify vs ON vs.project_id = p.project_id AND vs.date = cs.date
)
SELECT cl.project_id, cl.date, cl.channel_connector, cl.claimed_conversions,
       v.verified_total, cs.claimed_total,
       v.verification_source_type, v.verification_source_id, v.lead_event_name,
       cs.claimed_total / NULLIF(v.verified_total, 0) AS duplication_rate,
       v.verified_total * cl.claimed_conversions
           / NULLIF(cs.claimed_total, 0) AS deduplicated_contribution,
       'estimation' AS estimate_label, cl.pull_id
FROM claimed cl
JOIN claimed_sum cs ON cs.project_id = cl.project_id AND cs.date = cl.date
JOIN verified v ON v.project_id = cl.project_id AND v.date = cl.date
"""


def _fact_row(pid, d, conn, metric, dim, val, breakdown_value="x"):
    return (pid, d, conn, metric, dim, breakdown_value, float(val),
            f"pull_{conn}_1", "2026-06-01T00:00:00Z")


def _seed_ga4_project(con):
    """Project 'ga4proj': verification_source_type='ga4'. Three contiguous days.

    Day 2026-06-01: GA4 verified=100 (country partition totals 100; device_category ALSO
    totals 100 -- a parallel partition we must NOT sum with country). meta-ads claims 80 on
    ad_id (its parallel campaign_id/adset_id also total 80). Single claimant v1.
    Days 06-02 / 06-03: other values, to prove multi-day + reconciliation.
    """
    con.execute(_FACT_DDL)
    con.execute(_DIM_PROJECT_DDL)
    con.execute(_CLAIMANTS_DDL)
    con.execute("INSERT INTO conversion_claimants VALUES ('meta-ads', 'ad_id')")
    con.execute(
        "INSERT INTO dim_project VALUES "
        "('ga4proj','EUR','Europe/Paris','ga4','ds_ga4','generate_lead')"
    )

    rows = []
    # ---- 2026-06-01: verified=100, claimed=80 -> rate=0.8, contribution=100 (== verified)
    d = "2026-06-01"
    # GA4 country partition (canonical) totals 100 across two country rows
    rows.append(_fact_row("ga4proj", d, "google-analytics", "conversions", "country", 60, "FR"))
    rows.append(_fact_row("ga4proj", d, "google-analytics", "conversions", "country", 40, "US"))
    # GA4 device_category parallel partition ALSO totals 100 (must NOT be summed with country)
    rows.append(
        _fact_row("ga4proj", d, "google-analytics", "conversions", "device_category", 100, "mobile")
    )
    # meta-ads: three parallel partitions each total 80; claimant seed pins 'ad_id'
    rows.append(_fact_row("ga4proj", d, "meta-ads", "conversions", "ad_id", 80, "ad_1"))
    rows.append(_fact_row("ga4proj", d, "meta-ads", "conversions", "adset_id", 80, "set_1"))
    rows.append(_fact_row("ga4proj", d, "meta-ads", "conversions", "campaign_id", 80, "camp_1"))

    # ---- 2026-06-02: verified=200, claimed=150 -> rate=0.75
    d = "2026-06-02"
    rows.append(_fact_row("ga4proj", d, "google-analytics", "conversions", "country", 200, "FR"))
    rows.append(_fact_row("ga4proj", d, "meta-ads", "conversions", "ad_id", 150, "ad_1"))

    # ---- 2026-06-03: verified=0 -> rate NULL (honest degrade)
    d = "2026-06-03"
    rows.append(_fact_row("ga4proj", d, "google-analytics", "conversions", "country", 0, "FR"))
    rows.append(_fact_row("ga4proj", d, "meta-ads", "conversions", "ad_id", 50, "ad_1"))

    con.executemany(
        "INSERT INTO fact_daily_kpi VALUES (?,?,?,?,?,?,?,?,?)", rows
    )
    con.execute(_DEDUP_ESTIMATE_SQL)


@pytest.fixture()
def ga4_con():
    con = duckdb.connect(":memory:")
    _seed_ga4_project(con)
    try:
        yield con
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Value assertions.
# ---------------------------------------------------------------------------


def test_duplication_rate_manual_single_channel(ga4_con):
    """2026-06-01: verified=100, claimed=80 -> rate=0.8, contribution=100.

    Formule du doc §2.2 : contribution = claimed x verified / SUM(claimed)
    = 80 x 100/80 = 100 == verified. Avec UNE seule régie, TOUTE la vérité lui est
    attribuée — la mise à l'échelle joue dans les DEUX sens (rate < 1 = sous-
    attribution : les régies revendiquent MOINS que le réel, la contribution est
    scalée vers le haut ; rate > 1 = sur-attribution, scalée vers le bas).
    L'invariant testé par test_sum_contributions_equals_verified : Σ == verified.
    """
    row = ga4_con.execute(
        "SELECT verified_total, claimed_total, duplication_rate, "
        "deduplicated_contribution FROM dedup_estimate "
        "WHERE project_id='ga4proj' AND date='2026-06-01' AND channel_connector='meta-ads'"
    ).fetchone()
    verified, claimed_total, rate, contribution = row
    assert verified == pytest.approx(100.0)
    assert claimed_total == pytest.approx(80.0)
    assert rate == pytest.approx(0.8)
    assert contribution == pytest.approx(100.0)  # == verified (une seule régie)


def test_claimed_is_canonical_ad_id_not_summed_parallel(ga4_con):
    """HONESTY 2: claimed_conversions is meta-ads' 'ad_id' total (80), NOT 240 (the sum of
    ad_id + adset_id + campaign_id parallel partitions)."""
    claimed = ga4_con.execute(
        "SELECT claimed_conversions FROM dedup_estimate "
        "WHERE project_id='ga4proj' AND date='2026-06-01' AND channel_connector='meta-ads'"
    ).fetchone()[0]
    assert claimed == pytest.approx(80.0), (
        f"claimed={claimed}; must be the canonical ad_id partition (80), not the summed "
        "parallel partitions (240)"
    )


def test_verified_ga4_is_canonical_country_not_summed_parallel(ga4_con):
    """verified_total for ga4 is the 'country' canonical total (100), NOT 200 (country +
    device_category parallel partitions summed)."""
    verified = ga4_con.execute(
        "SELECT verified_total FROM dedup_estimate "
        "WHERE project_id='ga4proj' AND date='2026-06-01' AND channel_connector='meta-ads'"
    ).fetchone()[0]
    assert verified == pytest.approx(100.0), (
        f"verified={verified}; must be canonical country total (100), not summed parallel "
        "partitions (200)"
    )


def test_contribution_sums_to_verified(ga4_con):
    """Reconciliation: SUM(deduplicated_contribution) per (project,date) == verified_total
    on days with verified > 0."""
    rows = ga4_con.execute(
        "SELECT date, MAX(verified_total), SUM(deduplicated_contribution) "
        "FROM dedup_estimate WHERE verified_total IS NOT NULL AND verified_total <> 0 "
        "GROUP BY date"
    ).fetchall()
    assert rows, "expected verified days"
    for d, verified, sum_contrib in rows:
        assert sum_contrib == pytest.approx(verified), f"{d}: sum contrib != verified"


def test_verified_zero_gives_null_rate(ga4_con):
    """2026-06-03: verified=0 -> duplication_rate NULL (NULLIF(verified,0) guard; never a
    disguised 0% duplication nor a division error). deduplicated_contribution is 0.0 here
    (0 * claimed / claimed_total = 0), NOT NULL -- claimed_total is non-zero, only verified
    is 0, so the rescale yields an honest 0 contribution."""
    row = ga4_con.execute(
        "SELECT verified_total, duplication_rate, deduplicated_contribution "
        "FROM dedup_estimate "
        "WHERE project_id='ga4proj' AND date='2026-06-03' AND channel_connector='meta-ads'"
    ).fetchone()
    verified, rate, contribution = row
    assert verified == pytest.approx(0.0)
    assert rate is None, "duplication_rate must be NULL when verified=0"
    assert contribution == pytest.approx(0.0)


def test_lead_event_name_carried_for_provenance(ga4_con):
    """HONESTY 1: lead_event_name is carried into the output (provenance) even though it
    does NOT (yet) filter verified_total (BLOCKED 16.1 AC10 Phase B)."""
    ev = ga4_con.execute(
        "SELECT DISTINCT lead_event_name FROM dedup_estimate WHERE project_id='ga4proj'"
    ).fetchall()
    assert ev == [("generate_lead",)], ev


def test_estimate_label_constant(ga4_con):
    """AD-9: every row carries estimate_label='estimation'."""
    labels = {
        r[0] for r in ga4_con.execute(
            "SELECT DISTINCT estimate_label FROM dedup_estimate"
        ).fetchall()
    }
    assert labels == {"estimation"}, labels


def test_grain_unique(ga4_con):
    """AI-05: one row per (project, date, channel_connector)."""
    total = ga4_con.execute("SELECT COUNT(*) FROM dedup_estimate").fetchone()[0]
    distinct = ga4_con.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT project_id, date, channel_connector "
        "FROM dedup_estimate)"
    ).fetchone()[0]
    assert total == distinct, "grain not unique"


# ---------------------------------------------------------------------------
# Opt-out: verification_source_type NULL -> zero rows.
# ---------------------------------------------------------------------------


def test_opt_out_project_yields_zero_rows():
    """A project WITHOUT a designated source of truth produces NO dedup_estimate rows,
    even though it HAS claimed régie conversions (dedup is opt-in per project)."""
    con = duckdb.connect(":memory:")
    try:
        con.execute(_FACT_DDL)
        con.execute(_DIM_PROJECT_DDL)
        con.execute(_CLAIMANTS_DDL)
        con.execute("INSERT INTO conversion_claimants VALUES ('meta-ads', 'ad_id')")
        # opt-out: verification_source_type NULL
        con.execute(
            "INSERT INTO dim_project VALUES "
            "('optout','EUR','Europe/Paris',NULL,NULL,NULL)"
        )
        con.executemany(
            "INSERT INTO fact_daily_kpi VALUES (?,?,?,?,?,?,?,?,?)",
            [
                _fact_row(
                    "optout", "2026-06-01", "google-analytics", "conversions", "country", 100, "FR"
                ),
                _fact_row("optout", "2026-06-01", "meta-ads", "conversions", "ad_id", 80, "ad_1"),
            ],
        )
        con.execute(_DEDUP_ESTIMATE_SQL)
        n = con.execute(
            "SELECT COUNT(*) FROM dedup_estimate WHERE project_id='optout'"
        ).fetchone()[0]
        assert n == 0, "opt-out project must produce ZERO dedup_estimate rows"
    finally:
        con.close()


# ---------------------------------------------------------------------------
# review-17-2 F-1 (MAJEUR): the WEIGHTED multi-channel core -- the epic's own
# Given/When/Then example (Meta 80 / Google 60 / LinkedIn 20, verified 100 ->
# contributions 50 / 37.5 / 12.5). With a single claimant, contribution == verified
# trivially and the reconciliation proves NOTHING about the weighting; this test is
# the only real proof of the repartition math.
# ---------------------------------------------------------------------------


def test_multi_channel_weighted_contributions():
    """Meta 80 + Google 60 + LinkedIn 20 revendiques, verite GA4 = 100 :
    rate = 160/100 = 1.6 ; contributions = 80*100/160=50, 60*100/160=37.5,
    20*100/160=12.5 ; Sigma contributions == verified (100)."""
    con = duckdb.connect(":memory:")
    try:
        con.execute(_FACT_DDL)
        con.execute(_DIM_PROJECT_DDL)
        con.execute(_CLAIMANTS_DDL)
        # Three declarative claimants (google-ads / linkedin-ads are FUTURE connectors --
        # the seed extension path B-3: one row each, zero SQL change).
        con.executemany(
            "INSERT INTO conversion_claimants VALUES (?, ?)",
            [
                ("meta-ads", "ad_id"),
                ("google-ads", "campaign_id"),
                ("linkedin-ads", "campaign_id"),
            ],
        )
        con.execute(
            "INSERT INTO dim_project VALUES "
            "('multiproj','EUR','Europe/Paris','ga4','ds_ga4','generate_lead')"
        )
        d = "2026-06-01"
        con.executemany(
            "INSERT INTO fact_daily_kpi VALUES (?,?,?,?,?,?,?,?,?)",
            [
                # GA4 verified truth: country partition totals 100.
                _fact_row("multiproj", d, "google-analytics", "conversions", "country", 100, "FR"),
                # The three claimants (each on its declared canonical partition).
                _fact_row("multiproj", d, "meta-ads", "conversions", "ad_id", 80, "ad_1"),
                _fact_row("multiproj", d, "google-ads", "conversions", "campaign_id", 60, "c_1"),
                _fact_row("multiproj", d, "linkedin-ads", "conversions", "campaign_id", 20, "c_9"),
            ],
        )
        con.execute(_DEDUP_ESTIMATE_SQL)
        rows = con.execute(
            "SELECT channel_connector, claimed_conversions, claimed_total, verified_total, "
            "duplication_rate, deduplicated_contribution FROM dedup_estimate "
            "WHERE project_id='multiproj' ORDER BY channel_connector"
        ).fetchall()
        assert len(rows) == 3, "one row per claiming channel"
        by_channel = {r[0]: r for r in rows}
        for _, claimed, claimed_total, verified, rate, _c in rows:
            assert claimed_total == pytest.approx(160.0)
            assert verified == pytest.approx(100.0)
            assert rate == pytest.approx(1.6)
        # The epic's hand-computed repartition: claimed x verified / Sigma claimed.
        assert by_channel["meta-ads"][5] == pytest.approx(50.0)
        assert by_channel["google-ads"][5] == pytest.approx(37.5)
        assert by_channel["linkedin-ads"][5] == pytest.approx(12.5)
        # Invariant: Sigma contributions == verified.
        total = sum(r[5] for r in rows)
        assert total == pytest.approx(100.0)
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Shopify as source of truth: verified_total reads orders_count 'day_total'.
# ---------------------------------------------------------------------------


def test_shopify_source_of_truth_uses_orders_count():
    """verification_source_type='shopify' -> verified_total = orders_count 'day_total'.
    Day: orders_count=50, meta-ads claims 90 -> rate=1.8, contribution=50."""
    con = duckdb.connect(":memory:")
    try:
        con.execute(_FACT_DDL)
        con.execute(_DIM_PROJECT_DDL)
        con.execute(_CLAIMANTS_DDL)
        con.execute("INSERT INTO conversion_claimants VALUES ('meta-ads', 'ad_id')")
        con.execute(
            "INSERT INTO dim_project VALUES "
            "('shopproj','EUR','Europe/Paris','shopify','ds_shop',NULL)"
        )
        con.executemany(
            "INSERT INTO fact_daily_kpi VALUES (?,?,?,?,?,?,?,?,?)",
            [
                # shopify orders_count day_total = 50 (the verified truth for sales)
                _fact_row(
                    "shopproj", "2026-06-01", "shopify", "orders_count", "day_total", 50, "all"
                ),
                # shopify ALSO lands revenue -- must NOT be picked as verified_total
                _fact_row("shopproj", "2026-06-01", "shopify", "revenue", "day_total", 9999, "all"),
                # meta-ads claims 90 conversions on ad_id
                _fact_row("shopproj", "2026-06-01", "meta-ads", "conversions", "ad_id", 90, "ad_1"),
                # GA4 conversions present but IRRELEVANT (truth is shopify, not ga4)
                _fact_row(
                    "shopproj", "2026-06-01", "google-analytics", "conversions",
                    "country", 300, "FR"
                ),
            ],
        )
        con.execute(_DEDUP_ESTIMATE_SQL)
        row = con.execute(
            "SELECT verified_total, claimed_total, duplication_rate, "
            "deduplicated_contribution, verification_source_type FROM dedup_estimate "
            "WHERE project_id='shopproj' AND date='2026-06-01'"
        ).fetchone()
        verified, claimed_total, rate, contribution, vtype = row
        assert vtype == "shopify"
        assert verified == pytest.approx(50.0), (
            f"verified={verified}; must be orders_count (50), not revenue (9999) nor GA4 (300)"
        )
        assert claimed_total == pytest.approx(90.0)
        assert rate == pytest.approx(1.8)
        assert contribution == pytest.approx(50.0)
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Stripe as source of truth: verified NULL v1 (module 15.7 not delivered) -> rate NULL.
# ---------------------------------------------------------------------------


def test_stripe_source_of_truth_verified_null_v1():
    """verification_source_type='stripe' -> verified_total NULL v1 (15.7 not delivered),
    duplication_rate NULL, deduplicated_contribution NULL. Never an invented number."""
    con = duckdb.connect(":memory:")
    try:
        con.execute(_FACT_DDL)
        con.execute(_DIM_PROJECT_DDL)
        con.execute(_CLAIMANTS_DDL)
        con.execute("INSERT INTO conversion_claimants VALUES ('meta-ads', 'ad_id')")
        con.execute(
            "INSERT INTO dim_project VALUES "
            "('stripeproj','EUR','Europe/Paris','stripe','ds_stripe',NULL)"
        )
        con.executemany(
            "INSERT INTO fact_daily_kpi VALUES (?,?,?,?,?,?,?,?,?)",
            [
                _fact_row(
                    "stripeproj", "2026-06-01", "meta-ads", "conversions", "ad_id", 70, "ad_1"
                ),
            ],
        )
        con.execute(_DEDUP_ESTIMATE_SQL)
        row = con.execute(
            "SELECT verified_total, claimed_total, duplication_rate, "
            "deduplicated_contribution FROM dedup_estimate WHERE project_id='stripeproj'"
        ).fetchone()
        assert row is not None, "stripe project still emits a claimed row (verified NULL)"
        verified, claimed_total, rate, contribution = row
        assert verified is None, "stripe verified_total must be NULL v1 (15.7 not delivered)"
        assert claimed_total == pytest.approx(70.0)
        assert rate is None, "duplication_rate must be NULL when verified is NULL"
        assert contribution is None, "contribution must be NULL when verified is NULL"
    finally:
        con.close()
