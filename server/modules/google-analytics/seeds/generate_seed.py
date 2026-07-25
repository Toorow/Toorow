"""GA4 seed CSV generator — Story 1.4, T1.2.

Produces 90 days of realistic GA4 KPI data for the local dev/CI loop.
Output file: server/modules/google-analytics/seeds/ga4_seed.csv

Usage:
    python generate_seed.py                 # writes ga4_seed.csv next to this file
    python generate_seed.py --out /tmp/x.csv

# AD-4: country uses GA4 full-name format; ISO-3166 normalization deferred to Story 4.1
# AI-09: linear trend + anomaly injection added for Story 5.4 anomaly detection signal.
"""

from __future__ import annotations

import argparse
import csv
import os
import random
from datetime import date, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Seed constants — realistic weekly seasonality and device/country baselines.
# These patterns make heatmap widgets (Story 1.6) and anomaly detection
# (Story 5.4) meaningful; a flat seed would not prove the visualization loop.
# ---------------------------------------------------------------------------

# AI-09: anomaly injection day index (0-based, out of 90 days).
# Day 75 is injected with sessions = 3× baseline for Story 5.4 testability.
# Configurable via ANOMALY_SEED_DAY env var (default 75).
# AI-09: anomaly injected at day {ANOMALY_SEED_DAY} for Story 5.4 testability
ANOMALY_SEED_DAY: int = int(os.environ.get("ANOMALY_SEED_DAY", "75"))

# AI-09: mild linear trend — sessions grow +0.5%/day over 90 days.
# Applied as a multiplier on top of weekly seasonality.
TREND_RATE_PER_DAY: float = 0.005  # +0.5% per day

# AI-66 (2026-07-21): deterministic seed defaults. The evals harness (Epic 14) pins
# fixture_sha256 against this data, so a regenerated seed MUST be byte-reproducible across
# machines/days -- an unseeded random.Random() + date.today() made it machine-day-local.
# A fixed RNG seed removes the OS-entropy non-determinism; the end-date anchor matches the
# evals corpus as_of_anchor (2026-07-15) so the 90-day window always covers the corpus
# dates. Override via env (TOOROW_SEED_RNG / TOOROW_SEED_END_DATE) for ad-hoc random runs.
_ANCHOR_END_DATE: date = date.fromisoformat(
    os.environ.get("TOOROW_SEED_END_DATE", "2026-07-15")
)
_DEFAULT_RNG_SEED: int = int(os.environ.get("TOOROW_SEED_RNG", "15072026"))

WEEKDAY_MULTIPLIERS: dict[int, float] = {
    0: 1.00,  # Monday
    1: 1.05,  # Tuesday (peak)
    2: 1.03,  # Wednesday
    3: 1.02,  # Thursday
    4: 0.95,  # Friday (slight drop before weekend)
    5: 0.65,  # Saturday (~35% lower)
    6: 0.60,  # Sunday (lowest — weekend trough)
}

# Per-device daily session baseline (France reference; other countries scaled)
DEVICE_BASELINES: dict[str, int] = {
    "desktop": 1200,
    "mobile": 800,
    "tablet": 200,
}

# Country traffic weights relative to France = 1.0
COUNTRY_WEIGHTS: dict[str, float] = {
    "France": 1.00,
    "Germany": 0.65,
    "United Kingdom": 0.55,
    "United States": 0.45,
    "Spain": 0.40,
}

# Per-device conversion rate range (min, max) — desktop converts best
DEVICE_CVR_RANGE: dict[str, tuple[float, float]] = {
    "desktop": (0.03, 0.07),
    "mobile": (0.01, 0.04),
    "tablet": (0.02, 0.05),
}

NOISE_FACTOR = 0.15  # +/-15% uniform noise so lines are not flat

COLUMNS = ["date", "device_category", "country", "sessions", "active_users", "conversions"]

