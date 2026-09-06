"""Deterministic revenue-side fee & tax fixture for the Story 41.5 dbt tests.

WHY THIS SCRIPT EXISTS (read this before deleting it as "test scaffolding").
There is no dbt seed for the ``mirror.*`` schema: the repo convention for feeding mirror
tables into a DEV DuckDB is a direct ``CREATE`` + ``INSERT`` (``seed_plan_mirror.py``,
``seed_fx_source_currency_bindings.py``, ``seed_fee_tax_mirror.py``). Without this script
every Epic-41 revenue relation is empty in a local build, both 41.5 views return zero
rows, and EVERY ON/OFF assertion in this story's suite passes TRIVIALLY -- a green build
that proves nothing.

A SIBLING, NOT AN EXTENSION (orchestrator ruling Q2). ``seed_fee_tax_mirror.py`` is
41.3's and is NOT modified: each story owns its own fixture. This script is idempotent,
uses FIXED DATES (never ``date.today()``), namespaces every key under ``feetaxrev_dev_``
and re-creates the shared mirror DDL with ``CREATE TABLE IF NOT EXISTS`` so it is
ORDER-INDEPENDENT with the other two seeders on the same DuckDB file.

    uv run python dbt/seeds/mediaplan/seed_plan_mirror.py        --duckdb-path dev.duckdb
    uv run python dbt/seeds/feetax/seed_fee_tax_mirror.py        --duckdb-path dev.duckdb
    uv run python dbt/seeds/feetax/seed_fee_tax_revenue_mirror.py --duckdb-path dev.duckdb

======================= WHAT THIS FIXTURE CANNOT REACH, STATED ==================
⚠️ THREE OF THE FOUR HOLES BELOW ARE CLOSED SINCE 2026-08-04, and the reason is worth
more than the fix: they were unreachable because ``adjust`` shipped no DuckDB seed
loader, and it SHIPS ONE NOW (``server/modules/adjust/seeds/load_adjust_seed.py``,
Story 53.10). Nobody edited five modules; one already-written loader was wired in, and
``POSTURE_PROJECT`` below carries R6, R8 and R9 at once. The paragraph that said "it is
not this story's to fix and fixing it would edit five other modules" was TRUE when
written and had quietly stopped being true -- which is exactly why a named hole is
worth more than a vague one: it is cheap to re-measure.

Still true: only ``shopify``, ``stripe`` and now ``adjust`` ship a loader.
``woocommerce``, ``square``, ``klaviyo`` and ``cm360`` still ship none (retro item
C.9/7). What that still costs:

  * TRANSACTION_COUNT_UNAVAILABLE. ``shopify`` and ``stripe`` staging emit their count
    metric on exactly the days they emit revenue, so "a PER_TRANSACTION rule fired and
    no count landed" is unreachable here. The guard is still asserted as an invariant
    over the whole model (no non-NULL per-transaction component may coexist with a NULL
    count), which is the half that catches an "assume 1" regression. ``adjust`` does not
    close this one: it declares no ``transaction_count_metric`` at all, so no
    PER_TRANSACTION rule can be scoped to it in the first place.
  * A TAX_EXCLUSIVE landed posture still has no emitter. Every seedable connector is
    TAX_INCLUSIVE or UNDECLARED, so the HT-side symmetry of the normalisation is
    exercised by derivation and not by a source that lands HT.

Saying this out loud is the point: a fixture that pretends to cover a case it cannot
reach is worse than one that names the hole -- and a named hole gets closed the day its
blocker expires, which a vague one never does.

======================= THE FIXTURE PROJECTS ===================================
Each exists to make exactly ONE assertion non-vacuous.

  feetaxrev_dev_off        flag FALSE, WITH REAL REVENUE FACTS. It carries revenue
                           precisely so the OFF assertion can be FALSIFIED: with no fact
                           row an anti-join is structurally unable to fail and proves
                           only that the test ran (41.3's review finding F5).
  feetaxrev_dev_posture    R9 + R8 + R6, from the adjust connector's OWN golden pull.
                           R9: `revenue` is SALES with landed_tax_posture UNDECLARED, so
                           its basis is UNRESOLVED, BOTH derived money columns are NULL
                           and the row carries REVENUE_TAX_POSTURE_UNDECLARED. It must
                           NEVER produce an HT/TTC figure to compare against cost --
                           this is the ONLY project on which completeness criterion [4]
                           of tax-fees can be falsified at all.
                           R8: `ad_revenue` and `all_revenue` land on the SAME days with
                           comparable magnitudes and are ATTRIBUTED, so they must stay
                           out of the ROAS numerator while remaining inspectable.
                           R6: the connector fans one wide row into THREE breakdown
                           series (network / campaign_id / app_token) that EACH total
                           the day. The C4 collapse must pick ONE. Summing them would
                           TRIPLE every fee, and until now no fixture could catch that.
                           Its dates are the golden pull's (2026-07-01/02), not D1/D2.
  feetaxrev_dev_vat        R1, the pinned worked example. 12 345.67 EUR TTC at 20 %:
                           net 10 288 058 333 + tax 2 057 611 667 = 12 345 670 000.
  feetaxrev_dev_half       R2, the ROUND_HALF_UP-vs-banker's pin. 12 000.000003 EUR
                           lands 12 000 000 003 micros; /1.2 is 10 000 000 002.5
                           ALGEBRAICALLY -- SQL rounds half away from zero to
                           10 000 000 003 while money.py's banker's rounding would give
                           10 000 000 002. The IDENTITY net + tax == gross holds either
                           way, because the tax is an integer SUBTRACTION.
  feetaxrev_dev_zero       R3. rate 0.000000 -> net == gross, tax EXACTLY 0, NO GAP.
                           A real zero, distinguishable from a missing one.
  feetaxrev_dev_badrate    R4. A NEGATIVE rate. Migration 119 REFUSES it at declaration
                           (CHECK rate >= 0), so the DuckDB mirror -- which carries no
                           CHECK constraints -- is the ONLY place the model's
                           fail-closed VAT guard can be exercised at all. Same posture
                           41.3 took with feetax_dev_wht_bad.
  feetaxrev_dev_absent     R5, THE DAY-ONE HOT PATH. Revenue lands, no SALES_TAX rule
                           exists: landed and gross_ttc populated, net_ht NULL,
                           SALES_TAX_RULE_ABSENT. Asserted POSITIVELY, not as the mirror
                           image of a complete scenario.
  feetaxrev_dev_dedup      R7. TWO revenue connectors on the same project-day. The
                           alignment view must read the WINNER ALONE and therefore be
                           STRICTLY LESS than their sum -- a Stripe payment settling a
                           Shopify order measures the SAME sale.
  feetaxrev_dev_srctype    R10. A VAT rule scoped source_type = COMMERCE_REVENUE.
                           attr_source_type is NULL on every row today (nothing writes
                           app.datastream_source_types), so the matcher must read NULL
                           as UNRESOLVED. Reading it as known-false would compute
                           NOTHING while reporting a complete, trustworthy total.
  feetaxrev_dev_knownfalse R11, the OTHER half of E41-AD9. A country='DE' VAT rule on a
                           row whose country RESOLVES to FR (via the Epic-37 project
                           posture) contributes +0 and raises NO gap, while a second,
                           unconditioned VAT rule fires normally -- so the row stays
                           COMPLETE. Pairs with R10: known-false vs unresolvable.
  feetaxrev_dev_measured   R12. A connector that lands a MEASURED `fees` fact AND
                           carries a declared PAYMENT_FEE rule. The measurement wins,
                           payment_fee_rule_superseded is TRUE, and the two never sum.
  feetaxrev_dev_pertx      R13. 1.4 % PERCENTAGE + 0.25 EUR PER_TRANSACTION over 412
                           orders: 172 839 380 + 103 000 000 = 275 839 380 micros, the
                           per-transaction half an EXACT integer multiplication with no
                           rounding at all.
  feetaxrev_dev_flatfee    R13b, the DISCRIMINATING TWIN. The SAME shape with a FLAT
                           0.25 EUR rule instead: applied ONCE, 250 000 micros, NOT
                           multiplied by 412. Without this pair the PER_TRANSACTION
                           vocabulary change is untested -- a regression that silently
                           re-merged the two behaviours would pass everything else.
  feetaxrev_dev_refused    E41-FR05. A PER_TRANSACTION rule denominated in USD on an EUR
                           project. REFUSED, never summed and NEVER CONVERTED:
                           converting would apply FX a second time outside the read
                           locus (Epic 39.10).
  feetaxrev_dev_formbad    F2 defence in depth. A SALES_TAX / SPEND_TIERS rule over
                           GROSS_REVENUE -- which migration 119's
                           ck_fee_tax_rules_form_base_target now makes UNREPRESENTABLE at
                           declaration. The mirror carries no CHECK, so this is exactly
                           the mirror-lag / pre-constraint row the model's own
                           REVENUE_FORM_UNSUPPORTED guard exists for. CONSTRAINTS PROTECT
                           THE FUTURE; GUARDS PROTECT THE ROWS ALREADY WRITTEN.
  feetaxrev_dev_roas       R14. Paid-media cost AND commerce revenue on the same
                           project-day, with a complete cost ladder, so ROAS HT and
                           gross margin are actually computed. A SECOND day carries a
                           country-conditioned cost rule that cannot resolve, so its
                           ladder is INCOMPLETE, its cost is NULL and its ratio is NULL
                           with COST_LADDER_INCOMPLETE -- the flattering-lie guard.

ASCII-only stdout (project rule).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Reuse the real connector seed loaders so the fixture rows land in fact_daily_kpi
# EXACTLY like a real pull -- through each module's own staging model, never by writing
# to the mart. EUR source currency everywhere, so the EUR->EUR identity FX rate applies
# and fact_daily_kpi.value equals the seeded figure exactly.
sys.path.insert(0, str(_REPO_ROOT / "server" / "modules" / "shopify" / "seeds"))
sys.path.insert(0, str(_REPO_ROOT / "server" / "modules" / "stripe" / "seeds"))
sys.path.insert(0, str(_REPO_ROOT / "server" / "modules" / "meta-ads" / "seeds"))
sys.path.insert(0, str(_REPO_ROOT / "server" / "modules" / "adjust" / "seeds"))
from load_adjust_seed import generate_rows as _adjust_rows  # noqa: E402
from load_adjust_seed import load_duckdb as _load_adjust_duckdb  # noqa: E402
from load_meta_seed import load_duckdb as _load_meta_duckdb  # noqa: E402
from load_shopify_seed import load_duckdb as _load_shopify_duckdb  # noqa: E402
from load_stripe_seed import load_duckdb as _load_stripe_duckdb  # noqa: E402

PULL_SHOPIFY = "pull_feetaxrev_41_5_shopify"
PULL_STRIPE = "pull_feetaxrev_41_5_stripe"
PULL_META = "pull_feetaxrev_41_5_meta"
PULL_ADJUST = "pull_feetaxrev_41_5_adjust"

D1 = date(2026, 5, 1)
D2 = date(2026, 5, 2)

WINDOW_FROM = date(2026, 1, 1)

OFF_PROJECT = "feetaxrev_dev_off"
KNOWN_FALSE_PROJECT = "feetaxrev_dev_knownfalse"
#: R6 + R8 + R9 at once, and the three of them ONLY exist because `adjust` finally
#: ships a DuckDB seed loader (Story 53.10). See the docstring section above.
POSTURE_PROJECT = "feetaxrev_dev_posture"

PROJECTS = [
    OFF_PROJECT,
    POSTURE_PROJECT,
    "feetaxrev_dev_vat",
    "feetaxrev_dev_half",
    "feetaxrev_dev_zero",
    "feetaxrev_dev_badrate",
    "feetaxrev_dev_absent",
    "feetaxrev_dev_dedup",
    "feetaxrev_dev_srctype",
    KNOWN_FALSE_PROJECT,
    "feetaxrev_dev_measured",
    "feetaxrev_dev_pertx",
    "feetaxrev_dev_flatfee",
    "feetaxrev_dev_refused",
    "feetaxrev_dev_formbad",
    "feetaxrev_dev_roas",
]

# Only the OFF project reads FALSE.
FLAG_ON = {p: (p != OFF_PROJECT) for p in PROJECTS}

# R11 needs a country the Epic-37 bridge can actually RESOLVE, and only the governed
# single-country rung is reachable here (no connector emits breakdown_dimension='country'
# together with a revenue metric, and datastream_country_binding_dim is deliberately
# empty because migration 104 is unapplied). Story 37.9 / migration 269 moved that rung's
# input from project_preferences.geographic_mode / local_markets (which nothing reads any
# more) to mirror.country_market_projection. So exactly ONE project publishes a Country
# meaning -- one row, one tracked country, market_kind='market' -- and every other
# publishes NOTHING, which is what preserves COUNTRY_UNRESOLVED as the hot path.
KNOWN_FALSE_COUNTRY = "FR"
KNOWN_FALSE_MARKET_ID = "france"
KNOWN_FALSE_MARKET_LABEL = "France"

# ---------------------------------------------------------------------------
# Shopify orders. (project_id, date, order_id, revenue, refund_amount, orders_count)
# revenue is TAX-INCLUSIVE at the source (manifest total_price -> revenue).
# ---------------------------------------------------------------------------
_SHOPIFY_ROWS: list[tuple[str, date, str, float, float, int]] = [
    (OFF_PROJECT,                 D1, "sro_off_1",       750.00,      0.0,   5),
    ("feetaxrev_dev_vat",         D1, "sro_vat_1",     12345.67,      0.0,  10),
    # 12 000.000003 EUR -> 12 000 000 003 micros: /1.2 is an EXACT half-micro.
    ("feetaxrev_dev_half",        D1, "sro_half_1",    12000.000003,  0.0,  10),
    ("feetaxrev_dev_zero",        D1, "sro_zero_1",     5000.00,      0.0,  10),
    ("feetaxrev_dev_badrate",     D1, "sro_bad_1",      1000.00,      0.0,  10),
    ("feetaxrev_dev_absent",      D1, "sro_abs_1",      8000.00,      0.0,  10),
    # R7: shopify is priority 1, stripe is priority 3 -- the winner is shopify ALONE and
    # 10 000 000 000 is STRICTLY LESS than the 14 000 000 000 the naive sum would give.
    ("feetaxrev_dev_dedup",       D1, "sro_dedup_1",   10000.00,      0.0,  10),
    ("feetaxrev_dev_srctype",     D1, "sro_st_1",       1000.00,      0.0,  10),
    (KNOWN_FALSE_PROJECT,         D1, "sro_kf_1",       1000.00,      0.0,  10),
    # R13 / R13b: 412 orders is the count a PER_TRANSACTION rule multiplies, and the
    # number a FLAT rule must NOT multiply by.
    ("feetaxrev_dev_pertx",       D1, "sro_ptx_1",     12345.67,      0.0, 412),
    ("feetaxrev_dev_flatfee",     D1, "sro_flt_1",     12345.67,      0.0, 412),
    ("feetaxrev_dev_refused",     D1, "sro_ref_1",      1000.00,      0.0,  10),
    ("feetaxrev_dev_formbad",     D1, "sro_fb_1",       1000.00,      0.0,  10),
    ("feetaxrev_dev_roas",        D1, "sro_roas_1",    12000.00,      0.0,  10),
    ("feetaxrev_dev_roas",        D2, "sro_roas_2",    12000.00,      0.0,  10),
]

# ---------------------------------------------------------------------------
# Stripe payments. (project_id, date, charge_id, revenue, refunds, fees,
#                   transaction_count, order_count)
# `fees` is the MEASURED gateway fee -- the one derivation the platform does not have to
# make, and the reason the tax and the fee are handled asymmetrically.
# ---------------------------------------------------------------------------
_STRIPE_ROWS: list[tuple[str, date, str, float, float, float, int, int]] = [
    ("feetaxrev_dev_dedup",    D1, "sch_dedup_1",  4000.00, 0.0,   0.00, 40, 40),
    # R12: a real 173.00 EUR measured fee AND a declared 1.4 % rule on the same row.
    ("feetaxrev_dev_measured", D1, "sch_meas_1",  12345.67, 0.0, 173.00, 412, 412),
]

# ---------------------------------------------------------------------------
# Meta-ads cost, so the ROAS project has a cost ladder to align against.
# (project_id, date, campaign_id, spend)
# ---------------------------------------------------------------------------
_COST_ROWS: list[tuple[str, date, str, float]] = [
    ("feetaxrev_dev_roas", D1, "frr_camp_1", 2000.00),
    ("feetaxrev_dev_roas", D2, "frr_camp_1", 2000.00),
]

# ---------------------------------------------------------------------------
# Rules. Column order mirrors migration 119's app.fee_tax_rules_dim_v exactly:
# (id, project_id, scope_kind, scope_ref, category, form, rate, amount_micros,
#  cpm_micros, currency, base_target, cascade_phase, sequence_order, effective_from,
#  effective_to, status, origin, dedup_hash, label)
# ---------------------------------------------------------------------------


def _rule(
    rule_id: str,
    project_id: str,
    category: str,
    form: str,
    *,
    rate: str | None = None,
    amount_micros: int | None = None,
    currency: str | None = None,
    base_target: str = "GROSS_REVENUE",
    phase: int = 6,
    sequence_order: int = 0,
    status: str = "confirmed",
    label: str = "",
) -> tuple:
    return (
        rule_id, project_id, "project", None, category, form, rate, amount_micros,
        None, currency, base_target, phase, sequence_order, WINDOW_FROM, None,
        status, "operator", None, label,
    )


_RULES: list[tuple] = [
    _rule("ftr_rev_vat_1", "feetaxrev_dev_vat", "SALES_TAX", "PERCENTAGE",
          rate="0.200000", label="VAT 20 pct"),
    _rule("ftr_rev_half_1", "feetaxrev_dev_half", "SALES_TAX", "PERCENTAGE",
          rate="0.200000", label="VAT 20 pct half-micro pin"),
    # A REAL zero, not a missing one: the computation runs and the answer is 0.
    _rule("ftr_rev_zero_1", "feetaxrev_dev_zero", "SALES_TAX", "PERCENTAGE",
          rate="0.000000", label="VAT 0 pct"),
    # Migration 119 REFUSES this at declaration; only the CHECK-free mirror can carry it.
    _rule("ftr_rev_bad_1", "feetaxrev_dev_badrate", "SALES_TAX", "PERCENTAGE",
          rate="-0.050000", label="negative VAT rate"),
    _rule("ftr_rev_dedup_1", "feetaxrev_dev_dedup", "SALES_TAX", "PERCENTAGE",
          rate="0.200000", label="VAT 20 pct"),
    _rule("ftr_rev_st_1", "feetaxrev_dev_srctype", "SALES_TAX", "PERCENTAGE",
          rate="0.200000", label="VAT 20 pct scoped to COMMERCE_REVENUE"),
    # R11: the KNOWN-FALSE rule (country DE on an FR row) and the one that DOES fire.
    _rule("ftr_rev_kf_de", KNOWN_FALSE_PROJECT, "SALES_TAX", "PERCENTAGE",
          rate="0.190000", sequence_order=1, label="VAT DE 19 pct -- known false here"),
    _rule("ftr_rev_kf_all", KNOWN_FALSE_PROJECT, "SALES_TAX", "PERCENTAGE",
          rate="0.200000", sequence_order=0, label="VAT 20 pct unconditioned"),
    _rule("ftr_rev_meas_vat", "feetaxrev_dev_measured", "SALES_TAX", "PERCENTAGE",
          rate="0.200000", label="VAT 20 pct"),
    # R12: this rule must be SUPPRESSED by the measured `fees` fact, never summed with it.
    _rule("ftr_rev_meas_fee", "feetaxrev_dev_measured", "PAYMENT_FEE", "PERCENTAGE",
          rate="0.014000", label="gateway 1.4 pct -- superseded by the measurement"),
    _rule("ftr_rev_ptx_vat", "feetaxrev_dev_pertx", "SALES_TAX", "PERCENTAGE",
          rate="0.200000", label="VAT 20 pct"),
    _rule("ftr_rev_ptx_pct", "feetaxrev_dev_pertx", "PAYMENT_FEE", "PERCENTAGE",
          rate="0.014000", sequence_order=0, label="gateway 1.4 pct"),
    _rule("ftr_rev_ptx_fix", "feetaxrev_dev_pertx", "PAYMENT_FEE", "PER_TRANSACTION",
          amount_micros=250_000, currency="EUR", sequence_order=1,
          label="gateway 0.25 EUR PER TRANSACTION"),
    _rule("ftr_rev_flt_vat", "feetaxrev_dev_flatfee", "SALES_TAX", "PERCENTAGE",
          rate="0.200000", label="VAT 20 pct"),
    # R13b is a CONTROLLED comparison, not merely a second example: this project is
    # byte-for-byte R13's shape -- same revenue, same 412 orders, same 1.4 % rule, same
    # 0.25 EUR amount -- with the SECOND rule's FORM as the only difference. That is
    # what makes "the two forms are two behaviours" a measurement rather than an
    # assertion.
    _rule("ftr_rev_flt_pct", "feetaxrev_dev_flatfee", "PAYMENT_FEE", "PERCENTAGE",
          rate="0.014000", sequence_order=0, label="gateway 1.4 pct"),
    _rule("ftr_rev_flt_fix", "feetaxrev_dev_flatfee", "PAYMENT_FEE", "FLAT",
          amount_micros=250_000, currency="EUR", sequence_order=1,
          label="gateway 0.25 EUR ONCE for the row"),
    _rule("ftr_rev_ref_vat", "feetaxrev_dev_refused", "SALES_TAX", "PERCENTAGE",
          rate="0.200000", label="VAT 20 pct"),
    _rule("ftr_rev_ref_fix", "feetaxrev_dev_refused", "PAYMENT_FEE", "PER_TRANSACTION",
          amount_micros=250_000, currency="USD", sequence_order=1,
          label="USD gateway fee on an EUR project -- REFUSED"),
    # F2 defence in depth: no revenue evaluator routes SPEND_TIERS.
    _rule("ftr_rev_fb_1", "feetaxrev_dev_formbad", "SALES_TAX", "SPEND_TIERS",
          label="tiered sales tax -- unroutable on the revenue side"),
    _rule("ftr_rev_roas_vat", "feetaxrev_dev_roas", "SALES_TAX", "PERCENTAGE",
          rate="0.200000", label="VAT 20 pct"),
    # The COST side of the ROAS project: an unconditioned agency fee (day 1 and day 2
    # both compose) plus a country-conditioned platform fee that can NEVER resolve, which
    # is what makes day 2's ladder incomplete... except that a rule's effective window is
    # the only per-day lever the mirror gives us, so the country rule is scoped to day 2
    # onwards. Day 1 therefore composes and day 2 does not: one project, both outcomes.
    (
        "ftr_rev_roas_agency", "feetaxrev_dev_roas", "project", None, "AGENCY_FEE",
        "PERCENTAGE", "0.100000", None, None, None, "NET_MEDIA", 5, 0,
        WINDOW_FROM, None, "confirmed", "operator", None, "agency 10 pct",
    ),
    (
        "ftr_rev_roas_country", "feetaxrev_dev_roas", "project", None, "PLATFORM_FEE",
        "PERCENTAGE", "0.030000", None, None, None, "NET_MEDIA", 2, 0,
        D2, None, "confirmed", "operator", None,
        "platform 3 pct conditioned on a country that cannot resolve (day 2 only)",
    ),
]

# (rule_id, condition_key, condition_value). A rule with NO row here is UNCONSTRAINED
# and matches every row -- absence is never "no match".
_CONDITIONS: list[tuple[str, str, str]] = [
    # R10: source_type is NULL on every row today, so this must read UNRESOLVED.
    ("ftr_rev_st_1", "source_type", "COMMERCE_REVENUE"),
    # R11: known-false. The row's country RESOLVES to FR, so 'DE' is a real FALSE.
    ("ftr_rev_kf_de", "country", "DE"),
    # The cost-side rule that makes day 2's ladder incomplete.
    ("ftr_rev_roas_country", "country", "FR"),
]

# SPEND_TIERS bands for the unroutable-form fixture. The model must refuse the rule on
# its FORM before it ever looks at the ladder, so the bands are only here to make the
# rule structurally plausible.
_TIERS: list[tuple[str, int, int, str, str]] = [
    ("ftr_rev_fb_1", 0, 0, "0.050000", "cliff"),
]

# ---------------------------------------------------------------------------
# DDL. CREATE TABLE IF NOT EXISTS everywhere -- never CREATE OR REPLACE, which would
# drop rows another seeder or a real mirror_sync landed. Shapes are byte-identical to
# seed_fee_tax_mirror.py's, which is what makes the two order-independent.
# ---------------------------------------------------------------------------
_FEE_TAX_DDL = """
CREATE SCHEMA IF NOT EXISTS mirror;

