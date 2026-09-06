"""Deterministic fee & tax mirror + fact-cost seeder for the Story 41.3 dbt tests.

WHY THIS SCRIPT EXISTS (read this before deleting it as "test scaffolding").
Story 41.1 ships the Postgres side of Epic 41 -- migration 119, the three flattening
views, and the six ``sources_mirror.yml`` declarations -- but it deliberately ships NO
DuckDB seeder (decision D8: that is 41.3's). There is no dbt seed for the ``mirror.*``
schema: the established repo convention for feeding mirror tables into a DEV DuckDB is
a DIRECT ``CREATE`` + ``INSERT`` (see ``dbt/seeds/mediaplan/seed_plan_mirror.py`` and
``dbt/seeds/fx/seed_fx_source_currency_bindings.py``). Without this script every Epic-41
relation is empty in a local build, ``fee_tax_ladder_daily`` returns zero rows, and
EVERY ON/OFF assertion in the story's test suite passes TRIVIALLY -- a green build that
proves nothing. The whole cascade would ship unexercised.

The DDL below is also the EXECUTABLE STATEMENT OF THE CONTRACT that Story 41.1 must
satisfy in production: the column names and order mirror migration 119's
``app.fee_tax_rules_dim_v`` / ``app.fee_tax_rule_conditions_v`` /
``app.fee_tax_rule_tiers_v`` / ``app.datastreams_dim_v`` exactly (note that the rules
relation's primary key is ``id``, NOT ``rule_id`` -- the dbt layer aliases it). If the
two ever disagree, the real ``dbt build`` fails loudly here rather than silently in a
customer's invoice.

DETERMINISTIC with FIXED DATES (never date.today()) and idempotent: every insert is
preceded by a DELETE of exactly this fixture's own keys, so re-running never
duplicates and never touches another story's rows.

Fixture projects (each one exists to make ONE story assertion non-vacuous):

  feetax_dev_off            flag FALSE, WITH REAL FACTS, no rules. It carries spend
                            precisely so test_epic41_module_off_zero_rows.sql can
                            FALSIFY "OFF => zero rows". The earlier factless version
                            made that anti-join structurally unable to fail (review
                            finding F5): with nothing in fact_daily_kpi the ladder emits
                            nothing whether or not the flag is read.

  feetax_dev_complete       The COMPLETE path: platform 3 % + a datastream-scoped
                            platform 1 % (single datastream => RESOLVED) + WHT 15 % +
                            agency 10 % + VAT 20 %, ALL UNCONDITIONED, so no rule
                            constrains an unresolvable attribute and the ladder
                            composes fully. Also carries the two rules that must NOT
                            contribute: one status='proposed' and one VERIFICATION.
  feetax_dev_gapped         Scenario S12 verbatim -- the day-one honest end state:
                            three COUNTRY-CONDITIONED rules (DST GB 2 %, DST FR 3 %,
                            VAT FR 20 %) plus ONE unconditioned agency fee, over
                            2 campaigns x 3 days = 6 rows. Country never resolves, so
                            the DST/VAT phases are NULL while net media and the agency
                            fee stay populated.
  feetax_dev_scope          D2: TWO datastreams on the same connector => a
                            datastream-scoped rule is AMBIGUOUS (one is never picked),
                            plus a rule whose scope_ref is unknown => the whole project
                            is RULE_SCOPE_UNRESOLVED.
  feetax_dev_plan           D5 + the C4 regression guard: a plan_version-scoped rule
                            over a connector emitting all THREE cost series. Under the
                            pre-C4 MIN(breakdown_dimension) pick the ladder would have
                            run at ad_id grain and the plan mapping (which keys on
                            campaign_id) could not have resolved; with C4 it fires.
                            The mapped campaign MATCHes, the unmapped one is
                            KNOWN-FALSE (+0 micros, NOT a gap).
  feetax_dev_refused        E41-FR05: a FLAT rule in USD on an EUR project. Refused,
                            never summed, never converted.
  feetax_dev_source_type    A source_type-scoped rule. 41.2's bridge emits
                            attr_source_type NULL on EVERY row today (nothing writes
                            app.datastream_source_types yet, and 41.2 correctly refused
                            to fabricate 'UNKNOWN' in SQL), so the matcher must read
                            NULL as UNRESOLVED. Reading it as known-false would compute
                            NOTHING while reporting a complete, trustworthy total --
                            the worst available failure mode.
  feetax_dev_tiers_cliff    S6:  bands [(0, 5 %), (20 000 EUR, 3 %)], mode 'cliff'.
  feetax_dev_tiers_marginal S6b: the SAME bands and the SAME three days, mode
                            'marginal'. Day 3 answers 210 M vs 270 M -- the two modes
                            and the inclusive frame are pinned independently.
  feetax_dev_tiers_bad      A SPEND_TIERS rule with NO band ladder: must raise
                            TIERS_MALFORMED, never contribute a silent 0.
  feetax_dev_wht_overflow   F10: rate 0.999999 (which migration 119 ADMITS) over a
                            20 M EUR base. base/(1-w) exceeds INT64, and before the
                            macro's magnitude guard DuckDB raised a conversion error --
                            a RED BUILD where E41-NFR02 requires a typed gap.
  feetax_dev_form_bad       F2: (AGENCY_FEE, GROSS_UP) -- legal under every CHECK, but
                            no phase branch executes it. It must GAP; before the routing
                            guard it contributed +0 under a green complete flag.
  feetax_dev_flat           F8: a FLAT rule that actually produces a number. Across the
                            previous fixture set the only FLAT rule was currency-REFUSED,
                            so the FLAT branch never once emitted a figure and the
                            once-per-ROW multiplication was invisible.
  feetax_dev_credit         F12: NEGATIVE spend (a platform credit). Every rate-derived
                            component is then legitimately negative and must pass
                            through -- a data condition must not turn the build red.
  feetax_dev_wht_bad        A GROSS_UP rate of exactly 1.000000. Postgres refuses this
                            at declaration (migration 119's ck_fee_tax_rules_gross_up_rate),
                            so the DuckDB mirror is the ONLY place the ladder's
                            fail-closed division guard can be exercised at all.

``mirror.datastream_country_binding_dim`` is created SHAPE-STABLE AND LEFT EMPTY on
purpose. That is production's real state (migration 104 is not applied and nothing
writes binding_kind='datastream'), and it is what makes the COUNTRY_UNRESOLVED
scenarios exercise the HOT path rather than a hypothetical. Do NOT seed a binding row
into it "to make the happy path work".

Usage (the orchestrator runs this BEFORE ``dbt build`` on the same DuckDB file, and
AFTER ``seed_plan_mirror.py`` so the plan mirror tables already carry their real
shape):

    uv run python dbt/seeds/feetax/seed_fee_tax_mirror.py --duckdb-path /tmp/dev.duckdb

ASCII-only stdout (project rule).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

# Reuse the meta-ads seed loader so the cost rows are the canonical parse shape and
# land in fact_daily_kpi exactly like a real Meta pull (the seed_plan_mirror.py
# precedent). EUR source currency => the EUR->EUR identity rate, so
# fact_daily_kpi.value equals the seeded spend exactly.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_META_SEEDS = _REPO_ROOT / "server" / "modules" / "meta-ads" / "seeds"
sys.path.insert(0, str(_META_SEEDS))
from load_meta_seed import load_duckdb as _load_meta_duckdb  # noqa: E402

# Story 41.2's PURE generator -- THE single source of truth for what auto-population
# emits. The auto fixture below iterates it and must NEVER transcribe its output into a
# literal: a hand-written approximation of a generator drifts from it, and that drift is
# precisely what let an unsatisfiable `source_type_scope` reach production shape (review
# finding F1 -- every auto rule would have gapped the moment an operator confirmed it,
# on a correctly-configured project). Importing it means the fixture breaks the day the
# payload changes, which is the entire point.
sys.path.insert(0, str(_REPO_ROOT / "server"))
from core.fee_tax_country_defaults import build_auto_rule_payloads  # noqa: E402

CONNECTOR = "meta-ads"
PULL_ID = "pull_feetax_41_3"

D1 = date(2026, 5, 1)
D2 = date(2026, 5, 2)
D3 = date(2026, 5, 3)

WINDOW_FROM = date(2026, 1, 1)

# The OFF project CARRIES REAL FACTS (review finding F5). It used to be namespaced and
# factless, which made T1's "OFF => zero ladder rows" anti-joins STRUCTURALLY unable to
# fail: with no fact row there is nothing for the ladder to emit whether the flag is read
# or not, so the assertion proved only that the test ran. A dev-named project with real
# spend is what turns it into a real proof -- and it stays out of T2's __epic41_* seed
# isolation assertion, which is about the seed CSV namespace, not about mirror + raw
# landings that are SUPPOSED to reach the marts.
OFF_PROJECT = "feetax_dev_off"
AUTO_PROJECT = "feetax_dev_auto"

PROJECTS = [
    OFF_PROJECT,
    "feetax_dev_complete",
    "feetax_dev_gapped",
    "feetax_dev_scope",
    "feetax_dev_plan",
    "feetax_dev_refused",
    "feetax_dev_source_type",
    "feetax_dev_tiers_cliff",
    "feetax_dev_tiers_marginal",
    "feetax_dev_tiers_bad",
    "feetax_dev_wht_bad",
    "feetax_dev_wht_overflow",
    "feetax_dev_form_bad",
    "feetax_dev_flat",
    "feetax_dev_credit",
    AUTO_PROJECT,
]

# F1: the project whose rules come from Story 41.2's REAL auto-population generator.
# It is the only fixture project with a published Country meaning, because it is the
# only one that must RESOLVE a country -- everything else deliberately publishes
# NOTHING so COUNTRY_UNRESOLVED remains the hot path S12/T17 assert. Story 37.9 moved
# the geographic authority from project_preferences.geographic_mode / local_markets
# (which nothing reads any more) to mirror.country_market_projection: one flat row per
# (project, assigned country) at the published hierarchy version, market_kind='market'
# for a tracked reporting market. Rung 3 resolves a project that tracks EXACTLY ONE
# country, so this single row is the whole posture.
AUTO_COUNTRY = "FR"
AUTO_MARKET_ID = "france"
AUTO_MARKET_LABEL = "France"

# Only the OFF project reads FALSE. Every other fixture project has the module ON.
FLAG_ON = {p: (p != OFF_PROJECT) for p in PROJECTS}

PLAN_ID = "mpl_feetax_41_3"
PLAN_VERSION_ID = "mpv_feetax_41_3"

# ---------------------------------------------------------------------------
# Raw Meta cost rows. (project_id, date, data_level, campaign_id, adset_id, ad_id, spend)
# ---------------------------------------------------------------------------
_COST_ROWS: list[tuple[str, date, str, str, str | None, str | None, float]] = []


def _campaign_rows(project: str, campaign: str, days: list[date], spend: float) -> None:
    for day in days:
        _COST_ROWS.append((project, day, "CAMPAIGN", campaign, None, None, spend))


_campaign_rows("feetax_dev_complete", "fdc_camp_1", [D1, D2, D3], 12345.67)
_campaign_rows("feetax_dev_complete", "fdc_camp_2", [D1, D2, D3], 1000.00)

_campaign_rows("feetax_dev_gapped", "fdg_camp_1", [D1, D2, D3], 2000.00)
_campaign_rows("feetax_dev_gapped", "fdg_camp_2", [D1, D2, D3], 3000.00)

_campaign_rows("feetax_dev_scope", "fds_camp_1", [D1], 500.00)

# feetax_dev_plan: THREE parallel cost series on one connector-day, each totalling the
# day (5 500.00 EUR). This is the meta-ads shape that would TRIPLE every fee without
# the canonical-dimension collapse, and it is what makes
# test_epic41_no_double_count_single_breakdown.sql discriminating rather than vacuous.
_campaign_rows("feetax_dev_plan", "fdp_camp_mapped", [D1], 4000.00)
_campaign_rows("feetax_dev_plan", "fdp_camp_unmapped", [D1], 1500.00)
_COST_ROWS.append(("feetax_dev_plan", D1, "ADSET", "fdp_camp_mapped", "fdp_adset_1", None, 4000.00))
_COST_ROWS.append(
    ("feetax_dev_plan", D1, "ADSET", "fdp_camp_unmapped", "fdp_adset_2", None, 1500.00)
)
_COST_ROWS.append(
    ("feetax_dev_plan", D1, "CREATIVE", "fdp_camp_mapped", "fdp_adset_1", "fdp_ad_1", 4000.00)
)
_COST_ROWS.append(
    ("feetax_dev_plan", D1, "CREATIVE", "fdp_camp_unmapped", "fdp_adset_2", "fdp_ad_2", 1500.00)
)

_campaign_rows("feetax_dev_refused", "fdr_camp_1", [D1], 1000.00)

_campaign_rows("feetax_dev_source_type", "fst_camp_1", [D1], 1000.00)

# F5: the OFF project now carries REAL spend, so "OFF => zero ladder rows" is a claim
# the anti-joins can actually falsify.
_campaign_rows(OFF_PROJECT, "fdo_camp_1", [D1, D2], 750.00)

# S6: 8 000 -> 17 000 -> 24 000 cumulative, crossing the 20 000 EUR floor on day 3.
# ONE campaign per day, so the cliff band pick is exercised at row grain.
_campaign_rows("feetax_dev_tiers_cliff", "fdt_camp_1", [D1], 8000.00)
_campaign_rows("feetax_dev_tiers_cliff", "fdt_camp_1", [D2], 9000.00)
_campaign_rows("feetax_dev_tiers_cliff", "fdt_camp_1", [D3], 7000.00)

# S6b + review finding F9: the SAME three day totals, but split across TWO campaigns a
# day. With one row per day the row-level cumulative degenerates to the day-level one
# and `tranche_excl` never differs from it, so the marginal telescoping was never
# actually exercised. Split 3000/5000, 4000/5000, 3000/4000 -> per-row components
# 150/250, 200/250, 150/120 M -> day totals 400/450/270 M, period 1 120 M.
_campaign_rows("feetax_dev_tiers_marginal", "fdt_camp_a", [D1], 3000.00)
_campaign_rows("feetax_dev_tiers_marginal", "fdt_camp_b", [D1], 5000.00)
_campaign_rows("feetax_dev_tiers_marginal", "fdt_camp_a", [D2], 4000.00)
_campaign_rows("feetax_dev_tiers_marginal", "fdt_camp_b", [D2], 5000.00)
_campaign_rows("feetax_dev_tiers_marginal", "fdt_camp_a", [D3], 3000.00)
_campaign_rows("feetax_dev_tiers_marginal", "fdt_camp_b", [D3], 4000.00)

_campaign_rows("feetax_dev_tiers_bad", "fdb_camp_1", [D1], 1000.00)

_campaign_rows("feetax_dev_wht_bad", "fdw_camp_1", [D1], 1000.00)

# F10: a rate migration 119 ADMITS (0.999999) over a base large enough that
# base/(1-w) exceeds INT64. 20 000 000.00 EUR = 2e13 micros; 2e13/1e-6 = 2e19 > 9.22e18.
# Before the magnitude guard this ABORTED THE BUILD with a DuckDB conversion error.
_campaign_rows("feetax_dev_wht_overflow", "fdx_camp_1", [D1], 20000000.00)

# F2: a legal-but-unroutable (category, form) pair.
_campaign_rows("feetax_dev_form_bad", "ffb_camp_1", [D1], 1000.00)

# F8: the FLAT allocation. Deliberately awkward weights so the largest-remainder path
# is exercised rather than an even split: 1000.00 + 2333.33 = 3333.33 per day.
#   camp_a floor(500000000 x 1000000000 / 3333330000) = 150000150
#   camp_b 500000000 - 150000150                      = 349999850
# Two days, so the test can assert the DAY total is exactly 500000000 twice over -- the
# whole point of the ruling, since before it the fee was billed once PER CAMPAIGN.
_campaign_rows("feetax_dev_flat", "ffl_camp_a", [D1, D2], 1000.00)
_campaign_rows("feetax_dev_flat", "ffl_camp_b", [D1, D2], 2333.33)

# F12: a NEGATIVE cost row -- a platform credit / make-good, which really does land as
# negative spend. Every rate-derived component is then legitimately negative and must
# pass through untouched instead of turning a data condition into a red build.
_campaign_rows("feetax_dev_credit", "fcr_camp_1", [D1], -500.00)

# F1: the END-TO-END auto-population fixture. Ordinary meta-ads campaign spend; the
# country is resolved by 41.2's PROJECT-POSTURE rung (see _AUTO_POSTURE_SQL below).
_campaign_rows(AUTO_PROJECT, "fau_camp_1", [D1, D2], 10000.00)


def _auto_rules() -> tuple[list[tuple], list[tuple[str, str, str]]]:
    """The auto-population fixture, BUILT BY ITERATING 41.2's generator.

    Returns (rule rows, condition rows) in this seeder's mirror shape. Two deliberate
    transformations and nothing else:
      * status is forced to 'confirmed'. The generator emits 'proposed' (inert until a
        human confirms, C.2); the whole point of this fixture is the state AFTER an
        operator confirms, because that is when a latent unsatisfiable precondition
        would first null a real customer's totals.
      * `conditions` is flattened to rows, exactly as migration 119's
        app.fee_tax_rule_conditions_v does in Postgres.
    Everything else -- category, form, rate, base_target, cascade_phase, sequence_order,
    effective_from, origin, dedup_hash, label -- is passed through UNTOUCHED. If the
    generator starts emitting a precondition the warehouse cannot resolve, this fixture
    gaps and test_epic41_auto_population_composes.sql fails. That is the guard.
    """
    rules: list[tuple] = []
    conditions: list[tuple[str, str, str]] = []
    for index, payload in enumerate(build_auto_rule_payloads([AUTO_COUNTRY])):
        rule_id = f"ftr_dev_auto_{index}"
        rules.append((
            rule_id,
            AUTO_PROJECT,
            payload["scope_kind"],
            payload["scope_ref"],
            payload["category"],
            payload["form"],
            None if payload.get("rate") is None else str(payload["rate"]),
            payload.get("amount_micros"),
            payload.get("cpm_micros"),
            payload.get("currency"),
            payload["base_target"],
            payload["cascade_phase"],
            payload["sequence_order"],
            payload["effective_from"],
            payload["effective_to"],
            "confirmed",
            payload["origin"],
            payload["dedup_hash"],
            payload["label"],
        ))
        for key, values in (payload.get("conditions") or {}).items():
            for value in values:
                conditions.append((rule_id, key, value))
    return rules, conditions


_AUTO_RULES, _AUTO_CONDITIONS = _auto_rules()


# ---------------------------------------------------------------------------
# Rules. Column order mirrors migration 119's app.fee_tax_rules_dim_v exactly.
# (id, project_id, scope_kind, scope_ref, category, form, rate, amount_micros,
#  cpm_micros, currency, base_target, cascade_phase, sequence_order,
#  effective_from, effective_to, status, origin, dedup_hash, label)
#
# dedup_HASH, not dedup_key (migration 119, renamed 2026-07-27): every write goes
# through operations.execute_operation, whose _is_secret_key rejects any payload field
# matching its secret pattern unless the name ends in _id / _ref / _hash -- so a column
# called dedup_key made create_rule raise OperationValidationError on EVERY call. The
# value is an sha256 with an ftk_ prefix, so the new name is also the accurate one.
# This seeder hand-builds mirror.fee_tax_rules for local dbt while the live sync reads
# app.fee_tax_rules_dim_v: the two column lists MUST agree or a green local build lies.
# created_by / created_at / updated_at are appended by the writer.
#
# Rule ids are READABLE rather than ULIDs on purpose: the DuckDB mirror carries no
# CHECK constraint, and a failing test that prints
# 'ftr_dev_complete_platform' instead of 'ftr_01JZ...' is a test a human can debug.
# ---------------------------------------------------------------------------
_RULES: list[tuple] = [
    # ---- feetax_dev_complete: the fully-composable ladder -------------------
    ("ftr_dev_complete_platform", "feetax_dev_complete", "project", None,
     "PLATFORM_FEE", "PERCENTAGE", "0.030000", None, None, None,
     "NET_MEDIA", 2, 1, WINDOW_FROM, None, "confirmed", "operator", None,
     "Platform fee 3% of net media"),
    # scope_precedence 3 but a DIFFERENT slot (phase 2, seq 2), so it does not
    # override the project rule -- both platform fees fire, which is the D.5 point
    # about the slot being the override handle rather than the phase.
    ("ftr_dev_complete_ds", "feetax_dev_complete", "datastream", "dse_feetax_complete_1",
     "PLATFORM_FEE", "PERCENTAGE", "0.010000", None, None, None,
     "NET_MEDIA", 2, 2, WINDOW_FROM, None, "confirmed", "operator", None,
     "Ad-serving fee 1% (datastream-scoped, single datastream => RESOLVED)"),
    ("ftr_dev_complete_wht", "feetax_dev_complete", "project", None,
     "WHT_GROSS_UP", "GROSS_UP", "0.150000", None, None, None,
     "RUNNING_SUBTOTAL", 4, 1, WINDOW_FROM, None, "confirmed", "operator", None,
     "Withholding tax gross-up 15%"),
    ("ftr_dev_complete_agency", "feetax_dev_complete", "project", None,
     "AGENCY_FEE", "PERCENTAGE", "0.100000", None, None, None,
     "RUNNING_SUBTOTAL", 5, 1, WINDOW_FROM, None, "confirmed", "operator", None,
     "Agency fee 10%"),
    ("ftr_dev_complete_vat", "feetax_dev_complete", "project", None,
     "SALES_TAX", "PERCENTAGE", "0.200000", None, None, None,
     "RUNNING_SUBTOTAL", 6, 1, WINDOW_FROM, None, "confirmed", "operator", None,
     "VAT 20%"),
    # MUST NOT FIRE: human-in-the-loop (C.2). A 50% platform fee that leaked into the
    # cascade would be unmissable in the assertions.
    ("ftr_dev_complete_proposed", "feetax_dev_complete", "project", None,
     "PLATFORM_FEE", "PERCENTAGE", "0.500000", None, None, None,
     "NET_MEDIA", 2, 9, WINDOW_FROM, None, "proposed", "auto_country", None,
     "PROPOSED 50% -- must never enter the cascade"),
    # MUST NOT CONTRIBUTE: Epic 27 invariant 4, VERIFICATION stays KEEP_SEPARATE (41.4).
    ("ftr_dev_complete_verification", "feetax_dev_complete", "project", None,
     "VERIFICATION", "CPM", None, None, 2500000, "EUR",
     "MEASURED_IMPRESSIONS", 2, 8, WINDOW_FROM, None, "confirmed", "operator", None,
     "IAS verification CPM 2.50 EUR -- KEEP_SEPARATE, 41.4's overlay"),

    # ---- feetax_dev_gapped: S12, the day-one honest end state ---------------
    ("ftr_dev_gapped_dst_gb", "feetax_dev_gapped", "project", None,
     "REGULATORY_TAX", "PERCENTAGE", "0.020000", None, None, None,
     "RUNNING_SUBTOTAL", 3, 1, WINDOW_FROM, None, "confirmed", "auto_country",
     "ftk_a761b37cab6448f51a6dd331a87c01c7cd2af14cbcfa2648382b5ef826177694",
     "DST GB 2% (country-conditioned)"),
    ("ftr_dev_gapped_dst_fr", "feetax_dev_gapped", "project", None,
     "REGULATORY_TAX", "PERCENTAGE", "0.030000", None, None, None,
     "RUNNING_SUBTOTAL", 3, 2, WINDOW_FROM, None, "confirmed", "auto_country",
     "ftk_be187e96211e34e1f48a50201f8b22326b5c789cc2697852acc0cd282821cb05",
     "DST FR 3% (country-conditioned)"),
    ("ftr_dev_gapped_vat_fr", "feetax_dev_gapped", "project", None,
     "SALES_TAX", "PERCENTAGE", "0.200000", None, None, None,
     "RUNNING_SUBTOTAL", 6, 1, WINDOW_FROM, None, "confirmed", "auto_country",
     "ftk_7fbb0a6814634f6dfa2b0ffbd9e0702d383f006a595f58c6c8f9e31e83cf7673",
     "VAT FR 20% (country-conditioned)"),
    # The anti-vacuity rule: without it S12 could "pass" by everything being NULL.
    ("ftr_dev_gapped_agency", "feetax_dev_gapped", "project", None,
     "AGENCY_FEE", "PERCENTAGE", "0.100000", None, None, None,
     "RUNNING_SUBTOTAL", 5, 1, WINDOW_FROM, None, "confirmed", "operator", None,
     "Agency fee 10% (UNCONDITIONED -- must still fire)"),

    # ---- feetax_dev_scope: D2 ambiguity + unknown scope_ref -----------------
    ("ftr_dev_scope_ambiguous", "feetax_dev_scope", "datastream", "dse_feetax_scope_a",
     "PLATFORM_FEE", "PERCENTAGE", "0.050000", None, None, None,
     "NET_MEDIA", 2, 1, WINDOW_FROM, None, "confirmed", "operator", None,
     "Datastream-scoped 5% on a connector with TWO datastreams => AMBIGUOUS"),
    ("ftr_dev_scope_unknown", "feetax_dev_scope", "datastream", "dse_feetax_absent",
     "REGULATORY_TAX", "PERCENTAGE", "0.010000", None, None, None,
     "RUNNING_SUBTOTAL", 3, 1, WINDOW_FROM, None, "confirmed", "operator", None,
     "scope_ref unknown to the mirror => RULE_SCOPE_UNRESOLVED for the whole project"),

    # ---- feetax_dev_plan: D5 + the C4 guard ---------------------------------
    ("ftr_dev_plan_platform", "feetax_dev_plan", "plan_version", PLAN_VERSION_ID,
     "PLATFORM_FEE", "PERCENTAGE", "0.020000", None, None, None,
     "NET_MEDIA", 2, 1, WINDOW_FROM, None, "confirmed", "media_plan", None,
     "Plan-scoped platform fee 2% -- mapped campaign MATCHes, unmapped is known-false"),

    # ---- feetax_dev_refused: E41-FR05 ---------------------------------------
    ("ftr_dev_refused_flat_usd", "feetax_dev_refused", "project", None,
     "PLATFORM_FEE", "FLAT", None, 500000000, None, "USD",
     "NET_MEDIA", 2, 1, WINDOW_FROM, None, "confirmed", "operator", None,
     "FLAT 500 USD on an EUR project -- refused, never summed, never converted"),

    # ---- source_type scoping: NULL must read UNRESOLVED, never "matches nothing" --
    # 41.2's view emits attr_source_type NULL on EVERY row today (nothing writes
    # app.datastream_source_types yet -- the declaration surface is 41.6/41.8, and 41.2
    # deliberately refused to fabricate 'UNKNOWN' in SQL). A matcher that read NULL as
    # known-false would silently compute NOTHING for every source-type-scoped rule while
    # reporting a complete, trustworthy total. It must gap instead.
    # In production this condition row arrives from source_type_scope, UNION ALL'd into
    # app.fee_tax_rule_conditions_v as condition_key='source_type' (C3) -- one uniform
    # matching mechanism, no arrays anywhere.
    ("ftr_dev_source_type", "feetax_dev_source_type", "project", None,
     "PLATFORM_FEE", "PERCENTAGE", "0.030000", None, None, None,
     "NET_MEDIA", 2, 1, WINDOW_FROM, None, "confirmed", "operator", None,
     "Scoped to source_type=PAID_MEDIA -- unresolvable today, so it must GAP"),

    # ---- the two tier modes --------------------------------------------------
    ("ftr_dev_tiers_cliff", "feetax_dev_tiers_cliff", "project", None,
     "PLATFORM_FEE", "SPEND_TIERS", None, None, None, None,
     "NET_MEDIA", 2, 1, WINDOW_FROM, None, "confirmed", "operator", None,
     "Volume tier, CLIFF: 5% up to 20 000 EUR cumulative, 3% beyond"),
    ("ftr_dev_tiers_marginal", "feetax_dev_tiers_marginal", "project", None,
     "PLATFORM_FEE", "SPEND_TIERS", None, None, None, None,
     "NET_MEDIA", 2, 1, WINDOW_FROM, None, "confirmed", "operator", None,
     "Volume tier, MARGINAL: same bands, per-tranche rates"),

    # A SPEND_TIERS rule with NO band row at all: the ladder must gap
    # (TIERS_MALFORMED), never silently contribute 0. Deliberately absent from _TIERS.
    ("ftr_dev_tiers_bad", "feetax_dev_tiers_bad", "project", None,
     "PLATFORM_FEE", "SPEND_TIERS", None, None, None, None,
     "NET_MEDIA", 2, 1, WINDOW_FROM, None, "confirmed", "operator", None,
     "SPEND_TIERS with no band ladder -- must raise TIERS_MALFORMED"),

    # ---- the fail-closed gross-up guards -------------------------------------
    ("ftr_dev_wht_bad", "feetax_dev_wht_bad", "project", None,
     "WHT_GROSS_UP", "GROSS_UP", "1.000000", None, None, None,
     "RUNNING_SUBTOTAL", 4, 1, WINDOW_FROM, None, "confirmed", "operator", None,
     "WHT rate 1.0 -- division by zero if unguarded; must gap, never divide"),
    # F10: a rate the CHECK ADMITS, over a base whose quotient exceeds INT64. Before the
    # magnitude guard this raised a DuckDB conversion error and took the BUILD down --
    # a red build where E41-NFR02 requires a typed gap.
    ("ftr_dev_wht_overflow", "feetax_dev_wht_overflow", "project", None,
     "WHT_GROSS_UP", "GROSS_UP", "0.999999", None, None, None,
     "RUNNING_SUBTOTAL", 4, 1, WINDOW_FROM, None, "confirmed", "operator", None,
     "WHT rate 0.999999 on a 20 M EUR base -- grossed total overflows INT64, must gap"),

    # ---- F2: a legal (category, form) pair that NO phase branch can execute ----
    # Every CHECK in migration 119 passes: GROSS_UP has a rate, the base_target is
    # legal, the phase is in 1..6. But phase 5 has no GROSS_UP branch, so before the
    # routing guard this fell to ELSE 0 and reported agency_fee_micros = 0 with
    # is_ladder_complete = TRUE -- an understated invoice presented as trustworthy.
    ("ftr_dev_form_bad", "feetax_dev_form_bad", "project", None,
     "AGENCY_FEE", "GROSS_UP", "0.100000", None, None, None,
     "RUNNING_SUBTOTAL", 5, 1, WINDOW_FROM, None, "confirmed", "llm", None,
     "AGENCY_FEE x GROSS_UP -- no phase routes it, so it must GAP not contribute 0"),

    # ---- F8: a FLAT rule that actually produces a number -----------------------
    # Across the previous fixture set there was exactly ONE FLAT rule and its component
    # was REFUSED for currency, so the FLAT branch never once produced a figure and the
    # once-per-row multiplication was invisible. 500.00 EUR, project-scoped, EUR.
    ("ftr_dev_flat", "feetax_dev_flat", "project", None,
     "PLATFORM_FEE", "FLAT", None, 500000000, None, "EUR",
     "NET_MEDIA", 2, 1, WINDOW_FROM, None, "confirmed", "operator", None,
     "FLAT 500.00 EUR -- applies ONCE per (project, date, currency), then allocated"),

    # ---- F12: a rate rule over NEGATIVE spend ----------------------------------
    ("ftr_dev_credit", "feetax_dev_credit", "project", None,
     "PLATFORM_FEE", "PERCENTAGE", "0.030000", None, None, None,
     "NET_MEDIA", 2, 1, WINDOW_FROM, None, "confirmed", "operator", None,
     "Platform fee 3% over a credit -- the component is legitimately negative"),
] + _AUTO_RULES

# (rule_id, condition_key, condition_value)
_CONDITIONS: list[tuple[str, str, str]] = [
    ("ftr_dev_gapped_dst_gb", "country", "GB"),
    ("ftr_dev_gapped_dst_fr", "country", "FR"),
    ("ftr_dev_gapped_vat_fr", "country", "FR"),
    ("ftr_dev_source_type", "source_type", "PAID_MEDIA"),
] + _AUTO_CONDITIONS

# (rule_id, tier_index, threshold_micros, rate, mode) -- tier_index is 0-BASED.
_TIERS: list[tuple[str, int, int, str, str]] = [
    ("ftr_dev_tiers_cliff", 0, 0, "0.050000", "cliff"),
    ("ftr_dev_tiers_cliff", 1, 20000000000, "0.030000", "cliff"),
    ("ftr_dev_tiers_marginal", 0, 0, "0.050000", "marginal"),
    ("ftr_dev_tiers_marginal", 1, 20000000000, "0.030000", "marginal"),
]

# (project_id, datastream_id, connector, data_role, source_kind)
_DATASTREAMS: list[tuple[str, str, str | None, str, str]] = [
    ("feetax_dev_complete", "dse_feetax_complete_1", CONNECTOR, "paid_media", "api"),
    # TWO datastreams on ONE connector => D2 ambiguity. One is never picked.
    ("feetax_dev_scope", "dse_feetax_scope_a", CONNECTOR, "paid_media", "api"),
    ("feetax_dev_scope", "dse_feetax_scope_b", CONNECTOR, "paid_media", "api"),
    # connector NULL: a managed_feed datastream. Mirrored on purpose (41.2 must see
    # it), skipped by 41.3's scope resolution because it can never match a fact row.
    ("feetax_dev_complete", "dse_feetax_feed_1", None, "paid_media", "managed_feed"),
]

# (datastream_id, source_type, declared_by)
_SOURCE_TYPES: list[tuple[str, str, str]] = [
    ("dse_feetax_complete_1", "PAID_MEDIA", "seed"),
    ("dse_feetax_scope_a", "PAID_MEDIA", "seed"),
    ("dse_feetax_scope_b", "PAID_MEDIA", "seed"),
]


# ---------------------------------------------------------------------------
# DDL. Column names/order/types are the CONTRACT migration 119 must satisfy.
# CREATE TABLE IF NOT EXISTS everywhere -- never CREATE OR REPLACE, which would drop
# rows a real mirror_sync landed.
# ---------------------------------------------------------------------------
_FEE_TAX_DDL = """
CREATE SCHEMA IF NOT EXISTS mirror;

