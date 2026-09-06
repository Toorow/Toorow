"""Tests for Story 27.1 -- metric-semantics foundation (Epic 27).

Offline (no DB): seed parsing + pure projection (the bit-identical invariant 3),
cascade specificity reducers over in-memory rows, scope/rule validation, and the AD-2
"no provider name in the module" grep.

Live-Postgres (skipped when TEST_POSTGRES_DSN is unset): the real DDL of migration 049,
idempotent seed import, the scope CHECK, the COALESCE unicity, FK CASCADE, and the
append-only REVOKE on the audit table.  Pattern calqué sur test_dataset_access_grants.py.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from tests.conftest import purge_fixture_org

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import metric_semantics as ms  # noqa: E402

from tests.support.updated_at_trigger import ensure_set_updated_at

# ---------------------------------------------------------------------------
# Postgres availability check (calqué sur test_dataset_access_grants.py)
# ---------------------------------------------------------------------------


def _pg_reachable() -> bool:
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False
    try:
        import psycopg

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pg_available = pytest.mark.skipif(not _pg_reachable(), reason="platform Postgres not reachable")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SEEDS_DIR = _REPO_ROOT / "dbt" / "seeds"


# ---------------------------------------------------------------------------
# Independent seed re-derivation (robustesse seeds mouvants).
#
# The dbt seeds EVOLVE: every connector-epic session appends rows to
# metric_source_priority.csv (e.g. a new revenue source) and dim_metric.csv.
# Tests over the REAL seeds therefore NEVER hard-code the expected member lists or
# priority orders -- they would duplicate the seed and break on every legitimate
# evolution. Instead each real-seed test re-derives its expectation with an
# INDEPENDENT, minimal csv.DictReader read here (a second implementation that must
# converge with the module's parser -- the bit-identical invariant 3 as a true
# cross-check, not a tautology). Frozen literal expectations live ONLY in the
# tmp_path fixture tests, whose synthetic seeds the test itself writes.
# ---------------------------------------------------------------------------


def _independent_priority_orders(seeds_dir: Path) -> dict[str, list[str]]:
    """Re-derive {metric -> ordered connectors} straight from the CSV (no module code).

    Order reproduces the mart's ``ORDER BY COALESCE(priority, 99), connector``: sort by
    ``(int(priority) or 99, connector)``. Deliberately does NOT call
    ms.parse_metric_source_priority so the assertion is a genuine two-implementation
    cross-check that survives seed evolution."""
    import csv as _csv  # noqa: PLC0415

    rows_by_metric: dict[str, list[tuple[int, str]]] = {}
    order: list[str] = []
    with (seeds_dir / "metric_source_priority.csv").open(
        "r", newline="", encoding="utf-8"
    ) as handle:
        for row in _csv.DictReader(handle):
            metric = (row.get("metric") or "").strip()
            connector = (row.get("connector") or "").strip()
            if not metric or not connector:
                continue
            try:
                priority = int((row.get("priority") or "").strip())
            except ValueError:
                priority = 99  # COALESCE(priority, 99) parity
            if metric not in rows_by_metric:
                rows_by_metric[metric] = []
                order.append(metric)
            rows_by_metric[metric].append((priority, connector))
    return {
        metric: [c for _, c in sorted(rows_by_metric[metric], key=lambda m: (m[0], m[1]))]
        for metric in order
    }


def _independent_dim_metric_names(seeds_dir: Path) -> list[str]:
    """Re-derive the ordered canonical metric names straight from dim_metric.csv."""
    import csv as _csv  # noqa: PLC0415

    names: list[str] = []
    with (seeds_dir / "dim_metric.csv").open(
        "r", newline="", encoding="utf-8"
    ) as handle:
        for row in _csv.DictReader(handle):
            name = (row.get("name") or "").strip()
            if name:
                names.append(name)
    return names


# Cross-source marts that exist as dbt models today (the target_mart allow-list). Kept
# here as the test's OWN independent copy so target_mart assertions never call the module.
_KNOWN_CROSS_SOURCE_MARTS = {"cross_source_conversions", "cross_source_revenue"}


def _independent_target_mart(metric: str) -> str | None:
    """Independent derivation of the routed mart: cross_source_<metric> iff it exists."""
    candidate = f"cross_source_{metric}"
    return candidate if candidate in _KNOWN_CROSS_SOURCE_MARTS else None


_EXPECTED_METRICS = [
    "sessions",
    "active_users",
    "conversions",
    "screen_page_views",
    "cost",
    "impressions",
    "clicks",
    "revenue",
    "roas",
    "ctr",
    "cpa",
    "average_position",
    "installs",
    "ad_revenue",
    "all_revenue",
    # Added post-27.1 by connector work (cm360 / Square) -- seed grew 15 -> 19.
    "conversions_value",
    "unique_reach",
    "average_frequency",
    "viewability_rate",
    "profile_views",
]


# ---------------------------------------------------------------------------
# Offline -- seed parsing & pure projection (no DB)
# ---------------------------------------------------------------------------


def test_dim_metric_parses_all_metrics():
    """§1: parse_dim_metric reads all metrics of dim_metric.csv (count + names).

    Deliberate tripwire on the seed: the CSV grew 15 -> 20 via connector work
    (cm360 / Square added conversions_value, unique_reach, average_frequency,
    viewability_rate, profile_views) after Story 27.1; _EXPECTED_METRICS was updated to match.
    """
    defs = ms.parse_dim_metric(_SEEDS_DIR)
    assert len(defs) == len(_EXPECTED_METRICS)
    assert [d["canonical_name"] for d in defs] == _EXPECTED_METRICS


def test_additive_and_aggregation_typed():
    """§2: additive/aggregation_type typed (ratios non-additive; revenue/cost additive+sum)."""
    by_name = {d["canonical_name"]: d for d in ms.parse_dim_metric(_SEEDS_DIR)}

    assert by_name["revenue"]["additive"] is True
    assert by_name["revenue"]["aggregation_type"] == "sum"
    assert by_name["cost"]["additive"] is True
    assert by_name["cost"]["aggregation_type"] == "sum"

    for ratio in ("roas", "ctr", "cpa"):
        assert by_name[ratio]["additive"] is False
        assert by_name[ratio]["aggregation_type"] == "ratio"


def test_ratio_numerator_denominator():
    """§3: ratios roas->(revenue,cost), ctr->(clicks,impressions), cpa->(cost,conversions)."""
    by_name = {d["canonical_name"]: d for d in ms.parse_dim_metric(_SEEDS_DIR)}
    assert (by_name["roas"]["ratio_numerator"], by_name["roas"]["ratio_denominator"]) == (
        "revenue",
        "cost",
    )
    assert (by_name["ctr"]["ratio_numerator"], by_name["ctr"]["ratio_denominator"]) == (
        "clicks",
        "impressions",
    )
    assert (by_name["cpa"]["ratio_numerator"], by_name["cpa"]["ratio_denominator"]) == (
        "cost",
        "conversions",
    )


def test_average_position_impression_weighted():
    """§4: average_position -> impression_weighted_average, additive False, ratios NULL."""
    by_name = {d["canonical_name"]: d for d in ms.parse_dim_metric(_SEEDS_DIR)}
    ap = by_name["average_position"]
    assert ap["aggregation_type"] == "impression_weighted_average"
    assert ap["additive"] is False
    assert ap["ratio_numerator"] is None
    assert ap["ratio_denominator"] is None


def test_sum_metrics_have_null_ratios():
    """Sum metrics carry no ratio numerator/denominator (empty seed cells -> None)."""
    by_name = {d["canonical_name"]: d for d in ms.parse_dim_metric(_SEEDS_DIR)}
    for name in ("sessions", "revenue", "cost", "impressions", "installs"):
        assert by_name[name]["ratio_numerator"] is None
        assert by_name[name]["ratio_denominator"] is None


def test_priority_seed_two_groups_exact_members():
    """§5: metric_source_priority.csv -> one group per metric; members == the CSV.

    Robustesse seeds mouvants: the metric set and members are re-derived from the CSV
    (independent read), never hard-coded -- the seed grows every connector epic. Strong
    STRUCTURAL invariants remain: exactly one group per metric present in the seed, and
    each group's members == exactly the connectors the CSV lists for that metric.
    """
    expected = _independent_priority_orders(_SEEDS_DIR)
    groups = ms.parse_metric_source_priority(_SEEDS_DIR)
    by_metric = {g["canonical_name"]: g for g in groups}
    # Exactly one group per metric present in the seed (no more, no fewer, no dups).
    assert len(groups) == len(expected)
    assert set(by_metric) == set(expected)
    for metric, connectors in expected.items():
        assert set(by_metric[metric]["members"]) == set(connectors)


def test_priority_order_matches_independent_sort():
    """§6/§7: every metric's priority_order == the independent (priority, connector) sort.

    Robustesse seeds mouvants: the expected order is re-derived from the CSV, never
    hard-coded (revenue/conversions gain sources across epics). The parser and the
    independent read must converge for EVERY metric, and each order must be strictly
    ranked (priority ascending, alphabetical tie-break) -- a real cross-check.
    """
    expected = _independent_priority_orders(_SEEDS_DIR)
    by_metric = {g["canonical_name"]: g for g in ms.parse_metric_source_priority(_SEEDS_DIR)}
    assert set(by_metric) == set(expected)
    for metric, order in expected.items():
        got = by_metric[metric]["priority_order"]
        assert got == order
        # Structural: strictly ranked (priority asc, then connector alpha) with no dups.
        assert got == sorted(set(got), key=got.index)  # no duplicate connectors
        assert len(got) == len(set(got))


def test_group_naming_and_method():
    """Each seed group is a PRIORITY rule named f'{metric}-priority'."""
    for group in ms.parse_metric_source_priority(_SEEDS_DIR):
        assert group["name"] == f"{group['canonical_name']}-priority"
        assert group["method"] == ms.METHOD_PRIORITY


def test_bit_identical_projection_matches_seed(tmp_path):
    """§8: bit-identical equivalence (invariant 3): projection == a hand-built seed referential.

    Compares canonical STRUCTURES (not formatted strings): metric set, additive/
    aggregation/ratio per row, and ordered priority per metric.  We rebuild the seed
    referential from a copy written under tmp_path to prove the projection reads the
    file (no hidden coupling to the repo path)."""
    seed_dim = (
        "name,additive,aggregation_rule,ratio_numerator,ratio_denominator\n"
        "revenue,true,sum,,\n"
        "cost,true,sum,,\n"
        "roas,false,ratio,revenue,cost\n"
    )
    seed_prio = (
        "metric,connector,priority\n"
        "revenue,shopify,1\n"
        "revenue,stripe,2\n"
    )
    (tmp_path / "dim_metric.csv").write_text(seed_dim, encoding="utf-8")
    (tmp_path / "metric_source_priority.csv").write_text(seed_prio, encoding="utf-8")

    projection = ms.platform_defaults_from_seeds(tmp_path)

    # Referential built directly from the seed text above.
    expected_definitions = [
        {"canonical_name": "revenue", "aggregation_type": "sum", "additive": True,
         "ratio_numerator": None, "ratio_denominator": None},
        {"canonical_name": "cost", "aggregation_type": "sum", "additive": True,
         "ratio_numerator": None, "ratio_denominator": None},
        {"canonical_name": "roas", "aggregation_type": "ratio", "additive": False,
         "ratio_numerator": "revenue", "ratio_denominator": "cost"},
    ]
    # Story 39.1: `monetary` is DERIVED (pure classifier), not a CSV column, so it is
    # EXCLUDED from the bit-identical seed<->defaults equivalence exactly as `target_mart`
    # is excluded from the groups comparison. Strip it before the structural equality.
    stripped_definitions = [
        {k: v for k, v in d.items() if k != "monetary"} for d in projection["definitions"]
    ]
    assert stripped_definitions == expected_definitions
    assert len(projection["groups"]) == 1
    revenue_group = projection["groups"][0]
    assert revenue_group["canonical_name"] == "revenue"
    assert revenue_group["priority_order"] == ["shopify", "stripe"]


def test_bit_identical_real_seed_flags_and_priorities():
    """§8 (real seeds): every metric name + every priority order matches the live CSVs.

    Robustesse seeds mouvants: both the metric-name list and the per-metric priority
    orders are re-derived from the CSVs by an INDEPENDENT reader (never hard-coded), so
    the bit-identical invariant 3 stays a genuine two-implementation cross-check that
    survives seed growth (new metrics, new revenue sources) across connector epics.
    """
    projection = ms.platform_defaults_from_seeds(_SEEDS_DIR)
    # metric set equal to dim_metric.csv names (independent read of the same CSV)
    assert [d["canonical_name"] for d in projection["definitions"]] == (
        _independent_dim_metric_names(_SEEDS_DIR)
    )
    expected_orders = _independent_priority_orders(_SEEDS_DIR)
    by_metric = {g["canonical_name"]: g for g in projection["groups"]}
    assert set(by_metric) == set(expected_orders)
    for metric, order in expected_orders.items():
        assert by_metric[metric]["priority_order"] == order


def test_tiebreak_alphabetical_on_equal_priority(tmp_path):
    """§9: same priority -> alphabetical tie-break (ORDER BY ..., connector)."""
    (tmp_path / "dim_metric.csv").write_text(
        "name,additive,aggregation_rule,ratio_numerator,ratio_denominator\n"
        "conversions,true,sum,,\n",
        encoding="utf-8",
    )
    # zulu and alpha share priority 1 -> alpha must come first; charlie priority 2 last.
    (tmp_path / "metric_source_priority.csv").write_text(
        "metric,connector,priority\n"
        "conversions,zulu,1\n"
        "conversions,alpha,1\n"
        "conversions,charlie,2\n",
        encoding="utf-8",
    )
    group = ms.parse_metric_source_priority(tmp_path)[0]
    assert group["priority_order"] == ["alpha", "zulu", "charlie"]


def test_unlisted_priority_defaults_to_99(tmp_path):
    """A non-integer priority cell falls back to 99 (COALESCE(priority,99) parity)."""
    (tmp_path / "dim_metric.csv").write_text(
        "name,additive,aggregation_rule,ratio_numerator,ratio_denominator\n"
        "revenue,true,sum,,\n",
        encoding="utf-8",
    )
    (tmp_path / "metric_source_priority.csv").write_text(
        "metric,connector,priority\n"
        "revenue,late,\n"
        "revenue,early,1\n",
        encoding="utf-8",
    )
    group = ms.parse_metric_source_priority(tmp_path)[0]
    # early (priority 1) beats late (blank -> 99).
    assert group["priority_order"] == ["early", "late"]


# ---------------------------------------------------------------------------
# Offline -- Story 27.3 extensions: target_mart on PRIORITY defaults + cost pilot
# ---------------------------------------------------------------------------


def test_default_target_mart_pure():
    """§32: _default_target_mart is pure -- conversions/revenue map, cost is None."""
    assert ms._default_target_mart("conversions") == "cross_source_conversions"
    assert ms._default_target_mart("revenue") == "cross_source_revenue"
    assert ms._default_target_mart("cost") is None
    # A metric with no cross_source_<metric> model -> None (never invented).
    assert ms._default_target_mart("installs") is None


def test_priority_groups_expose_target_mart():
    """§30: projection exposes target_mart per PRIORITY group; order/priorities unchanged.

    Robustesse seeds mouvants: target_mart is asserted structurally -- for EVERY seed
    group, target_mart == cross_source_<metric> iff that mart exists (independent
    allow-list), else None -- without hard-coding the connector list. priority_order is
    checked against the independent seed read, so it survives new sources per epic.
    """
    projection = ms.platform_defaults_from_seeds(_SEEDS_DIR)
    expected_orders = _independent_priority_orders(_SEEDS_DIR)
    by_metric = {g["canonical_name"]: g for g in projection["groups"]}
    assert set(by_metric) == set(expected_orders)
    for metric, order in expected_orders.items():
        group = by_metric[metric]
        # target_mart is derived purely from the metric name (known-mart allow-list).
        assert group["target_mart"] == _independent_target_mart(metric)
        # priority_order untouched (27.1 equivalence preserved), independent of the seed.
        assert group["priority_order"] == order


def test_bit_identical_extended_with_target_mart(tmp_path):
    """§31: the equivalence stays true AND now covers target_mart (never vs the CSV).

    The CSV has no target_mart column; we assert the projection's target_mart against
    FROZEN expected values on a synthetic tmp_path seed (F-3: no circular comparison to
    ms._default_target_mart -- the frozen values are the sole offline truth). Divergence
    would fail."""
    (tmp_path / "dim_metric.csv").write_text(
        "name,additive,aggregation_rule,ratio_numerator,ratio_denominator\n"
        "revenue,true,sum,,\n"
        "conversions,true,sum,,\n"
        "cost,true,sum,,\n",
        encoding="utf-8",
    )
    (tmp_path / "metric_source_priority.csv").write_text(
        "metric,connector,priority\n"
        "revenue,shopify,1\n"
        "conversions,google-analytics,1\n"
        "cost,some-source,1\n",
        encoding="utf-8",
    )
    projection = ms.platform_defaults_from_seeds(tmp_path)
    by_metric = {g["canonical_name"]: g for g in projection["groups"]}
    # Frozen expected target_mart values (a mart exists iff cross_source_<metric> is a
    # known dbt model): revenue/conversions route, cost does NOT.
    assert by_metric["revenue"]["target_mart"] == "cross_source_revenue"
    assert by_metric["conversions"]["target_mart"] == "cross_source_conversions"
    assert by_metric["cost"]["target_mart"] is None


# RETIRE 2026-08-17 (AI-295) -- `test_cost_pilot_constant_shape`.
# Il tenait la forme de `PLATFORM_COST_VERIFICATION_GROUP`, la constante qui
# ecrivait un groupe de chevauchement supplementaire a chaque bootstrap d org.
# La constante et son ecrivain sont partis avec le second modele : la
# reconciliation est le Rule Set gouverne, publie par un Projet, et rien d autre.


# ---------------------------------------------------------------------------
# Offline -- cascade specificity reducers (pure, over in-memory rows)
# ---------------------------------------------------------------------------


def _def_row(name, scope, org_id=None, project_id=None, extra=None):
    row = {
        "canonical_name": name,
        "scope_level": scope,
        "org_id": org_id,
        "project_id": project_id,
    }
    if extra:
        row.update(extra)
    return row


def test_cascade_platform_only():
    """§10: only PLATFORM rows -> the defaults are returned."""
    rows = [_def_row("revenue", ms.SCOPE_PLATFORM), _def_row("cost", ms.SCOPE_PLATFORM)]
    resolved = ms.reduce_definitions_by_specificity(rows)
    assert set(resolved) == {"revenue", "cost"}
    assert resolved["revenue"]["scope_level"] == ms.SCOPE_PLATFORM


def test_cascade_org_beats_platform():
    """§11: an ORG row overrides the PLATFORM row of the same canonical_name."""
    rows = [
        _def_row("revenue", ms.SCOPE_PLATFORM, extra={"additive": True}),
        _def_row("revenue", ms.SCOPE_ORG, org_id="org_1", extra={"additive": False}),
    ]
    resolved = ms.reduce_definitions_by_specificity(rows)
    assert resolved["revenue"]["scope_level"] == ms.SCOPE_ORG
    assert resolved["revenue"]["additive"] is False


def test_cascade_project_beats_org_and_platform():
    """§12: a PROJECT row overrides both ORG and PLATFORM (most specific wins)."""
    rows = [
        _def_row("revenue", ms.SCOPE_PLATFORM),
        _def_row("revenue", ms.SCOPE_ORG, org_id="org_1"),
        _def_row("revenue", ms.SCOPE_PROJECT, project_id="proj_1"),
    ]
    resolved = ms.reduce_definitions_by_specificity(rows)
    assert resolved["revenue"]["scope_level"] == ms.SCOPE_PROJECT


def test_cascade_union_of_levels():
    """§13: a PLATFORM-only metric and a PROJECT-only metric both appear at their level."""
    rows = [
        _def_row("revenue", ms.SCOPE_PLATFORM),
        _def_row("custom_kpi", ms.SCOPE_PROJECT, project_id="proj_1"),
    ]
    resolved = ms.reduce_definitions_by_specificity(rows)
    assert resolved["revenue"]["scope_level"] == ms.SCOPE_PLATFORM
    assert resolved["custom_kpi"]["scope_level"] == ms.SCOPE_PROJECT


def test_cascade_deterministic_repeat():
    """§18: two reductions of the same rows give the identical result."""
    rows = [
        _def_row("revenue", ms.SCOPE_PLATFORM),
        _def_row("revenue", ms.SCOPE_ORG, org_id="org_1"),
    ]
    assert ms.reduce_definitions_by_specificity(rows) == ms.reduce_definitions_by_specificity(rows)


# RETIRES 2026-08-17 (AI-295) -- les trois tests de la cascade de reconciliation
# (`_grp_row`, platform_priority, org_beats_platform, none_when_uncovered).
# Ils prouvaient que la specificite ORG > PLATFORM se reduisait correctement.
# C etait exact, et c est precisement le mecanisme retire : une regle appartient
# au Projet qui l a publiee, il n y a plus d echelle a monter. La cascade
# repondait pour un projet inexistant, mesure dans le commit precedent.


# ---------------------------------------------------------------------------
# Offline -- scope & reconciliation validation (mirrors the CHECK)
# ---------------------------------------------------------------------------


def test_scope_platform_rejects_org_id():
    """§19: PLATFORM with a non-null org_id is rejected (mirrors the CHECK)."""
    ms.validate_scope(ms.SCOPE_PLATFORM, None, None)  # accepted
    with pytest.raises(ms.InvalidScope):
        ms.validate_scope(ms.SCOPE_PLATFORM, "org_1", None)
    with pytest.raises(ms.InvalidScope):
        ms.validate_scope(ms.SCOPE_PLATFORM, None, "proj_1")


def test_scope_org_requires_org_id():
    """§20: ORG requires org_id and a NULL project_id."""
    ms.validate_scope(ms.SCOPE_ORG, "org_1", None)  # accepted
    with pytest.raises(ms.InvalidScope):
        ms.validate_scope(ms.SCOPE_ORG, None, None)
    with pytest.raises(ms.InvalidScope):
        ms.validate_scope(ms.SCOPE_ORG, "org_1", "proj_1")


def test_scope_project_requires_project_id():
    """§21: PROJECT requires project_id (org_id optional)."""
    ms.validate_scope(ms.SCOPE_PROJECT, None, "proj_1")  # accepted (org_id optional)
    ms.validate_scope(ms.SCOPE_PROJECT, "org_1", "proj_1")  # accepted
    with pytest.raises(ms.InvalidScope):
        ms.validate_scope(ms.SCOPE_PROJECT, "org_1", None)


def test_scope_unknown_level_rejected():
    """An unknown scope level is rejected."""
    with pytest.raises(ms.InvalidScope):
        ms.validate_scope("GALAXY", None, None)


def test_priority_rule_requires_priority_order():
    """§25: method=PRIORITY with an empty priority_order -> typed error."""
    with pytest.raises(ms.InvalidReconciliationRule):
        ms.validate_reconciliation_rule("PRIORITY", {"priority_order": []})
    ms.validate_reconciliation_rule("PRIORITY", {"priority_order": ["a"]})  # accepted


def test_dedup_rule_requires_join_key():
    """§26: method=DEDUP_ID with no join_key -> typed error."""
    with pytest.raises(ms.InvalidReconciliationRule):
        ms.validate_reconciliation_rule("DEDUP_ID", {"join_key": None})
    ms.validate_reconciliation_rule("DEDUP_ID", {"join_key": "transaction_id"})  # accepted


def test_unknown_method_rejected():
    """An unknown reconciliation method is rejected."""
    with pytest.raises(ms.InvalidReconciliationRule):
        ms.validate_reconciliation_rule("MAGIC", {})


# ---------------------------------------------------------------------------
# Offline -- idempotency guards (the change detectors that gate 'upserted' audit)
# ---------------------------------------------------------------------------


def test_definition_change_detector():
    """§23/§24: first write is a change; an identical re-UPSERT is not (no audit)."""
    after = {"id": "metdef_1", "canonical_name": "revenue", "additive": True,
             "created_at": "t0", "updated_at": "t0"}
    assert ms._definition_changed(None, after) is True  # create
    # Same content, only volatile fields differ -> not a change.
    before = dict(after, id="metdef_1", created_at="t-1", updated_at="t-1")
    assert ms._definition_changed(before, after) is False
    # A real semantic change is detected.
    changed = dict(after, additive=False)
    assert ms._definition_changed(before, changed) is True


# RETIRE 2026-08-17 (AI-295) -- `test_rule_change_detector`.
# `_rule_changed` gardait l idempotence de l upsert sur `reconciliation_rules`.
# Plus personne n ecrit cette table ; le detecteur de changement des DEFINITIONS
# (`test_definition_change_detector`, juste au-dessus) reste, lui, en service.


# ---------------------------------------------------------------------------
# Offline -- AD-2: no provider name hard-coded in the module (test façon 26.1)
# ---------------------------------------------------------------------------


def test_no_provider_name_in_module():
    """§27 (AD-2): metric_semantics.py contains no connector/provider name.

    Connector names must come from the seeds / DB rows, never from code. The seed FILE
    NAMES (dim_metric, metric_source_priority) are dbt artefacts, not provider names."""
    source = Path(ms.__file__).read_text(encoding="utf-8")
    lowered = source.lower()
    forbidden = [
        "google-analytics", "google_analytics", "meta-ads", "meta_ads",
        "tiktok", "linkedin", "shopify", "stripe", "adjust",
        "facebook", "pinterest", "amazon", "microsoft", "snapchat",
    ]
    hits = [name for name in forbidden if name in lowered]
    assert not hits, f"provider name(s) hard-coded in metric_semantics.py: {hits}"


# ---------------------------------------------------------------------------
# Live Postgres -- real DDL, idempotent import, CHECK, unicity, CASCADE, append-only
# ---------------------------------------------------------------------------

_MIGRATION_049 = _REPO_ROOT / "infra" / "nango" / "migrations" / "049_metric_semantics.sql"


def _apply_migration_049(conn) -> None:
    """Apply migration 049 idempotently to a throwaway test DB (replayable)."""
    sql = _MIGRATION_049.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def _ensure_set_updated_at(conn) -> None:
    """See `tests.support.updated_at_trigger`: ask before replacing."""
    ensure_set_updated_at(conn)


@pg_available
@pytest.mark.pg_owner
def test_ddl_creates_tables_replayable():
    """§28: the 5 tables + audit exist after 049; re-applying 049 is a no-op."""
    from core.db import get_connection

    with get_connection() as conn:
        _ensure_set_updated_at(conn)
        _apply_migration_049(conn)
        _apply_migration_049(conn)  # replay must not error
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'app'
                  AND table_name IN ('metric_definitions','source_metric_mappings',
                      'overlap_groups','overlap_group_members','reconciliation_rules',
                      'metric_semantics_audit')
                """
            )
            present = {r[0] for r in cur.fetchall()}
    assert present == {
        "metric_definitions", "source_metric_mappings", "overlap_groups",
        "overlap_group_members", "reconciliation_rules", "metric_semantics_audit",
    }