CREATE TABLE IF NOT EXISTS mirror.fee_tax_rules (
    id             VARCHAR,
    project_id     VARCHAR,
    scope_kind     VARCHAR,
    scope_ref      VARCHAR,
    category       VARCHAR,
    form           VARCHAR,
    rate           DECIMAL(12,6),
    amount_micros  BIGINT,
    cpm_micros     BIGINT,
    currency       VARCHAR,
    base_target    VARCHAR,
    cascade_phase  SMALLINT,
    sequence_order INTEGER,
    effective_from DATE,
    effective_to   DATE,
    status         VARCHAR,
    origin         VARCHAR,
    dedup_hash     VARCHAR,
    label          VARCHAR,
    created_by     VARCHAR,
    created_at     TIMESTAMP,
    updated_at     TIMESTAMP
);

CREATE TABLE IF NOT EXISTS mirror.fee_tax_rule_conditions (
    rule_id         VARCHAR,
    condition_key   VARCHAR,
    condition_value VARCHAR
);

CREATE TABLE IF NOT EXISTS mirror.fee_tax_rule_tiers (
    rule_id          VARCHAR,
    tier_index       INTEGER,
    threshold_micros BIGINT,
    rate             DECIMAL(12,6),
    mode             VARCHAR
);

CREATE TABLE IF NOT EXISTS mirror.datastreams_dim (
    project_id    VARCHAR,
    datastream_id VARCHAR,
    connector     VARCHAR,
    data_role     VARCHAR,
    source_kind   VARCHAR
);

