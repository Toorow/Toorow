"""Seed ``mirror.project_tax_fee_activation`` for the local DuckDB dbt build (Story 48.4).

Story 48.4 moves every Tax model's activation gate off
``mirror.project_preferences.fee_tax_alignment_enabled`` and onto
``mirror.project_tax_fee_activation``, which projects
``app.project_tax_fee_activation_v``. In production the mirror writes that relation;
locally the dbt build reads a DuckDB file that no ``mirror_sync`` ever touched, so
the relation has to be created here or every fee/tax model fails to compile.

WHY A SIBLING RATHER THAN AN EDIT. ``seed_fee_tax_mirror.py`` is byte-frozen by
``test_epic41_verification_overlay_additive.test_forbidden_files_are_unchanged_from_head``.
Story 41.4 met the same wall and answered it the same way -- a sibling seeder that
imports what it needs and edits nothing -- so this follows the precedent rather than
widening the exemption.

THE DERIVED PROJECTS DO NOT CHANGE MEANING. For every project the earlier seeders
wrote, activation is DERIVED from their flag: a fixture project that was ON stays ON
and a fixture project that was OFF stays OFF. What changes is which relation the models
believe, not which projects are enabled -- so an Epic 41 assertion that passed before
Story 48.4 passes after it for the same reason.

AND A DERIVED FIXTURE MADE THE DEFECT MIGRATION 146 EXISTS FOR INEXPRESSIBLE (repair of
the 2026-08-10 review, finding A). Migration 146:11-15 states the defect in one line:

    "a Project whose capability was disabled through the Change Set kept producing
     Tax rows"

-- i.e. the two booleans DIVERGE, `fee_tax_alignment_enabled` says ON and
`tax_fees_active` says OFF. A fixture that derives the second from the first cannot
produce that state at all:

    select pref, governed, count(*)  ->  (False, False, 4)   (True, True, 39)

so `test_epic41_module_off_zero_rows.sql`, which enumerated OFF from the PREFERENCES
column, was testing a boolean no model reads on a universe that could not contradict it.
Both halves are repaired: this seeder now lands ONE project in the divergent state, and
that test enumerates OFF from `project_tax_fee_activation` and REFUSES a fixture in
which the two columns always agree.

``GOVERNED_OFF_PROJECT`` is that project: preferences TRUE, activation FALSE, WITH REAL
SPEND and a confirmed rule. Both are load-bearing -- a project with no facts and no rule
makes "OFF => zero rows" unfalsifiable whatever the gate reads, which is the same
finding (F5) the OFF project of ``seed_fee_tax_mirror`` was repaired for.

Run it after the other fee/tax seeders:

    uv run python dbt/seeds/feetax/seed_tax_fee_activation_mirror.py --duckdb-path <dev.duckdb>
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]

# The meta-ads seed loader, so the divergent project's spend is the canonical parse shape
# and lands in fact_daily_kpi exactly like a real Meta pull -- the same route
# seed_fee_tax_verification.py takes, for the same reason. EUR source currency => the
# EUR->EUR identity rate, so fact_daily_kpi.value equals the seeded spend exactly.
sys.path.insert(0, str(_REPO_ROOT / "server" / "modules" / "meta-ads" / "seeds"))
from load_meta_seed import load_duckdb as _load_meta_duckdb  # noqa: E402

# The production column list, in the order `app.project_tax_fee_activation_v` emits it
# after migration 148. The mirror copies a view with SELECT *, so a column added there
# has to be added here too or a model referencing it compiles locally and not in cloud.
_ACTIVATION_DDL = """
CREATE TABLE IF NOT EXISTS mirror.project_tax_fee_activation (
    project_id                        VARCHAR,
    project_configuration_version_id  VARCHAR,
    capability_state                  VARCHAR,
    tax_fees_active                   BOOLEAN,
    rule_set_id                       VARCHAR,
    rule_set_version_id               VARCHAR,
    rule_set_content_hash             VARCHAR,
    rounding                          VARCHAR,
    default_money_basis               VARCHAR,
    rule_count                        BIGINT,
    reporting_currency                VARCHAR,
    money_policy_version_id           VARCHAR,
    money_policy_content_hash         VARCHAR
)
"""

#: Fixture identities. They name no real Project and no real rule set: a local
#: DuckDB file is not a tenant, and the repository rules forbid a production
#: identifier reaching the tree.
_FIXTURE_CONFIG_VERSION = "pcv_EXAMPLE_LOCAL"
_FIXTURE_RULE_SET = "grs_EXAMPLE_LOCAL"
_FIXTURE_RULE_SET_VERSION = "grsv_EXAMPLE_LOCAL"
_FIXTURE_MONEY_POLICY_VERSION = "grsv_EXAMPLE_LOCAL_MONEY"
_ZERO_HASH = "0" * 64

#: The DIVERGENT project: `fee_tax_alignment_enabled` TRUE, `tax_fees_active` FALSE.
#: This is migration 146's stated defect, made expressible. Nothing else in the fixture
#: universe is in this state, and the module-OFF test refuses a universe in which nothing
#: is.
GOVERNED_OFF_PROJECT = "feetax_dev_governed_off"

#: Its rules. Both CONFIRMED and in window, so a model that read the PREFERENCES flag
#: would emit them -- which is exactly what makes the anti-joins in
#: `test_epic41_module_off_zero_rows.sql` and
#: `test_epic41_verification_module_off_zero_rows.sql` able to fail.
_GOVERNED_OFF_RULE_ID = "ftr_dev_governed_off_vat"
#: The VERIFICATION arm. `feetax_dev_off` carries cost rows but NO measured_impressions,
#: which is why 41.4's OFF test had to admit in its own header that the overlay "would
#: emit nothing for it even if it ignored the flag". This project carries both, so that
#: admission no longer has to be made.
_GOVERNED_OFF_CPM_RULE_ID = "ftr_dev_governed_off_cpm"
_GOVERNED_OFF_CPM_MICROS = 2500000
_GOVERNED_OFF_MEASURED_IMPRESSIONS = 500000
_GOVERNED_OFF_IAS_PULL = "pull_feetax_governed_off_ias"

#: Its spend. Fixed dates, never date.today().
_GOVERNED_OFF_PULL = "pull_feetax_governed_off"
_GOVERNED_OFF_D1 = date(2026, 5, 1)
_GOVERNED_OFF_D2 = date(2026, 5, 2)
_GOVERNED_OFF_LOADED_AT = "2026-05-03T00:00:00Z"
_GOVERNED_OFF_CAMPAIGN = "fdgo_camp_1"
_GOVERNED_OFF_SPEND = 640.00
_GOVERNED_OFF_WINDOW_FROM = date(2026, 1, 1)


def _seed_governed_off_facts(duckdb_path: Path) -> int:
    """Land the divergent project's meta-ads spend."""
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(str(duckdb_path))
    try:
        exists = con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'raw_meta_ads_daily'"
        ).fetchone()
        if exists:
            con.execute(
                "DELETE FROM raw_meta_ads_daily WHERE pull_id = ?", [_GOVERNED_OFF_PULL]
            )
    finally:
        con.close()

    rows = [
        {
            "date": day.isoformat(),
            "data_level": "CAMPAIGN",
            "campaign_id": _GOVERNED_OFF_CAMPAIGN,
            "campaign_name": _GOVERNED_OFF_CAMPAIGN,
            "adset_id": None,
            "adset_name": None,
            "ad_id": None,
            "creative_id": None,
            "spend": _GOVERNED_OFF_SPEND,
            "impressions": 1000,
            "clicks": 10,
            "conversions": 1,
            "project_id": GOVERNED_OFF_PROJECT,
            "cost_source_currency": "EUR",
        }
        for day in (_GOVERNED_OFF_D1, _GOVERNED_OFF_D2)
    ]
    return _load_meta_duckdb(
        rows, _GOVERNED_OFF_PULL, _GOVERNED_OFF_LOADED_AT, str(duckdb_path)
    )


