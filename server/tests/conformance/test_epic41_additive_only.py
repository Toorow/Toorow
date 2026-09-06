"""Story 41.3 (Epic 41) -- the STRUCTURAL half of AC14 / AC9: the cascade is additive.

The numeric half is dbt/tests/test_epic41_totals_bit_identical.sql. This is the half a
number cannot prove: that NOTHING PRE-EXISTING READS the Epic-41 overlay, so the overlay
CANNOT change a pre-existing figure whatever its own arithmetic does. "True by
construction" is only worth saying if a machine can check it -- this is the machine.

Pattern: server/tests/conformance/test_no_geographic_hardcode.py (assert over the
SHIPPED artifacts, not over intent, because the failure mode is silent).

Seven checks:
  1. no node OUTSIDE the Epic-41 set depends on an Epic-41 node;
  2. the three models are materialized: view (E41-NFR03 -- no state is persisted);
  3. the three models depend only on the allowed set (no accidental edge into a
     semantic or dedup mart);
  4. dbt/models/marts/schema.yml and dbt/dbt_project.yml carry no `fee_tax` string --
     the Epic-41 YAML lives in its own files so the additive claim stays checkable with
     `git diff --name-only`;
  5. no banned, dialect-specific construct in the three models (§B.5) -- every one of
     them must live behind a target.type branch inside one of the TWO macros;
  6. ruling R3: ZERO country / market / source_type resolution logic in 41.3 -- those
     attributes may enter only through ref('fee_tax_country_resolution');
  7. decision D9: 41.3 touches neither mirror_sync.py nor sources_mirror.yml, and both
     already declare the six Epic-41 mirror relations -- so a missing 41.1 registration
     fails HERE with a clear message instead of as an opaque dbt compile error.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DBT = _REPO_ROOT / "dbt"
_MANIFEST = _DBT / "target" / "manifest.json"

# The three models Story 41.3 owns. fee_tax_country_resolution is Story 41.2's and is
# deliberately NOT in this list: 41.3 consumes it and must never edit it.
_OWNED_MODELS = (
    "fee_tax_rules_effective",
    "fee_tax_ladder_daily",
    "fee_tax_ladder_rollup",
)

_OWNED_SQL = [_DBT / "models" / "marts" / f"{name}.sql" for name in _OWNED_MODELS]

# Everything Epic 41 introduced, across 41.2 and 41.3. A node belongs to the epic when
# its unique_id carries one of these tokens -- which also catches dbt's auto-generated
# generic-test names (not_null_fee_tax_ladder_daily_project_id, ...).
_EPIC41_TOKENS = ("fee_tax", "epic41")

# What the three models are allowed to read. Anything else is an accidental edge.
_ALLOWED_DEPENDENCIES = {
    "model.connector.fact_daily_kpi",
    "model.connector.fee_tax_country_resolution",
    "model.connector.fee_tax_rules_effective",
    "model.connector.fee_tax_ladder_daily",
    "model.connector.fee_tax_ladder_rollup",
    "source.connector.mirror.project_preferences",
    # Story 48.4: the ONE Tax & Fees activation authority. It SUBSTITUTES for
    # project_preferences in the activation role -- that column was a second
    # boolean beside app.project_capabilities and these models believed it.
    "source.connector.mirror.project_tax_fee_activation",
    "source.connector.mirror.fee_tax_rules",
    "source.connector.mirror.fee_tax_rule_conditions",
    "source.connector.mirror.fee_tax_rule_tiers",
    "source.connector.mirror.datastreams_dim",
    "source.connector.mirror.media_plan_versions",
    # media_plans is read ONLY to recover a plan's owning project_id: neither
    # media_plan_versions nor plan_line_mappings carries one, and without it a
    # plan-scoped rule and the plan_line rollup leak across tenants that share a
    # connector account (review finding F4).
    "source.connector.mirror.media_plans",
    "source.connector.mirror.plan_line_mappings",
    "seed.connector.epic41_cascade_fixture",
}

# §B.5. Each of these is either DuckDB-only or BigQuery-only; a model that writes one
# directly is not portable, and the story's whole adapter surface is TWO macros.
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

# Ruling R3: these belong to Story 41.2's fee_tax_country_resolution and to nothing in
# 41.3. If a line of 41.3 resolves a country, a market or a source type, it is wrong.
_R3_FORBIDDEN = (
    "dim_country",
    "datastream_country_binding_dim",
    "datastream_source_types",
    "market_bindings",
)

# The six mirror relations Story 41.1 registers, once, for the whole epic (D9).
_EPIC41_MIRROR_RELATIONS = (
    "fee_tax_rules",
    "fee_tax_rule_conditions",
    "fee_tax_rule_tiers",
    "datastream_source_types",
    "datastreams_dim",
    "datastream_country_binding_dim",
)

_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
_SQL_COMMENT = re.compile(r"--[^\n]*")


def _strip_comments(sql: str) -> str:
    """Return the model's EXECUTABLE text.

    The banned-construct and R3 checks are about the SQL that reaches the warehouse, not
    about prose: these headers necessarily NAME the constructs they forbid (that is what
    makes them useful to a reviewer), so grepping the raw file would forbid documenting
    the rule. Comments are stripped first; anything left is code.
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
    """The three models Story 41.3 ships are on disk (guards every check below)."""
    missing = [str(path) for path in _OWNED_SQL if not path.exists()]
    assert not missing, f"Story 41.3 models missing: {missing}"


