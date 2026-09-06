"""Story 41.1 -- activation flag + FeeTaxRule model, store and governed mirror.

Offline (no Postgres): the pure validation matrix, money exactness, the closed
vocabularies parsed back out of migration 119, the no-hardcode guards, and the mirror
registration / guard contracts.

Pg-gated (skipped unless TEST_POSTGRES_DSN points at a database where migration 119 is
applied): off-by-default, the geographic-CHECK non-regression, the audited write path,
DB defence-in-depth, the three flattening views, list semantics and FK cascade.

Registration assertions live HERE and not in test_mirror_sync.py: that file is shared,
and additive discipline says a story adds its own file rather than editing a common one.
"""

from __future__ import annotations

import ast
import os
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from core import fee_tax_rules as ftr

# Keep the background workers off for anything this file imports transitively
# (pattern: test_dataset_access_grants.py). core.fee_tax_rules itself starts nothing.
os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MIGRATION = _REPO_ROOT / "infra" / "nango" / "migrations" / "119_fee_tax_alignment.sql"
_MODULE = _REPO_ROOT / "server" / "core" / "fee_tax_rules.py"
_MIRROR_SYNC = _REPO_ROOT / "server" / "core" / "mirror_sync.py"
_SOURCES_YML = _REPO_ROOT / "dbt" / "models" / "staging" / "sources_mirror.yml"

TEST_ORG_ID = "org_test_fixture"


# ---------------------------------------------------------------------------
# Postgres availability (pattern: test_dataset_access_grants.py).
# ---------------------------------------------------------------------------


def _pg_state() -> tuple[bool, bool]:
    """(postgres reachable, migration 119 applied)."""
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False, False
    try:
        import psycopg

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('app.fee_tax_rules') IS NOT NULL")
                applied = bool(cur.fetchone()[0])
        return True, applied
    except Exception:
        return False, False


_PG_REACHABLE, _PG_HAS_119 = _pg_state()

pg_available = pytest.mark.skipif(not _PG_REACHABLE, reason="platform Postgres not reachable")
fee_tax_schema = pytest.mark.skipif(
    not (_PG_REACHABLE and _PG_HAS_119),
    reason="migration 119 is not applied on TEST_POSTGRES_DSN",
)


# ---------------------------------------------------------------------------
# Payload builders.
# ---------------------------------------------------------------------------


def _percentage(**over: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "scope_kind": "project",
        "category": "AGENCY_FEE",
        "form": "PERCENTAGE",
        "rate": "0.030000",
        "base_target": "NET_MEDIA",
        "cascade_phase": 4,
        "effective_from": "2026-01-01",
        "origin": "operator",
    }
    payload.update(over)
    return payload


def _flat(**over: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "scope_kind": "project",
        "category": "PAYMENT_FEE",
        "form": "FLAT",
        "amount_micros": 1_000_000,
        "currency": "EUR",
        "base_target": "RUNNING_SUBTOTAL",
        "cascade_phase": 6,
        "effective_from": "2026-01-01",
        "origin": "operator",
    }
    payload.update(over)
    return payload


def _cpm(**over: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "scope_kind": "project",
        "category": "VERIFICATION",
        "form": "CPM",
        "cpm_micros": 250_000,
        "currency": "EUR",
        "base_target": "MEASURED_IMPRESSIONS",
        "cascade_phase": 2,
        "effective_from": "2026-01-01",
        "origin": "operator",
    }
    payload.update(over)
    return payload


def _tiered(**over: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "scope_kind": "project",
        "category": "PLATFORM_FEE",
        "form": "SPEND_TIERS",
        "tiers": {
            "bands": [
                {"threshold_micros": 0, "rate": "0.050000"},
                {"threshold_micros": 1_000_000, "rate": "0.030000"},
            ]
        },
        "base_target": "NET_MEDIA",
        "cascade_phase": 1,
        "effective_from": "2026-01-01",
        "origin": "operator",
    }
    payload.update(over)
    return payload


def _gross_up(**over: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "scope_kind": "project",
        "category": "WHT_GROSS_UP",
        "form": "GROSS_UP",
        "rate": "0.100000",
        "base_target": "RUNNING_SUBTOTAL",
        "cascade_phase": 5,
        "effective_from": "2026-01-01",
        "origin": "operator",
    }
    payload.update(over)
    return payload


def _per_transaction(**over: Any) -> dict[str, Any]:
    """Story 41.5's first-class per-transaction gateway fee (0.25 EUR per transaction).

    FLAT-shaped payload (amount + currency), PAYMENT_FEE-only, GROSS_REVENUE-only.
    """
    payload: dict[str, Any] = {
        "scope_kind": "project",
        "category": "PAYMENT_FEE",
        "form": "PER_TRANSACTION",
        "amount_micros": 250_000,
        "currency": "EUR",
        "base_target": "GROSS_REVENUE",
        "cascade_phase": 6,
        "effective_from": "2026-01-01",
        "origin": "operator",
    }
    payload.update(over)
    return payload


# ===========================================================================
# Offline -- pure validation (AC3, AC6)
# ===========================================================================


def test_1_minimal_rule_normalises_defaults():
    out = ftr.validate_rule(_percentage())
    assert out["status"] == "proposed"
    assert out["conditions"] == {}
    assert out["source_type_scope"] == []
    assert out["label"] == ""
    assert out["scope_ref"] is None
    assert out["sequence_order"] == 0
    assert out["dedup_hash"] is None
    assert out["effective_to"] is None
    assert out["effective_from"] == date(2026, 1, 1)
    assert out["rate"] == Decimal("0.03")


def test_2_percentage_and_gross_up_require_a_rate():
    for builder in (_percentage, _gross_up):
        payload = builder()
        payload.pop("rate")
        with pytest.raises(ftr.FeeTaxRuleValidationError):
            ftr.validate_rule(payload)


def test_3_gross_up_rate_of_one_is_refused():
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(_gross_up(rate="1.000000"))
    assert ftr.validate_rule(_gross_up(rate="0.900000"))["rate"] == Decimal("0.9")


def test_4_flat_requires_amount_and_currency():
    payload = _flat()
    payload.pop("amount_micros")
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(payload)
    payload = _flat()
    payload.pop("currency")
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(payload)


def test_5_cpm_requires_cpm_micros_and_currency():
    payload = _cpm()
    payload.pop("cpm_micros")
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(payload)
    payload = _cpm()
    payload.pop("currency")
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(payload)


def test_6a_spend_tiers_shape_is_enforced():
    payload = _tiered()
    payload.pop("tiers")
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(payload)
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(_tiered(tiers={"bands": []}))
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(
            _tiered(
                tiers={
                    "bands": [
                        {"threshold_micros": 1_000_000, "rate": "0.05"},
                        {"threshold_micros": 1_000_000, "rate": "0.03"},
                    ]
                }
            )
        )
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(_tiered(tiers={"bands": [{"threshold_micros": 0}]}))


def test_6b_tier_mode_defaults_to_cliff_and_is_a_closed_vocabulary():
    assert ftr.validate_rule(_tiered())["tiers"]["mode"] == "cliff"
    marginal = _tiered()
    marginal["tiers"]["mode"] = "marginal"
    assert ftr.validate_rule(marginal)["tiers"]["mode"] == "marginal"
    bad = _tiered()
    bad["tiers"]["mode"] = "progressive"
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(bad)


def test_6c_bare_array_tiers_are_rejected_not_normalised():
    """Ruled policy: ONE shape. A bare array of bands is refused outright."""
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(_tiered(tiers=[{"threshold_micros": 0, "rate": "0.05"}]))


def test_7_scope_ref_is_null_iff_scope_is_project():
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(_percentage(scope_ref="dstr_x"))
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(_percentage(scope_kind="datastream"))
    out = ftr.validate_rule(_percentage(scope_kind="datastream", scope_ref="dstr_x"))
    assert out["scope_ref"] == "dstr_x"


# --- Test 8: vocabulary parity between the Python frozensets and migration 119 ---


def _sql_text() -> str:
    return _MIGRATION.read_text(encoding="utf-8")


def _check_vocabulary(column: str) -> set[str]:
    match = re.search(
        rf"CHECK\s*\(\s*{re.escape(column)}\s+IN\s*\(([^)]*)\)",
        _sql_text(),
        re.DOTALL,
    )
    assert match is not None, f"no CHECK ... IN (...) for {column} in migration 119"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def test_8_vocabularies_mirror_the_migration_checks():
    assert _check_vocabulary("scope_kind") == set(ftr.SCOPE_KINDS)
    assert _check_vocabulary("category") == set(ftr.CATEGORIES)
    assert _check_vocabulary("form") == set(ftr.FORMS)
    assert _check_vocabulary("base_target") == set(ftr.BASE_TARGETS)
    assert _check_vocabulary("status") == set(ftr.STATUSES)
    assert _check_vocabulary("origin") == set(ftr.ORIGINS)
    assert _check_vocabulary("source_type") == set(ftr.SOURCE_TYPES)


def test_8b_tier_modes_mirror_the_migration_check():
    match = re.search(r"COALESCE\(tiers->>'mode',\s*'cliff'\)\s+IN\s*\(([^)]*)\)", _sql_text())
    assert match is not None
    assert set(re.findall(r"'([^']+)'", match.group(1))) == set(ftr.TIER_MODES)
    assert ftr.TIER_MODE_DEFAULT in ftr.TIER_MODES


# ---------------------------------------------------------------------------
# Story 41.5 + adversarial-review finding F2 -- routed-pair coherence (AC13).
#
# EVERY REJECTION BELOW IS TWINNED WITH AN ACCEPTANCE. A one-sided assertion is the
# usual way a guard rots: it can be satisfied by an implementation that refuses too
# much, which would look green while quietly breaking legitimate rules. The twins pin
# "this pair exists, in exactly these places".
#
# What the guard prevents: the cascade's phase CTEs route a FIXED set of forms per
# category and fall through to `ELSE 0` for anything else, WITHOUT refusing the rule and
# WITHOUT gapping the row. (AGENCY_FEE, GROSS_UP) therefore understated an invoice by
# the whole withholding component behind a green is_ladder_complete.
# ---------------------------------------------------------------------------