def _seed_governed_off_measurement(con) -> int:  # noqa: ANN001
    """One IAS measurement row for the divergent project.

    ``raw_ias_daily`` is created by ``seed_fee_tax_verification.py``, which runs BEFORE
    this seeder in ``seed_all_connectors._FEE_TAX_SEEDERS``. If it is absent this seeder
    was run alone; say so and land nothing rather than create a SECOND definition of a
    raw table's shape.
    """
    exists = con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'raw_ias_daily'"
    ).fetchone()
    if not exists:
        print(
            "WARNING: raw_ias_daily is absent -- run"
            " dbt/seeds/feetax/seed_fee_tax_verification.py first, or the verification"
            " overlay's OFF assertion stays as weak as it was before this seeder."
        )
        return 0
    con.execute("DELETE FROM raw_ias_daily WHERE pull_id = ?", [_GOVERNED_OFF_IAS_PULL])
    con.execute(
        "INSERT INTO raw_ias_daily"
        " (date, campaign_id, campaign_name, measured_impressions, viewable_impressions,"
        "  eligible_impressions, invalid_traffic_ads, brand_safety_passed_ads,"
        "  brand_safety_failed_ads, report_profile, pull_id, loaded_at, project_id)"
        " VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL,"
        "         'verification_quality', ?, ?, ?)",
        [
            _GOVERNED_OFF_D1.isoformat(),
            _GOVERNED_OFF_CAMPAIGN,
            _GOVERNED_OFF_CAMPAIGN,
            _GOVERNED_OFF_MEASURED_IMPRESSIONS,
            _GOVERNED_OFF_IAS_PULL,
            _GOVERNED_OFF_LOADED_AT,
            GOVERNED_OFF_PROJECT,
        ],
    )
    return 1