def test_no_pre_existing_node_depends_on_epic41() -> None:
    """CHECK 1 -- the machine-checkable form of "additive by construction".

    If nothing pre-existing reads the overlay, the overlay cannot move a pre-existing
    number, whatever its own arithmetic does (E41-NFR01, arbitration B2).
    """
    manifest = _load_manifest()
    offenders: list[str] = []
    for section in ("nodes", "exposures", "metrics"):
        for unique_id, node in manifest.get(section, {}).items():
            if _is_epic41(unique_id):
                continue
            for dep in node.get("depends_on", {}).get("nodes", []):
                if _is_epic41(dep):
                    offenders.append(f"{unique_id} -> {dep}")
    assert not offenders, (
        "a pre-existing dbt node now depends on an Epic-41 node, so the overlay is no"
        f" longer additive: {offenders}"
    )


def test_epic41_models_are_views() -> None:
    """CHECK 2 -- materialized: view, so no state is persisted (E41-NFR03).

    dbt_project.yml defaults marts to `table` and this story may not edit it, so each
    model must override in-file. A table would also mean the SPEND_TIERS cumulative base
    was materialised rather than computed at view time.
    """
    manifest = _load_manifest()
    for name in _OWNED_MODELS:
        unique_id = f"model.connector.{name}"
        node = manifest["nodes"].get(unique_id)
        assert node is not None, f"{unique_id} is absent from the manifest"
        materialized = node.get("config", {}).get("materialized")
        assert materialized == "view", (
            f"{name} is materialized as {materialized!r}; E41-NFR03 requires a view"
            " (no state, tiers computed at view time)"
        )


def test_epic41_models_depend_only_on_the_allowed_set() -> None:
    """CHECK 3 -- no accidental edge into a semantic, dedup or plan mart."""
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
        "an Epic-41.3 model reads a relation outside its declared dependency set:"
        f" {offenders}"
    )


def test_existing_yaml_untouched_by_epic41() -> None:
    """CHECK 4 -- the Epic-41 YAML lives in NEW files, never in the shared ones."""
    for relative in ("models/marts/schema.yml", "dbt_project.yml"):
        text = (_DBT / relative).read_text(encoding="utf-8")
        assert "fee_tax" not in text, (
            f"dbt/{relative} mentions fee_tax. Story 41.3 must add its entries to"
            " dbt/models/marts/schema_fee_tax.yml (and its seed entries to"
            " dbt/seeds/schema_epic41.yml) so the additive claim stays checkable with"
            " `git diff --name-only`."
        )


def test_no_banned_dialect_construct_in_epic41_models() -> None:
    """CHECK 5 -- portability: every adapter-specific construct sits inside a macro.

    BigQuery validity is asserted by CONSTRUCTION and reviewed by reading -- the mirror
    has no BigQuery write path yet (mirror_sync.py's BigQuery branch is a log line), so
    no BigQuery pass is claimed anywhere in this story.
    """
    offenders: list[str] = []
    for path in _OWNED_SQL:
        code = _strip_comments(path.read_text(encoding="utf-8"))
        for pattern, why in _BANNED_PATTERNS:
            if re.search(pattern, code):
                offenders.append(f"{path.name}: /{pattern}/ -- {why}")
    assert not offenders, "banned dialect-specific construct in an Epic-41.3 model: " + str(
        offenders
    )