# ---------------------------------------------------------------------------
# Epic 10, story 10.1: user_type_daily profile seed (GA4 newVsReturning).
# Split weights applied to the SAME per-day baseline total the standard seed
# produces, so SUM(sessions) over user_type equals the standard daily total
# within noise tolerance (reconciliation AC9c proves this on the mart).
# new ~38%, returning ~60%, unknown ~2% (weights sum to 1.0).
# ---------------------------------------------------------------------------
USER_TYPE_WEIGHTS: dict[str, float] = {
    "new": 0.38,
    "returning": 0.60,
    "unknown": 0.02,
}

USER_TYPE_COLUMNS = ["date", "user_type", "sessions", "active_users", "conversions"]

# ---------------------------------------------------------------------------
# Epic 10, story 10.3: pages_daily seeds (GA4 landingPage + pagePath).
#
# TWO independent marginal top-N lists (Task 1 design decision): entry pages
# (sessions by landing_page) and top pages (screen_page_views by page). Both use a
# Zipf-like distribution over ~30 URL paths (a few high-traffic pages, a long tail),
# then are TOP-N bounded to page_top_n per day so the seed mirrors the real pull
# (the tail beyond N is dropped). Weekday seasonality + linear trend carried from
# the standard seed. ASCII-only stdout (AI-03).
#
# NOT a full reconciliation: because the seed drops the tail beyond N, the page
# partitions do NOT sum to the connector daily total -- this is intentional and
# matches the mart's top-N documentation (test_ga4_pages_reconciliation.py asserts
# other partitions unchanged + <= N distinct pages/day, NOT a sum-to-total).
# ---------------------------------------------------------------------------

# ~30 URL paths for the Zipf-like page catalog. Order = descending popularity rank
# (index 0 = most popular). A realistic mix of a few hero pages + a long tail.
PAGE_CATALOG: list[str] = [
    "/",
    "/pricing",
    "/features",
    "/blog",
    "/about",
    "/contact",
    "/login",
    "/signup",
    "/docs",
    "/docs/quickstart",
    "/blog/launch",
    "/blog/roadmap",
    "/blog/case-study",
    "/product",
    "/product/analytics",
    "/product/reporting",
    "/integrations",
    "/integrations/ga4",
    "/customers",
    "/careers",
    "/careers/engineering",
    "/legal/privacy",
    "/legal/terms",
    "/support",
    "/support/faq",
    "/changelog",
    "/status",
    "/partners",
    "/webinars",
    "/press",
]

# GA4 page top-N bound (mirrors manifest extraction_capabilities.row_limit +
# connector _get_page_top_n). Env override GA4_PAGE_TOP_N, clamped to [10, 200].
_PAGE_TOP_N_DEFAULT = 50
_PAGE_TOP_N_MIN = 10
_PAGE_TOP_N_MAX = 200

# Baseline day-total views for the #1 (rank-0) page, before Zipf decay. Zipf weight
# for rank r is 1/(r+1), so page r gets base / (r+1) views (a few hero pages carry
# most traffic, the tail is small). Landing sessions use a smaller baseline than
# page views (a page is viewed more often than it is the session entry point).
LANDING_RANK0_BASELINE = 900
PAGEVIEW_RANK0_BASELINE = 2500

LANDING_COLUMNS = ["date", "landing_page", "sessions"]
PATHS_COLUMNS = ["date", "page", "screen_page_views"]

# ---------------------------------------------------------------------------
# Epic 16, story 16.1: acquisition seeds (GA4 last-click + first-click).
#
# TWO independent marginal top-N lists (Task 1): last click = conversions + sessions
# by session_source_medium x session_campaign; first click = conversions by
# first_user_source_medium. Both use a realistic channel distribution (a few high-
# converting paid/organic channels + a long tail), then are TOP-N bounded to
# get_page_top_n() per day (mirrors the pull). Weekday seasonality + linear trend
# carried from the standard seed. ASCII-only stdout (AI-03).
#
# NOT a full reconciliation: the seed drops the tail beyond N, so the acquisition
# partitions do NOT sum to the connector daily total -- intentional, matches the mart's
# top-N documentation (test_ga4_acquisition_reconciliation.py asserts other partitions
# unchanged + <= N distinct values/day + acquisition never exceeds the connector total).
# ---------------------------------------------------------------------------