@pg_available
@pytest.mark.pg_owner
def test_scope_check_enforced():
    """§30: Postgres rejects inconsistent scope triplets."""
    import psycopg
    from core.db import get_connection

    with get_connection() as conn:
        _ensure_set_updated_at(conn)
        _apply_migration_049(conn)
        # PLATFORM with an org_id -> CHECK violation.
        for bad in (
            ("PLATFORM", "org_x", None),
            ("ORG", None, None),
            ("PROJECT", None, None),
        ):
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "INSERT INTO app.metric_definitions "
                        "(id, canonical_name, aggregation_type, additive, scope_level, "
                        " org_id, project_id, created_by) "
                        "VALUES (%s, 'm', 'sum', true, %s, %s, %s, 'system')",
                        (f"metdef_{uuid.uuid4().hex}", bad[0], bad[1], bad[2]),
                    )
                    conn.rollback()
                    raise AssertionError(f"scope {bad} should have been rejected")
                except psycopg.errors.CheckViolation:
                    conn.rollback()


@pg_available
@pytest.mark.pg_owner
def test_coalesce_unicity_on_platform():
    """§31: a second (PLATFORM, NULL, NULL, 'revenue') is rejected (COALESCE unicity)."""
    import psycopg
    from core.db import get_connection

    with get_connection() as conn:
        _ensure_set_updated_at(conn)
        _apply_migration_049(conn)
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.metric_definitions"
                " WHERE canonical_name = 'revenue' AND scope_level = 'PLATFORM'"
            )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.metric_definitions "
                "(id, canonical_name, aggregation_type, additive, scope_level, created_by) "
                "VALUES (%s, 'revenue', 'sum', true, 'PLATFORM', 'system')",
                (f"metdef_{uuid.uuid4().hex}",),
            )
        conn.commit()
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "INSERT INTO app.metric_definitions "
                    "(id, canonical_name, aggregation_type, additive, scope_level, created_by) "
                    "VALUES (%s, 'revenue', 'sum', true, 'PLATFORM', 'system')",
                    (f"metdef_{uuid.uuid4().hex}",),
                )
                conn.rollback()
                raise AssertionError("duplicate PLATFORM revenue should be rejected")
            except psycopg.errors.UniqueViolation:
                conn.rollback()
        # cleanup
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.metric_definitions"
                " WHERE canonical_name = 'revenue' AND scope_level = 'PLATFORM'"
            )
        conn.commit()