def _pair_sets_from_migration() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Parse migration 119's two routed-pair CHECKs back into Python sets.

    A CHECK round-trip, like test_8: it only goes green when the SQL and the module data
    agree, so the two cannot drift. Deliberately parses the SHIPPED SQL rather than
    trusting a hand-copied list.
    """
    text = _sql_text()
    start_a = text.index("CONSTRAINT ck_fee_tax_rules_category_form")
    start_b = text.index("CONSTRAINT ck_fee_tax_rules_form_base_target")
    body_a = text[start_a:start_b]
    body_b = text[start_b:]

    category_forms: dict[str, set[str]] = {}
    for match in re.finditer(
        r"\(category = '([A-Z_]+)'\s+AND form (?:IN \(([^)]*)\)|= '([A-Z_]+)')\)",
        body_a,
    ):
        values = match.group(2) or match.group(3) or ""
        category_forms[match.group(1)] = set(re.findall(r"'([A-Z_]+)'", values)) or {
            match.group(3)
        }

    form_targets: dict[str, set[str]] = {}
    for match in re.finditer(
        r"\(form (?:IN \(([^)]*)\)|= '([A-Z_]+)')"
        r"\s+AND base_target (?:IN \(([^)]*)\)|= '([A-Z_]+)')\)",
        body_b,
        re.DOTALL,
    ):
        forms = set(re.findall(r"'([A-Z_]+)'", match.group(1) or "")) or {match.group(2)}
        targets = set(re.findall(r"'([A-Z_]+)'", match.group(3) or "")) or {match.group(4)}
        for form in forms:
            form_targets.setdefault(form, set()).update(targets)

    return category_forms, form_targets


def test_8c_the_routed_pair_tables_mirror_migration_119():
    """The Python pair tables and the two CHECKs are ONE vocabulary in two homes."""
    category_forms, form_targets = _pair_sets_from_migration()
    assert category_forms, "ck_fee_tax_rules_category_form did not parse"
    assert form_targets, "ck_fee_tax_rules_form_base_target did not parse"
    assert category_forms == {key: set(value) for key, value in ftr._CATEGORY_FORMS.items()}
    assert form_targets == {key: set(value) for key, value in ftr._FORM_BASE_TARGETS.items()}
    # Every category and every form is covered: a vocabulary value with no routing row
    # would be silently unusable (or, worse, unconstrained).
    assert set(ftr._CATEGORY_FORMS) == set(ftr.CATEGORIES)
    assert set(ftr._FORM_BASE_TARGETS) == set(ftr.FORMS)
    # And the pair tables never admit a form or a base_target outside the vocabularies.
    for forms in ftr._CATEGORY_FORMS.values():
        assert forms <= ftr.FORMS
    for targets in ftr._FORM_BASE_TARGETS.values():
        assert targets <= ftr.BASE_TARGETS


def test_8d_agency_fee_gross_up_is_refused_but_wht_gross_up_is_accepted():
    """F2's headline case (case 1), with its twin (case 2).

    Refusing GROSS_UP outright would pass case 1 and break withholding entirely, which
    is exactly what the twin exists to catch.
    """
    with pytest.raises(ftr.FeeTaxRuleValidationError) as refused:
        ftr.validate_rule(_gross_up(category="AGENCY_FEE"))
    message = str(refused.value)
    assert "AGENCY_FEE" in message and "GROSS_UP" in message
    assert "WHT_GROSS_UP" in message, "the refusal must say where GROSS_UP IS legal"

    accepted = ftr.validate_rule(_gross_up())
    assert accepted["category"] == "WHT_GROSS_UP"
    assert accepted["form"] == "GROSS_UP"


def test_8e_cpm_is_verification_only_and_targets_measured_impressions_only():
    """Case 3 (two rejections) and case 4 (the acceptance twin, 41.4's shape)."""
    # (PLATFORM_FEE, CPM) -- routed by no phase.
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(_cpm(category="PLATFORM_FEE"))
    # CPM over NET_MEDIA -- F2's second instance: the verification cost vanishes.
    with pytest.raises(ftr.FeeTaxRuleValidationError) as refused:
        ftr.validate_rule(_cpm(base_target="NET_MEDIA"))
    assert "MEASURED_IMPRESSIONS" in str(refused.value)

    accepted = ftr.validate_rule(_cpm())
    assert (accepted["category"], accepted["form"], accepted["base_target"]) == (
        "VERIFICATION",
        "CPM",
        "MEASURED_IMPRESSIONS",
    )


def test_8f_per_transaction_is_payment_fee_only_over_gross_revenue():
    """Cases 5, 6 and 7 -- the new form's scope AND its FLAT-shaped payload."""
    # Case 5: the original PER_TRANSACTION guard, now expressed inside CHECK A.
    with pytest.raises(ftr.FeeTaxRuleValidationError) as refused:
        ftr.validate_rule(_per_transaction(category="PLATFORM_FEE", base_target="NET_MEDIA"))
    assert "PLATFORM_FEE" in str(refused.value)
    assert "PER_TRANSACTION" in str(refused.value)
    # Right category, wrong base: refused by the (form, base_target) half.
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(_per_transaction(base_target="RUNNING_SUBTOTAL"))

    # Case 6: the acceptance twin.
    accepted = ftr.validate_rule(_per_transaction())
    assert (accepted["category"], accepted["form"], accepted["base_target"]) == (
        "PAYMENT_FEE",
        "PER_TRANSACTION",
        "GROSS_REVENUE",
    )
    assert accepted["amount_micros"] == 250_000
    assert accepted["currency"] == "EUR"
    assert accepted["rate"] is None

    # Case 7: FLAT's coherence rule applies -- an absolute amount needs both fields.
    without_amount = _per_transaction()
    without_amount.pop("amount_micros")
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(without_amount)
    without_currency = _per_transaction()
    without_currency.pop("currency")
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(without_currency)
    # And it must NOT be swept into the PERCENTAGE/GROSS_UP currency-refusal branch.
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(_per_transaction(rate="0.014000"))


def test_8g_spend_tiers_is_about_the_base_not_about_banning_tiers():
    """Case 8. CHECK B refuses a tiered rule on a REVENUE base, nothing more."""
    with pytest.raises(ftr.FeeTaxRuleValidationError) as refused:
        ftr.validate_rule(_tiered(category="SALES_TAX", base_target="GROSS_REVENUE"))
    assert "SPEND_TIERS" in str(refused.value)
    accepted = ftr.validate_rule(_tiered(category="SALES_TAX", base_target="NET_MEDIA"))
    assert accepted["form"] == "SPEND_TIERS"


def test_8h_a_percentage_may_not_take_a_percentage_of_an_impression_count():
    """The last derived line of CHECK B, with its twin.

    PERCENTAGE / FLAT over MEASURED_IMPRESSIONS has no evaluator: a percentage of a
    COUNT is not money. The twin proves PERCENTAGE still reaches the revenue bases,
    which is what Story 41.5's SALES_TAX rules need.
    """
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(_percentage(base_target="MEASURED_IMPRESSIONS"))
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(_flat(base_target="MEASURED_IMPRESSIONS"))
    for base_target in ("NET_MEDIA", "RUNNING_SUBTOTAL", "NET_REVENUE", "GROSS_REVENUE"):
        assert (
            ftr.validate_rule(_percentage(category="SALES_TAX", base_target=base_target))[
                "base_target"
            ]
            == base_target
        )


def test_9_cascade_phase_and_sequence_order_bounds():
    for phase in (0, 7):
        with pytest.raises(ftr.FeeTaxRuleValidationError):
            ftr.validate_rule(_percentage(cascade_phase=phase))
    for phase in range(1, 7):
        assert ftr.validate_rule(_percentage(cascade_phase=phase))["cascade_phase"] == phase
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(_percentage(sequence_order=-1))


def test_9b_two_rules_may_share_a_phase_and_sequence_slot():
    """Phase-entry semantics: sequence_order is a display key, not a unique slot."""
    first = ftr.validate_rule(_percentage(cascade_phase=4, sequence_order=0))
    second = ftr.validate_rule(_percentage(cascade_phase=4, sequence_order=0, rate="0.010000"))
    assert (first["cascade_phase"], first["sequence_order"]) == (
        second["cascade_phase"],
        second["sequence_order"],
    )


def test_10_conditions_recognise_market_and_refuse_the_unknown():
    assert ftr.validate_rule(_percentage(conditions={"country": ["FR"]}))["conditions"] == {
        "country": ["FR"]
    }
    assert ftr.validate_rule(_percentage(conditions={"market": ["fr-metro"]}))["conditions"] == {
        "market": ["fr-metro"]
    }
    both = ftr.validate_rule(
        _percentage(conditions={"country": ["FR"], "market": ["fr-metro"]})
    )["conditions"]
    assert both == {"country": ["FR"], "market": ["fr-metro"]}
    assert ftr.validate_rule(_percentage(conditions={}))["conditions"] == {}
    for bad in ({"region": ["emea"]}, {"country": "FR"}, {"country": []}):
        with pytest.raises(ftr.FeeTaxRuleValidationError):
            ftr.validate_rule(_percentage(conditions=bad))


def test_10b_market_is_a_declarable_condition_key():
    """Without `market`, the whole Epic 37 bridge would be inert: a multi-country
    market resolves a market_id but no single country."""
    assert "market" in ftr.CONDITION_KEYS
    assert ftr.CONDITION_KEYS == {"country", "market", "placement_type", "connector", "tax_code"}


def test_10c_source_type_is_not_a_declarable_condition_key():
    """The flattening view emits condition_key='source_type' from source_type_scope;
    a declarable key of the same name would collide with it."""
    assert "source_type" not in ftr.CONDITION_KEYS


def test_11_source_type_scope_is_a_closed_vocabulary():
    assert ftr.validate_rule(_percentage(source_type_scope=["PAID_MEDIA"]))[
        "source_type_scope"
    ] == ["PAID_MEDIA"]
    assert ftr.validate_rule(_percentage(source_type_scope=[]))["source_type_scope"] == []
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(_percentage(source_type_scope=["PAID"]))


def test_12_effective_window():
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(_percentage(effective_from="2026-02-01", effective_to="2026-01-01"))
    assert ftr.validate_rule(_percentage(effective_to=None))["effective_to"] is None
    assert ftr.validate_rule(_percentage(effective_to="2026-12-31"))["effective_to"] == date(
        2026, 12, 31
    )


def test_12b_dedup_hash_is_a_slot_the_validator_never_fills():
    assert ftr.validate_rule(_percentage())["dedup_hash"] is None
    assert ftr.validate_rule(_percentage(origin="auto_country"))["dedup_hash"] is None
    for bad in ("", "   "):
        with pytest.raises(ftr.FeeTaxRuleValidationError):
            ftr.validate_rule(_percentage(dedup_hash=bad))
    assert (
        ftr.validate_rule(_percentage(dedup_hash="auto:tax:2026"))["dedup_hash"] == "auto:tax:2026"
    )


# ===========================================================================
# Offline -- exactness (AC6, C.6, E41-NFR02)
# ===========================================================================


def test_13_micros_must_be_integers_never_rounded():
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(_flat(amount_micros=1.5))
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(_flat(amount_micros=True))
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(_cpm(cpm_micros=1.5))
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(_tiered(tiers={"bands": [{"threshold_micros": 1.5, "rate": "0.05"}]}))


def test_14_rates_are_exact_decimals_and_a_float_is_refused():
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(_percentage(rate=0.03))
    assert ftr.validate_rule(_percentage(rate="0.030000"))["rate"] == Decimal("0.030000")
    exact = ftr.validate_rule(_percentage(rate=Decimal("0.03")))["rate"]
    assert exact == Decimal("0.03")
    assert str(exact) == "0.030000"
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(_percentage(rate="0.0300001"))


def test_15_normalisation_is_idempotent():
    for builder in (_percentage, _flat, _cpm, _tiered, _gross_up):
        once = ftr.validate_rule(builder())
        assert ftr.validate_rule(dict(once)) == once


def test_15b_unknown_rule_fields_are_refused():
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(_percentage(project_id="proj_x"))


# ===========================================================================
# Offline -- no hardcode + AD-2 (AC9)
# ===========================================================================


def _module_constants() -> list[ast.Constant]:
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"), filename=str(_MODULE))
    return [node for node in ast.walk(tree) if isinstance(node, ast.Constant)]


def test_16a_no_iso_country_literal_in_the_module():
    """The existing conformance test scans a fixed list of six geography modules and
    does NOT cover this one -- hence a story-local guard."""
    offenders = [
        node.value
        for node in _module_constants()
        if isinstance(node.value, str) and re.fullmatch(r"[A-Z]{2}", node.value)
    ]
    assert not offenders, f"an ISO-3166 alpha-2 literal ships in fee_tax_rules.py: {offenders}"


def test_16b_no_rate_literal_in_the_module():
    """No float anywhere, and no Decimal built from a decimal string literal: a shipped
    rate would be one client's tax policy applied to every other client."""
    source = _MODULE.read_text(encoding="utf-8")
    floats = [node.value for node in _module_constants() if isinstance(node.value, float)]
    assert not floats, f"a float literal ships in fee_tax_rules.py: {floats}"
    assert not re.search(r"Decimal\(\s*[\"']\d*\.\d+[\"']\s*\)", source)
    assert not re.search(r"\b(VAT|DST)\b\s*[=:]", source)


def test_16c_no_tax_or_fee_rate_ships_anywhere_in_core_or_in_a_connector():
    """The capability criterion, widened from one module to the whole class.

    `tax-fees.md` says "rates are hardcoded in a connector or in core code" --
    a connector, not this module. test_16b guards `fee_tax_rules.py` alone, so a
    rate shipped in `server/modules/<any>/` or in a neighbouring core module
    would have satisfied it while breaking the criterion.

    A shipped rate is one client's tax policy applied to every other client, and
    it cannot be corrected per Project because nobody knows it is there.
    """
    import pathlib as _pathlib
    import re as _re

    roots = (
        _pathlib.Path(__file__).resolve().parents[2] / "modules",
        _pathlib.Path(__file__).resolve().parents[2] / "core",
    )
    categories = _re.compile(
        r"\b(VAT|DST|SALES_TAX|GST|AGENCY_FEE|REGULATORY_TAX|PLATFORM_FEE)\b"
    )
    # A decimal literal next to a tax category on the same line. Narrow on
    # purpose: a bare `0.15` is a legitimate threshold in a hundred places, and
    # a scan that flagged those would be turned off within a week.
    literal = _re.compile(r"\b0\.\d+\b|Decimal\(\s*[\"\']\d*\.\d+")

    shipped = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if "test" in path.name:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), start=1):
                code = line.split("#", 1)[0]
                if categories.search(code) and literal.search(code):
                    shipped.append(f"{path.name}:{number}: {code.strip()[:80]}")
    assert shipped == [], shipped