CREATE TABLE IF NOT EXISTS mirror.datastream_source_types (
    datastream_id VARCHAR,
    source_type   VARCHAR,
    declared_by   VARCHAR,
    declared_at   TIMESTAMP
);

CREATE TABLE IF NOT EXISTS mirror.datastream_country_binding_dim (
    project_id    VARCHAR,
    connector     VARCHAR,
    datastream_id VARCHAR,
    market_id     VARCHAR
);
"""

# The columns an Epic-41 revenue model actually BINDS against.
_PREFS_REQUIRED = (
    "project_id",
    "canonical_currency",
    "fee_tax_alignment_enabled",
)


def _seed_facts(duckdb_path: str) -> tuple[int, int, int, int]:
    """Land the fixture's raw rows through the REAL connector loaders (idempotent)."""
    import duckdb  # noqa: PLC0415

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")

    # Idempotency: drop our own pulls first. Guard on the table existing, because the
    # loaders run their CREATE DDL themselves and calling them with an empty row list
    # raises in DuckDB's executemany.
    con = duckdb.connect(duckdb_path)
    try:
        for table, pull_id in (
            ("raw_shopify_orders", PULL_SHOPIFY),
            ("raw_stripe_payments", PULL_STRIPE),
            ("raw_meta_ads_daily", PULL_META),
            ("raw_adjust_daily", PULL_ADJUST),
        ):
            exists = con.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [table]
            ).fetchone()
            if exists:
                con.execute(f"DELETE FROM {table} WHERE pull_id = ?", [pull_id])
    finally:
        con.close()

    # The loaders take ONE project_id per call, so group by project.
    n_shopify = 0
    for project_id in sorted({row[0] for row in _SHOPIFY_ROWS}):
        rows = [
            {
                "date": day.isoformat(),
                "order_id": order_id,
                "transaction_id": None,
                "revenue": revenue,
                "refund_amount": refund,
                "orders_count": orders,
                "revenue_source_currency": "EUR",
            }
            for pid, day, order_id, revenue, refund, orders in _SHOPIFY_ROWS
            if pid == project_id
        ]
        n_shopify += _load_shopify_duckdb(
            rows, PULL_SHOPIFY, loaded_at, duckdb_path, project_id=project_id
        )

    n_stripe = 0
    for project_id in sorted({row[0] for row in _STRIPE_ROWS}):
        rows = [
            {
                "date": day.isoformat(),
                "charge_id": charge_id,
                "payment_intent_id": None,
                "client_reference_id": None,
                "revenue": revenue,
                "refunds": refunds,
                "fees": fees,
                "transaction_count": tx_count,
                "order_count": order_count,
                "revenue_source_currency": "EUR",
            }
            for pid, day, charge_id, revenue, refunds, fees, tx_count, order_count
            in _STRIPE_ROWS
            if pid == project_id
        ]
        n_stripe += _load_stripe_duckdb(
            rows, PULL_STRIPE, loaded_at, duckdb_path, project_id=project_id
        )

    cost_rows = [
        {
            "date": day.isoformat(),
            "data_level": "CAMPAIGN",
            "campaign_id": campaign_id,
            "campaign_name": campaign_id,
            "adset_id": None,
            "adset_name": None,
            "ad_id": None,
            "creative_id": None,
            "spend": spend,
            "impressions": 1000,
            "clicks": 10,
            "conversions": 1,
            "project_id": project_id,
            "cost_source_currency": "EUR",
        }
        for project_id, day, campaign_id, spend in _COST_ROWS
    ]
    n_cost = _load_meta_duckdb(cost_rows, PULL_META, loaded_at, duckdb_path)

    # R6 + R8 + R9, on ONE project, from the connector's OWN golden pull rather than
    # from numbers invented here. The three rows carry `revenue` (SALES), `ad_revenue`
    # and `all_revenue` (both ATTRIBUTED), all three declared `UNDECLARED` in
    # fee_tax_revenue_scope.csv -- so this project is simultaneously:
    #   R9  a SALES figure whose tax basis nobody can state, which must produce
    #       REVENUE_TAX_POSTURE_UNDECLARED and never an HT/TTC comparison;
    #   R8  claimed revenue with a large value on the same day, which must never
    #       reach the ROAS numerator;
    #   R6  three parallel breakdown series (network / campaign_id / app_token) that
    #       EACH total the day, which the C4 collapse must pick ONE of rather than sum.
    # Its dates are the golden pull's own (2026-07-01/02), NOT D1/D2: bending the
    # fixture's dates to match the commerce scenarios would make the seed drift from
    # what the connector actually returns, which is the one thing the adjust loader's
    # docstring is built to prevent.
    n_adjust = _load_adjust_duckdb(
        _adjust_rows(project_id=POSTURE_PROJECT), PULL_ADJUST, loaded_at, duckdb_path
    )
    return n_shopify, n_stripe, n_cost, n_adjust