@pg_available
@pytest.mark.pg_owner
def test_import_platform_defaults_writes_definitions_and_nothing_else():
    """AI-295: the import defines metrics. It no longer writes a reconciliation.

    It used to upsert `app.overlap_groups` / `_members` / `reconciliation_rules`
    from `metric_source_priority.csv` plus a supplementary cost-verification
    group -- the SECOND reconciliation model, written on every org bootstrap
    through a route no screen calls. The runtime stopped reading those rows in
    Story 49.4 and the two read surfaces stopped the commit before this one, so a
    writer with no reader is a trap: the next reader finds rows and believes them.

    COUNTED FROM ZERO, not from a delta. The overlap-group assertion below is an
    absolute count taken after deleting what a previous run may have left. The
    test it replaces passed on leftover rows -- `cost-verification` was already in
    the database from an earlier test in the same session, so "exactly one group"
    was true without the import writing anything.
    """
    from core.db import get_connection

    with get_connection() as conn:
        _ensure_set_updated_at(conn)
        _apply_migration_049(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.overlap_groups WHERE scope_level = 'PLATFORM'")
        conn.commit()

    # Robustesse seeds mouvants: derive the expected count from the seed projection,
    # never hard-code it.
    projection = ms.platform_defaults_from_seeds(_SEEDS_DIR)
    n_defs = len(projection["definitions"])
    counts1 = ms.import_platform_defaults(seeds_dir=_SEEDS_DIR)
    assert counts1["definitions"] == n_defs
    assert counts1["groups"] == 0
    assert counts1["members"] == 0
    assert counts1["rules"] == 0

    # Re-run: same content, no duplicate rows.
    ms.import_platform_defaults(seeds_dir=_SEEDS_DIR)

    with get_connection() as conn:
        with conn.cursor() as cur:
                # SCOPED TO WHAT THE IMPORT OWNS (2026-08-16). This counted every
                # PLATFORM row of a SHARED table, so any fixture that ever wrote one
                # at that scope -- and one did, twice, because its cleanup ran in the
                # same transaction as a DELETE on an append-only ledger and was rolled
                # back with it -- made this measure the suite instead of the seed.
                # `created_by = 'system'` is what the importer stamps.
            cur.execute(
                "SELECT COUNT(*) FROM app.metric_definitions WHERE scope_level = 'PLATFORM' "
                "AND created_by = 'system'"
            )
            assert cur.fetchone()[0] == n_defs
            # THE GUARD: two imports, zero reconciliation rows.
            cur.execute(
                "SELECT COUNT(*) FROM app.overlap_groups WHERE scope_level = 'PLATFORM'"
            )
            assert cur.fetchone()[0] == 0, (
                "import_platform_defaults must not write the retired reconciliation store"
            )

    # §35: live re-export of PLATFORM definitions == the seed projection (bit-identical).
    live = ms.list_metric_definitions_by_scope(scope_level=ms.SCOPE_PLATFORM)
    live_by_name = {d["canonical_name"]: d for d in live}
    for expected in projection["definitions"]:
        got = live_by_name[expected["canonical_name"]]
        assert got["additive"] == expected["additive"]
        assert got["aggregation_type"] == expected["aggregation_type"]
        assert got["ratio_numerator"] == expected["ratio_numerator"]
        assert got["ratio_denominator"] == expected["ratio_denominator"]

    # The ordered seed is still READ -- by `controls_quality._seed_priorities`, which
    # turns it into the editable Project draft a governed publication starts from.
    # The seed was never the problem; the second live store was.
    from core.controls_quality import _seed_priorities

    seeded = _seed_priorities(_SEEDS_DIR)
    for group in projection["groups"]:
        assert seeded[group["canonical_name"]] == group["priority_order"]


# RETIRES 2026-08-17 (AI-295) -- `test_cost_pilot_imported_keep_separate` et
# `test_cost_pilot_idempotent_no_second_audit`. Ils prouvaient qu un groupe
# supplementaire etait ecrit, puis qu il n etait ecrit qu une fois. L ecrivain est
# parti : la reconciliation est le Rule Set gouverne, publie par un Projet.
# Le second passait d ailleurs sur des lignes laissees par un run precedent --
# `test_import_platform_defaults_writes_definitions_and_nothing_else` compte
# desormais a partir de zero.


@pg_available
@pytest.mark.pg_owner
def test_cost_is_defined_once_by_the_seed():
    """§35 (live): `cost` is defined once, from dim_metric.csv, and only there.

    Named after the pilot until AI-295, because the pilot was the thing that could
    have defined it a second time. The pilot is gone; the property it protected --
    one definition per canonical metric -- is not, so the test stays under a name
    that says what it checks."""
    from core.db import get_connection

    with get_connection() as conn:
        _ensure_set_updated_at(conn)
        _apply_migration_049(conn)
    ms.import_platform_defaults(seeds_dir=_SEEDS_DIR)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM app.metric_definitions "
                "WHERE scope_level = 'PLATFORM' AND canonical_name = 'cost'"
            )
            assert cur.fetchone()[0] == 1
                # SCOPED TO WHAT THE IMPORT OWNS (2026-08-16). This counted every
                # PLATFORM row of a SHARED table, so any fixture that ever wrote one
                # at that scope -- and one did, twice, because its cleanup ran in the
                # same transaction as a DELETE on an append-only ledger and was rolled
                # back with it -- made this measure the suite instead of the seed.
                # `created_by = 'system'` is what the importer stamps.
            cur.execute(
                "SELECT COUNT(*) FROM app.metric_definitions WHERE scope_level = 'PLATFORM' "
                "AND created_by = 'system'"
            )
            # DERIVED, not a literal. This said `15` while the seed produces 20 --
            # the seed grew and the number did not, so the test failed for the
            # catalogue's growth rather than for the duplicate it exists to forbid.
            assert cur.fetchone()[0] == len(
                ms.platform_defaults_from_seeds(_SEEDS_DIR)["definitions"]
            )