def _seed_governed_off_governance(con) -> None:  # noqa: ANN001
    """Preferences TRUE + one confirmed rule, for the project activation will hold OFF.

    Idempotent by delete-then-insert of its OWN keys only: it can never touch a row of
    ``seed_fee_tax_mirror`` or ``seed_fee_tax_verification``.
    """
    con.execute(
        "DELETE FROM mirror.project_preferences WHERE project_id = ?",
        [GOVERNED_OFF_PROJECT],
    )
    con.execute(
        "INSERT INTO mirror.project_preferences"
        " (project_id, canonical_currency, reporting_timezone, fee_tax_alignment_enabled)"
        " VALUES (?, 'EUR', 'Europe/Paris', TRUE)",
        [GOVERNED_OFF_PROJECT],
    )
    rule_ids = [_GOVERNED_OFF_RULE_ID, _GOVERNED_OFF_CPM_RULE_ID]
    placeholders = ", ".join("?" for _ in rule_ids)
    for relation, key in (
        ("mirror.fee_tax_rules", "id"),
        ("mirror.fee_tax_rule_conditions", "rule_id"),
        ("mirror.fee_tax_rule_tiers", "rule_id"),
    ):
        con.execute(
            f"DELETE FROM {relation} WHERE {key} IN ({placeholders})", rule_ids
        )
    con.execute(
        "INSERT INTO mirror.fee_tax_rules"
        " (id, project_id, scope_kind, scope_ref, category, form, rate,"
        "  amount_micros, cpm_micros, currency, base_target, cascade_phase,"
        "  sequence_order, effective_from, effective_to, status, origin,"
        "  dedup_hash, label, created_by, created_at, updated_at)"
        " VALUES (?, ?, 'project', NULL, 'SALES_TAX', 'PERCENTAGE', '0.200000',"
        "         NULL, NULL, NULL, 'RUNNING_SUBTOTAL', 6, 1, ?, NULL, 'confirmed',"
        "         'operator', NULL, ?, 'seed', now(), now())",
        [
            _GOVERNED_OFF_RULE_ID,
            GOVERNED_OFF_PROJECT,
            _GOVERNED_OFF_WINDOW_FROM,
            "VAT 20% -- CONFIRMED, and the capability is disabled, so it must NOT fire",
        ],
    )
    con.execute(
        "INSERT INTO mirror.fee_tax_rules"
        " (id, project_id, scope_kind, scope_ref, category, form, rate,"
        "  amount_micros, cpm_micros, currency, base_target, cascade_phase,"
        "  sequence_order, effective_from, effective_to, status, origin,"
        "  dedup_hash, label, created_by, created_at, updated_at)"
        " VALUES (?, ?, 'project', NULL, 'VERIFICATION', 'CPM', NULL,"
        "         NULL, ?, 'EUR', 'MEASURED_IMPRESSIONS', 2, 8, ?, NULL, 'confirmed',"
        "         'operator', NULL, ?, 'seed', now(), now())",
        [
            _GOVERNED_OFF_CPM_RULE_ID,
            GOVERNED_OFF_PROJECT,
            _GOVERNED_OFF_CPM_MICROS,
            _GOVERNED_OFF_WINDOW_FROM,
            "Verification CPM 2.50 EUR -- CONFIRMED, capability disabled, must NOT fire",
        ],
    )


