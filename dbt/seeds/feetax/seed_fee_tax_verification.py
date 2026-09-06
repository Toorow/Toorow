"""Deterministic fixture seeder for Story 41.4 -- the KEEP_SEPARATE verification overlay.

WHY THIS IS A SIBLING AND NOT AN EDIT TO ``seed_fee_tax_mirror.py`` (ruling Q1).
Two reasons, and the second is the load-bearing one:

  * Story 41.5 will want the same fixture universe. If 41.4 grew the 41.3 seeder, 41.5
    would have to queue behind it for the same file; as a sibling, 41.5 simply adds
    ``seed_fee_tax_revenue.py`` the same way.
  * 41.4's Task 1 EXTRACTS the condition matcher out of ``fee_tax_ladder_daily.sql`` and
    gates that refactor on 41.3's singular tests staying BYTE-IDENTICAL. That statement is
    only meaningful if 41.3's fixtures provably did not move underneath it. Because this
    script touches no pre-existing seeder file and deletes/inserts ONLY its own keys,
    ``seed_fee_tax_mirror.py`` stays byte-identical to HEAD and 41.3's fixture rows stay
    exactly as they were.

The mirror DDL is IMPORTED rather than copied, so the mirror shape has exactly one
definition and cannot drift. Every statement in it is ``CREATE TABLE IF NOT EXISTS``, so
running this script AFTER ``seed_fee_tax_mirror.py`` is a no-op for the shared tables --
and running it BEFORE also works.

THERE IS NO IAS SEED LOADER IN THE REPOSITORY (no ``server/modules/ias/seeds/``
directory), so ``_seed_ias_rows`` is hand-rolled here. Its ``CREATE TABLE`` column list is
copied VERBATIM from ``server/modules/ias/connector.py``'s ``_RAW_CREATE_DDL``; if the two
ever disagree, a real ``dbt build`` fails loudly here rather than silently in a customer's
invoice. Only ``measured_impressions`` is populated -- the other five metric columns are
left NULL on purpose, so ``fact_daily_kpi``'s ``HAVING SUM(...) IS NOT NULL`` (AD-9) emits
NO ROW for them. An absent metric is not a real zero.

DETERMINISTIC with FIXED DATES (never ``date.today()``) and idempotent: every insert is
preceded by a DELETE of exactly this fixture's own keys (``feetax_dev_verif_*`` projects,
``ftr_dev_verif_*`` rule ids, the three ``pull_feetax_41_4*`` pull ids), so re-running
never duplicates and CAN NEVER TOUCH a Story 41.3 fixture row.

The nine fixture projects (each exists to make ONE assertion non-vacuous):

  feetax_dev_verif_twin_a       Guard 3's CONTROL arm: meta-ads fvt_camp_1, 12345.67 on
                                D1, three ladder rules, and NO verification whatsoever.
  feetax_dev_verif_twin_b       Guard 3's TREATMENT arm: byte-for-byte the same spend,
                                the same three ladder rules, the same day and campaign --
                                PLUS a confirmed VERIFICATION CPM rule and IAS facts. T3
                                asserts the two projects' fee_tax_ladder_daily rows are
                                equal column-for-column at tolerance EXACTLY 0, which
                                fails the moment verification cost lands anywhere in the
                                ladder. Also T1's pinned split: camp_x 1 234 567 ->
                                3 086 417 500 and camp_y 765 433 -> 1 913 582 500, summing
                                to EXACTLY 5 000 000 000. Both IAS ids are deliberately
                                UNLIKE the meta id, so the spend link is
                                UNLINKED_NO_SPEND_MATCH -- the expected steady state.
  feetax_dev_verif_halfup       T2's rounding boundary: CPM 2 500 500 over 999 997
                                impressions = 2 500 492 498.5, an exact half whose integer
                                part is EVEN, so ROUND_HALF_UP (2 500 492 499) and
                                banker's rounding (2 500 492 498) DISAGREE. Also the
                                "verification with no spend at all in the project" case.
  feetax_dev_verif_linked       T5b rung 1: IAS and meta-ads share the campaign ref
                                shared_camp_1, so LINKED_VIA_CAMPAIGN_REF fires.
  feetax_dev_verif_ambiguous    T5b: meta-ads AND linkedin-ads both carry dup_camp_1, so
                                the id is ambiguous across TWO spend connectors and ONE IS
                                NEVER PICKED.
  feetax_dev_verif_no_rule      T5a / ruling Q4: real impressions, NO rule. cost NULL (not
                                0), impressions fully visible, gap_codes '' and
                                is_allocation_complete TRUE.
  feetax_dev_verif_no_base      T5a: a confirmed CPM rule and NO IAS facts at all -- the
                                feed is not connected. One rule_without_base reason row
                                with VERIFICATION_BASE_MISSING, never a silent drop.
  feetax_dev_verif_refused      T5a / E41-FR05: a CPM rule in USD on an EUR project.
                                REFUSED -- never summed, NEVER CONVERTED (converting would
                                apply FX a second time outside the read locus).
  feetax_dev_verif_mixed_metric T8: the meta loader lands impressions = 1000 while IAS
                                lands measured_impressions = 250 000. Only the latter is
                                priced (625 000 000 micros); `impressions` is delivery
                                volume, not measured volume, and pricing it would inflate
                                verification cost by the whole unmeasured tail.

THE THREE CPM RANGE PROJECTS (added 2026-08-10, review finding C). ``fee_tax_cpm_micros``
and the overlay both guard ``cpm_micros`` NULL / negative / >= 1e10 and the overlay's
header says "Fail closed", but EVERY fixture CPM was 2 500 000 or 2 500 500:

    select id, cpm_micros from mirror.fee_tax_rules where form = 'CPM'
    -> 8 rules, none NULL, none negative, none >= 1e10

so ``VERIFICATION_CPM_OUT_OF_RANGE`` was a value declared in ``accepted_values`` that no
build could ever emit, and the guard could be reduced to ``cpm_micros IS NULL`` with
``dbt build --select fee_tax_verification_allocation`` still reporting PASS=30 ERROR=0.
The refusal is now exercised at each of its three boundaries -- one project per boundary,
so a failure names which one:

  feetax_dev_verif_cpm_negative CPM -2 500 000. A negative rate would SUBTRACT from an
                                invoice; it must refuse, not compute.
  feetax_dev_verif_cpm_overflow CPM 10 000 000 000 = exactly 1e10, the FIRST value at
                                which the macro's exact arithmetic would overflow the
                                CAST. Chosen ON the boundary, not comfortably past it.
  feetax_dev_verif_cpm_null     CPM NULL. Migration 119 requires a CPM rule to declare
                                one; a mirror that lands NULL anyway must gap, not treat
                                the absence as zero.

``__epic41_off__`` and ``feetax_dev_off`` are Story 41.3's and are NOT re-created here --
the module-OFF assertion reads the existing OFF project, and adding a second one would
only make the anti-join harder to read.

Usage (the orchestrator runs this AFTER its sibling, on the same DuckDB file, BEFORE
``dbt build``):

    uv run python dbt/seeds/feetax/seed_fee_tax_verification.py --duckdb-path /tmp/dev.duckdb

ASCII-only stdout (project rule).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Same directory: reuse Story 41.3's mirror DDL rather than copying it, so the mirror
# shape has exactly ONE definition and the two seeders cannot drift apart. Importing the
# module executes its top-level fixture construction, which is pure Python and touches no
# database.
sys.path.insert(0, str(Path(__file__).parent))
from seed_fee_tax_mirror import (  # noqa: E402
    _FEE_TAX_DDL,
    _PLAN_DDL,
    _prefs_create_ddl,
)

# The meta-ads seed loader, so the spend rows are the canonical parse shape and land in
# fact_daily_kpi exactly like a real Meta pull. EUR source currency => the EUR->EUR
# identity rate, so fact_daily_kpi.value equals the seeded spend exactly.
sys.path.insert(0, str(_REPO_ROOT / "server" / "modules" / "meta-ads" / "seeds"))
from load_meta_seed import load_duckdb as _load_meta_duckdb  # noqa: E402

# The linkedin-ads loader, used ONLY by the ambiguity fixture: it is what puts a SECOND
# spend connector on the same campaign ref, which is the whole point of that case.
sys.path.insert(0, str(_REPO_ROOT / "server" / "modules" / "linkedin-ads" / "seeds"))
from load_linkedin_seed import load_duckdb as _load_linkedin_duckdb  # noqa: E402

META_CONNECTOR = "meta-ads"

PULL_META = "pull_feetax_41_4"
PULL_IAS = "pull_feetax_41_4_ias"
PULL_LINKEDIN = "pull_feetax_41_4_li"

# Fixed, never date.today(). D1 matches Story 41.3's D1 by design: the projects are
# disjoint, so sharing a day costs nothing and makes a side-by-side read easier.
D1 = date(2026, 5, 1)
LOADED_AT = "2026-05-02T00:00:00Z"

WINDOW_FROM = date(2026, 1, 1)

TWIN_A = "feetax_dev_verif_twin_a"
TWIN_B = "feetax_dev_verif_twin_b"

# The three CPM range boundaries. One project each, so a red names the boundary.
CPM_NEGATIVE = "feetax_dev_verif_cpm_negative"
CPM_OVERFLOW = "feetax_dev_verif_cpm_overflow"
CPM_NULL = "feetax_dev_verif_cpm_null"

#: EXACTLY the macro's capacity bound, not a comfortable multiple of it: the guard reads
#: `>= 10000000000`, so this value is the first that must refuse.
CPM_OVERFLOW_MICROS = 10000000000
CPM_NEGATIVE_MICROS = -2500000

PROJECTS = [
    TWIN_A,
    TWIN_B,
    "feetax_dev_verif_halfup",
    "feetax_dev_verif_linked",
    "feetax_dev_verif_ambiguous",
    "feetax_dev_verif_no_rule",
    "feetax_dev_verif_no_base",
    "feetax_dev_verif_refused",
    "feetax_dev_verif_mixed_metric",
    CPM_NEGATIVE,
    CPM_OVERFLOW,
    CPM_NULL,
]

# ---------------------------------------------------------------------------
# Meta-ads spend. (project_id, campaign_id, spend)  -- all on D1, CAMPAIGN grain.
#
# The TWINS carry byte-for-byte identical spend: that identity is what makes T3's
# differential meaningful. Change one and you must change the other.
# ---------------------------------------------------------------------------
_META_SPEND: list[tuple[str, str, float]] = [
    (TWIN_A, "fvt_camp_1", 12345.67),
    (TWIN_B, "fvt_camp_1", 12345.67),
    # Shares its campaign ref with the IAS row -> LINKED_VIA_CAMPAIGN_REF.
    ("feetax_dev_verif_linked", "shared_camp_1", 1000.00),
    # Half of the id collision; linkedin-ads below is the other half.
    ("feetax_dev_verif_ambiguous", "dup_camp_1", 800.00),
    # The meta loader always lands impressions = 1000, which is exactly what T8 needs:
    # a project emitting BOTH `impressions` and `measured_impressions`.
    ("feetax_dev_verif_mixed_metric", "mm_camp_1", 1000.00),
]

# LinkedIn spend -- the ambiguity fixture ONLY. linkedin-ads lands `cost` at campaign_id
# grain, so the same campaign ref now exists on TWO distinct spend connectors.
_LINKEDIN_SPEND: list[tuple[str, str, float]] = [
    ("feetax_dev_verif_ambiguous", "dup_camp_1", 900.00),
]

# ---------------------------------------------------------------------------
# IAS measurement rows. (project_id, campaign_id, measured_impressions) -- all on D1.
# Only measured_impressions is populated; see the module docstring.
# ---------------------------------------------------------------------------
_IAS_ROWS: list[tuple[str, str, int]] = [
    # T1's pinned proportional split: 1 234 567 : 765 433 impressions ->
    # 3 086 417 500 : 1 913 582 500 micros, summing to EXACTLY 5 000 000 000.
    (TWIN_B, "camp_x", 1234567),
    (TWIN_B, "camp_y", 765433),
    # T2: lands on an exact half whose integer part is even.
    ("feetax_dev_verif_halfup", "camp_half", 999997),
    ("feetax_dev_verif_linked", "shared_camp_1", 500000),
    ("feetax_dev_verif_ambiguous", "dup_camp_1", 400000),
    ("feetax_dev_verif_no_rule", "camp_nr", 1000000),
    ("feetax_dev_verif_refused", "camp_r", 100000),
    ("feetax_dev_verif_mixed_metric", "mm_camp_1", 250000),
    # The three range projects each carry REAL measured impressions: without a base the
    # rule would surface as a rule_without_base reason row (VERIFICATION_BASE_MISSING)
    # and the CPM guard would never be reached at all.
    (CPM_NEGATIVE, "camp_cpm_neg", 300000),
    (CPM_OVERFLOW, "camp_cpm_ovf", 300000),
    (CPM_NULL, "camp_cpm_null", 300000),
]

# ---------------------------------------------------------------------------
# Rules. Column order mirrors migration 119's app.fee_tax_rules_dim_v exactly:
# (id, project_id, scope_kind, scope_ref, category, form, rate, amount_micros,
#  cpm_micros, currency, base_target, cascade_phase, sequence_order, effective_from,
#  effective_to, status, origin, dedup_hash, label)
#
# Rule ids are READABLE rather than ULIDs on purpose (the sibling's convention): a failing
# test that prints 'ftr_dev_verif_twin_b_cpm' is a test a human can debug.
# ---------------------------------------------------------------------------


def _ladder_rules(project: str, suffix: str) -> list[tuple]:
    """The three NON-verification rules both twins carry, IDENTICALLY.

    Written as a function precisely so the two arms cannot drift: T3 compares their
    fee_tax_ladder_daily output at tolerance exactly 0, and a hand-copied second block
    would eventually diverge by a digit and turn a real regression into a fixture bug.

    Over 12 345.67 EUR of spend they compose to:
        net_media            12 345 670 000
        platform  3 %           370 370 100
        (regulatory / WHT are REAL zeros -- no rule routes there)
        agency   10 %         1 271 604 010   on the 12 716 040 100 running subtotal
        subtotal_ht          13 987 644 110
        sales_tax 20 %        2 797 528 822
        total_ttc            16 785 172 932   <- T4's pinned figure, on BOTH twins
    """
    return [
        (f"ftr_dev_verif_{suffix}_platform", project, "project", None,
         "PLATFORM_FEE", "PERCENTAGE", "0.030000", None, None, None,
         "NET_MEDIA", 2, 1, WINDOW_FROM, None, "confirmed", "operator", None,
         "Platform fee 3% of net media"),
        (f"ftr_dev_verif_{suffix}_agency", project, "project", None,
         "AGENCY_FEE", "PERCENTAGE", "0.100000", None, None, None,
         "RUNNING_SUBTOTAL", 5, 1, WINDOW_FROM, None, "confirmed", "operator", None,
         "Agency fee 10%"),
        (f"ftr_dev_verif_{suffix}_vat", project, "project", None,
         "SALES_TAX", "PERCENTAGE", "0.200000", None, None, None,
         "RUNNING_SUBTOTAL", 6, 1, WINDOW_FROM, None, "confirmed", "operator", None,
         "VAT 20%"),
    ]


def _cpm_rule(
    project: str, suffix: str, cpm_micros: int | None, currency: str, label: str
) -> tuple:
    """A confirmed VERIFICATION / CPM / MEASURED_IMPRESSIONS rule.

    cascade_phase and sequence_order are carried because migration 119 requires them, but
    NO PHASE ROUTES A VERIFICATION RULE -- 41.3 excludes the category before condition
    evaluation, and 41.4 reads the rule from an entirely separate model. They are
    provenance here, nothing more.
    """
    return (
        f"ftr_dev_verif_{suffix}_cpm", project, "project", None,
        "VERIFICATION", "CPM", None, None, cpm_micros, currency,
        "MEASURED_IMPRESSIONS", 2, 8, WINDOW_FROM, None, "confirmed", "operator", None,
        label,
    )


_RULES: list[tuple] = (
    _ladder_rules(TWIN_A, "twin_a")
    + _ladder_rules(TWIN_B, "twin_b")
    + [
        # The ONLY difference between the twins, and the entire point of Guard 3.
        _cpm_rule(TWIN_B, "twin_b", 2500000,
                  "EUR", "IAS verification CPM 2.50 EUR -- twin B's ONLY extra rule"),
        _cpm_rule("feetax_dev_verif_halfup", "halfup", 2500500,
                  "EUR", "CPM 2.5005 EUR -- lands on an exact half, ROUND_HALF_UP"),
        _cpm_rule("feetax_dev_verif_linked", "linked", 2500000,
                  "EUR", "CPM 2.50 EUR -- vendor and DSP share an id space"),
        _cpm_rule("feetax_dev_verif_ambiguous", "ambiguous", 2500000,
                  "EUR", "CPM 2.50 EUR -- the campaign ref collides on TWO spend connectors"),
        _cpm_rule("feetax_dev_verif_no_base", "no_base", 2500000,
                  "EUR", "CPM 2.50 EUR declared, NO IAS feed connected -- must gap visibly"),
        _cpm_rule("feetax_dev_verif_refused", "refused", 2500000,
                  "USD", "CPM 2.50 USD on an EUR project -- refused, never converted"),
        _cpm_rule("feetax_dev_verif_mixed_metric", "mixed", 2500000,
                  "EUR", "CPM 2.50 EUR -- only measured_impressions is priced"),
        # THE THREE RANGE BOUNDARIES (review finding C). Each must refuse with
        # VERIFICATION_CPM_OUT_OF_RANGE and a NULL cost -- never a 0, never a number.
        _cpm_rule(CPM_NEGATIVE, "cpm_negative", CPM_NEGATIVE_MICROS,
                  "EUR", "CPM -2.50 EUR -- a negative rate must REFUSE, not subtract"),
        _cpm_rule(CPM_OVERFLOW, "cpm_overflow", CPM_OVERFLOW_MICROS,
                  "EUR", "CPM 1e10 micros -- exactly the macro's capacity bound"),
        _cpm_rule(CPM_NULL, "cpm_null", None,
                  "EUR", "CPM NULL -- an absent rate is a gap, never a zero"),
    ]
)

# No conditions and no tier bands: every rule here is UNCONDITIONED and project-scoped, so
# the condition matcher's empty-conditions fast path (unconstrained == MATCH) is the path
# these fixtures exercise. The heterogeneous-condition cases belong to Story 41.3's
# fixtures and are deliberately not duplicated.
_RULE_IDS = [r[0] for r in _RULES]

# ---------------------------------------------------------------------------
# The IAS raw table. Column list copied VERBATIM from
# server/modules/ias/connector.py::_RAW_CREATE_DDL -- this is the executable statement of
# a contract, so do not "tidy" it.
# ---------------------------------------------------------------------------
_IAS_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_ias_daily (
    date                     VARCHAR,
    campaign_id              VARCHAR,
    campaign_name            VARCHAR,
    measured_impressions     BIGINT,
    viewable_impressions     BIGINT,
    eligible_impressions     BIGINT,
    invalid_traffic_ads      BIGINT,
    brand_safety_passed_ads  BIGINT,
    brand_safety_failed_ads  BIGINT,
    report_profile           VARCHAR,
    pull_id                  VARCHAR,
    loaded_at                VARCHAR,
    project_id               VARCHAR
)
"""