@pg_available
@pytest.mark.pg_owner
def test_fk_cascade_org_delete_keeps_platform():
    """§32/§33: deleting an org drops its ORG definitions; PLATFORM defaults survive."""
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_id = f"ms_org_{suffix}"
    with get_connection() as conn:
        _ensure_set_updated_at(conn)
        _apply_migration_049(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, created_by) "
                "VALUES (%s, %s, %s, 'system')",
                (org_id, f"MSOrg-{suffix}", f"ms-org-{suffix}"),
            )
        conn.commit()

    # A PLATFORM default + an ORG override for the same metric.
    ms.upsert_metric_definition(
        canonical_name=f"kpi_{suffix}", aggregation_type="sum", additive=True,
        scope_level=ms.SCOPE_PLATFORM, created_by="system",
    )
    ms.upsert_metric_definition(
        canonical_name=f"kpi_{suffix}", aggregation_type="sum", additive=False,
        scope_level=ms.SCOPE_ORG, org_id=org_id, created_by="system",
    )
    try:
        # Delete the org -> ORG row cascades away, PLATFORM survives.
        with get_connection() as conn:
            # Through the graph the production purge walks: `mdm_business_domains`
            # holds an org by ON DELETE RESTRICT, which a hand-written DELETE
            # cannot know and the next governed table would break again.
            purge_fixture_org(conn, org_id)
            conn.commit()

        assert ms.get_metric_definition(
            scope_level=ms.SCOPE_ORG, canonical_name=f"kpi_{suffix}", org_id=org_id
        ) is None
        assert ms.get_metric_definition(
            scope_level=ms.SCOPE_PLATFORM, canonical_name=f"kpi_{suffix}"
        ) is not None
    finally:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM app.metric_definitions WHERE canonical_name = %s",
                    (f"kpi_{suffix}",),
                )
            conn.commit()