# Realistic source/medium channels, descending by conversion popularity (index 0 = top).
# Includes the paid / organic / direct / referral / email mix the story asks for, plus
# the (not set)->unknown and (direct)->direct canonical buckets (never dropped).
ACQ_SOURCE_MEDIUM: list[str] = [
    "google / organic",
    "google / cpc",
    "direct",            # (direct) -> 'direct' canonical bucket
    "facebook / cpc",
    "newsletter / email",
    "bing / organic",
    "linkedin / cpc",
    "youtube / referral",
    "partner.example.com / referral",
    "instagram / social",
    "reddit / referral",
    "unknown",           # (not set) -> 'unknown' canonical bucket
]

# Long tail of campaign names (for the last-click session_campaign axis).
ACQ_CAMPAIGNS: list[str] = [
    "brand-search",
    "spring-sale",
    "retargeting",
    "lookalike-eu",
    "newsletter-oct",
    "webinar-attribution",
    "black-friday",
    "always-on",
    "unknown",           # (not set) -> 'unknown' campaign bucket
]

# Baseline day-total conversions for the rank-0 channel before Zipf decay.
# CALIBRATED against the standard seed (review-16-1 central verification): an acquisition
# partition is a SUBSET of the same day's conversions, so its sum must stay <= the
# connector day total (~272 conversions/day, ~2990 sessions/day from the standard seed).
# Grid sum ~= RANK0 * H(12) * H(9) ~= RANK0 * 8.78. The two seeds draw NOISE
# INDEPENDENTLY (both share weekday/trend multipliers), and observed daily ratio swings
# reach ~1.25x -- so the MEAN must sit low enough that noise never crosses the 1.05
# reconciliation margin: RANK0=18 lands ~158/day (~0.58x the day total); worst-case
# noise ~0.58 x 1.25 ~= 0.73 < 1.05. (First calibration 26 -> ~0.84x still tripped the
# dbt guard on 4 high-noise days; the original 60 -> ~1.9x was the real bug.)
ACQ_RANK0_CONVERSIONS = 18
# Sessions per conversion multiplier (a channel drives more sessions than conversions).
# 10 -> ~1580 sessions/day (~0.53x the ~2990/day standard total): subset-consistent.
ACQ_SESSIONS_PER_CONVERSION = 10

ACQ_SESSION_COLUMNS = [
    "date", "session_source_medium", "session_campaign", "conversions", "sessions"
]
ACQ_FIRST_USER_COLUMNS = ["date", "first_user_source_medium", "conversions"]


def _acq_zipf(rank: int, base: float) -> float:
    """Zipf-like decay: rank-0 carries base, rank r ~ base / (r + 1)."""
    return base / (rank + 1)


def generate_acquisition_session_rows(
    end_date: date | None = None,
    *,
    rng: random.Random | None = None,
    anomaly_seed_day: int | None = None,
) -> list[dict]:
    """Generate 90 days of GA4 last-click acquisition seed rows (top-N bounded).

    Columns: date, session_source_medium, session_campaign, conversions, sessions.
    Zipf-like over the (source_medium x campaign) grid, weekday seasonality + linear
    trend + noise, top-N bounded to get_page_top_n() per day. ASCII-only stdout (AI-03).
    """
    if end_date is None:
        end_date = _ANCHOR_END_DATE
    if rng is None:
        rng = random.Random(_DEFAULT_RNG_SEED)
    if anomaly_seed_day is None:
        anomaly_seed_day = ANOMALY_SEED_DAY

    start_date = end_date - timedelta(days=90)
    dates = [start_date + timedelta(days=i) for i in range(90)]
    top_n = get_page_top_n()

    rows: list[dict] = []
    for day_idx, day in enumerate(dates):
        multiplier = WEEKDAY_MULTIPLIERS[day.weekday()]
        trend_multiplier = 1.0 + (day_idx * TREND_RATE_PER_DAY)

        day_rows: list[dict] = []
        for s_rank, source_medium in enumerate(ACQ_SOURCE_MEDIUM):
            for c_rank, campaign in enumerate(ACQ_CAMPAIGNS):
                base = (
                    _acq_zipf(s_rank, ACQ_RANK0_CONVERSIONS)
                    * _acq_zipf(c_rank, 1.0)
                    * multiplier
                    * trend_multiplier
                )
                conversions = max(0, round(_noisy(base, rng)))
                if conversions == 0:
                    continue
                # AI-09 parity: triple the rank-0 combo on the anomaly day.
                if day_idx == anomaly_seed_day and s_rank == 0 and c_rank == 0:
                    conversions = max(1, round(conversions * 3.0))
                sessions = max(
                    conversions,
                    round(conversions * ACQ_SESSIONS_PER_CONVERSION * rng.uniform(0.85, 1.15)),
                )
                day_rows.append({
                    "date": day.isoformat(),
                    "session_source_medium": source_medium,
                    "session_campaign": campaign,
                    "conversions": conversions,
                    "sessions": sessions,
                })

        # TOP-N bound: keep only the highest-converting combos (orderBys conversions
        # DESC + limit=top_n). The tail is dropped by design.
        day_rows.sort(key=lambda r: r["conversions"], reverse=True)
        rows.extend(day_rows[:top_n])
    return rows