def test_17_the_migration_seeds_nothing():
    assert "INSERT INTO" not in _sql_text().upper()


def test_18_no_connector_or_provider_name_in_the_module():
    """AD-2: `connector` may appear only as a condition-key name, never as a value."""
    source = _MODULE.read_text(encoding="utf-8").lower()
    for provider in ("meta-ads", "google-ads", "tiktok", "linkedin", "facebook", "dv360"):
        assert provider not in source
    assert "connector" in source  # the condition key itself


# ===========================================================================
# Offline -- mirror registration + guards (AC8; D7, D9)
# ===========================================================================

_MIRROR_ENTRIES = (
    "fee_tax_rules",
    "fee_tax_rule_conditions",
    "fee_tax_rule_tiers",
    "datastream_source_types",
    "datastreams_dim",
    "datastream_country_binding_dim",
)


def test_19_all_six_entries_are_registered():
    from core import mirror_sync

    for name in _MIRROR_ENTRIES:
        assert name in mirror_sync._DEFAULT_TABLES, f"_DEFAULT_TABLES is missing {name}"
        assert name in mirror_sync._ALLOWED_TABLES, f"_ALLOWED_TABLES is missing {name}"


def test_20_every_mirror_table_is_declared_as_a_dbt_source():
    import yaml
    from core import mirror_sync

    document = yaml.safe_load(_SOURCES_YML.read_text(encoding="utf-8"))
    declared = {table["name"] for table in document["sources"][0]["tables"]}
    missing = sorted(set(mirror_sync._DEFAULT_TABLES) - declared)
    assert not missing, f"mirror tables with no dbt source declaration: {missing}"


def test_20b_no_jsonb_or_array_reaches_the_mirror():
    from core import mirror_sync

    sql = mirror_sync._FEE_TAX_RULES_DIM_SQL
    assert "conditions" not in sql
    assert "tiers" not in sql
    assert "source_type_scope" not in sql
    # The scalars-only projection is a Postgres view, so the column list cannot drift.
    assert "fee_tax_rules_dim_v" in sql
    assert "config" not in mirror_sync._DATASTREAMS_DIM_SQL


def test_20b2_the_flattening_views_carry_the_frozen_column_contract():
    """41.2 and 41.3 code against these names sight-unseen."""
    sql = _sql_text()
    conditions_view = sql.split("CREATE OR REPLACE VIEW app.fee_tax_rule_conditions_v")[1]
    conditions_view = conditions_view.split(";")[0]
    for column in ("rule_id", "condition_key", "condition_value"):
        assert column in conditions_view
    # source_type_scope rides the conditions view as condition_key = 'source_type'.
    assert "'source_type'" in conditions_view
    assert "unnest(r.source_type_scope)" in conditions_view

    tiers_view = sql.split("CREATE OR REPLACE VIEW app.fee_tax_rule_tiers_v")[1].split(";")[0]
    for column in ("rule_id", "tier_index", "threshold_micros", "rate", "mode"):
        assert column in tiers_view

    dim_view = sql.split("CREATE OR REPLACE VIEW app.datastreams_dim_v")[1].split(";")[0]
    for column in ("project_id", "datastream_id", "connector", "data_role", "source_kind"):
        assert column in dim_view
    assert "module_name AS connector" in dim_view


class _FakeCursor:
    def __init__(self, exists: bool) -> None:
        self._exists = exists
        self.executed: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> tuple[bool]:
        return (self._exists,)


def test_20c_absent_market_bindings_yields_a_shape_stable_empty_projection():
    from core import mirror_sync

    empty = mirror_sync._datastream_country_binding_sql(_FakeCursor(False))
    real = mirror_sync._datastream_country_binding_sql(_FakeCursor(True))
    assert empty is mirror_sync._BINDING_DIM_EMPTY_SQL
    assert real is mirror_sync._BINDING_DIM_SQL
    assert "WHERE FALSE" in empty
    assert "app.market_bindings" in real

    def _aliases(sql: str) -> list[str]:
        return re.findall(r"AS (\w+)", sql) or []

    # Both branches project the same four columns, in the same order.
    assert mirror_sync._BINDING_DIM_COLUMNS == ("project_id", "connector", "datastream_id",
                                                "market_id")
    assert _aliases(empty) == list(mirror_sync._BINDING_DIM_COLUMNS)
    # The real branch takes project_id and market_id unaliased from the JOIN.
    assert _aliases(real) == ["connector", "datastream_id"]
    for column in mirror_sync._BINDING_DIM_COLUMNS:
        assert column in real


def test_20d_the_dispatch_refactor_did_not_restyle_the_ad3_column_list():
    from core import mirror_sync

    assert mirror_sync._CURATED_SQL["connection_ref_dim"] is mirror_sync._CONNECTION_REF_DIM_SQL
    assert "nango_connection_id" not in mirror_sync._CONNECTION_REF_DIM_SQL


def _docstring_ids(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            out.add(id(first.value))
    return out


def test_20e_the_mirror_never_opts_into_rls_enforcement():
    """app.datastreams is under FORCE ROW LEVEL SECURITY with a default-open policy
    (migration 059). Installing an access context here would make datastreams_dim
    mirror ZERO rows -- RLS filters, it does not raise -- and 41.2's source_type
    resolution would fail with no error anywhere.

    Asserted over the CODE, not the text: the module docstring is REQUIRED to explain
    the invariant, so a naive substring scan would forbid documenting it.
    """
    tree = ast.parse(_MIRROR_SYNC.read_text(encoding="utf-8"), filename=str(_MIRROR_SYNC))
    docstrings = _docstring_ids(tree)

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and "set_local_access_context"
        in (getattr(node.func, "attr", "") or "", getattr(node.func, "id", "") or "")
    ]
    assert not calls, "mirror_sync must never install an access context"

    imported = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    ]
    assert "set_local_access_context" not in imported

    live_strings = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]
    assert not [text for text in live_strings if "enforce_epic36" in text]