@pg_available
@pytest.mark.pg_owner
def test_upsert_writes_audit_and_idempotent():
    """§22/§24: an upsert writes one audit row; a no-op re-upsert writes none."""
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    name = f"auditkpi_{suffix}"
    with get_connection() as conn:
        _ensure_set_updated_at(conn)
        _apply_migration_049(conn)

    ms.upsert_metric_definition(
        canonical_name=name, aggregation_type="sum", additive=True,
        scope_level=ms.SCOPE_PLATFORM, created_by="tester@example.com",
    )
    ms.upsert_metric_definition(  # identical -> no new audit
        canonical_name=name, aggregation_type="sum", additive=True,
        scope_level=ms.SCOPE_PLATFORM, created_by="tester@example.com",
    )
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT action, before, after FROM app.metric_semantics_audit "
                    "WHERE entity_type = 'metric_definition' "
                    "AND after->>'canonical_name' = %s ORDER BY created_at",
                    (name,),
                )
                rows = cur.fetchall()
        assert len(rows) == 1, "identical re-upsert must not emit a second audit row"
        assert rows[0][0] == "metric_definition.created"
        assert rows[0][1] is None  # before=None on create
        assert rows[0][2] is not None  # after present
    finally:
        # TWO TRANSACTIONS, and the order matters. These ran as one, and
        # `app.metric_semantics_audit` is APPEND-ONLY: its DELETE is refused --
        # correctly, an audit trail that can be erased is not one -- and the
        # refusal rolled the whole block back, so the DEFINITION survived too.
        # Two rows of PLATFORM-scoped fixture pollution accumulated that way, and
        # they are what made `test_import_platform_defaults` count 22 where the
        # seed produces 20.
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM app.metric_definitions WHERE canonical_name = %s", (name,)
                )
            conn.commit()