def seed(duckdb_path: Path) -> int:
    """Create and fill the relation. Returns the number of projects projected."""

    import duckdb  # noqa: PLC0415 -- a dev-only dependency, imported where it is used

    # Outside the connection below, because the meta loader opens its own.
    facts = _seed_governed_off_facts(duckdb_path)

    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS mirror")
        present = con.execute(
            "SELECT COUNT(*) FROM information_schema.tables"
            " WHERE table_schema = 'mirror' AND table_name = 'project_preferences'"
        ).fetchone()[0]
        if not present:
            raise SystemExit(
                "ERROR: mirror.project_preferences is absent, so there is nothing to"
                " derive activation from. Run seed_fee_tax_mirror.py first."
            )
        # BEFORE the derivation, so the divergent project gets a derived row that the
        # override below can flip. Its preferences flag is TRUE, so the derivation puts
        # it in the ON set -- and the override is the only thing that takes it out.
        _seed_governed_off_governance(con)
        measured = _seed_governed_off_measurement(con)

        con.execute(_ACTIVATION_DDL)
        con.execute("DELETE FROM mirror.project_tax_fee_activation")
        # A rule_count of 0 with tax_fees_active TRUE is impossible in production (the
        # view requires a published version), so the fixture states 1: the local rules
        # come from mirror.fee_tax_rules, which the sibling seeder fills.
        con.execute(
            """
            INSERT INTO mirror.project_tax_fee_activation
            SELECT
                p.project_id,
                CASE WHEN COALESCE(CAST(p.fee_tax_alignment_enabled AS BOOLEAN), FALSE)
                     THEN ? ELSE NULL END,
                CASE WHEN COALESCE(CAST(p.fee_tax_alignment_enabled AS BOOLEAN), FALSE)
                     THEN 'ready' ELSE 'disabled' END,
                COALESCE(CAST(p.fee_tax_alignment_enabled AS BOOLEAN), FALSE),
                CASE WHEN COALESCE(CAST(p.fee_tax_alignment_enabled AS BOOLEAN), FALSE)
                     THEN ? ELSE NULL END,
                CASE WHEN COALESCE(CAST(p.fee_tax_alignment_enabled AS BOOLEAN), FALSE)
                     THEN ? ELSE NULL END,
                CASE WHEN COALESCE(CAST(p.fee_tax_alignment_enabled AS BOOLEAN), FALSE)
                     THEN ? ELSE NULL END,
                'half_up',
                'native_source',
                CASE WHEN COALESCE(CAST(p.fee_tax_alignment_enabled AS BOOLEAN), FALSE)
                     THEN 1 ELSE 0 END,
                p.canonical_currency,
                CASE WHEN COALESCE(CAST(p.fee_tax_alignment_enabled AS BOOLEAN), FALSE)
                     THEN ? ELSE NULL END,
                CASE WHEN COALESCE(CAST(p.fee_tax_alignment_enabled AS BOOLEAN), FALSE)
                     THEN ? ELSE NULL END
            FROM mirror.project_preferences p
            """,
            [
                _FIXTURE_CONFIG_VERSION,
                _FIXTURE_RULE_SET,
                _FIXTURE_RULE_SET_VERSION,
                _ZERO_HASH,
                _FIXTURE_MONEY_POLICY_VERSION,
                _ZERO_HASH,
            ],
        )
        # ------------------------------------------------------- THE DIVERGENCE ---
        # The state migration 146 was written for: the capability was disabled through
        # the Change Set, so the governed view says OFF while the legacy preferences
        # flag still says ON. `capability_state = 'disabled'` and every governed
        # attribute is NULLed, exactly as app.project_tax_fee_activation_v projects a
        # project with no published rule set.
        con.execute(
            """
            UPDATE mirror.project_tax_fee_activation
            SET capability_state                 = 'disabled',
                tax_fees_active                  = FALSE,
                project_configuration_version_id = NULL,
                rule_set_id                      = NULL,
                rule_set_version_id              = NULL,
                rule_set_content_hash            = NULL,
                rule_count                       = 0,
                money_policy_version_id          = NULL,
                money_policy_content_hash        = NULL
            WHERE project_id = ?
            """,
            [GOVERNED_OFF_PROJECT],
        )
        divergent = con.execute(
            """
            SELECT COUNT(*)
            FROM mirror.project_preferences p
            JOIN mirror.project_tax_fee_activation a ON a.project_id = p.project_id
            WHERE COALESCE(CAST(p.fee_tax_alignment_enabled AS BOOLEAN), FALSE)
              AND NOT COALESCE(CAST(a.tax_fees_active AS BOOLEAN), FALSE)
            """
        ).fetchone()[0]
        if divergent < 1:
            raise SystemExit(
                "ERROR: no project ended in the state migration 146 exists for"
                " (preferences ON, activation OFF). Without it every activation-gate"
                " assertion is vacuous. Check that"
                f" {GOVERNED_OFF_PROJECT} reached mirror.project_preferences."
            )

        count = con.execute(
            "SELECT COUNT(*) FROM mirror.project_tax_fee_activation"
        ).fetchone()[0]
        enabled = con.execute(
            "SELECT COUNT(*) FROM mirror.project_tax_fee_activation WHERE tax_fees_active"
        ).fetchone()[0]
        print(
            f"  divergent (pref ON / governed OFF): {divergent}"
            f"  facts landed for {GOVERNED_OFF_PROJECT}: {facts}"
            f"  ias rows: {measured}"
        )
        print(
            f"mirror.project_tax_fee_activation: {count} project(s) projected,"
            f" {enabled} active"
        )
        return int(count)
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duckdb-path", required=True, type=Path)
    args = parser.parse_args()
    if not args.duckdb_path.exists():
        print(f"ERROR: no DuckDB file at {args.duckdb_path}", file=sys.stderr)
        raise SystemExit(1)
    seed(args.duckdb_path)


if __name__ == "__main__":  # pragma: no cover
    main()