_IAS_INSERT_SQL = """
INSERT INTO raw_ias_daily
    (date, campaign_id, campaign_name,
     measured_impressions, viewable_impressions, eligible_impressions,
     invalid_traffic_ads, brand_safety_passed_ads, brand_safety_failed_ads,
     report_profile, pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, ?, ?, ?)
"""


def _delete_pull_if_table_exists(con, table: str, pull_id: str) -> None:  # noqa: ANN001
    """Idempotency without the empty-``executemany`` trap.

    ``seed_fee_tax_mirror._seed_cost_rows`` documents the trap this avoids: a loader's
    ``load_duckdb`` runs its CREATE DDL and then calls ``executemany`` UNCONDITIONALLY,
    and DuckDB raises ``InvalidInputException`` on an empty parameter-set list -- so you
    cannot "prime" the table with an empty load just to make a DELETE safe. Guard the
    DELETE on the table actually existing instead.
    """
    exists = con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [table]
    ).fetchone()
    if exists:
        con.execute(f"DELETE FROM {table} WHERE pull_id = ?", [pull_id])


def _seed_ias_rows(duckdb_path: str) -> int:
    """Land the fixture's raw IAS measurement rows (hand-rolled: there is no IAS loader)."""
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    try:
        con.execute(_IAS_CREATE_DDL)
        con.execute("DELETE FROM raw_ias_daily WHERE pull_id = ?", [PULL_IAS])
        values = [
            (
                D1.isoformat(),
                campaign_id,
                campaign_id,
                measured_impressions,
                "verification_quality",
                PULL_IAS,
                LOADED_AT,
                project_id,
            )
            for project_id, campaign_id, measured_impressions in _IAS_ROWS
        ]
        con.executemany(_IAS_INSERT_SQL, values)
    finally:
        con.close()
    return len(values)