def test_20c2_the_binding_dim_join_is_scoped_to_one_project():
    """Review finding F5. `binding_id` is free TEXT with no foreign key, so joining on
    it alone would let a binding written under project A that names project B's
    datastream emit (project_B, market_of_A) into a client-facing warehouse dim."""
    from core import mirror_sync

    sql = " ".join(mirror_sync._BINDING_DIM_SQL.split())
    assert "d.id = mb.binding_id" in sql
    assert "d.project_id = mb.project_id" in sql


# --- F2: guarded skip, so deploying before migration 119 cannot blind the mirror ---


def test_20g_every_migration_119_relation_is_guarded():
    """Migrations are applied BY HAND and 119 is human-gated, so this module can be
    deployed before 119 exists. Each entry that reads a 119 relation must be able to
    skip; otherwise the first UndefinedTable aborts the WHOLE sync, the pre-existing
    mirrors stop updating, and the health endpoint reports mirror_sync: null -- which
    is indistinguishable from "never synced", so the lag alert cannot fire."""
    from core import mirror_sync

    guarded = set(mirror_sync._GUARDED_RELATIONS)
    # A SUPERSET, not an equality. Written as `==` on 2026-07-27, this line went red
    # on 2026-08-10 because five legitimate relations had been guarded since --
    # `master_data_nodes_dim`, `master_data_aliases_dim`,
    # `master_data_node_attributes_dim`, `managed_feed_grain`, `project_money_policy`.
    # Every one of them was a neighbouring story doing exactly what this test asks
    # for, and the test failed them for it. An equality here holds the invariant
    # "nobody guards anything new", which nobody wants; the invariant worth holding
    # is "these six stay guarded", plus the two derived checks below that no unguarded
    # entry slips in and that no guard points at a relation no migration creates.
    must_stay_guarded = {
        "fee_tax_rules",
        "fee_tax_rule_conditions",
        "fee_tax_rule_tiers",
        "datastream_source_types",
        "datastreams_dim",
        # Story 48.4: the one activation authority. Guarded for the same reason as
        # the rest -- a deployment that has not applied 146/148 must SKIP the entry,
        # not abort the whole nightly sync on it.
        "project_tax_fee_activation",
    }
    assert must_stay_guarded <= guarded, must_stay_guarded - guarded
    # Every guarded relation is created by SOME migration in the tree.
    #
    # This used to read migration 119 alone, which was right while 119 was the only
    # migration that created a guarded relation and became wrong the moment another
    # one did: the assertion would have failed for a correctly guarded 146 relation
    # and passed for a 119 relation guarded against a table nobody creates. The
    # invariant worth holding is "guarded against something that exists somewhere",
    # so the search covers the catalogue.
    catalogue = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(_MIGRATION.parent.glob("*.sql"))
    )
    for relation in mirror_sync._GUARDED_RELATIONS.values():
        bare = relation.split(".", 1)[1]
        assert f"app.{bare}" in catalogue, (
            f"{relation} is guarded but no migration creates it"
        )
    # The binding dim is guarded the OTHER way (shape-stable empty), because 41.3
    # source()s it while migration 104 is still unapplied.
    assert "datastream_country_binding_dim" not in guarded
    assert "datastream_country_binding_dim" in mirror_sync._CURATED_SQL_FACTORY
    # Nothing may be registered without one of the two guards or an unconditional
    # relation that already exists in the tree.
    unguarded_new = set(_MIRROR_ENTRIES) - guarded - set(mirror_sync._CURATED_SQL_FACTORY)
    assert not unguarded_new, f"new mirror entries with no absence guard: {unguarded_new}"


def _fake_pg(monkeypatch, *, present: set[str], captured: list[str]):
    """Patch core.db.get_connection so _fetch_from_postgres sees a schema in which only
    *present* relations exist. Rows are empty; we only care about skip vs fetch."""
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    class _Cur:
        def __init__(self) -> None:
            self.description = [("col_a",)]
            self._last: Any = None

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def execute(self, sql: str, params: Any = None) -> None:
            if "to_regclass" in sql:
                self._last = (params[0] in present,) if params else (False,)
                return
            captured.append(" ".join(sql.split()))
            self._last = None

        def fetchone(self):
            return self._last

        def fetchall(self):
            return []

    @contextmanager
    def _conn():
        cur = _Cur()
        conn = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur
        yield conn

    monkeypatch.setattr("core.db.get_connection", _conn)


def test_20h_an_absent_119_relation_skips_instead_of_raising(monkeypatch):
    from core import mirror_sync

    captured: list[str] = []
    _fake_pg(monkeypatch, present=set(), captured=captured)
    assert mirror_sync._fetch_from_postgres("fee_tax_rules") is None
    assert captured == []  # it never even issued the SELECT

    captured.clear()
    _fake_pg(monkeypatch, present={"app.fee_tax_rules_dim_v"}, captured=captured)
    # Three values now: the declared Postgres types travel with the rows so the
    # mirror stops re-inferring them from the data.
    cols, rows, types = mirror_sync._fetch_from_postgres("fee_tax_rules")
    assert rows == []
    assert captured == ["SELECT * FROM app.fee_tax_rules_dim_v"]


def test_20i_a_skip_does_not_abort_the_sync_and_is_reported(monkeypatch):
    """The three non-negotiables: the run completes, it still records synced_at so
    monitoring never goes dark, and the skip is visible in the result."""
    from core import mirror_sync

    captured: list[str] = []
    _fake_pg(monkeypatch, present=set(), captured=captured)
    monkeypatch.delenv("TOOROW_DUCKDB_PATH", raising=False)
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")

    result = mirror_sync.sync_tables(
        ["project_preferences", "fee_tax_rules", "datastreams_dim", "context_events"]
    )

    assert "error" not in result
    assert "synced_at" in result  # the lag alert can still fire
    assert result["skipped"] == ["fee_tax_rules", "datastreams_dim"]
    # The tables AFTER the skipped ones still synced -- that is the whole point.
    assert set(result["synced"]) == {"project_preferences", "context_events"}


def test_20j_skipped_is_always_present_even_when_empty(monkeypatch):
    from core import mirror_sync

    captured: list[str] = []
    _fake_pg(monkeypatch, present=set(), captured=captured)
    monkeypatch.delenv("TOOROW_DUCKDB_PATH", raising=False)
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    result = mirror_sync.sync_tables(["project_preferences"])
    assert result["skipped"] == []


def test_20f_curated_entries_cover_the_clean_names():
    from core import mirror_sync

    for name in ("fee_tax_rules", "fee_tax_rule_conditions", "fee_tax_rule_tiers",
                 "datastreams_dim"):
        assert name in mirror_sync._CURATED_SQL
    assert "datastream_country_binding_dim" in mirror_sync._CURATED_SQL_FACTORY
    # Story 48.4 turned datastream_source_types from a real table into a CURATED
    # projection. It used to ride the generic SELECT * branch on
    # app.datastream_source_types -- a table with no production writer, so the
    # mirror carried an empty relation and every source-type-scoped rule matched
    # everything. It now reads app.datastream_source_types_v: the latest OBSERVED
    # evidence per Datastream, which is a deliberate projection rather than
    # "whatever the table happens to hold", exactly like connection_ref_dim.
    assert mirror_sync._CURATED_SQL["datastream_source_types"].endswith(
        "app.datastream_source_types_v"
    )
    assert "datastream_source_types" not in mirror_sync._CURATED_SQL_FACTORY
    # The one activation authority (Story 48.4). Curated AND guarded: a deployment
    # that has not applied migration 146/148 skips the entry instead of aborting the
    # whole nightly sync on the first fee/tax relation.
    assert mirror_sync._CURATED_SQL["project_tax_fee_activation"].endswith(
        "app.project_tax_fee_activation_v"
    )
    assert mirror_sync._GUARDED_RELATIONS["project_tax_fee_activation"] == (
        "app.project_tax_fee_activation_v"
    )


# ===========================================================================
# Offline -- the AD-27 replay contract (review finding F1)
#
# These need no database: prepare_operation is pure, so the exact invariant that
# was broken can be proven here rather than only in a pg-gated test.
# ===========================================================================


def _spec_pair(**over: Any) -> tuple[Any, Any]:
    normalised = ftr.validate_rule(_percentage(**over))
    kwargs = dict(
        project_id="proj_replay",
        org_id="org_replay",
        normalised=normalised,
        created_by="tester@example.com",
        idempotency_key="fixed-key",
    )
    return ftr.create_rule_spec(**kwargs), ftr.create_rule_spec(**kwargs)


def test_f1_two_identical_creates_produce_the_same_request_hash():
    """The F1 regression, proven without Postgres.

    `_existing_operation` matches on (org, command_type, idempotency-key hash) only,
    then `_replayed_result` compares request hashes. When `resource_path` carried a
    freshly minted rule id, the second call matched the first operation, saw a
    different request hash, and raised OperationIdempotencyConflict -- an untyped
    RuntimeError, i.e. a 500 -- instead of replaying.
    """
    from core.operations import prepare_operation

    first, second = _spec_pair()
    assert first == second
    prepared_first = prepare_operation(first)
    prepared_second = prepare_operation(second)
    assert prepared_first.idempotency_key_hash == prepared_second.idempotency_key_hash
    assert prepared_first.request_hash == prepared_second.request_hash


def test_f1b_the_resource_path_carries_no_minted_id():
    first, second = _spec_pair()
    assert first.resource_path == second.resource_path
    assert not any("ftr_" in node for node in first.resource_path)
    assert first.resource_path[-1].startswith("fee_tax_rule:")


def test_f1c_the_same_key_with_different_content_still_conflicts():
    """Replay must not become "swallow": re-using a key for a DIFFERENT rule has to
    keep producing a different request hash so execute_operation refuses it."""
    from core.operations import prepare_operation

    same_key, _ = _spec_pair()
    other_normalised = ftr.validate_rule(_percentage(rate="0.040000"))
    other = ftr.create_rule_spec(
        project_id="proj_replay",
        org_id="org_replay",
        normalised=other_normalised,
        created_by="tester@example.com",
        idempotency_key="fixed-key",
    )
    assert prepare_operation(same_key).idempotency_key_hash == (
        prepare_operation(other).idempotency_key_hash
    )
    assert prepare_operation(same_key).request_hash != prepare_operation(other).request_hash