def test_no_v_suffixed_source_name() -> None:
    """CHECK 5b (ruling R1) -- the app.*_v views are the Postgres feed ONLY.

    The mirror entry, the dbt source name and the DuckDB/BigQuery relation are all
    unsuffixed. A `_v` inside a source() call would mean dbt is reading a name that does
    not exist in the warehouse.
    """
    pattern = re.compile(r"source\(\s*['\"]mirror['\"]\s*,\s*['\"]([a-z0-9_]+)['\"]\s*\)")
    offenders: list[str] = []
    for path in _OWNED_SQL:
        for relation in pattern.findall(path.read_text(encoding="utf-8")):
            if relation.endswith("_v"):
                offenders.append(f"{path.name}: source('mirror', '{relation}')")
    assert not offenders, f"a _v-suffixed mirror relation reached dbt (R1): {offenders}"


def test_zero_resolution_logic_in_epic41_models() -> None:
    """CHECK 6 (ruling R3) -- 41.2 owns the resolution, 41.3 owns the arithmetic.

    Country / market / source_type may enter 41.3 ONLY through
    ref('fee_tax_country_resolution'). Two stories resolving the same attribute would
    put the ladder in two places, which is exactly the collision the epic's
    file-ownership discipline exists to prevent.
    """
    offenders: list[str] = []
    for path in _OWNED_SQL:
        raw = path.read_text(encoding="utf-8")
        code = _strip_comments(raw)
        for forbidden in _R3_FORBIDDEN:
            if forbidden in code:
                offenders.append(f"{path.name}: reads {forbidden}")
        # The retired seam marker must not exist even as a comment: its presence would
        # invite a second story to graft resolution logic into this file.
        if "fee-tax-country-bridge" in raw:
            offenders.append(f"{path.name}: carries the RETIRED seam marker")
    assert not offenders, (
        "Epic-41.3 must contain ZERO country/market/source_type resolution logic (R3):"
        f" {offenders}"
    )

    # The positive half: the ladder must actually consume 41.2's view.
    ladder = (_DBT / "models" / "marts" / "fee_tax_ladder_daily.sql").read_text(encoding="utf-8")
    assert ladder.count("fee_tax_country_resolution") >= 1, (
        "fee_tax_ladder_daily does not ref() fee_tax_country_resolution -- the single"
        " LEFT JOIN on 41.2's frozen contract IS the attribute entry point"
    )


def test_d9_mirror_registration_is_41_1s_and_is_present() -> None:
    """CHECK 7 (decision D9) -- Story 41.1 owns every mirror registration in this epic.

    41.3 must NOT patch mirror_sync.py or sources_mirror.yml. If a registration is
    missing, that is a 41.1 BLOCKER to report, not a file to edit -- so it fails here,
    by name, instead of surfacing as an opaque dbt compile error three layers down.
    """
    mirror_sync = (_REPO_ROOT / "server" / "core" / "mirror_sync.py").read_text(encoding="utf-8")
    sources_yml = (
        _DBT / "models" / "staging" / "sources_mirror.yml"
    ).read_text(encoding="utf-8")

    missing_sync = [
        relation
        for relation in _EPIC41_MIRROR_RELATIONS
        if f'"{relation}"' not in mirror_sync
    ]
    assert not missing_sync, (
        "mirror_sync.py does not register these Epic-41 relations (Story 41.1 blocker --"
        f" 41.3 must not patch that file): {missing_sync}"
    )

    missing_source = [
        relation
        for relation in _EPIC41_MIRROR_RELATIONS
        if f"- name: {relation}" not in sources_yml
    ]
    assert not missing_source, (
        "sources_mirror.yml does not declare these Epic-41 relations (Story 41.1 blocker"
        f" -- 41.3 must not patch that file): {missing_source}"
    )

    assert "fee_tax_alignment_enabled" in sources_yml, (
        "sources_mirror.yml does not document project_preferences.fee_tax_alignment_enabled"
        " -- the activation column Story 41.3 reads (Story 41.1 blocker)"
    )