def _seed_mirror(duckdb_path: str) -> None:
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    try:
        con.execute(_FEE_TAX_DDL)

        # project_preferences is created by seed_fee_tax_mirror.py's SHAPE-COMPLETE
        # fallback (or by a real mirror_sync). If it is absent this seeder must NOT
        # invent a partial one: a table missing fee_tax_alignment_enabled would make
        # every 41.5 model bind against a column that is not there and take the whole
        # build down with an opaque error three layers away from the cause.
        prefs_exists = con.execute(
            "SELECT COUNT(*) FROM information_schema.tables"
            " WHERE table_schema = 'mirror' AND table_name = 'project_preferences'"
        ).fetchone()[0]
        if not prefs_exists:
            raise SystemExit(
                "ERROR: mirror.project_preferences is absent. Run"
                " 'uv run python dbt/seeds/feetax/seed_fee_tax_mirror.py"
                " --duckdb-path <file>' first -- it owns the shape-complete dev"
                " fallback, and Story 41.5 does not duplicate it."
            )
        present = {
            row[0]
            for row in con.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = 'mirror' AND table_name = 'project_preferences'"
            ).fetchall()
        }
        missing = [c for c in _PREFS_REQUIRED if c not in present]
        if missing:
            raise SystemExit(
                "ERROR: mirror.project_preferences is missing the columns Story 41.5"
                f" binds against: {missing}. Run seed_fee_tax_mirror.py (or"
                " mirror_sync) first."
            )

        placeholders = ", ".join("?" for _ in PROJECTS)
        con.execute(
            f"DELETE FROM mirror.project_preferences WHERE project_id IN ({placeholders})",
            PROJECTS,
        )
        for project_id in PROJECTS:
            con.execute(
                "INSERT INTO mirror.project_preferences"
                " (project_id, canonical_currency, reporting_timezone,"
                "  fee_tax_alignment_enabled)"
                " VALUES (?, 'EUR', 'Europe/Paris', ?)",
                [project_id, FLAG_ON[project_id]],
            )

        # The ONE project with a published Country meaning, so R11's country is a real
        # FALSE rather than an UNRESOLVED. Story 37.9: the meaning is published to
        # mirror.country_market_projection (migration 269's flat projection), never to
        # the retired project_preferences geo columns. A missing relation means
        # create_mirror was not run first; that dies loudly on the INSERT, pointing at
        # the one shape definition -- silently turning the known-false fixture into an
        # unresolvable one would make
        # test_epic41_revenue_known_false_vs_unresolvable.sql pass for the wrong reason.
        con.execute(
            "DELETE FROM mirror.country_market_projection WHERE project_id = ?",
            [KNOWN_FALSE_PROJECT],
        )
        con.execute(
            "INSERT INTO mirror.country_market_projection"
            " (project_id, registry_id, hierarchy_version_id, vocabulary_version_id,"
            "  hierarchy_content_hash, country_code, market_id, market_label,"
            "  market_kind, region_id, region_label, display_order)"
            " VALUES (?, 'mdr_EXAMPLE_LOCAL', 'mdv_EXAMPLE_LOCAL',"
            "         'vocab_EXAMPLE_LOCAL', 'sha256:EXAMPLE_LOCAL', ?, ?, ?,"
            "         'market', NULL, NULL, 1)",
            [KNOWN_FALSE_PROJECT, KNOWN_FALSE_COUNTRY, KNOWN_FALSE_MARKET_ID,
             KNOWN_FALSE_MARKET_LABEL],
        )

        rule_ids = [r[0] for r in _RULES]
        rule_ph = ", ".join("?" for _ in rule_ids)
        con.execute(f"DELETE FROM mirror.fee_tax_rules WHERE id IN ({rule_ph})", rule_ids)
        con.execute(
            f"DELETE FROM mirror.fee_tax_rule_conditions WHERE rule_id IN ({rule_ph})",
            rule_ids,
        )
        con.execute(
            f"DELETE FROM mirror.fee_tax_rule_tiers WHERE rule_id IN ({rule_ph})",
            rule_ids,
        )

        for rule in _RULES:
            con.execute(
                "INSERT INTO mirror.fee_tax_rules"
                " (id, project_id, scope_kind, scope_ref, category, form, rate,"
                "  amount_micros, cpm_micros, currency, base_target, cascade_phase,"
                "  sequence_order, effective_from, effective_to, status, origin,"
                "  dedup_hash, label, created_by, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                "         'seed', now(), now())",
                list(rule),
            )

        for rule_id, key, value in _CONDITIONS:
            con.execute(
                "INSERT INTO mirror.fee_tax_rule_conditions"
                " (rule_id, condition_key, condition_value) VALUES (?, ?, ?)",
                [rule_id, key, value],
            )

        for band in _TIERS:
            con.execute(
                "INSERT INTO mirror.fee_tax_rule_tiers"
                " (rule_id, tier_index, threshold_micros, rate, mode)"
                " VALUES (?, ?, ?, ?, ?)",
                list(band),
            )
    finally:
        con.close()