def generate_acquisition_first_user_rows(
    end_date: date | None = None,
    *,
    rng: random.Random | None = None,
    anomaly_seed_day: int | None = None,
) -> list[dict]:
    """Generate 90 days of GA4 first-click acquisition seed rows (top-N bounded).

    Columns: date, first_user_source_medium, conversions. Zipf-like over
    ACQ_SOURCE_MEDIUM, weekday seasonality + linear trend + noise, top-N bounded to
    get_page_top_n() per day. ASCII-only stdout (AI-03).
    """
    if end_date is None:
        end_date = _ANCHOR_END_DATE
    if rng is None:
        rng = random.Random(_DEFAULT_RNG_SEED)
    if anomaly_seed_day is None:
        anomaly_seed_day = ANOMALY_SEED_DAY

    start_date = end_date - timedelta(days=90)
    dates = [start_date + timedelta(days=i) for i in range(90)]
    top_n = get_page_top_n()

    rows: list[dict] = []
    for day_idx, day in enumerate(dates):
        multiplier = WEEKDAY_MULTIPLIERS[day.weekday()]
        trend_multiplier = 1.0 + (day_idx * TREND_RATE_PER_DAY)

        day_rows: list[dict] = []
        for s_rank, source_medium in enumerate(ACQ_SOURCE_MEDIUM):
            base = _acq_zipf(s_rank, ACQ_RANK0_CONVERSIONS) * multiplier * trend_multiplier
            conversions = max(0, round(_noisy(base, rng)))
            if conversions == 0:
                continue
            if day_idx == anomaly_seed_day and s_rank == 0:
                conversions = max(1, round(conversions * 3.0))
            day_rows.append({
                "date": day.isoformat(),
                "first_user_source_medium": source_medium,
                "conversions": conversions,
            })

        day_rows.sort(key=lambda r: r["conversions"], reverse=True)
        rows.extend(day_rows[:top_n])
    return rows


# ---------------------------------------------------------------------------
# Epic 17, story 17.4: GA4 transactions seed (GA4 transactionId e-commerce),
# CORRELATED with the Shopify orders seed so the transaction_reconciliation mart
# has a realistic MEASURED coverage.
#
# CALIBRATION (leçon 16.1: a partition/overlap must be a SUBSET of its source, never
# more than the source): the GA4 transactions seed is built by SAMPLING the Shopify
# seed's transaction_ids -- a ~85% recovery (GA4 confirms most Shopify sales), plus a
# small handful of GA4-only ids (transactions GA4 saw that Shopify did not, e.g. test
# orders / cross-domain noise). The Shopify seed also leaves ~3% of orders WITHOUT a
# transaction_id (F-7 blind spot) -- those are NEVER in GA4 (no join key), and the mart
# reports them as shopify_orders_without_txn. Net: matched ~= 85% of the reconcilable
# Shopify orders, with a few ga4_only and shopify_only rows. Deterministic (fixed seed).
# ---------------------------------------------------------------------------

