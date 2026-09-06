"""Story 41.5 (Epic 41) -- the STRUCTURAL half of AC8 / AC11: the revenue overlay is additive.

The numeric half is dbt/tests/test_epic41_revenue_totals_bit_identical.sql. This is the
half a number cannot prove: that NOTHING PRE-EXISTING READS the 41.5 overlay, so the
overlay CANNOT change a pre-existing figure whatever its own arithmetic does. "True by
construction" is only worth saying if a machine can check it -- this is the machine.

Pattern: server/tests/conformance/test_epic41_additive_only.py (41.3's), which this file
deliberately does NOT edit: each story adds its own conformance file so the additive
claim stays checkable with `git diff --name-only`.

Checks:
  1. no node OUTSIDE the Epic-41 token set depends on a 41.5 model;
  2. both models are materialized: view (no state is persisted);
  3. both depend only on the allowed set (no accidental edge into a semantic or dedup
     mart);
  4. the SHARED YAML files carry no 41.5 model or seed entry;
  4b. the `form` vocabulary is IDENTICAL across its THREE homes -- migration 119's CHECK,
     fee_tax_rules.FORMS and schema_fee_tax.yml's accepted_values -- so PER_TRANSACTION
     cannot exist in two of them and be missing from the third;
  4c. the two routed-pair CHECKs exist in 119, each carrying the reason AND the
     instruction that widening requires fixing the evaluator FIRST;
  5. no banned, dialect-specific construct in either model;
  6. ruling R3 -- zero country/market/source_type resolution logic, and the normalized
     model DOES consume 41.2's bridge;
  7. the AD-4 ratio guard -- neither model writes a fact row, and 'roas' never appears as
     a fact METRIC value.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DBT = _REPO_ROOT / "dbt"
_MANIFEST = _DBT / "target" / "manifest.json"
_MIGRATION = _REPO_ROOT / "infra" / "nango" / "migrations" / "119_fee_tax_alignment.sql"
_RULES_MODULE = _REPO_ROOT / "server" / "core" / "fee_tax_rules.py"
_SCHEMA_FEE_TAX = _DBT / "models" / "marts" / "schema_fee_tax.yml"

_OWNED_MODELS = (
    "fee_tax_revenue_normalized_daily",
    "fee_tax_revenue_alignment_daily",
)

_OWNED_SQL = [_DBT / "models" / "marts" / f"{name}.sql" for name in _OWNED_MODELS]

# THE NAMING IS LOAD-BEARING. 41.3's test_epic41_additive_only.py classifies every node
# whose unique_id lacks one of these tokens as PRE-EXISTING. A model called
# `revenue_tax_normalized_daily` (the name amendment C.5 originally used) would therefore
# be reported as a pre-existing node depending on fee_tax_ladder_rollup, and 41.3's
# ALREADY-LANDED conformance test would fail -- for a story that changed nothing of
# 41.3's. The fee_tax_ prefix keeps that test green WITHOUT EDITING IT.
_EPIC41_TOKENS = ("fee_tax", "epic41")

_ALLOWED_DEPENDENCIES = {
    "model.connector.fact_daily_kpi",
    "model.connector.fee_tax_country_resolution",
    "model.connector.fee_tax_rules_effective",
    "model.connector.fee_tax_ladder_rollup",
    "model.connector.fee_tax_revenue_normalized_daily",
    "source.connector.mirror.project_preferences",
    # Story 48.4: the ONE Tax & Fees activation authority. It SUBSTITUTES for
    # project_preferences in the activation role -- that column was a second
    # boolean beside app.project_capabilities and these models believed it.
    "source.connector.mirror.project_tax_fee_activation",
    "source.connector.mirror.fee_tax_rule_conditions",
    "seed.connector.fee_tax_revenue_scope",
    "seed.connector.metric_source_priority",
    "seed.connector.epic41_revenue_fixture",
}

# Each of these is DuckDB-only or BigQuery-only; the story's whole adapter surface is
# THREE macros (two reused, one new).
_BANNED_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"::", "the postfix :: cast is DuckDB/Postgres syntax, not BigQuery"),
    (r"\bDOUBLE\b", "DOUBLE is not a BigQuery type and destroys exact decimal arithmetic"),
    (r"\bVARCHAR\b", "VARCHAR is DuckDB; use STRING, which both engines accept"),
    (r"\bTEXT\b", "TEXT is not a BigQuery type"),
    (r"\bDECIMAL\s*\(", "a parameterised DECIMAL(p,s) must live inside a macro branch"),
    (r"\bNUMERIC\s*\(", "a parameterised NUMERIC(p,s) must live inside a macro branch"),
    (r"\bQUALIFY\b", "QUALIFY is not portable; use ROW_NUMBER() + WHERE _rn = 1"),
    (r"\blist_contains\b", "list_contains is DuckDB-only"),
    (r"\bstring_split\b", "string_split is DuckDB-only"),
    (r"\bjson_extract\b", "no JSON reaches dbt (decision D1) -- the mirror is relational"),
    (r"\bDATE\s*-", "DATE - DATE is DuckDB-only; use the days_between macro"),
)

_R3_FORBIDDEN = (
    "dim_country",
    "datastream_country_binding_dim",
    "datastream_source_types",
    "market_bindings",
)

# The 41.5 names that must NOT appear in any shared YAML. Note the PRECISE scope: this
# story DOES edit schema_fee_tax.yml, but only to add PER_TRANSACTION to one
# accepted_values list. That is a VOCABULARY edit, not a 41.5 model entry -- so this check
# asserts the absence of the 41.5 NAMES, never the absence of any diff. Asserting "no
# diff" would fail on the authorised edit and tempt someone to delete the check.
_OWNED_NAMES = ("revenue_normalized", "revenue_alignment", "revenue_scope")

_SHARED_YAML = (
    "models/marts/schema.yml",
    "models/marts/schema_fee_tax.yml",
    "models/marts/schema_fee_tax_bridge.yml",
    "seeds/schema.yml",
    "seeds/schema_epic41.yml",
    "dbt_project.yml",
)

_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
_SQL_COMMENT = re.compile(r"--[^\n]*")


def _strip_comments(sql: str) -> str:
    """Return the model's EXECUTABLE text.

    The banned-construct and R3 checks are about the SQL that reaches the warehouse, not
    about prose: these headers necessarily NAME the constructs they forbid, which is what
    makes them useful to a reviewer. Comments are stripped first; anything left is code.
    """
    return _SQL_COMMENT.sub("", _JINJA_COMMENT.sub("", sql))


def _load_manifest() -> dict:
    if not _MANIFEST.exists():
        pytest.skip(
            f"{_MANIFEST} is absent -- run `cd dbt && dbt build` (or at least `dbt parse`)"
            " before this conformance test; the additive proof is a manifest proof."
        )
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))


def _is_epic41(unique_id: str) -> bool:
    return any(token in unique_id for token in _EPIC41_TOKENS)


def test_owned_model_files_exist() -> None:
    missing = [str(path) for path in _OWNED_SQL if not path.exists()]
    assert not missing, f"Story 41.5 models missing: {missing}"


def test_no_pre_existing_node_depends_on_epic41_revenue() -> None:
    """CHECK 1 -- if nothing pre-existing reads the overlay, the overlay cannot move a
    pre-existing number, whatever its own arithmetic does (E41-NFR01)."""
    manifest = _load_manifest()
    owned = {f"model.connector.{name}" for name in _OWNED_MODELS}
    offenders: list[str] = []
    for section in ("nodes", "exposures", "metrics"):
        for unique_id, node in manifest.get(section, {}).items():
            if _is_epic41(unique_id):
                continue
            for dep in node.get("depends_on", {}).get("nodes", []):
                if dep in owned:
                    offenders.append(f"{unique_id} -> {dep}")
    assert not offenders, (
        "a pre-existing dbt node now depends on a Story 41.5 model, so the revenue"
        f" overlay is no longer additive: {offenders}"
    )


def test_epic41_revenue_models_are_views() -> None:
    """CHECK 2 -- materialized: view. dbt_project.yml defaults marts to `table` and this
    story may not edit it, so each model overrides IN-FILE."""
    manifest = _load_manifest()
    for name in _OWNED_MODELS:
        unique_id = f"model.connector.{name}"
        node = manifest["nodes"].get(unique_id)
        assert node is not None, f"{unique_id} is absent from the manifest"
        materialized = node.get("config", {}).get("materialized")
        assert materialized == "view", (
            f"{name} is materialized as {materialized!r}; E41-NFR03 requires a view"
        )


def test_epic41_revenue_models_depend_only_on_the_allowed_set() -> None:
    """CHECK 3 -- no accidental edge into a semantic, dedup or plan mart.

    In particular NOT cross_source_revenue: this story mirrors its dedup PATTERN and
    reuses its seed, but ref()ing the view itself would inherit a float revenue_total
    with no currency and no tax normalisation.
    """
    manifest = _load_manifest()
    offenders: list[str] = []
    for name in _OWNED_MODELS:
        node = manifest["nodes"].get(f"model.connector.{name}")
        if node is None:
            continue
        for dep in node.get("depends_on", {}).get("nodes", []):
            if dep not in _ALLOWED_DEPENDENCIES:
                offenders.append(f"{name} -> {dep}")
    assert not offenders, (
        f"a Story 41.5 model reads a relation outside its declared dependency set: {offenders}"
    )


def test_shared_yaml_carries_no_41_5_entry() -> None:
    """CHECK 4 -- the 41.5 YAML lives in NEW files, never in the shared ones."""
    offenders: list[str] = []
    for relative in _SHARED_YAML:
        text = (_DBT / relative).read_text(encoding="utf-8")
        for name in _OWNED_NAMES:
            if name in text:
                offenders.append(f"dbt/{relative} mentions {name}")
    assert not offenders, (
        "Story 41.5 entries must live in dbt/models/marts/schema_fee_tax_revenue.yml and"
        f" dbt/seeds/schema_epic41_revenue.yml so the additive claim stays checkable: {offenders}"
    )


def _forms_from_migration() -> set[str]:
    text = _MIGRATION.read_text(encoding="utf-8")
    match = re.search(r"CHECK\s*\(\s*form\s+IN\s*\(([^)]*)\)", text, re.DOTALL)
    assert match is not None, "no CHECK (form IN (...)) in migration 119"
    return set(re.findall(r"'([A-Z_]+)'", match.group(1)))


def _forms_from_python() -> set[str]:
    text = _RULES_MODULE.read_text(encoding="utf-8")
    match = re.search(r"FORMS\s*=\s*frozenset\(\{([^}]*)\}\)", text, re.DOTALL)
    assert match is not None, "no FORMS frozenset in server/core/fee_tax_rules.py"
    return set(re.findall(r'"([A-Z_]+)"', match.group(1)))


def _forms_from_yaml() -> set[str]:
    text = _SCHEMA_FEE_TAX.read_text(encoding="utf-8")
    match = re.search(
        r"name:\s*fee_tax_rules_effective_form_accepted.*?values:\s*\[([^\]]*)\]",
        text,
        re.DOTALL,
    )
    assert match is not None, (
        "no fee_tax_rules_effective_form_accepted values in schema_fee_tax.yml"
    )
    return set(re.findall(r'"([A-Z_]+)"', match.group(1)))


def test_form_vocabulary_is_identical_in_all_three_homes() -> None:
    """CHECK 4b -- the `form` vocabulary now has THREE homes, and they must agree.

    41.1 established the pattern for two homes ("a ONE-FOR-ONE mirror ... a story-local
    test asserts the two agree so they cannot drift"). Story 41.5 added a third:
    fee_tax_rules_effective selects EVERY confirmed rule regardless of category, so the
    first confirmed PER_TRANSACTION rule flows through it and would fail
    schema_fee_tax.yml's accepted_values test if only the migration and the Python module
    had been updated. That is the exact miss this check exists to make impossible.
    """
    migration = _forms_from_migration()
    python = _forms_from_python()
    yaml_values = _forms_from_yaml()
    assert migration == python == yaml_values, (
        "the `form` vocabulary has drifted between its three homes -- "
        f"migration 119: {sorted(migration)}, fee_tax_rules.FORMS: {sorted(python)}, "
        f"schema_fee_tax.yml: {sorted(yaml_values)}"
    )
    assert "PER_TRANSACTION" in migration, (
        "PER_TRANSACTION is Story 41.5's first-class form: a per-transaction gateway fee"
        " must never be expressed as an overloaded FLAT"
    )


def test_routed_pair_checks_are_present_and_self_explaining() -> None:
    """CHECK 4c -- the two coherence CHECKs exist AND carry their reason + instruction.

    A bare CHECK does not satisfy this. A future engineer who hits one of these while
    adding a legitimate combination WILL widen it; if the comment does not say what must
    change first, they re-open the silent-zero hole. So the presence of the instruction is
    part of the constraint, not decoration.
    """
    text = _MIGRATION.read_text(encoding="utf-8")
    for constraint in ("ck_fee_tax_rules_category_form", "ck_fee_tax_rules_form_base_target"):
        assert f"CONSTRAINT {constraint}" in text, f"{constraint} is missing from migration 119"

    assert "ELSE 0" in text and "COMPLETE" in text, (
        "the routed-pair guards must state WHY they exist: an unrouted pair reaches the"
        " cascade, contributes exactly zero and leaves the row reporting itself complete"
    )
    assert "BEFORE YOU WIDEN EITHER CONSTRAINT" in text, (
        "the routed-pair guards must instruct that the evaluator's routing is added FIRST"
        " -- widening one of these alone does not enable a feature, it re-opens the"
        " silent-zero hole"
    )
    # PER_TRANSACTION => PAYMENT_FEE lives INSIDE the category x form set, not as a
    # separate named object.
    assert "ck_fee_tax_rules_per_transaction_scope" not in text, (
        "the PER_TRANSACTION scope guard is SUBSUMED by ck_fee_tax_rules_category_form;"
        " a separate named constraint would be a second place to forget"
    )


def test_no_banned_dialect_construct_in_epic41_revenue_models() -> None:
    """CHECK 5 -- portability: every adapter-specific construct sits inside a macro.

    BigQuery validity is asserted by CONSTRUCTION and reviewed by reading -- the mirror
    has no BigQuery write path, so no BigQuery pass is claimed anywhere in this story.
    """
    offenders: list[str] = []
    for path in _OWNED_SQL:
        code = _strip_comments(path.read_text(encoding="utf-8"))
        for pattern, why in _BANNED_PATTERNS:
            if re.search(pattern, code):
                offenders.append(f"{path.name}: /{pattern}/ -- {why}")
    assert not offenders, "banned dialect-specific construct in a Story 41.5 model: " + str(
        offenders
    )


def test_zero_resolution_logic_in_epic41_revenue_models() -> None:
    """CHECK 6 (ruling R3) -- 41.2 owns the resolution, 41.5 owns the arithmetic."""
    offenders: list[str] = []
    for path in _OWNED_SQL:
        code = _strip_comments(path.read_text(encoding="utf-8"))
        for forbidden in _R3_FORBIDDEN:
            if forbidden in code:
                offenders.append(f"{path.name}: reads {forbidden}")
    assert not offenders, (
        "Story 41.5 must contain ZERO country/market/source_type resolution logic"
        f" (R3): {offenders}"
    )

    normalized = (
        _DBT / "models" / "marts" / "fee_tax_revenue_normalized_daily.sql"
    ).read_text(encoding="utf-8")
    assert normalized.count("fee_tax_country_resolution") >= 1, (
        "fee_tax_revenue_normalized_daily does not ref() fee_tax_country_resolution --"
        " the single LEFT JOIN on 41.2's frozen contract IS the attribute entry point"
    )
    # And it must call the SHARED matcher rather than becoming its third copy (epic retro
    # item C.9/6: 41.4 extracted it precisely so 41.5 would not re-copy it).
    assert "fee_tax_condition_matcher" in normalized, (
        "fee_tax_revenue_normalized_daily must CALL the shared condition matcher macro;"
        " a third copy of that logic would let two surfaces compute different totals from"
        " the same rules"
    )


def test_no_connector_name_in_epic41_revenue_models() -> None:
    """AD-2 / E41-NFR05 -- which metric of which connector is revenue is SEED DATA.

    A connector name in the SQL would make the scope a code change rather than a
    configuration one, which is exactly what fee_tax_revenue_scope.csv exists to prevent.
    """
    connectors = (
        "shopify", "woocommerce", "stripe", "square", "adjust", "klaviyo", "cm360",
        "google-sheets", "meta-ads", "tiktok-ads", "linkedin-ads", "google-analytics",
    )
    offenders: list[str] = []
    for path in _OWNED_SQL:
        code = _strip_comments(path.read_text(encoding="utf-8"))
        for connector in connectors:
            if connector in code:
                offenders.append(f"{path.name}: names the connector {connector!r}")
    assert not offenders, f"a connector name reached a Story 41.5 model (AD-2): {offenders}"


def test_ad4_no_ratio_is_ever_written_as_a_fact() -> None:
    """CHECK 7 (AC7) -- the machine-checkable half of the AD-4 guard.

    AD-4 forbids ratios as STORED ADDITIVE FACTS, because a stored ratio double-counts or
    mis-averages at every downstream SUM. A ratio in a READ VIEW at a declared grain is
    the sanctioned pattern (semantic_roas is the precedent). So: neither model may write
    to fact_daily_kpi, and 'roas' must never appear as a fact METRIC VALUE -- only as a
    ratio_kind or a column name.
    """
    manifest = _load_manifest()
    for name in _OWNED_MODELS:
        node = manifest["nodes"].get(f"model.connector.{name}")
        assert node is not None
        assert node.get("config", {}).get("materialized") == "view"

    for path in _OWNED_SQL:
        code = _strip_comments(path.read_text(encoding="utf-8"))
        assert "INSERT INTO" not in code.upper(), f"{path.name} writes rows"
        # A metric literal is what test_projection_additive_only.sql scans for. The
        # allowed spellings are the ratio_kind values and the column name.
        for bad in ("'roas'", '"roas"'):
            assert bad not in code, (
                f"{path.name} emits the bare literal {bad}: a ratio must never be"
                " expressible as a fact_daily_kpi metric value (AD-4)"
            )