# RETIRE 2026-08-17 (AI-295) -- `test_set_overlap_group_members_is_additive_never_removes`.
# Il epinglait que `set_overlap_group_members` etait ADDITIVE : un membre retire
# d un appel ulterieur survivait. La fonction est partie avec le magasin qu elle
# ecrivait. L equivalent gouverne est ailleurs et se lit autrement : une version
# publiee EST son appartenance, donc il n y a plus d ajout partiel a proteger.


@pg_available
@pytest.mark.pg_owner
def test_upsert_idempotent_with_synonyms_and_non_additive_dims():
    """§24 (F-2): a re-upsert IDENTICAL to a row carrying synonyms (JSONB) and
    non_additive_dimensions (TEXT[]) emits NO second audit row.

    Guards against the JSONB/array round-trip making before/after diverge: after the
    first write, the stored synonyms come back as parsed JSON and the dims as a list;
    the change detector must treat an identical re-upsert as a no-op (exactly 1 audit)."""
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    name = f"synkpi_{suffix}"
    synonyms = [{"lang": "fr", "terms": ["CA"]}]
    non_additive = ["date"]
    with get_connection() as conn:
        _ensure_set_updated_at(conn)
        _apply_migration_049(conn)

    ms.upsert_metric_definition(
        canonical_name=name, aggregation_type="sum", additive=True,
        scope_level=ms.SCOPE_PLATFORM, created_by="tester@example.com",
        synonyms=synonyms, non_additive_dimensions=non_additive,
    )
    ms.upsert_metric_definition(  # byte-identical re-upsert -> must NOT emit a 2nd audit
        canonical_name=name, aggregation_type="sum", additive=True,
        scope_level=ms.SCOPE_PLATFORM, created_by="tester@example.com",
        synonyms=synonyms, non_additive_dimensions=non_additive,
    )
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM app.metric_semantics_audit "
                    "WHERE entity_type = 'metric_definition' "
                    "AND after->>'canonical_name' = %s",
                    (name,),
                )
                assert cur.fetchone()[0] == 1, (
                    "identical re-upsert with synonyms/non_additive_dimensions must not "
                    "emit a second audit (JSONB/array round-trip must not diverge)"
                )
    finally:
        # TWO TRANSACTIONS, and the order matters. These ran as one, and
        # `app.metric_semantics_audit` is APPEND-ONLY: its DELETE is refused --
        # correctly, an audit trail that can be erased is not one -- and the
        # refusal rolled the whole block back, so the DEFINITION survived too.
        # Two rows of PLATFORM-scoped fixture pollution accumulated that way, and
        # they are what made `test_import_platform_defaults` count 22 where the
        # seed produces 20.
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM app.metric_definitions WHERE canonical_name = %s", (name,)
                )
            conn.commit()