# Fraction of Shopify (with-txn) orders that reappear in GA4 (measured recovery ~85%).
TXN_GA4_RECOVERY = 0.85
# A few GA4-only transaction ids per ~100 orders (transactions GA4 saw, Shopify did not).
TXN_GA4_ONLY_FRACTION = 0.04
# GA4 conversions per transaction (a transaction is one purchase conversion event).
TXN_CONVERSIONS_PER = 1

TXN_COLUMNS = ["date", "transaction_id", "conversions", "purchase_revenue"]


def _load_shopify_seed_generator():
    """Import the Shopify seed generator (module-owned) to correlate the GA4 txn seed.

    The Shopify seed is the SOURCE of truth for the transaction_ids; the GA4 txn seed is a
    SUBSET of it (plus a few ga4_only). We import it by path (seeds/ is not a package),
    mirroring the integration-test import pattern.
    """
    import importlib.util  # noqa: PLC0415

    # __file__ = server/modules/google-analytics/seeds/generate_seed.py
    # parents[2] = server/modules ; then shopify/seeds/generate_shopify_seed.py
    shopify_gen_path = (
        Path(__file__).resolve().parents[2]
        / "shopify"
        / "seeds"
        / "generate_shopify_seed.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_shopify_seed_for_ga4_txn", str(shopify_gen_path)
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def generate_transactions_rows(
    end_date: date | None = None,
    *,
    rng: random.Random | None = None,
    shopify_rows: list[dict] | None = None,
) -> list[dict]:
    """Generate GA4 transactions seed rows correlated with the Shopify orders seed.

    Columns: date, transaction_id, conversions, purchase_revenue. Built by sampling
    ~TXN_GA4_RECOVERY of the Shopify seed's WITH-txn orders (the measured recovery) and
    adding a few GA4-only ids. purchase_revenue mirrors the Shopify revenue for matched
    rows (GA4 and Shopify agree on the sale amount within the seed), independent noise
    otherwise. MAGNITUDE DISCIPLINE (leçon 16.1): the matched set is a strict SUBSET of the
    Shopify with-txn orders -- GA4 never reports MORE matched transactions than Shopify has.

    ASCII-only stdout (AI-03).
    """
    if end_date is None:
        end_date = _ANCHOR_END_DATE
    if rng is None:
        rng = random.Random(17_4_2026)

    if shopify_rows is None:
        shopify_gen = _load_shopify_seed_generator()
        # Same end_date so the date windows align; the Shopify generator owns its own
        # fixed seed internally, so its transaction_ids are reproducible.
        shopify_rows = shopify_gen.generate_rows(days=90, end_date=end_date)

    rows: list[dict] = []
    # 1. Matched + shopify_only: walk Shopify orders that HAVE a transaction_id. Keep
    #    ~TXN_GA4_RECOVERY of them in GA4 (matched); the rest are shopify_only (absent here).
    ga4_only_pool: list[str] = []
    for r in shopify_rows:
        txn = r.get("transaction_id")
        if txn is None:
            # F-7 blind spot: no transaction_id -> never reaches GA4 (no join key).
            continue
        if rng.random() < TXN_GA4_RECOVERY:
            rows.append(
                {
                    "date": r["date"],
                    "transaction_id": str(txn),
                    "conversions": TXN_CONVERSIONS_PER,
                    # GA4 and Shopify agree on the amount for a matched sale (within seed).
                    "purchase_revenue": round(float(r["revenue"]), 2),
                }
            )
        # A small share of Shopify ids seed the ga4_only synthetic pool's date/amount basis.
        if rng.random() < TXN_GA4_ONLY_FRACTION:
            ga4_only_pool.append(r["date"])

    # 2. GA4-only: a few transactions GA4 saw with ids Shopify never produced. Derive ids
    #    OUTSIDE the Shopify id space (prefix 'ga4only-') so they can NEVER accidentally
    #    match a Shopify id -- an honest ga4_only bucket.
    for i, d in enumerate(ga4_only_pool):
        rows.append(
            {
                "date": d,
                "transaction_id": f"ga4only-{i}-{rng.randint(10_000, 99_999)}",
                "conversions": TXN_CONVERSIONS_PER,
                "purchase_revenue": round(rng.uniform(28.0, 220.0), 2),
            }
        )

    return rows


def get_page_top_n() -> int:
    """Resolve the seed's top-N page bound (env GA4_PAGE_TOP_N, clamped [10, 200])."""
    try:
        n = int(os.environ.get("GA4_PAGE_TOP_N", str(_PAGE_TOP_N_DEFAULT)))
    except (ValueError, TypeError):
        n = _PAGE_TOP_N_DEFAULT
    return max(_PAGE_TOP_N_MIN, min(_PAGE_TOP_N_MAX, n))


def _generate_page_rows(
    value_column: str,
    rank0_baseline: float,
    end_date: date | None,
    rng: random.Random | None,
    anomaly_seed_day: int | None,
) -> list[dict]:
    """Generic Zipf-like top-N page seed generator (shared by landing + paths).

    Produces, per day, a Zipf-weighted value for each page in PAGE_CATALOG
    (value ~ rank0_baseline / (rank + 1)), applies weekday seasonality + linear
    trend + noise, then keeps only the TOP page_top_n rows for the day (sorted by
    value DESC). Emits rows shaped {date, <dim>, <value_column>} where <dim> is
    'landing_page' or 'page' per PAGE_CATALOG entry. The dim column name is derived
    from value_column so callers stay thin.
    """
    if end_date is None:
        end_date = _ANCHOR_END_DATE
    if rng is None:
        rng = random.Random(_DEFAULT_RNG_SEED)
    if anomaly_seed_day is None:
        anomaly_seed_day = ANOMALY_SEED_DAY

    dim_column = "landing_page" if value_column == "sessions" else "page"

    start_date = end_date - timedelta(days=90)
    dates = [start_date + timedelta(days=i) for i in range(90)]
    top_n = get_page_top_n()

    rows: list[dict] = []
    for day_idx, day in enumerate(dates):
        multiplier = WEEKDAY_MULTIPLIERS[day.weekday()]
        trend_multiplier = 1.0 + (day_idx * TREND_RATE_PER_DAY)

        day_rows: list[dict] = []
        for rank, path in enumerate(PAGE_CATALOG):
            # Zipf-like decay: rank-0 page carries rank0_baseline, rank r ~ 1/(r+1).
            base = (rank0_baseline / (rank + 1)) * multiplier * trend_multiplier
            value = max(1, round(_noisy(base, rng)))
            # AI-09 parity: triple the hero page on the anomaly day so the seed has
            # a detectable spike on the pages profile too (isolated to rank-0).
            if day_idx == anomaly_seed_day and rank == 0:
                value = max(1, round(value * 3.0))
            day_rows.append({
                "date": day.isoformat(),
                dim_column: path,
                value_column: value,
            })

        # TOP-N bound: keep only the highest-value pages for the day (the pull's
        # orderBys metric DESC + limit=page_top_n). The tail is dropped by design.
        day_rows.sort(key=lambda r: r[value_column], reverse=True)
        rows.extend(day_rows[:top_n])
    return rows


def generate_landing_rows(
    end_date: date | None = None,
    *,
    rng: random.Random | None = None,
    anomaly_seed_day: int | None = None,
) -> list[dict]:
    """Generate 90 days of GA4 entry-page seed rows (sessions by landing_page).

    Columns: date, landing_page, sessions. Zipf-like over PAGE_CATALOG, top-N bounded
    to page_top_n per day. ASCII-only stdout (AI-03).
    """
    return _generate_page_rows(
        "sessions", LANDING_RANK0_BASELINE, end_date, rng, anomaly_seed_day
    )


def generate_paths_rows(
    end_date: date | None = None,
    *,
    rng: random.Random | None = None,
    anomaly_seed_day: int | None = None,
) -> list[dict]:
    """Generate 90 days of GA4 top-page seed rows (screen_page_views by page).

    Columns: date, page, screen_page_views. Zipf-like over PAGE_CATALOG, top-N
    bounded to page_top_n per day. ASCII-only stdout (AI-03).
    """
    return _generate_page_rows(
        "screen_page_views", PAGEVIEW_RANK0_BASELINE, end_date, rng, anomaly_seed_day
    )

# Seed CSV uses canonical names directly — real GA4 API fields mapped in
# connector.py (Story 2.7+)


def _noisy(base: float, rng: random.Random) -> float:
    """Add ±NOISE_FACTOR uniform noise to a base value."""
    return base * (1.0 + rng.uniform(-NOISE_FACTOR, NOISE_FACTOR))


def generate_rows(
    end_date: date | None = None,
    *,
    rng: random.Random | None = None,
    anomaly_seed_day: int | None = None,
) -> list[dict]:
    """Generate 90 days of GA4 seed rows ending on *end_date* (exclusive).

    Parameters
    ----------
    end_date:
        The day *after* the last data day (i.e. today). Defaults to today.
    rng:
        Optional seeded random instance for reproducible tests.
    anomaly_seed_day:
        Day index (0-based) for the injected anomaly. Defaults to ANOMALY_SEED_DAY.
        AI-09: pass 75 (or ANOMALY_SEED_DAY) to inject a 3× sessions spike.
    """
    if end_date is None:
        end_date = _ANCHOR_END_DATE
    if rng is None:
        rng = random.Random(_DEFAULT_RNG_SEED)
    if anomaly_seed_day is None:
        anomaly_seed_day = ANOMALY_SEED_DAY

    start_date = end_date - timedelta(days=90)
    # 90 days: [start_date, end_date)  i.e. end_date-90 ... end_date-1
    dates = [start_date + timedelta(days=i) for i in range(90)]

    rows: list[dict] = []
    for day_idx, day in enumerate(dates):
        multiplier = WEEKDAY_MULTIPLIERS[day.weekday()]

        # AI-09: linear trend — sessions grow +0.5%/day (TREND_RATE_PER_DAY).
        # Applied on top of weekday seasonality to make anomaly detection signal
        # meaningful over 90-day window. Other metrics (active_users, conversions)
        # also trend naturally since they derive from sessions.
        trend_multiplier = 1.0 + (day_idx * TREND_RATE_PER_DAY)

        for device, base_sessions in DEVICE_BASELINES.items():
            cvr_lo, cvr_hi = DEVICE_CVR_RANGE[device]
            for country, weight in COUNTRY_WEIGHTS.items():
                base = base_sessions * weight * multiplier * trend_multiplier
                sessions = max(1, round(_noisy(base, rng)))

                # AI-09: anomaly injected at day ANOMALY_SEED_DAY for Story 5.4 testability.
                # Sessions value is 3× the expected baseline on that date.
                # Only sessions is spiked — active_users and conversions keep normal values
                # so the anomaly is isolated to the sessions metric.
                if day_idx == anomaly_seed_day:
                    sessions = max(1, round(sessions * 3.0))

                active_users = max(1, min(sessions, round(sessions * rng.uniform(0.70, 0.95))))
                conversions = max(
                    0, min(active_users, round(active_users * rng.uniform(cvr_lo, cvr_hi)))
                )
                rows.append(
                    {
                        "date": day.isoformat(),
                        "device_category": device,
                        "country": country,
                        "sessions": sessions,
                        "active_users": active_users,
                        "conversions": conversions,
                    }
                )
    return rows


def _daily_session_baseline(day_idx: int, day: date) -> float:
    """Return the per-day TOTAL sessions baseline (summed over device x country).

    This mirrors the base (pre-noise) sessions the standard seed produces, summed
    across all device x country cells for the day. The user_type seed splits THIS
    total by USER_TYPE_WEIGHTS so the two profiles reconcile within noise (AC9c).
    """
    multiplier = WEEKDAY_MULTIPLIERS[day.weekday()]
    trend_multiplier = 1.0 + (day_idx * TREND_RATE_PER_DAY)
    total = 0.0
    for base_sessions in DEVICE_BASELINES.values():
        for weight in COUNTRY_WEIGHTS.values():
            total += base_sessions * weight * multiplier * trend_multiplier
    return total


def generate_user_type_rows(
    end_date: date | None = None,
    *,
    rng: random.Random | None = None,
    anomaly_seed_day: int | None = None,
) -> list[dict]:
    """Generate 90 days x 3 user_type values of GA4 user_type_daily seed rows.

    Columns: date, user_type, sessions, active_users, conversions.
    Split: new ~38%, returning ~60%, unknown ~2% of the daily session total
    (USER_TYPE_WEIGHTS), with +/-15% noise and the SAME weekday seasonality +
    linear trend carried from the standard seed. The daily total sessions over
    the three user_type slices equals the standard_daily total within noise
    tolerance (reconciliation AC9c).

    ASCII-only stdout (AI-03).
    """
    if end_date is None:
        end_date = _ANCHOR_END_DATE
    if rng is None:
        rng = random.Random(_DEFAULT_RNG_SEED)
    if anomaly_seed_day is None:
        anomaly_seed_day = ANOMALY_SEED_DAY

    start_date = end_date - timedelta(days=90)
    dates = [start_date + timedelta(days=i) for i in range(90)]

    rows: list[dict] = []
    for day_idx, day in enumerate(dates):
        day_total = _daily_session_baseline(day_idx, day)
        # Anomaly parity: the standard seed triples EVERY sessions cell on the
        # anomaly day, so the daily total triples too -- mirror it here so the
        # reconciliation holds on the anomaly day as well.
        if day_idx == anomaly_seed_day:
            day_total *= 3.0

        for user_type, weight in USER_TYPE_WEIGHTS.items():
            base = day_total * weight
            sessions = max(1, round(_noisy(base, rng)))
            active_users = max(1, min(sessions, round(sessions * rng.uniform(0.70, 0.95))))
            conversions = max(
                0, min(active_users, round(active_users * rng.uniform(0.02, 0.06)))
            )
            rows.append(
                {
                    "date": day.isoformat(),
                    "user_type": user_type,
                    "sessions": sessions,
                    "active_users": active_users,
                    "conversions": conversions,
                }
            )
    return rows


def write_csv(rows: list[dict], path: str | os.PathLike, columns: list[str] | None = None) -> None:
    """Write rows to *path* as a CSV with header.

    *columns* defaults to the standard_daily COLUMNS; pass USER_TYPE_COLUMNS for
    the user_type_daily profile (Story 10.1).
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns or COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate GA4 seed CSV")
    parser.add_argument(
        "--profile",
        choices=[
            "standard_daily",
            "user_type_daily",
            "pages_daily_landing",
            "pages_daily_paths",
            "acquisition_session",
            "acquisition_first_user",
            "transactions",
        ],
        default="standard_daily",
        help="Report profile to generate (default: standard_daily)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output CSV path (defaults per profile next to this file)",
    )
    args = parser.parse_args()

    here = Path(__file__).parent
    if args.profile == "user_type_daily":
        out = args.out or str(here / "ga4_user_type_seed.csv")
        rows = generate_user_type_rows()
        write_csv(rows, out, columns=USER_TYPE_COLUMNS)
    elif args.profile == "pages_daily_landing":
        out = args.out or str(here / "ga4_landing_seed.csv")
        rows = generate_landing_rows()
        write_csv(rows, out, columns=LANDING_COLUMNS)
    elif args.profile == "pages_daily_paths":
        out = args.out or str(here / "ga4_paths_seed.csv")
        rows = generate_paths_rows()
        write_csv(rows, out, columns=PATHS_COLUMNS)
    elif args.profile == "acquisition_session":
        out = args.out or str(here / "ga4_acquisition_session_seed.csv")
        rows = generate_acquisition_session_rows()
        write_csv(rows, out, columns=ACQ_SESSION_COLUMNS)
    elif args.profile == "acquisition_first_user":
        out = args.out or str(here / "ga4_acquisition_first_user_seed.csv")
        rows = generate_acquisition_first_user_rows()
        write_csv(rows, out, columns=ACQ_FIRST_USER_COLUMNS)
    elif args.profile == "transactions":
        out = args.out or str(here / "ga4_transactions_seed.csv")
        rows = generate_transactions_rows()
        write_csv(rows, out, columns=TXN_COLUMNS)
    else:
        out = args.out or str(here / "ga4_seed.csv")
        rows = generate_rows()
        write_csv(rows, out)
    print(f"Generated {len(rows)} rows -> {out}")


if __name__ == "__main__":
    main()