# --- The audit payload must never trip operations._is_secret_key -------------
#
# This is the bug the F1 fix exposed. The field was called `dedup_key`;
# operations._is_secret_key flags ANY identifier matching _SECRET_KEY unless it ends
# in _id / _ref / _hash, so `dedup_key` was secret-like and create_rule raised
# OperationValidationError for every rule carrying one -- i.e. every auto-populated
# rule Story 41.2 emits, 100% of the time. Renaming to `dedup_hash` clears the
# exemption AND is more accurate: the value is a content hash, not a key.


def test_f9_a_rule_carrying_a_dedup_hash_can_actually_be_audited():
    """The direct regression: this exact call raised OperationValidationError."""
    from core.operations import prepare_operation

    normalised = ftr.validate_rule(
        _percentage(dedup_hash="ftk_0f1e2d3c", origin="auto_country")
    )
    spec = ftr.create_rule_spec(
        project_id="proj_x",
        org_id="org_x",
        normalised=normalised,
        created_by="tester@example.com",
    )
    assert spec.request_payload["dedup_hash"] == "ftk_0f1e2d3c"
    prepare_operation(spec)  # raised "request_payload contains a secret-like key"


def test_f9b_the_field_name_itself_is_the_fix():
    """Pin the reason, so nobody renames it back to something ending in _key."""
    from core.operations import _is_secret_key

    assert _is_secret_key("dedup_key") is True
    assert _is_secret_key("dedup_hash") is False
    assert "dedup_hash" in ftr._RULE_FIELDS
    assert "dedup_key" not in ftr._RULE_FIELDS


def _string_key_dict_literals(path: Path) -> list[tuple[int, str]]:
    """Every string key of every dict literal in *path*, with its line number."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key in node.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                out.append((key.lineno, key.value))
    return out


def test_f9c_no_field_anywhere_in_the_module_is_secret_like():
    """Source-level sweep, so a FUTURE field cannot reintroduce this.

    Scans every dict literal in fee_tax_rules.py rather than only the payloads I can
    build offline -- update_rule / set_rule_status / set_fee_tax_alignment construct
    their specs inline, and a new command added later would be covered too. It
    over-approximates on purpose: a secret-like key in ANY dict in this module is
    worth stopping to think about.
    """
    from core.operations import _is_secret_key

    offenders = [
        f"{_MODULE.name}:{lineno} {key!r}"
        for lineno, key in _string_key_dict_literals(_MODULE)
        if _is_secret_key(key)
    ]
    assert not offenders, (
        "these keys would be rejected by operations._validate_json if they ever "
        "reached a request_payload / result / outbox_payload: " + ", ".join(offenders)
    )


def test_f9d_every_spec_payload_clears_the_json_validator():
    """Belt and braces over the one spec builder that is callable offline: run the
    real validator over request_payload, host_context, versions and
    provider_references for a rule that carries every risky field at once."""
    from core.operations import _validate_json

    normalised = ftr.validate_rule(
        _tiered(
            dedup_hash="ftk_deadbeef",
            origin="auto_country",
            conditions={"country": ["FR"], "market": ["fr-metro"]},
            source_type_scope=["PAID_MEDIA"],
            effective_from=date(2026, 3, 1),
            effective_to=date(2026, 12, 31),
        )
    )
    spec = ftr.create_rule_spec(
        project_id="proj_x",
        org_id="org_x",
        normalised=normalised,
        created_by="tester@example.com",
    )
    _validate_json(spec.request_payload, name="request_payload")
    _validate_json(spec.provider_references, name="provider_references")


def test_f1d_the_spec_survives_the_validate_json_boundary():
    """A Decimal or a date in request_payload would raise OperationValidationError."""
    from core.operations import prepare_operation

    normalised = ftr.validate_rule(
        _tiered(effective_from=date(2026, 3, 1), conditions={"country": ["FR"]})
    )
    spec = ftr.create_rule_spec(
        project_id="proj_x",
        org_id="org_x",
        normalised=normalised,
        created_by="tester@example.com",
    )
    prepare_operation(spec)  # raises if any value is not a JSON primitive


# ===========================================================================
# Offline -- SQL type bounds map to 422, not a psycopg DataError (500)
# ===========================================================================


def test_f7_out_of_range_money_and_rates_are_typed_422s():
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(_flat(amount_micros=2**63))
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(_cpm(cpm_micros=-(2**63) - 1))
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        # NUMERIC(12,6) holds 6 digits before the point.
        ftr.validate_rule(_percentage(rate="1000000"))
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(_percentage(sequence_order=2**31))
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.validate_rule(
            _tiered(tiers={"bands": [{"threshold_micros": 2**63, "rate": "0.05"}]})
        )
    # The boundaries themselves are accepted.
    assert ftr.validate_rule(_flat(amount_micros=2**63 - 1))["amount_micros"] == 2**63 - 1
    assert ftr.validate_rule(_percentage(rate="999999"))["rate"] == Decimal("999999")


# ===========================================================================
# Pg-gated helpers
# ===========================================================================


def _new_id(prefix: str) -> str:
    from ulid import ULID

    return f"{prefix}_{ULID()}"


def _seed_project(conn) -> str:
    project_id = _new_id("proj")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
            "VALUES (%s, %s, %s, %s, 'test-41-1')",
            (project_id, TEST_ORG_ID, project_id, project_id.replace("_", "-").lower()),
        )
    return project_id


def _seed_datastream(conn, project_id: str) -> str:
    """A managed_feed datastream: module_name NULL is legal for that source_kind, and an
    explicit source_kind keeps the 076 external-dispatch trigger happy whether or not
    migration 103 is applied on this database."""
    datastream_id = _new_id("dstr")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.datastreams "
            "(id, project_id, org_id, name, module_name, source_kind) "
            "VALUES (%s, %s, %s, %s, NULL, 'managed_feed')",
            (datastream_id, project_id, TEST_ORG_ID, datastream_id),
        )
    return datastream_id


_RAW_INSERT = """
    INSERT INTO app.fee_tax_rules
        (id, project_id, scope_kind, scope_ref, category, form, rate, amount_micros,
         cpm_micros, tiers, currency, base_target, cascade_phase, sequence_order,
         conditions, source_type_scope, effective_from, effective_to, status, origin,
         dedup_hash, label, created_by)
    VALUES (%(id)s, %(project_id)s, %(scope_kind)s, %(scope_ref)s, %(category)s,
            %(form)s, %(rate)s, %(amount_micros)s, %(cpm_micros)s, %(tiers)s::jsonb,
            %(currency)s, %(base_target)s, %(cascade_phase)s, %(sequence_order)s,
            %(conditions)s::jsonb, %(source_type_scope)s::text[], %(effective_from)s,
            %(effective_to)s, %(status)s, %(origin)s, %(dedup_hash)s, %(label)s,
            %(created_by)s)
"""


def _raw_params(project_id: str, **over: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "id": _new_id("ftr"),
        "project_id": project_id,
        "scope_kind": "project",
        "scope_ref": None,
        "category": "AGENCY_FEE",
        "form": "PERCENTAGE",
        "rate": Decimal("0.030000"),
        "amount_micros": None,
        "cpm_micros": None,
        "tiers": None,
        "currency": None,
        "base_target": "NET_MEDIA",
        "cascade_phase": 4,
        "sequence_order": 0,
        "conditions": "{}",
        "source_type_scope": [],
        "effective_from": date(2026, 1, 1),
        "effective_to": None,
        "status": "proposed",
        "origin": "operator",
        "dedup_hash": None,
        "label": "",
        "created_by": "test-41-1",
    }
    params.update(over)
    return params


def _raw_insert(conn, project_id: str, **over: Any) -> str:
    params = _raw_params(project_id, **over)
    with conn.cursor() as cur:
        cur.execute(_RAW_INSERT, params)
    return params["id"]


def _expect_db_rejection(conn, project_id: str, **over: Any) -> None:
    import psycopg

    with pytest.raises(psycopg.Error):
        with conn.transaction():
            _raw_insert(conn, project_id, **over)


def _count(conn, sql: str, params: tuple) -> int:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return int(cur.fetchone()[0])


# ===========================================================================
# Pg-gated -- activation (AC1, AC2, AC5)
# ===========================================================================


@fee_tax_schema
def test_21_off_by_default(live_postgres):
    conn = live_postgres
    project_id = _seed_project(conn)
    assert (
        _count(
            conn,
            "SELECT count(*) FROM app.project_preferences WHERE fee_tax_alignment_enabled",
            (),
        )
        == 0
    )
    assert ftr.is_fee_tax_alignment_active(project_id, conn) is False
    assert ftr.is_fee_tax_alignment_active("does-not-exist", conn) is False


def _geo_columns(conn) -> list[str]:
    """The geographic posture columns actually present.

    Migration 102 (local_markets) is not applied everywhere -- the applied set is not
    linear -- so the non-regression assertion adapts instead of pretending.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'app' AND table_name = 'project_preferences'"
        )
        present = {row[0] for row in cur.fetchall()}
    columns = ["geographic_mode", "local_market_country_codes"]
    if "local_markets" in present:
        columns.append("local_markets")
    return columns


# ---------------------------------------------------------------------------
# Publishing one ladder, the only way a Project gets Tax rules now.
#
# The five tests below used to drive `fee_tax_rules.set_fee_tax_alignment` and
# `app.fee_tax_rules` directly. Story 48.4 retired both: activation is DERIVED
# from `app.project_tax_fee_activation_v`, and the flattening views read the
# PUBLISHED ladder. The properties they asserted are unchanged and still worth
# guarding; only the authority moved, so they are re-pointed rather than deleted.
# ---------------------------------------------------------------------------