@pg_available
@pytest.mark.pg_owner
def test_audit_append_only_blocks_update_delete_truncate():
    """§34 (F-1): UPDATE/DELETE/TRUNCATE on the audit table RAISE (owner included).

    The REVOKE alone is ineffective against the owner role (the repo learned this in
    003/004), so 049 installs BEFORE UPDATE OR DELETE (row-level) + BEFORE TRUNCATE
    (statement-level) triggers that RAISE regardless of caller role. This is the only
    honest test: insert a real audit row, then prove every mutation path is refused."""
    import psycopg
    from core.db import get_connection

    audit_id = f"msaudit_{uuid.uuid4().hex}"
    with get_connection() as conn:
        _ensure_set_updated_at(conn)
        _apply_migration_049(conn)
        # Insert a real audit row to attempt mutations against.
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.metric_semantics_audit "
                "(id, identity, action, entity_type, entity_id, scope_level, after) "
                "VALUES (%s, 'system', 'metric_definition.created', 'metric_definition', "
                " 'metdef_x', 'PLATFORM', '{}'::jsonb)",
                (audit_id,),
            )
        conn.commit()

        try:
            # UPDATE must raise.
            with conn.cursor() as cur:
                with pytest.raises(psycopg.errors.RaiseException):
                    cur.execute(
                        "UPDATE app.metric_semantics_audit SET identity = 'hacker' "
                        "WHERE id = %s",
                        (audit_id,),
                    )
            conn.rollback()
            # DELETE must raise.
            with conn.cursor() as cur:
                with pytest.raises(psycopg.errors.RaiseException):
                    cur.execute(
                        "DELETE FROM app.metric_semantics_audit WHERE id = %s", (audit_id,)
                    )
            conn.rollback()
            # TRUNCATE must raise (statement-level guard; row triggers do not fire here).
            with conn.cursor() as cur:
                with pytest.raises(psycopg.errors.RaiseException):
                    cur.execute("TRUNCATE app.metric_semantics_audit")
            conn.rollback()
            # The row is still there -- nothing was mutated.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT identity FROM app.metric_semantics_audit WHERE id = %s",
                    (audit_id,),
                )
                assert cur.fetchone()[0] == "system"
        finally:
            # Disable the guard to clean up the test row (superuser/owner can drop it).
            with get_connection() as clean:
                with clean.cursor() as cur:
                    cur.execute(
                        "ALTER TABLE app.metric_semantics_audit DISABLE TRIGGER "
                        "metric_semantics_audit_append_only"
                    )
                    cur.execute(
                        "DELETE FROM app.metric_semantics_audit WHERE id = %s", (audit_id,)
                    )
                    cur.execute(
                        "ALTER TABLE app.metric_semantics_audit ENABLE TRIGGER "
                        "metric_semantics_audit_append_only"
                    )
                clean.commit()