def _seed_meta_rows(duckdb_path: str) -> int:
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    try:
        _delete_pull_if_table_exists(con, "raw_meta_ads_daily", PULL_META)
    finally:
        con.close()

    rows = [
        {
            "date": D1.isoformat(),
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
            # EUR so the EUR->EUR identity rate applies and fact cost == spend exactly.
            "cost_source_currency": "EUR",
        }
        for project_id, campaign_id, spend in _META_SPEND
    ]
    return _load_meta_duckdb(rows, PULL_META, LOADED_AT, duckdb_path)


def _seed_linkedin_rows(duckdb_path: str) -> int:
    """The ambiguity fixture's SECOND spend connector.

    ``load_linkedin_seed.load_duckdb`` takes a single ``project_id`` for the whole batch
    (unlike the meta loader, which reads it per row), so this loads one project. Today
    that is exactly one project; if a second ever needs LinkedIn rows, loop here rather
    than reaching into the loader.
    """
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    try:
        _delete_pull_if_table_exists(con, "raw_linkedin_ads_daily", PULL_LINKEDIN)
    finally:
        con.close()

    loaded = 0
    for project_id, campaign_id, cost in _LINKEDIN_SPEND:
        rows = [
            {
                "date": D1.isoformat(),
                "data_level": "CAMPAIGN",
                "campaign_id": campaign_id,
                "campaign_group_id": None,
                "costInLocalCurrency": cost,
                "impressions": 500,
                "clicks": 5,
                "externalWebsiteConversions": 1,
                "leadGenerationMailContactInfoShares": 0,
                "cost_source_currency": "EUR",
            }
        ]
        loaded += _load_linkedin_duckdb(
            rows, PULL_LINKEDIN, LOADED_AT, duckdb_path, project_id=project_id
        )
    return loaded