def _publish_ladder(conn, project_id, org_id, rules, *, payload=None):
    """Vocabulary -> money policy -> ladder. Every pin is a real stored version."""
    from core.currency_vocabulary import import_currency_vocabulary
    from core.governance_rule_sets import draft_version, ensure_rule_set, publish_version
    from core.money_policy import PROFILE_MONEY
    from core.tax_fee_rule_set import FAMILY_TAX_FEE, LADDER_NAME, PROFILE_TAX_FEE

    vocabulary = import_currency_vocabulary(
        conn, actor="test", source_version="ISO-4217:2026-01", effective_date="2026-01-01"
    )
    money_head = ensure_rule_set(
        conn,
        org_id=org_id,
        project_id=project_id,
        family="money_policy",
        name="project_money_policy",
        label="Money policy",
        actor="test",
    )
    money_draft = draft_version(
        conn,
        project_id=project_id,
        rule_set_id=money_head["id"],
        profile=PROFILE_MONEY,
        label="v1",
        payload={
            "reporting_currency": "EUR",
            "rounding": "half_even",
            "max_staleness_days": 7,
            "rate_source_priority": ["ecb"],
        },
        requires=[
            {
                "kind": "currency_vocabulary_version",
                "object_id": str(vocabulary["vocabulary_key"]),
                "version_id": str(vocabulary["id"]),
            }
        ],
        actor="test",
    )
    money = publish_version(
        conn,
        project_id=project_id,
        rule_set_id=money_head["id"],
        version_id=money_draft["id"],
        actor="test",
    )

    ladder_head = ensure_rule_set(
        conn,
        org_id=org_id,
        project_id=project_id,
        family=FAMILY_TAX_FEE,
        name=LADDER_NAME,
        label="Tax and fee ladder",
        actor="test",
    )
    ladder_draft = draft_version(
        conn,
        project_id=project_id,
        rule_set_id=ladder_head["id"],
        profile=PROFILE_TAX_FEE,
        label="v1",
        payload=payload or {},
        ordered_rules=rules,
        requires=[
            {
                "kind": "money_policy_version",
                "object_id": money_head["id"],
                "version_id": money["id"],
            }
        ],
        actor="test",
    )
    return publish_version(
        conn,
        project_id=project_id,
        rule_set_id=ladder_head["id"],
        version_id=ladder_draft["id"],
        actor="test",
    )


def _ladder_agency_rule(**overrides):
    rule = {
        "rule_key": "agency_fee",
        "label": "Agency fee",
        "scope_kind": "project",
        "scope_ref": None,
        "category": "AGENCY_FEE",
        "form": "PERCENTAGE",
        "rate": "0.15",
        "base_target": "NET_MEDIA",
        "cascade_phase": 5,
        "sequence_order": 10,
        "effective_from": "2026-01-01",
        "authority_kind": "agency_contract",
        "source_evidence": {
            "issuer": "Agency",
            "reference": "Master services agreement, schedule B",
            "reference_version": "v3",
            "document_ref": "MSA-2026-B",
            "published_on": "2026-01-01",
        },
    }
    rule.update(overrides)
    return rule


def _project_org(conn, project_id):
    with conn.cursor() as cur:
        cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
        return cur.fetchone()[0]


def test_22_activation_is_derived_and_fails_closed_when_nothing_is_published(live_postgres):
    """Activation is READ, not stored. A Project with no projection reads OFF.

    The preference boolean this used to write was a SECOND activation authority
    beside `app.project_capabilities`, and the two diverged: a capability
    disabled through a Change Set left the preference TRUE while every dbt Tax
    model still read the preference. There is now one answer, and it is derived.
    """
    conn = live_postgres
    project_id = _seed_project(conn)

    # No capability pin, no published ladder: fail closed rather than default on.
    assert ftr.is_fee_tax_alignment_active(project_id, conn) is False

    # Publishing a ladder satisfies ONE of the view's three conditions. It must
    # not be enough on its own -- the capability pin is the other authority, and
    # a ladder that switched Tax on by existing would be the divergence again.
    _publish_ladder(conn, project_id, _project_org(conn, project_id), [_ladder_agency_rule()])
    assert ftr.is_fee_tax_alignment_active(project_id, conn) is False


def test_23_the_retired_activation_door_refuses_and_names_its_replacement(live_postgres):
    """A retired door that raises a bare error strands its caller.

    The old writer could silently wipe a client's declared markets by
    copy-pasting an upsert; it cannot do that any more because it writes
    nothing at all. What matters now is that a caller who finds it is told
    WHERE activation moved to, in the exception itself.
    """
    conn = live_postgres
    project_id = _seed_project(conn)
    with pytest.raises(ftr.FeeTaxActivationRetired) as excinfo:
        ftr.set_fee_tax_alignment(
            conn, project_id=project_id, enabled=True, actor="tester@example.com"
        )
    message = str(excinfo.value)
    assert "Project Change Set" in message
    assert "change-sets" in message