-- app.fee_tax_rules_dim_v: SCALARS ONLY, 22 columns, PK named `id` (not rule_id).
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

-- app.fee_tax_rule_conditions_v: one row per (rule, condition key, condition value).
-- A rule with NO row here is UNCONSTRAINED and matches every row.
CREATE TABLE IF NOT EXISTS mirror.fee_tax_rule_conditions (
    rule_id         VARCHAR,
    condition_key   VARCHAR,
    condition_value VARCHAR
);

-- app.fee_tax_rule_tiers_v: one row per band, 0-based tier_index, `mode` denormalised.
CREATE TABLE IF NOT EXISTS mirror.fee_tax_rule_tiers (
    rule_id          VARCHAR,
    tier_index       INTEGER,
    threshold_micros BIGINT,
    rate             DECIMAL(12,6),
    mode             VARCHAR
);

-- app.datastreams_dim_v: `connector` is app.datastreams.module_name ALIASED, and it
-- is NULLABLE (managed_feed / external_bq datastreams).
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

-- SHAPE-STABLE AND DELIBERATELY EMPTY: migration 104 is not applied and nothing
-- writes binding_kind='datastream'. Empty is production's real state, not a bug.
CREATE TABLE IF NOT EXISTS mirror.datastream_country_binding_dim (
    project_id    VARCHAR,
    connector     VARCHAR,
    datastream_id VARCHAR,
    market_id     VARCHAR
);
"""

# The plan mirror, byte-identical in shape to seed_plan_mirror.py's DDL. Created here
# only so this seeder is order-independent: if seed_plan_mirror.py ran first these are
# all no-ops. All FIVE relations are created together -- creating only some of them
# would leave plan_vs_actual_daily referencing a missing source.
_PLAN_DDL = """
CREATE TABLE IF NOT EXISTS mirror.media_plans (
    id VARCHAR, project_id VARCHAR, name VARCHAR, currency VARCHAR,
    created_by VARCHAR, created_at TIMESTAMP, updated_at TIMESTAMP, archived_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS mirror.media_plan_versions (
    id VARCHAR, plan_id VARCHAR, version_number INTEGER, status VARCHAR,
    is_active BOOLEAN, source_note VARCHAR, created_by VARCHAR, created_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS mirror.media_plan_lines (
    id VARCHAR, version_id VARCHAR, line_key VARCHAR, label VARCHAR, channel VARCHAR,
    start_date DATE, end_date DATE, budget DECIMAL(14,2), buy_mode VARCHAR,
    is_plan_only BOOLEAN, sort_order INTEGER
);
CREATE TABLE IF NOT EXISTS mirror.plan_allocation_daily (
    version_id VARCHAR, line_id VARCHAR, day DATE, amount DECIMAL(14,2)
);
CREATE TABLE IF NOT EXISTS mirror.plan_line_mappings (
    id VARCHAR, plan_id VARCHAR, line_key VARCHAR, connector VARCHAR,
    campaign_ref VARCHAR, split_weight DECIMAL(7,6), status VARCHAR,
    created_by VARCHAR, created_at TIMESTAMP, updated_at TIMESTAMP
);
"""

# ---------------------------------------------------------------------------
# mirror.project_preferences -- the one relation this seeder does NOT own.
#
# In production it is written by mirror_sync (SELECT * FROM app.project_preferences)
# and read by dim_project and by every stg_*_daily FX join.
#
# CORRECTED 2026-08-22. This header said `fee_tax_country_resolution` binds
# `geographic_mode` and `local_markets` "for its project-posture rung", and that a
# missing column makes the whole Epic-41 sub-DAG unbuildable. Story 37.9 / migration
# 269 moved that model onto `mirror.country_market_projection` on 2026-08-17, and
# measured today the cascade binds NEITHER column -- so the sentence described a
# dependency that no longer exists, on the columns the Country capability replaced
# outright. It is the same shape as the three silent readers 37.9 found: prose that
# outlived what it described.
#
# Since this script is the only way to get a local warehouse WITHOUT Postgres, a
# "minimal" fallback that omits those columns is not a fallback at all -- it just
# moves the failure 200 lines into somebody else's model and forces the operator to
# ALTER the table by hand. So the fallback is SHAPE-COMPLETE, the defaulted columns
# are also ADD COLUMN IF NOT EXISTS'd on a table that ALREADY exists (self-healing a
# warehouse synced from a Postgres where 057 / 102 / 119 are not applied), and the
# required set is ASSERTED at the end -- so the next missing column fails HERE, by
# name, with the command that fixes it.
#
# Defaults are the Postgres DEFAULTs, not inventions: 'global' (057), '[]' (102),
# FALSE (119). DuckDB backfills existing rows with them, which is honest -- a
# Postgres without 057 genuinely has no geographic posture.
#
# (column, DuckDB type, DEFAULT literal or None, the migration/story that owns it)
_PREFS_COLUMNS: tuple[tuple[str, str, str | None, str], ...] = (
    ("project_id",                 "VARCHAR",   None,                    "Story 4.4"),
    ("canonical_currency",         "VARCHAR",   None,                    "Story 4.4"),
    ("reporting_timezone",         "VARCHAR",   None,                    "Story 4.4"),
    ("verification_source_type",   "VARCHAR",   None,                    "mig 028 / 17.1"),
    ("verification_source_id",     "VARCHAR",   None,                    "mig 028 / 17.1"),
    ("lead_event_name",            "VARCHAR",   None,                    "mig 028 / 17.1"),
    ("geographic_mode",            "VARCHAR",   "'global'",              "mig 057 / Epic 37"),
    ("local_market_country_codes", "VARCHAR[]", "CAST([] AS VARCHAR[])", "mig 057 / Epic 37"),
    ("local_markets",              "VARCHAR",   "'[]'",                  "mig 102 / Epic 37"),
    ("fee_tax_alignment_enabled",  "BOOLEAN",   "FALSE",                 "mig 119 / Story 41.1"),
    ("created_at",                 "TIMESTAMP", None,                    "Story 4.4"),
    ("updated_at",                 "TIMESTAMP", None,                    "Story 4.4"),
)

# The columns an Epic-41 model actually BINDS against. A missing one is a hard stop.
_PREFS_REQUIRED_BY_EPIC41 = (
    "project_id",                 # every join key
    "canonical_currency",         # the ladder row's currency (41.3)
    "fee_tax_alignment_enabled",  # 41.3's activation flag
)
# `geographic_mode` / `local_markets` left the required set on 2026-08-22: no Epic-41
# model binds them any more (37.9 / migration 269). They stay in `_PREFS_COLUMNS` above
# because mirror_sync copies the table with `SELECT *`, so a real mirror carries them --
# the SHAPE is still honest; the hard stop was not.


def _prefs_create_ddl() -> str:
    body = ",\n    ".join(
        f"{name:26} {dtype}" + (f" DEFAULT {default}" if default else "")
        for name, dtype, default, _owner in _PREFS_COLUMNS
    )
    return f"CREATE TABLE IF NOT EXISTS mirror.project_preferences (\n    {body}\n)"


def _prefs_existing_columns(con) -> set[str]:  # noqa: ANN001 -- duckdb conn, imported lazily
    return {
        row[0]
        for row in con.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'mirror' AND table_name = 'project_preferences'"
        ).fetchall()
    }


def _seed_cost_rows(duckdb_path: str) -> int:
    """Land the fixture's raw Meta cost rows (idempotent: delete our pull first)."""
    import duckdb  # noqa: PLC0415

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")

    # Idempotency: drop our own pull before re-seeding. We must NOT prime the table with
    # an empty `_load_meta_duckdb([], ...)` -- `load_duckdb` runs its CREATE DDL and then
    # calls `executemany` unconditionally, and DuckDB raises InvalidInputException on an
    # empty parameter-set list. So guard the DELETE on the table actually existing
    # instead; the real load below creates it when absent.
    con = duckdb.connect(duckdb_path)
    try:
        exists = con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'raw_meta_ads_daily'"
        ).fetchone()
        if exists:
            con.execute("DELETE FROM raw_meta_ads_daily WHERE pull_id = ?", [PULL_ID])
    finally:
        con.close()

    rows = [
        {
            "date": day.isoformat(),
            "data_level": data_level,
            "campaign_id": campaign_id,
            "campaign_name": campaign_id,
            "adset_id": adset_id,
            "adset_name": adset_id,
            "ad_id": ad_id,
            "creative_id": ad_id,
            "spend": spend,
            "impressions": 1000,
            "clicks": 10,
            "conversions": 1,
            "project_id": project_id,
            # EUR so the EUR->EUR identity rate applies and fact cost == spend exactly.
            "cost_source_currency": "EUR",
        }
        for project_id, day, data_level, campaign_id, adset_id, ad_id, spend in _COST_ROWS
    ]
    if not rows:
        # Same trap as the priming call above, one layer out: load_duckdb calls
        # executemany unconditionally and DuckDB rejects an empty parameter-set list.
        raise SystemExit("ERROR: _COST_ROWS is empty -- the fixture would seed no facts")
    return _load_meta_duckdb(rows, PULL_ID, loaded_at, duckdb_path)


def _seed_mirror(duckdb_path: str) -> None:
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    try:
        con.execute(_FEE_TAX_DDL)
        con.execute(_PLAN_DDL)

        # --- project_preferences: CREATE-if-absent + ALTER, never CREATE OR REPLACE ---
        # CREATE OR REPLACE would drop the real mirrored rows that dim_project (a
        # `table` mart) and every stg_*_daily FX join depend on.
        prefs_exists = con.execute(
            "SELECT COUNT(*) FROM information_schema.tables"
            " WHERE table_schema = 'mirror' AND table_name = 'project_preferences'"
        ).fetchone()[0]
        if not prefs_exists:
            print(
                "WARNING: mirror.project_preferences was absent -- creating the"
                " SHAPE-COMPLETE dev fallback so every Epic-41 model can bind against"
                " it. This is the no-Postgres dev path; run"
                " 'uv run python -m core.mirror_sync' wherever Postgres exists to get"
                " the real relation and the real preference values."
            )
            con.execute(_prefs_create_ddl())

        # Self-heal a table that predates migration 057 / 102 / 119 (or came from a
        # mirror_sync against a Postgres where they are unapplied). Each ALTER is a
        # no-op when the column is already there, so a fully-synced warehouse is
        # untouched -- and an operator never has to ALTER by hand to make the build run.
        present = _prefs_existing_columns(con)
        added: list[str] = []
        for name, dtype, default, owner in _PREFS_COLUMNS:
            if default is None or name in present:
                continue
            con.execute(
                "ALTER TABLE mirror.project_preferences"
                f" ADD COLUMN IF NOT EXISTS {name} {dtype} DEFAULT {default}"
            )
            added.append(f"{name} [{owner}, defaulted to {default}]")
        if added and prefs_exists:
            print(
                "WARNING: mirror.project_preferences was missing columns an Epic-41"
                " model binds against; added them with their Postgres DEFAULTs: "
                + ", ".join(added)
                + ". Re-run mirror_sync after applying the owning migrations to get"
                " real values instead of defaults."
            )

        # Fail HERE, by name, rather than as a Binder Error 200 lines into somebody
        # else's model. This is the guard the epic-41 DAG actually depends on.
        missing = [c for c in _PREFS_REQUIRED_BY_EPIC41 if c not in _prefs_existing_columns(con)]
        if missing:
            raise SystemExit(
                "ERROR: mirror.project_preferences lacks column(s) required by the"
                f" Epic-41 models: {', '.join(missing)}."
                " Run 'uv run python -m core.mirror_sync' (after applying migrations"
                " 057 / 102 / 119), or delete the DuckDB file and re-run this seeder to"
                " get the shape-complete dev fallback."
            )

        for project_id in PROJECTS:
            con.execute("DELETE FROM mirror.project_preferences WHERE project_id = ?", [project_id])
            # geographic_mode / local_markets / local_market_country_codes are
            # DELIBERATELY not named here. Their landed TYPE is not pinned -- a
            # populated mirror_sync lands Python lists/dicts through pandas while its
            # empty-table branch declares EVERY column VARCHAR (the hazard
            # sources_mirror.yml documents) -- so writing a literal into them could
            # fail on a cast. Leaving them to the column DEFAULT (or NULL on a
            # mirror-created table) is both type-safe and semantically identical for
            # these fixtures. Since 2026-08-17 (37.9 / migration 269) no Epic-41 model
            # reads them at all -- the posture rung reads
            # `mirror.country_market_projection` -- so leaving them unset is now the
            # honest state as well as the safe one: it is what a Project with no
            # published Country meaning looks like, which keeps attr_country NULL and
            # makes COUNTRY_UNRESOLVED the hot path S12/T17 assert.
            con.execute(
                "INSERT INTO mirror.project_preferences"
                " (project_id, canonical_currency, reporting_timezone,"
                "  fee_tax_alignment_enabled)"
                " VALUES (?, 'EUR', 'Europe/Paris', ?)",
                [project_id, FLAG_ON[project_id]],
            )

        # --- the ONE project with a published Country meaning --------------------
        # F1's fixture has to RESOLVE a country, and only one of the bridge's three
        # rungs is reachable here:
        #   rung 1 (a `country` breakdown on the row) is STRUCTURALLY UNREACHABLE for a
        #     cost row -- NO connector in fact_daily_kpi emits breakdown_dimension
        #     ='country' together with metric='cost' (amendment A.2 / AI-51: GA4 and GSC
        #     carry country but never cost; meta / tiktok / linkedin carry cost but never
        #     country). That absence is not an oversight, it is the reason
        #     COUNTRY_UNRESOLVED exists at all;
        #   rung 2 (a declared datastream->market binding) needs
        #     datastream_country_binding_dim, which this seeder is explicitly forbidden
        #     to populate (migration 104 is unapplied and empty IS production's state);
        #   rung 3, the governed single-country meaning, is the only one left.
        # Until 2026-08-17 rung 3 read project_preferences.geographic_mode /
        # local_markets; Story 37.9 / migration 269 replaced that authority outright
        # with mirror.country_market_projection, so THAT is where this fixture now
        # publishes -- one row, one tracked country, market_kind='market'. Every other
        # fixture project publishes NO row, which the mart reads as "cannot decide"
        # and is what preserves the COUNTRY_UNRESOLVED hot path.
        # A missing relation here means create_mirror was not run first; that dies
        # loudly on the INSERT, pointing at the one shape definition.
        con.execute(
            "DELETE FROM mirror.country_market_projection WHERE project_id = ?",
            [AUTO_PROJECT],
        )
        con.execute(
            "INSERT INTO mirror.country_market_projection"
            " (project_id, registry_id, hierarchy_version_id, vocabulary_version_id,"
            "  hierarchy_content_hash, country_code, market_id, market_label,"
            "  market_kind, region_id, region_label, display_order)"
            " VALUES (?, 'mdr_EXAMPLE_LOCAL', 'mdv_EXAMPLE_LOCAL',"
            "         'vocab_EXAMPLE_LOCAL', 'sha256:EXAMPLE_LOCAL', ?, ?, ?,"
            "         'market', NULL, NULL, 1)",
            [AUTO_PROJECT, AUTO_COUNTRY, AUTO_MARKET_ID, AUTO_MARKET_LABEL],
        )

        # --- rules / conditions / tiers ---------------------------------------
        rule_ids = [r[0] for r in _RULES]
        placeholders = ", ".join("?" for _ in rule_ids)
        con.execute(f"DELETE FROM mirror.fee_tax_rules WHERE id IN ({placeholders})", rule_ids)
        con.execute(
            f"DELETE FROM mirror.fee_tax_rule_conditions WHERE rule_id IN ({placeholders})",
            rule_ids,
        )
        con.execute(
            f"DELETE FROM mirror.fee_tax_rule_tiers WHERE rule_id IN ({placeholders})",
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

        for rule_id, tier_index, threshold, rate, mode in _TIERS:
            con.execute(
                "INSERT INTO mirror.fee_tax_rule_tiers"
                " (rule_id, tier_index, threshold_micros, rate, mode)"
                " VALUES (?, ?, ?, ?, ?)",
                [rule_id, tier_index, threshold, rate, mode],
            )

        # --- datastreams + source types ----------------------------------------
        ds_ids = [d[1] for d in _DATASTREAMS]
        ds_placeholders = ", ".join("?" for _ in ds_ids)
        con.execute(
            f"DELETE FROM mirror.datastreams_dim WHERE datastream_id IN ({ds_placeholders})",
            ds_ids,
        )
        con.execute(
            "DELETE FROM mirror.datastream_source_types WHERE datastream_id IN"
            f" ({ds_placeholders})",
            ds_ids,
        )
        for project_id, datastream_id, connector, data_role, source_kind in _DATASTREAMS:
            con.execute(
                "INSERT INTO mirror.datastreams_dim"
                " (project_id, datastream_id, connector, data_role, source_kind)"
                " VALUES (?, ?, ?, ?, ?)",
                [project_id, datastream_id, connector, data_role, source_kind],
            )
        for datastream_id, source_type, declared_by in _SOURCE_TYPES:
            con.execute(
                "INSERT INTO mirror.datastream_source_types"
                " (datastream_id, source_type, declared_by, declared_at)"
                " VALUES (?, ?, ?, now())",
                [datastream_id, source_type, declared_by],
            )

        # datastream_country_binding_dim is INTENTIONALLY left empty (see the docstring).

        # --- the plan a plan_version-scoped rule points at ----------------------
        con.execute("DELETE FROM mirror.plan_line_mappings WHERE plan_id = ?", [PLAN_ID])
        con.execute("DELETE FROM mirror.media_plan_versions WHERE id = ?", [PLAN_VERSION_ID])
        con.execute("DELETE FROM mirror.media_plans WHERE id = ?", [PLAN_ID])
        con.execute(
            "INSERT INTO mirror.media_plans"
            " (id, project_id, name, currency, created_by, created_at, updated_at, archived_at)"
            " VALUES (?, 'feetax_dev_plan', 'Fee/tax 41.3 plan', 'EUR', 'seed', now(), now(),"
            "         NULL)",
            [PLAN_ID],
        )
        con.execute(
            "INSERT INTO mirror.media_plan_versions"
            " (id, plan_id, version_number, status, is_active, source_note, created_by, created_at)"
            " VALUES (?, ?, 1, 'published', TRUE, 'seed', 'seed', now())",
            [PLAN_VERSION_ID, PLAN_ID],
        )
        # NO media_plan_lines and NO plan_allocation_daily rows on purpose: this plan
        # exists to resolve a RULE SCOPE, and plan_vs_actual_daily INNER JOINs its lines,
        # so it can never produce a plan-vs-actual row and can never perturb Story 22.4's
        # totals. Only fdp_camp_mapped is mapped -- fdp_camp_unmapped is the known-false
        # half of D5.
        con.execute(
            "INSERT INTO mirror.plan_line_mappings"
            " (id, plan_id, line_key, connector, campaign_ref, split_weight, status,"
            "  created_by, created_at, updated_at)"
            " VALUES ('plm_feetax_41_3_a', ?, 'feetax-line-a', ?, 'fdp_camp_mapped',"
            "         1.000000, 'active', 'seed', now(), now())",
            [PLAN_ID, CONNECTOR],
        )
    finally:
        con.close()


def run(duckdb_path: str) -> None:
    n_cost = _seed_cost_rows(duckdb_path)
    _seed_mirror(duckdb_path)
    print(
        "seed_fee_tax_mirror OK  "
        f"projects={len(PROJECTS)}  rules={len(_RULES)}  conditions={len(_CONDITIONS)}  "
        f"tier_bands={len(_TIERS)}  datastreams={len(_DATASTREAMS)}  "
        f"cost_rows={n_cost}  binding_dim=EMPTY(by design)  -> {duckdb_path}"
    )


def main() -> None:
    default_duckdb = os.environ.get("TOOROW_DUCKDB_PATH", "")
    parser = argparse.ArgumentParser(
        description="Seed the Story 41.3 fee & tax mirror + fact cost rows into a DuckDB file"
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