def run(duckdb_path: str) -> None:
    n_shopify, n_stripe, n_cost, n_adjust = _seed_facts(duckdb_path)
    _seed_mirror(duckdb_path)
    print(
        "seed_fee_tax_revenue_mirror OK  "
        f"projects={len(PROJECTS)}  rules={len(_RULES)}  "
        f"conditions={len(_CONDITIONS)}  tier_bands={len(_TIERS)}  "
        f"shopify_rows={n_shopify}  stripe_rows={n_stripe}  cost_rows={n_cost}"
        f"  adjust_rows={n_adjust}"
        f"  -> {duckdb_path}"
    )


def main() -> None:
    default_duckdb = os.environ.get("TOOROW_DUCKDB_PATH", "")
    parser = argparse.ArgumentParser(
        description=(
            "Seed the Story 41.5 revenue-side fee & tax mirror + commerce and cost"
            " facts into a DuckDB file"
        )
    )
    parser.add_argument(
        "--duckdb-path", default=default_duckdb, help="DuckDB file path (or TOOROW_DUCKDB_PATH)"
    )
    args = parser.parse_args()
    if not args.duckdb_path:
        raise SystemExit("ERROR: --duckdb-path (or TOOROW_DUCKDB_PATH) is required")
    run(args.duckdb_path)


if __name__ == "__main__":
    main()