def test_23b_nothing_gates_on_the_retired_activation_flag_any_more():
    """The divergence, asserted where it would actually come back.

    41.8 lets a governed LLM flip activation and the flip changes invoice
    totals, so a second boolean meaning "Tax is on" is not a harmless duplicate.
    The column itself still exists in `app.project_preferences` and is still
    mirrored -- an applied migration is never re-edited, and a conformance test
    requires the mirrored column to stay documented. A dormant column is not an
    authority; a READER is. So this asserts the reader is gone, everywhere, and
    fails the moment one comes back.
    """
    import inspect
    import pathlib as _pathlib

    from core import fee_tax_rules

    source = inspect.getsource(fee_tax_rules.is_fee_tax_alignment_active)
    body = source.split('"""')[-1]
    assert "project_preferences" not in body, (
        "activation is read from the projection view, not from the preference"
    )

    marts = _pathlib.Path(__file__).resolve().parents[3] / "dbt" / "models" / "marts"
    gating = []
    for path in sorted(marts.glob("fee_tax*.sql")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            # Comments explaining WHY the gate moved are the point; a live
            # reference is the regression.
            code = line.split("--", 1)[0]
            if "fee_tax_alignment_enabled" in code:
                gating.append(f"{path.name}:{number}")
    assert gating == [], gating


@fee_tax_schema
def test_24_create_rule_writes_one_rule_one_operation_one_audit_one_outbox(live_postgres):
    conn = live_postgres
    project_id = _seed_project(conn)
    actor = f"tester+{_new_id('a')}@example.com"
    row = ftr.create_rule(conn, project_id=project_id, rule=_percentage(), created_by=actor)

    assert row["id"].startswith("ftr_")
    assert row["inserted"] is True
    assert row["status"] == "proposed"
    assert _count(
        conn, "SELECT count(*) FROM app.fee_tax_rules WHERE project_id = %s", (project_id,)
    ) == 1
    assert _count(
        conn,
        "SELECT count(*) FROM app.operations WHERE command_type = %s AND actor = %s",
        (ftr.ACTION_RULE_CREATED, actor),
    ) == 1
    assert _count(
        conn,
        "SELECT count(*) FROM app.audit_log WHERE action = %s AND identity = %s",
        (ftr.ACTION_RULE_CREATED, actor),
    ) == 1
    assert _count(
        conn,
        "SELECT count(*) FROM app.operation_outbox o JOIN app.operations op "
        "ON op.id = o.operation_id WHERE op.actor = %s AND o.event_type = %s",
        (actor, ftr.ACTION_RULE_CREATED),
    ) == 1


@fee_tax_schema
def test_24b_decimal_date_jsonb_and_array_survive_the_operation_boundary(live_postgres):
    """_validate_json rejects Decimal/date, so the store stringifies at the boundary --
    a transport detail that must not be a data loss."""
    conn = live_postgres
    project_id = _seed_project(conn)
    payload = _tiered(
        effective_from=date(2026, 3, 1),
        effective_to=date(2026, 12, 31),
        source_type_scope=["PAID_MEDIA", "LEAD_GEN_MEDIA"],
        conditions={"country": ["FR"], "market": ["fr-metro"]},
    )
    payload["tiers"]["mode"] = "marginal"
    payload["tiers"]["bands"][0]["rate"] = Decimal("0.050000")
    ftr.create_rule(conn, project_id=project_id, rule=payload, created_by="tester@example.com")

    listed = ftr.list_rules(project_id, conn)
    assert len(listed) == 1
    stored = listed[0]
    assert stored["effective_from"] == date(2026, 3, 1)
    assert stored["effective_to"] == date(2026, 12, 31)
    assert stored["tiers"]["mode"] == "marginal"
    assert stored["tiers"]["bands"][0]["rate"] == Decimal("0.05")
    assert isinstance(stored["tiers"]["bands"][0]["rate"], Decimal)
    assert stored["tiers"]["bands"][0]["threshold_micros"] == 0
    assert stored["source_type_scope"] == ["PAID_MEDIA", "LEAD_GEN_MEDIA"]
    assert stored["conditions"] == {"country": ["FR"], "market": ["fr-metro"]}


@fee_tax_schema
def test_25_identical_create_replays_instead_of_duplicating(live_postgres):
    conn = live_postgres
    project_id = _seed_project(conn)
    actor = f"tester+{_new_id('a')}@example.com"
    key = f"fixed-key-{_new_id('k')}"
    first = ftr.create_rule(
        conn, project_id=project_id, rule=_percentage(), created_by=actor, idempotency_key=key
    )
    second = ftr.create_rule(
        conn, project_id=project_id, rule=_percentage(), created_by=actor, idempotency_key=key
    )
    assert first["id"] == second["id"]
    assert first["inserted"] is True
    assert second["inserted"] is False  # a replay created nothing
    assert _count(
        conn, "SELECT count(*) FROM app.fee_tax_rules WHERE project_id = %s", (project_id,)
    ) == 1
    assert _count(
        conn,
        "SELECT count(*) FROM app.audit_log WHERE action = %s AND identity = %s",
        (ftr.ACTION_RULE_CREATED, actor),
    ) == 1


@fee_tax_schema
def test_25b_dedup_hash_is_unique_per_project_and_only_when_present(live_postgres):
    import psycopg

    conn = live_postgres
    project_a = _seed_project(conn)
    project_b = _seed_project(conn)
    _raw_insert(conn, project_a, dedup_hash="auto:tax:2026")
    with pytest.raises(psycopg.Error):
        with conn.transaction():
            _raw_insert(conn, project_a, dedup_hash="auto:tax:2026")
    # A different project may carry the same key.
    _raw_insert(conn, project_b, dedup_hash="auto:tax:2026")
    # The index is PARTIAL: many NULL keys in one project are fine.
    _raw_insert(conn, project_a, dedup_hash=None)
    _raw_insert(conn, project_a, dedup_hash=None)
    assert _count(
        conn, "SELECT count(*) FROM app.fee_tax_rules WHERE project_id = %s", (project_a,)
    ) == 3


@fee_tax_schema
def test_25c_create_rule_reports_inserted_false_on_a_dedup_collision(live_postgres):
    """41.2's auto-population counters read this boolean to say "N created, M already
    present" honestly."""
    conn = live_postgres
    project_id = _seed_project(conn)
    first = ftr.create_rule(
        conn,
        project_id=project_id,
        rule=_percentage(dedup_hash="auto:tax:2026", origin="auto_country"),
        created_by="tester@example.com",
    )
    assert first["inserted"] is True
    second = ftr.create_rule(
        conn,
        project_id=project_id,
        # Same dedup_hash, different content -> a different idempotency key, so the
        # operation does NOT replay; the DB partial unique index is what dedups.
        rule=_percentage(dedup_hash="auto:tax:2026", origin="auto_country", rate="0.040000"),
        created_by="tester@example.com",
    )
    assert second["inserted"] is False
    assert second["id"] == first["id"]
    assert _count(
        conn, "SELECT count(*) FROM app.fee_tax_rules WHERE project_id = %s", (project_id,)
    ) == 1


@fee_tax_schema
def test_26_the_schema_refuses_an_incoherent_rule_even_without_the_validator(live_postgres):
    conn = live_postgres
    project_id = _seed_project(conn)
    # FLAT without a currency.
    _expect_db_rejection(
        conn, project_id, form="FLAT", rate=None, amount_micros=1_000_000, currency=None
    )
    # scope_kind='project' with a scope_ref.
    _expect_db_rejection(conn, project_id, scope_ref="dstr_x")
    # cascade_phase out of range.
    _expect_db_rejection(conn, project_id, cascade_phase=7)
    # GROSS_UP with rate = 1 (net/(1-rate) divides by zero).
    _expect_db_rejection(
        conn, project_id, form="GROSS_UP", category="WHT_GROSS_UP", rate=Decimal("1.000000")
    )
    # SPEND_TIERS with a BARE ARRAY (no bands key).
    _expect_db_rejection(
        conn,
        project_id,
        form="SPEND_TIERS",
        rate=None,
        tiers='[{"threshold_micros": 0, "rate": "0.05"}]',
    )
    # SPEND_TIERS with an unknown mode.
    _expect_db_rejection(
        conn,
        project_id,
        form="SPEND_TIERS",
        rate=None,
        tiers='{"mode": "progressive", "bands": [{"threshold_micros": 0, "rate": "0.05"}]}',
    )
    # An id that is not a prefixed ULID.
    _expect_db_rejection(conn, project_id, id="not-a-ulid")


@fee_tax_schema
def test_26d_the_schema_refuses_an_unrouted_pair_and_admits_every_routed_one(live_postgres):
    """Story 41.5 / F2 at the CHECK layer -- the backstop under validate_rule.

    Both halves, and both directions. The acceptance sweep is what stops the guard from
    being satisfied by a constraint that simply refuses too much: it inserts EVERY pair
    the module data declares routable and asserts the DB takes all of them.
    """
    conn = live_postgres
    project_id = _seed_project(conn)

    # (AGENCY_FEE, GROSS_UP) -- F2's headline case. A valid rate, so only CHECK A can
    # be what rejects it.
    _expect_db_rejection(
        conn,
        project_id,
        category="AGENCY_FEE",
        form="GROSS_UP",
        rate=Decimal("0.150000"),
        base_target="RUNNING_SUBTOTAL",
    )
    # CPM over NET_MEDIA -- F2's second case.
    _expect_db_rejection(
        conn,
        project_id,
        category="VERIFICATION",
        form="CPM",
        rate=None,
        cpm_micros=250_000,
        currency="EUR",
        base_target="NET_MEDIA",
    )
    # PER_TRANSACTION outside PAYMENT_FEE.
    _expect_db_rejection(
        conn,
        project_id,
        category="PLATFORM_FEE",
        form="PER_TRANSACTION",
        rate=None,
        amount_micros=250_000,
        currency="EUR",
        base_target="GROSS_REVENUE",
    )
    # A tiered rule on a revenue base.
    _expect_db_rejection(
        conn,
        project_id,
        category="SALES_TAX",
        form="SPEND_TIERS",
        rate=None,
        base_target="GROSS_REVENUE",
        tiers='{"bands": [{"threshold_micros": 0, "rate": "0.050000"}]}',
    )

    # --- the acceptance twins: every routed pair the module data declares ---
    payload_by_form: dict[str, dict[str, Any]] = {
        "PERCENTAGE": {"rate": Decimal("0.030000")},
        "GROSS_UP": {"rate": Decimal("0.100000")},
        "FLAT": {"rate": None, "amount_micros": 1_000_000, "currency": "EUR"},
        "PER_TRANSACTION": {"rate": None, "amount_micros": 250_000, "currency": "EUR"},
        "CPM": {"rate": None, "cpm_micros": 250_000, "currency": "EUR"},
        "SPEND_TIERS": {
            "rate": None,
            "tiers": '{"bands": [{"threshold_micros": 0, "rate": "0.050000"}]}',
        },
    }
    accepted = 0
    for category, forms in ftr._CATEGORY_FORMS.items():
        for form in sorted(forms):
            for base_target in sorted(ftr._FORM_BASE_TARGETS[form]):
                _raw_insert(
                    conn,
                    project_id,
                    category=category,
                    form=form,
                    base_target=base_target,
                    **payload_by_form[form],
                )
                accepted += 1
    assert accepted == _count(
        conn, "SELECT count(*) FROM app.fee_tax_rules WHERE project_id = %s", (project_id,)
    )


def test_26b_the_flattening_views_are_relational_and_treat_absence_as_all(live_postgres):
    """Conditions flatten to rows, and no condition means "applies to everything".

    Re-pointed at the PUBLISHED ladder: Story 48.4 redefined both views to read
    `app.tax_fee_published_ladder_v`, so a row written straight into
    `app.fee_tax_rules` no longer reaches them -- which is the point, since that
    store is no longer the authority.
    """
    conn = live_postgres
    project_id = _seed_project(conn)
    org_id = _project_org(conn, project_id)

    rich = _ladder_agency_rule(
        rule_key="dst_fr",
        label="Digital services tax pass-through",
        category="REGULATORY_TAX",
        rate="0.03",
        cascade_phase=3,
        authority_kind="statutory_reference",
        source_evidence={
            "issuer": "European Commission",
            "reference": "VAT Directive, standard rate table",
            "reference_version": "2026-01",
            "authoritative_url": "https://example.com/vat-rates",
            "published_on": "2026-01-01",
        },
        conditions={"country": ["FR", "MC"]},
        jurisdiction={
            "kind": "country",
            "id": "ctry_EXAMPLE",
            "hierarchy_version_id": "mdv_EXAMPLE",
            "label": "France",
        },
        rest_of_world_posture="exclude",
        unknown_posture="exclude",
        source_type_scope=["PAID_MEDIA"],
    )
    version = _publish_ladder(conn, project_id, org_id, [rich, _ladder_agency_rule()])

    rich_id = f"{version['id']}:dst_fr"
    plain_id = f"{version['id']}:agency_fee"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT condition_key, condition_value FROM app.fee_tax_rule_conditions_v "
            "WHERE rule_id = %s ORDER BY condition_key, condition_value",
            (rich_id,),
        )
        assert cur.fetchall() == [
            ("country", "FR"),
            ("country", "MC"),
            ("source_type", "PAID_MEDIA"),
        ]

        # Absence is "applies to everything", never a phantom NULL row.
        cur.execute(
            "SELECT count(*) FROM app.fee_tax_rule_conditions_v WHERE rule_id = %s",
            (plain_id,),
        )
        assert cur.fetchone()[0] == 0


@fee_tax_schema
def test_26c_the_scalars_only_view_exposes_neither_jsonb_nor_array(live_postgres):
    conn = live_postgres
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'app' AND table_name = 'fee_tax_rules_dim_v'"
        )
        columns = {row[0] for row in cur.fetchall()}
    assert "conditions" not in columns
    assert "tiers" not in columns
    assert "source_type_scope" not in columns
    assert {"id", "project_id", "rate", "status", "dedup_hash"} <= columns


@fee_tax_schema
def test_27_the_status_machine(live_postgres):
    conn = live_postgres
    project_id = _seed_project(conn)
    actor = f"tester+{_new_id('a')}@example.com"
    rule = ftr.create_rule(conn, project_id=project_id, rule=_percentage(), created_by=actor)
    assert rule["status"] == "proposed"

    confirmed = ftr.set_rule_status(
        conn, rule_id=rule["id"], status="confirmed", actor=actor
    )
    assert confirmed["status"] == "confirmed"
    assert _count(
        conn,
        "SELECT count(*) FROM app.audit_log WHERE action = %s AND identity = %s",
        (ftr.ACTION_RULE_STATUS_SET, actor),
    ) == 1

    # A no-op transition writes no second audit event.
    again = ftr.set_rule_status(conn, rule_id=rule["id"], status="confirmed", actor=actor)
    assert again["status"] == "confirmed"
    assert _count(
        conn,
        "SELECT count(*) FROM app.audit_log WHERE action = %s AND identity = %s",
        (ftr.ACTION_RULE_STATUS_SET, actor),
    ) == 1

    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.set_rule_status(conn, rule_id=rule["id"], status="archived", actor=actor)

    ftr.set_rule_status(conn, rule_id=rule["id"], status="disabled", actor=actor)
    with pytest.raises(ftr.FeeTaxRuleStateError):
        # A disabled rule must go back through human review before it may run again.
        ftr.set_rule_status(conn, rule_id=rule["id"], status="confirmed", actor=actor)

    with pytest.raises(ftr.FeeTaxRuleNotFoundError):
        ftr.set_rule_status(conn, rule_id="ftr_nope", status="confirmed", actor=actor)