def _seed_mirror(duckdb_path: str) -> None:
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    try:
        # Every statement is CREATE TABLE IF NOT EXISTS, so this is a no-op when the
        # sibling seeder already ran -- and it makes this script order-independent.
        con.execute(_FEE_TAX_DDL)
        con.execute(_PLAN_DDL)

        prefs_exists = con.execute(
            "SELECT COUNT(*) FROM information_schema.tables"
            " WHERE table_schema = 'mirror' AND table_name = 'project_preferences'"
        ).fetchone()[0]
        if not prefs_exists:
            print(
                "WARNING: mirror.project_preferences was absent -- creating the"
                " SHAPE-COMPLETE dev fallback (the sibling seeder's DDL, imported not"
                " copied). Run 'uv run python -m core.mirror_sync' wherever Postgres"
                " exists to get the real relation and the real preference values."
            )
            con.execute(_prefs_create_ddl())

        # Fail HERE, by name, rather than as a Binder Error deep inside somebody else's
        # model. geographic_mode / local_markets are left at their column DEFAULTs on
        # purpose: every fixture project here keeps the 'global' posture, which is what
        # keeps attr_country NULL. None of these rules is country-conditioned, so an
        # unresolved country raises no gap and the overlay composes cleanly.
        present = {
            row[0]
            for row in con.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = 'mirror' AND table_name = 'project_preferences'"
            ).fetchall()
        }
        missing = [
            c
            for c in ("project_id", "canonical_currency", "fee_tax_alignment_enabled")
            if c not in present
        ]
        if missing:
            raise SystemExit(
                "ERROR: mirror.project_preferences lacks column(s) required by the"
                f" Epic-41 models: {', '.join(missing)}. Run"
                " dbt/seeds/feetax/seed_fee_tax_mirror.py first (it self-heals the"
                " shape), or 'uv run python -m core.mirror_sync' where Postgres exists."
            )

        for project_id in PROJECTS:
            con.execute(
                "DELETE FROM mirror.project_preferences WHERE project_id = ?", [project_id]
            )
            con.execute(
                "INSERT INTO mirror.project_preferences"
                " (project_id, canonical_currency, reporting_timezone,"
                "  fee_tax_alignment_enabled)"
                " VALUES (?, 'EUR', 'Europe/Paris', TRUE)",
                [project_id],
            )

        # --- rules: delete-then-insert OUR OWN ids only -------------------------
        placeholders = ", ".join("?" for _ in _RULE_IDS)
        con.execute(f"DELETE FROM mirror.fee_tax_rules WHERE id IN ({placeholders})", _RULE_IDS)
        con.execute(
            f"DELETE FROM mirror.fee_tax_rule_conditions WHERE rule_id IN ({placeholders})",
            _RULE_IDS,
        )
        con.execute(
            f"DELETE FROM mirror.fee_tax_rule_tiers WHERE rule_id IN ({placeholders})",
            _RULE_IDS,
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

        # NO mirror.datastreams_dim rows and NO plan rows: every rule here is
        # project-scoped, so nothing in this fixture needs scope resolution. That keeps
        # this seeder's blast radius to three relations.
    finally:
        con.close()


def run(duckdb_path: str) -> None:
    n_meta = _seed_meta_rows(duckdb_path)
    n_linkedin = _seed_linkedin_rows(duckdb_path)
    n_ias = _seed_ias_rows(duckdb_path)
    _seed_mirror(duckdb_path)
    print(
        "seed_fee_tax_verification OK  "
        f"projects={len(PROJECTS)}  rules={len(_RULES)}  "
        f"meta_rows={n_meta}  linkedin_rows={n_linkedin}  ias_rows={n_ias}  "
        f"-> {duckdb_path}"
    )


def main() -> None:
    default_duckdb = os.environ.get("TOOROW_DUCKDB_PATH", "")
    parser = argparse.ArgumentParser(
        description="Seed the Story 41.4 verification-overlay fixtures into a DuckDB file"
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