@fee_tax_schema
def test_28_update_rule_revalidates_the_merged_rule(live_postgres):
    conn = live_postgres
    project_id = _seed_project(conn)
    actor = "tester@example.com"
    with pytest.raises(ftr.FeeTaxRuleNotFoundError):
        ftr.update_rule(conn, rule_id="ftr_nope", patch={"label": "x"}, updated_by=actor)

    rule = ftr.create_rule(conn, project_id=project_id, rule=_flat(), created_by=actor)
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.update_rule(conn, rule_id=rule["id"], patch={"currency": None}, updated_by=actor)
    unchanged = ftr.get_rule(conn, rule["id"])
    assert unchanged["currency"] == "EUR"

    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.update_rule(
            conn, rule_id=rule["id"], patch={"project_id": "proj_other"}, updated_by=actor
        )

    patched = ftr.update_rule(
        conn, rule_id=rule["id"], patch={"amount_micros": 2_000_000, "label": "Bank fee"},
        updated_by=actor,
    )
    assert patched["amount_micros"] == 2_000_000
    assert patched["label"] == "Bank fee"
    assert _count(
        conn,
        "SELECT count(*) FROM app.audit_log WHERE action = %s AND identity = %s",
        (ftr.ACTION_RULE_UPDATED, actor),
    ) >= 1


@fee_tax_schema
def test_f3_update_rule_is_not_a_second_door_into_the_status_machine(live_postgres):
    """Review finding F3. `merged.update(patch)` used to accept `status`, so
    update_rule could take a disabled rule straight to confirmed -- the ONE transition
    set_rule_status refuses -- and audit it as fee_tax.rule.updated, so an audit query
    on fee_tax.rule.status_set would never see the re-confirmation."""
    conn = live_postgres
    project_id = _seed_project(conn)
    actor = f"tester+{_new_id('a')}@example.com"
    rule = ftr.create_rule(conn, project_id=project_id, rule=_percentage(), created_by=actor)
    ftr.set_rule_status(conn, rule_id=rule["id"], status="disabled", actor=actor)

    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.update_rule(conn, rule_id=rule["id"], patch={"status": "confirmed"}, updated_by=actor)
    # Even bundled with a legitimate field change, the whole patch is refused.
    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.update_rule(
            conn, rule_id=rule["id"], patch={"label": "x", "status": "confirmed"},
            updated_by=actor,
        )
    assert ftr.get_rule(conn, rule["id"])["status"] == "disabled"
    # The only door still refuses it, and the revival path still exists.
    with pytest.raises(ftr.FeeTaxRuleStateError):
        ftr.set_rule_status(conn, rule_id=rule["id"], status="confirmed", actor=actor)
    assert ftr.set_rule_status(
        conn, rule_id=rule["id"], status="proposed", actor=actor
    )["status"] == "proposed"


@fee_tax_schema
def test_f4_malformed_jsonb_cannot_reach_the_flattening_views(live_postgres):
    """Review finding F4. A single bad row would make a view RAISE for EVERY project's
    rules, and one fetch error aborts the whole nightly sync."""
    conn = live_postgres
    project_id = _seed_project(conn)
    # A scalar condition value: jsonb_array_elements_text would error on it.
    _expect_db_rejection(conn, project_id, conditions='{"country": "FR"}')
    _expect_db_rejection(conn, project_id, conditions='{"country": {"a": 1}}')
    # tiers on a form that is not SPEND_TIERS.
    _expect_db_rejection(conn, project_id, tiers='{"bands": 5}')
    _expect_db_rejection(
        conn, project_id, tiers='{"mode": "cliff", "bands": [{"threshold_micros": 0,'
        ' "rate": "0.05"}]}'
    )
    # A band whose values cannot be cast by the tiers view.
    _expect_db_rejection(
        conn,
        project_id,
        form="SPEND_TIERS",
        rate=None,
        tiers='{"bands": [{"threshold_micros": "abc", "rate": "0.05"}]}',
    )
    _expect_db_rejection(
        conn,
        project_id,
        form="SPEND_TIERS",
        rate=None,
        tiers='{"bands": [{"threshold_micros": 0, "rate": "not-a-number"}]}',
    )
    # A rate that would overflow NUMERIC(12,6) in the view's cast.
    _expect_db_rejection(
        conn,
        project_id,
        form="SPEND_TIERS",
        rate=None,
        tiers='{"bands": [{"threshold_micros": 0, "rate": "1234567.0"}]}',
    )
    # An empty conditions object stays valid: it means "matches every row".
    _raw_insert(conn, project_id, conditions="{}")
    # And the views still answer for every rule in the table.
    assert _count(conn, "SELECT count(*) FROM app.fee_tax_rule_conditions_v", ()) >= 0
    assert _count(conn, "SELECT count(*) FROM app.fee_tax_rule_tiers_v", ()) >= 0


@fee_tax_schema
def test_f8_a_colliding_dedup_hash_on_update_is_a_typed_409(live_postgres):
    conn = live_postgres
    project_id = _seed_project(conn)
    actor = "tester@example.com"
    first = ftr.create_rule(
        conn, project_id=project_id, rule=_percentage(dedup_hash="auto:a"), created_by=actor
    )
    second = ftr.create_rule(
        conn,
        project_id=project_id,
        rule=_percentage(dedup_hash="auto:b", rate="0.040000"),
        created_by=actor,
    )
    with pytest.raises(ftr.FeeTaxRuleStateError):
        ftr.update_rule(
            conn, rule_id=second["id"], patch={"dedup_hash": "auto:a"}, updated_by=actor
        )
    assert ftr.get_rule(conn, second["id"])["dedup_hash"] == "auto:b"
    # Re-writing a rule's own key is not a collision.
    ftr.update_rule(conn, rule_id=first["id"], patch={"label": "kept"}, updated_by=actor)
    assert ftr.get_rule(conn, first["id"])["dedup_hash"] == "auto:a"


@fee_tax_schema
def test_29_list_rules_resolves_a_datastream(live_postgres):
    conn = live_postgres
    project_id = _seed_project(conn)
    project_rule = _raw_insert(conn, project_id, cascade_phase=1, sequence_order=0)
    rule_a = _raw_insert(
        conn, project_id, scope_kind="datastream", scope_ref="dstr_a", cascade_phase=2
    )
    _raw_insert(conn, project_id, scope_kind="datastream", scope_ref="dstr_b", cascade_phase=2)
    _raw_insert(
        conn, project_id, scope_kind="plan_version", scope_ref="mpv_1", cascade_phase=3
    )

    resolved = ftr.list_rules(project_id, conn, datastream_id="dstr_a")
    assert [row["id"] for row in resolved] == [project_rule, rule_a]

    with pytest.raises(ftr.FeeTaxRuleValidationError):
        ftr.list_rules(project_id, conn, datastream_id="dstr_a", scope_kind="project")

    exact = ftr.list_rules(project_id, conn, scope_kind="datastream", scope_ref="dstr_b")
    assert len(exact) == 1


@fee_tax_schema
def test_30_list_rules_shows_proposed_rules_and_filters_on_demand(live_postgres):
    conn = live_postgres
    project_id = _seed_project(conn)
    _raw_insert(conn, project_id, status="proposed", cascade_phase=1)
    _raw_insert(conn, project_id, status="confirmed", cascade_phase=2)
    assert len(ftr.list_rules(project_id, conn)) == 2
    confirmed = ftr.list_rules(project_id, conn, status="confirmed")
    assert len(confirmed) == 1
    assert confirmed[0]["status"] == "confirmed"


def test_31_fk_cascade_needs_no_rgpd_allowlist_edit(live_postgres):
    """org_purge discovers the tenant tree from the FK graph, so ON DELETE CASCADE
    is picked up with no migration-099 registration.

    Asserted on the CONSTRAINT rather than by deleting a Project. Forty-four
    tables reference `app.projects` with RESTRICT, so a raw `DELETE FROM
    app.projects` fails for reasons that have nothing to do with this claim --
    and deleting a Project in SQL is the anti-pattern the org-purge endpoint
    exists to replace.
    """
    conn = live_postgres
    project_id = _seed_project(conn)
    datastream_id = _seed_datastream(conn, project_id)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.datastream_source_types "
            "(datastream_id, source_type, declared_by) VALUES (%s, 'PAID_MEDIA', 'test')",
            (datastream_id,),
        )
        cur.execute("DELETE FROM app.datastreams WHERE id = %s", (datastream_id,))
    assert _count(
        conn,
        "SELECT count(*) FROM app.datastream_source_types WHERE datastream_id = %s",
        (datastream_id,),
    ) == 0

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.confdeltype
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            WHERE c.contype = 'f' AND t.relname = 'fee_tax_rules'
              AND c.confrelid = 'app.projects'::regclass
            """
        )
        actions = [row[0] for row in cur.fetchall()]
    # 'c' is ON DELETE CASCADE: the purge walks the graph and needs no allowlist.
    assert actions == ["c"], actions


@fee_tax_schema
def test_32_datastream_source_types_vocabulary_and_one_row_per_datastream(live_postgres):
    import psycopg

    conn = live_postgres
    project_id = _seed_project(conn)
    datastream_id = _seed_datastream(conn, project_id)
    with pytest.raises(psycopg.Error):
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.datastream_source_types "
                    "(datastream_id, source_type, declared_by) VALUES (%s, 'PAID', 'test')",
                    (datastream_id,),
                )
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.datastream_source_types "
            "(datastream_id, source_type, declared_by) VALUES (%s, 'UNKNOWN', 'test')",
            (datastream_id,),
        )
    with pytest.raises(psycopg.Error):
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.datastream_source_types "
                    "(datastream_id, source_type, declared_by) "
                    "VALUES (%s, 'PAID_MEDIA', 'test')",
                    (datastream_id,),
                )


@fee_tax_schema
def test_33_the_store_never_commits(live_postgres):
    """Every function takes the caller's conn; rolling back must erase everything."""
    conn = live_postgres
    project_id = _seed_project(conn)
    ftr.create_rule(conn, project_id=project_id, rule=_percentage(), created_by="tester")
    conn.rollback()
    assert _count(
        conn, "SELECT count(*) FROM app.fee_tax_rules WHERE project_id = %s", (project_id,)
    ) == 0
