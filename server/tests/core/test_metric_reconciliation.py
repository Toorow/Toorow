"""Tests for Story 27.3 -- metric routing resolver + conditional overlap gate (Epic 27).

Offline (no DB, no running server): the routing decision (fakes of the 27.1 cascade), the
conditional gate detect_unruled_overlaps (injected emitters + cascade), the AD-2 grep, and
the invariant-1/2 structural checks.

Live-Postgres (skipped when TEST_POSTGRES_DSN is unset): the real socle after
import_platform_defaults -- resolve_route reads the populated target_mart, the cost pilot
routes KEEP_SEPARATE, an ORG override shadows the PLATFORM default, and re-import keeps the
decision stable. Pattern calque sur test_dataset_access_grants.py / test_metric_semantics.py.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import metric_reconciliation as mr  # noqa: E402
from core import metric_semantics as ms  # noqa: E402

# ---------------------------------------------------------------------------
# Postgres availability check (calque sur test_metric_semantics.py)
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
_MIGRATION_049 = _REPO_ROOT / "infra" / "nango" / "migrations" / "049_metric_semantics.sql"


# ---------------------------------------------------------------------------
# Offline fakes -- injectable rule/definition/emitters/members
# ---------------------------------------------------------------------------


def _rule(method, *, target_mart=None, priority_order=None, scope_level="PLATFORM",
          overlap_group_id="ovg_test", join_key=None):
    return {
        "method": method,
        "scope_level": scope_level,
        "overlap_group_id": overlap_group_id,
        "priority_order": priority_order or [],
        "target_mart": target_mart,
        "join_key": join_key,
    }


def _rule_resolver(rule):
    """A cascade fake that always returns *rule* (or None)."""

    def _resolver(project_id, metric):
        return rule

    return _resolver


def _def_resolver(definition):
    def _resolver(project_id, metric):
        return definition

    return _resolver


def _emitters(*connectors):
    def _source(metric):
        return list(connectors)

    return _source


class _FakeModule:
    """A minimal LoadedModule stand-in (.name + .manifest) for emitter enumeration."""

    def __init__(self, name, canonical_metric_mapping):
        self.name = name
        self.manifest = {"canonical_metric_mapping": canonical_metric_mapping}


# ---------------------------------------------------------------------------
# Offline -- routing decision (fakes of the cascade, no DB)
# ---------------------------------------------------------------------------


def test_priority_conversions_routes_to_mart():
    """§1: PRIORITY conversions with an explicit target_mart -> ROUTED_TO_MART."""
    d = mr.resolve_route(
        "proj", "conversions",
        rule_resolver=_rule_resolver(
            _rule("PRIORITY", target_mart="cross_source_conversions",
                  priority_order=["google-analytics", "meta-ads"])
        ),
        definition_resolver=_def_resolver(None),
        emitters_source=_emitters(),
    )
    assert d.status == mr.RouteStatus.ROUTED_TO_MART
    assert d.target_mart == "cross_source_conversions"
    assert d.method == "PRIORITY"
    assert d.priority_order == ("google-analytics", "meta-ads")


def test_priority_revenue_routes_and_keeps_order():
    """§2: PRIORITY revenue -> cross_source_revenue, priority_order preserved (ordered)."""
    d = mr.resolve_route(
        "proj", "revenue",
        rule_resolver=_rule_resolver(
            _rule("PRIORITY", target_mart="cross_source_revenue",
                  priority_order=["shopify", "stripe", "adjust"])
        ),
        definition_resolver=_def_resolver(None),
    )
    assert d.target_mart == "cross_source_revenue"
    assert d.priority_order == ("shopify", "stripe", "adjust")


def test_dedup_id_routes_to_transaction_reconciliation():
    """§3: DEDUP_ID with join_key='transaction_id' -> transaction_reconciliation."""
    d = mr.resolve_route(
        "proj", "conversions",
        rule_resolver=_rule_resolver(
            _rule("DEDUP_ID", target_mart="transaction_reconciliation",
                  join_key="transaction_id")
        ),
        definition_resolver=_def_resolver(None),
    )
    assert d.status == mr.RouteStatus.ROUTED_TO_MART
    assert d.target_mart == "transaction_reconciliation"
    assert d.method == "DEDUP_ID"


def test_estimate_routes_to_dedup_estimate():
    """§4: ESTIMATE -> dedup_estimate."""
    d = mr.resolve_route(
        "proj", "conversions",
        rule_resolver=_rule_resolver(_rule("ESTIMATE", target_mart="dedup_estimate")),
        definition_resolver=_def_resolver(None),
    )
    assert d.status == mr.RouteStatus.ROUTED_TO_MART
    assert d.target_mart == "dedup_estimate"


def test_keep_separate_yields_series_no_total():
    """§5: KEEP_SEPARATE (cost) -> one SourceSeries per member, sorted, NO target_mart."""
    d = mr.resolve_route(
        "proj", "cost",
        rule_resolver=_rule_resolver(_rule("KEEP_SEPARATE", overlap_group_id="ovg_cost")),
        definition_resolver=_def_resolver({"additive": True}),
        members_source=lambda gid: ["ias", "doubleverify"],
    )
    assert d.status == mr.RouteStatus.KEEP_SEPARATE
    assert d.method == "KEEP_SEPARATE"
    assert d.target_mart is None
    assert [s.connector for s in d.series] == ["doubleverify", "ias"]  # sorted
    assert all(s.canonical_name == "cost" for s in d.series)


def test_additive_no_group_one_emitter_direct_sum():
    """§6: additive metric, no group, ONE emitter -> DIRECT_SUM, no reason (invariant 5)."""
    d = mr.resolve_route(
        "proj", "installs",
        rule_resolver=_rule_resolver(None),
        definition_resolver=_def_resolver({"additive": True}),
        emitters_source=_emitters("adjust"),
    )
    assert d.status == mr.RouteStatus.DIRECT_SUM
    assert d.reason is None
    assert d.target_mart is None


def test_additive_no_group_zero_emitter_direct_sum():
    """§7: additive, no group, ZERO emitter -> DIRECT_SUM (fail-soft)."""
    d = mr.resolve_route(
        "proj", "sessions",
        rule_resolver=_rule_resolver(None),
        definition_resolver=_def_resolver({"additive": True}),
        emitters_source=_emitters(),
    )
    assert d.status == mr.RouteStatus.DIRECT_SUM


def test_non_additive_no_group_not_combinable():
    """§8: non-additive (roas/average_position), no group, >=1 emitter -> NOT_COMBINABLE."""
    d = mr.resolve_route(
        "proj", "roas",
        rule_resolver=_rule_resolver(None),
        definition_resolver=_def_resolver({"additive": False}),
        emitters_source=_emitters("meta-ads"),
    )
    assert d.status == mr.RouteStatus.NOT_COMBINABLE
    assert d.reason.code == mr.CODE_NON_ADDITIVE_NO_RULE
    assert d.target_mart is None


def test_priority_null_target_unknown_mart_falls_through():
    """§9 guard rail: PRIORITY with NULL target_mart and no known cross_source_<metric>
    -> NOT routed (falls back per additivity)."""
    # 'installs' has no cross_source_installs mart -> derivation yields None -> fall-through.
    d_additive = mr.resolve_route(
        "proj", "installs",
        rule_resolver=_rule_resolver(_rule("PRIORITY", target_mart=None,
                                           priority_order=["adjust"])),
        definition_resolver=_def_resolver({"additive": True}),
        emitters_source=_emitters("adjust"),
    )
    assert d_additive.status != mr.RouteStatus.ROUTED_TO_MART
    assert d_additive.status == mr.RouteStatus.DIRECT_SUM  # 1 emitter, additive

    # Same, but >=2 emitters -> UNRULED_OVERLAP.
    d_overlap = mr.resolve_route(
        "proj", "installs",
        rule_resolver=_rule_resolver(_rule("PRIORITY", target_mart=None,
                                           priority_order=["a"])),
        definition_resolver=_def_resolver({"additive": True}),
        emitters_source=_emitters("a", "b"),
    )
    assert d_overlap.status == mr.RouteStatus.UNRULED_OVERLAP


def test_override_priority_org_not_materialized():
    """F-1: an ORG PRIORITY override with a DIFFERENT order than the PLATFORM seed does NOT
    route to the (PLATFORM-frozen) mart -> OVERRIDE_NOT_MATERIALIZED, never ROUTED_TO_MART.

    The pre-computed cross_source_* marts LEFT JOIN the seed priority (PLATFORM). Routing an
    override would silently apply the WRONG priority (AD-9). We refuse and hand back the
    honest per-source series + a structured reason, no target_mart, no total."""
    d = mr.resolve_route(
        "proj", "conversions",
        rule_resolver=_rule_resolver(
            _rule("PRIORITY", target_mart="cross_source_conversions",
                  scope_level="ORG",
                  # a DIFFERENT order than the PLATFORM seed default.
                  priority_order=["meta-ads", "google-analytics"]),
        ),
        definition_resolver=_def_resolver({"additive": True}),
        members_source=lambda gid: ["google-analytics", "meta-ads"],
    )
    assert d.status == mr.RouteStatus.OVERRIDE_NOT_MATERIALIZED
    assert d.status != mr.RouteStatus.ROUTED_TO_MART
    assert d.target_mart is None  # never a wrong number (AD-9)
    assert d.method == "PRIORITY"
    assert d.scope_level == "ORG"
    assert d.reason is not None
    assert d.reason.code == mr.CODE_OVERRIDE_NOT_MATERIALIZED
    # honest per-source view (like KEEP_SEPARATE): one series per member, sorted.
    assert [s.connector for s in d.series] == ["google-analytics", "meta-ads"]
    assert d.priority_order == ("meta-ads", "google-analytics")  # override order preserved


def test_override_priority_project_not_materialized():
    """F-1: a PROJECT PRIORITY override is likewise NOT materialized (only PLATFORM routes)."""
    d = mr.resolve_route(
        "proj", "revenue",
        rule_resolver=_rule_resolver(
            _rule("PRIORITY", target_mart="cross_source_revenue",
                  scope_level="PROJECT", priority_order=["stripe", "shopify"]),
        ),
        definition_resolver=_def_resolver({"additive": True}),
        members_source=lambda gid: ["shopify", "stripe"],
    )
    assert d.status == mr.RouteStatus.OVERRIDE_NOT_MATERIALIZED
    assert d.target_mart is None


def test_override_dedup_estimate_org_not_materialized():
    """F-1: DEDUP_ID/ESTIMATE overrides at ORG scope also fall to OVERRIDE_NOT_MATERIALIZED."""
    for method, mart, jk in (("DEDUP_ID", "transaction_reconciliation", "transaction_id"),
                             ("ESTIMATE", "dedup_estimate", None)):
        d = mr.resolve_route(
            "proj", "conversions",
            rule_resolver=_rule_resolver(
                _rule(method, target_mart=mart, scope_level="ORG", join_key=jk),
            ),
            definition_resolver=_def_resolver({"additive": True}),
            members_source=lambda gid: ["src_a", "src_b"],
        )
        assert d.status == mr.RouteStatus.OVERRIDE_NOT_MATERIALIZED, method
        assert d.target_mart is None, method


def test_platform_scope_priority_still_routes():
    """F-1 (mirror): a PLATFORM-scope PRIORITY rule STILL routes to the mart (ssi PLATFORM).

    Only PLATFORM is materialized by the pre-computed marts, so the PLATFORM default routes
    exactly as before -- the F-1 guard rail is scoped to overrides only."""
    d = mr.resolve_route(
        "proj", "conversions",
        rule_resolver=_rule_resolver(
            _rule("PRIORITY", target_mart="cross_source_conversions",
                  scope_level="PLATFORM",
                  priority_order=["google-analytics", "meta-ads"]),
        ),
        definition_resolver=_def_resolver(None),
    )
    assert d.status == mr.RouteStatus.ROUTED_TO_MART
    assert d.target_mart == "cross_source_conversions"
    assert d.scope_level == "PLATFORM"


def test_dedup_derives_target_when_absent():
    """§10: DEDUP_ID (join_key='transaction_id') with an ABSENT target_mart still derives
    transaction_reconciliation."""
    d = mr.resolve_route(
        "proj", "conversions",
        rule_resolver=_rule_resolver(
            _rule("DEDUP_ID", target_mart=None, join_key="transaction_id")
        ),
        definition_resolver=_def_resolver(None),
    )
    assert d.status == mr.RouteStatus.ROUTED_TO_MART
    assert d.target_mart == "transaction_reconciliation"


def test_dedup_id_wrong_join_key_does_not_route():
    """D-1 (honesty): DEDUP_ID with join_key='order_id' does NOT route to the mart.

    The transaction_reconciliation mart joins on transaction_id ONLY; a DEDUP_ID rule on a
    different key targets data the mart never materializes, so we take the honest no-route
    path instead of emitting a figure computed on the wrong join."""
    d = mr.resolve_route(
        "proj", "conversions",
        rule_resolver=_rule_resolver(
            _rule("DEDUP_ID", target_mart="transaction_reconciliation",
                  join_key="order_id"),
        ),
        definition_resolver=_def_resolver({"additive": True}),
        emitters_source=_emitters("src_a", "src_b"),
    )
    assert d.status != mr.RouteStatus.ROUTED_TO_MART
    assert d.target_mart is None


def test_estimate_route_carries_designated_truth_note():
    """D-2 (honesty): the ESTIMATE route carries the structured 'requires designated truth'
    note so the consumer knows the dim_project precondition it must verify."""
    d = mr.resolve_route(
        "proj", "conversions",
        rule_resolver=_rule_resolver(
            _rule("ESTIMATE", target_mart="dedup_estimate", scope_level="PLATFORM")
        ),
        definition_resolver=_def_resolver(None),
    )
    assert d.status == mr.RouteStatus.ROUTED_TO_MART
    assert d.reason is not None
    assert d.reason.code == mr.CODE_ESTIMATE_REQUIRES_DESIGNATED_TRUTH
    assert "dim_project" in d.reason.message


def test_keep_separate_carries_do_not_sum_reason():
    """D-5 (honesty): KEEP_SEPARATE carries an explicit FR reason -- never sum the series."""
    d = mr.resolve_route(
        "proj", "cost",
        rule_resolver=_rule_resolver(_rule("KEEP_SEPARATE", overlap_group_id="ovg_cost")),
        definition_resolver=_def_resolver({"additive": True}),
        members_source=lambda gid: ["ias", "doubleverify"],
    )
    assert d.status == mr.RouteStatus.KEEP_SEPARATE
    assert d.reason is not None
    assert d.reason.code == mr.CODE_KEEP_SEPARATE
    assert "additionner" in d.reason.message.lower()


def test_missing_project_falls_back_platform_defaults():
    """§11: an unknown project_id -> decision on PLATFORM defaults (fail-soft, no crash).

    Simulated by a rule_resolver that returns the PLATFORM rule regardless of project.
    """
    d = mr.resolve_route(
        "does_not_exist", "conversions",
        rule_resolver=_rule_resolver(
            _rule("PRIORITY", target_mart="cross_source_conversions",
                  priority_order=["google-analytics"])
        ),
        definition_resolver=_def_resolver(None),
    )
    assert d.status == mr.RouteStatus.ROUTED_TO_MART


def test_route_decision_is_deterministic():
    """§12: two resolve_route calls yield EQUAL RouteDecision (frozen dataclass ==)."""
    kwargs = dict(
        rule_resolver=_rule_resolver(_rule("KEEP_SEPARATE", overlap_group_id="g")),
        definition_resolver=_def_resolver({"additive": True}),
        members_source=lambda gid: ["ias", "doubleverify"],
    )
    d1 = mr.resolve_route("proj", "cost", **kwargs)
    d2 = mr.resolve_route("proj", "cost", **kwargs)
    assert d1 == d2


def test_route_decision_is_frozen_and_hashable():
    """§13: RouteDecision is immutable (frozen) and hashable; series/priority are tuples."""
    d = mr.resolve_route(
        "proj", "conversions",
        rule_resolver=_rule_resolver(
            _rule("PRIORITY", target_mart="cross_source_conversions", priority_order=["a"])
        ),
        definition_resolver=_def_resolver(None),
    )
    assert isinstance(d.priority_order, tuple)
    assert isinstance(d.series, tuple)
    hash(d)  # must not raise
    with pytest.raises(Exception):
        d.status = "MUTATED"  # frozen -> FrozenInstanceError


def test_keep_separate_series_are_tuples_of_sourceseries():
    """§13 (series shape): KEEP_SEPARATE series is a tuple of frozen SourceSeries."""
    d = mr.resolve_route(
        "proj", "cost",
        rule_resolver=_rule_resolver(_rule("KEEP_SEPARATE")),
        definition_resolver=_def_resolver({"additive": True}),
        members_source=lambda gid: ["b", "a"],
    )
    assert isinstance(d.series, tuple)
    assert all(isinstance(s, mr.SourceSeries) for s in d.series)
    with pytest.raises(Exception):
        d.series[0].connector = "x"  # frozen SourceSeries


# ---------------------------------------------------------------------------
# Offline -- conditional gate detect_unruled_overlaps (emitters + cascade injected)
# ---------------------------------------------------------------------------


def test_gate_two_emitters_no_rule_warns():
    """§14: >=2 emitters, no rule -> one UNRULED_OVERLAP reason (combination unavailable)."""
    reasons = mr.detect_unruled_overlaps(
        "proj",
        rule_resolver=_rule_resolver(None),
        emitters_source={"impressions": ["a", "b"]},
    )
    assert len(reasons) == 1
    assert reasons[0].code == mr.CODE_UNRULED_OVERLAP
    assert reasons[0].emitters == ("a", "b")


def test_gate_one_emitter_no_warning():
    """§15: a single emitter, no rule -> NO reason (invariant 5)."""
    reasons = mr.detect_unruled_overlaps(
        "proj", rule_resolver=_rule_resolver(None),
        emitters_source={"cost": ["ias"]},
    )
    assert reasons == []


def test_gate_rule_present_no_warning():
    """§16: >=2 emitters but a PRIORITY rule exists -> NO reason."""
    reasons = mr.detect_unruled_overlaps(
        "proj",
        rule_resolver=_rule_resolver(_rule("PRIORITY", priority_order=["a"])),
        emitters_source={"conversions": ["a", "b"]},
    )
    assert reasons == []


def test_gate_keep_separate_rule_no_warning():
    """§17: >=2 emitters but a KEEP_SEPARATE rule exists -> NO reason (overlap managed)."""
    reasons = mr.detect_unruled_overlaps(
        "proj",
        rule_resolver=_rule_resolver(_rule("KEEP_SEPARATE")),
        emitters_source={"cost": ["ias", "doubleverify"]},
    )
    assert reasons == []


def test_gate_disjoint_groups_resolved_no_warning():
    """§18: disjoint groups resolved by the cascade (a rule returned) -> NO reason."""
    reasons = mr.detect_unruled_overlaps(
        "proj",
        rule_resolver=_rule_resolver(_rule("SUM")),  # cascade resolves ONE -> not None
        emitters_source={"revenue": ["a", "b"]},
    )
    assert reasons == []


def test_emitters_sorted_deduped_and_dict_canonical():
    """§19: emitters_of sorts/dedupes and extracts the 'canonical' dict case."""
    modules = [
        _FakeModule("beta", {"conv": "conversions", "pos": {"canonical": "average_position"}}),
        _FakeModule("alpha", {"c2": "conversions"}),
        _FakeModule("beta_dup", {"c3": "conversions"}),
    ]
    got = mr.emitters_of("conversions", modules=modules)
    assert got == ("alpha", "beta", "beta_dup")  # sorted, deduped
    assert mr.emitters_of("average_position", modules=modules) == ("beta",)


def test_gate_unions_db_members_without_loaded_module():
    """F-2 (A.4): a member declared in overlap_group_members WITHOUT a loaded module counts
    as an emitter -> UNION with the manifests -> UNRULED when >=2 sources and no rule.

    One emitter comes from a loaded manifest, the OTHER only from the DB membership (no
    module). Alone, the manifest read sees a single source (no warning); the union makes it
    two -> the gate fires. members_reader is injected (fake DB) so this is offline."""
    modules = [_FakeModule("alpha", {"imp": "impressions"})]  # manifest emitter only
    # 'beta' is a declared member of the impressions group but has NO loaded module.
    members_reader = lambda: [("impressions", "beta")]  # noqa: E731
    reasons = mr.detect_unruled_overlaps(
        "proj",
        rule_resolver=_rule_resolver(None),
        modules=modules,
        members_reader=members_reader,
    )
    assert len(reasons) == 1
    assert reasons[0].code == mr.CODE_UNRULED_OVERLAP
    assert reasons[0].emitters == ("alpha", "beta")  # union, sorted


def test_gate_db_union_needs_two_sources():
    """F-2: a single DB-declared member (no co-emitter) stays below the >=2 threshold."""
    reasons = mr.detect_unruled_overlaps(
        "proj",
        rule_resolver=_rule_resolver(None),
        modules=[],  # no manifest emitter
        members_reader=lambda: [("cost", "cm360")],  # one declared member only
    )
    assert reasons == []


def test_emitters_of_unions_db_members():
    """F-2: emitters_of UNIONs the manifest emitters with the DB-declared members (sorted)."""
    modules = [_FakeModule("alpha", {"c": "conversions"})]
    got = mr.emitters_of(
        "conversions", modules=modules,
        members_reader=lambda: [("conversions", "beta"), ("revenue", "gamma")],
    )
    assert got == ("alpha", "beta")  # manifest + DB member, sorted; gamma is a diff metric


def test_gate_db_union_fail_soft_when_reader_raises():
    """F-2: a members_reader that raises degrades to the manifests only (never an exception).

    The gate must not crash if the DB read fails -- fail-soft to the manifest-only view."""
    def _boom():
        raise RuntimeError("db down")

    modules = [_FakeModule("alpha", {"imp": "impressions"}),
               _FakeModule("beta", {"imp2": "impressions"})]
    reasons = mr.detect_unruled_overlaps(
        "proj", rule_resolver=_rule_resolver(None),
        modules=modules, members_reader=_boom,
    )
    # DB read failed -> manifests only, but alpha+beta already overlap -> still warns once.
    assert len(reasons) == 1
    assert reasons[0].emitters == ("alpha", "beta")


def test_gate_returns_list_never_raises():
    """§20: detect_unruled_overlaps returns a LIST, never raises, no side effect."""
    out = mr.detect_unruled_overlaps(
        "proj", rule_resolver=_rule_resolver(None), emitters_source={}
    )
    assert isinstance(out, list)
    assert out == []


def test_pilot_cost_today_silent():
    """§21: DV/IAS as they REALLY are (no 'cost' emission) -> gate silent on cost.

    Reflects the verified manifests: DV/IAS emit verification metrics, not 'cost'. The
    cost-verification pilot exists to carry the rule, not to fire a premature warning."""
    modules = [
        _FakeModule("doubleverify", {
            "measured_impressions": "measured_impressions",
            "viewable_impressions": "viewable_impressions",
            "authentic_ads": "authentic_ads",
        }),
        _FakeModule("ias", {
            "measuredImps": "measured_impressions",
            "viewableImps": "viewable_impressions",
        }),
    ]
    reasons = mr.detect_unruled_overlaps(
        "proj", rule_resolver=_rule_resolver(None), modules=modules
    )
    # measured_impressions / viewable_impressions ARE co-emitted -> those overlap (correct),
    # but NO reason mentions 'cost' (neither source emits it).
    emitted_metrics = {m for r in reasons for m in [_metric_of(r)]}
    assert "cost" not in emitted_metrics


def _metric_of(reason: mr.ReconciliationReason) -> str:
    """Extract the quoted metric name from a reason message (test helper)."""
    msg = reason.message
    if "'" in msg:
        return msg.split("'")[1]
    return ""


# ---------------------------------------------------------------------------
# Offline -- AD-2 & invariants (grep of the module source)
# ---------------------------------------------------------------------------


def test_no_provider_name_in_module():
    """§22 (AD-2): metric_reconciliation.py hard-codes no connector/provider name.

    Mart names (cross_source_*, transaction_reconciliation, dedup_estimate) are dbt
    artefacts and tolerated (like the seed file names). This module reads emitters from the
    registry -- no verifier/connector name lives in the code."""
    source = Path(mr.__file__).read_text(encoding="utf-8").lower()
    forbidden = [
        "google-analytics", "google_analytics", "meta-ads", "meta_ads",
        "tiktok", "linkedin", "shopify", "stripe", "adjust",
        "facebook", "pinterest", "snapchat",
        "doubleverify", "ias", "cm360",  # the pilot verifiers live in metric_semantics, not here
    ]
    hits = [name for name in forbidden if name in source]
    assert not hits, f"provider name(s) hard-coded in metric_reconciliation.py: {hits}"


def test_invariant2_keep_separate_and_unruled_never_total():
    """§23 (invariant 2): KEEP_SEPARATE and UNRULED_OVERLAP expose series, never a total."""
    ks = mr.resolve_route(
        "proj", "cost",
        rule_resolver=_rule_resolver(_rule("KEEP_SEPARATE")),
        definition_resolver=_def_resolver({"additive": True}),
        members_source=lambda gid: ["a", "b"],
    )
    assert ks.target_mart is None
    assert len(ks.series) == 2

    unruled = mr.resolve_route(
        "proj", "impressions",
        rule_resolver=_rule_resolver(None),
        definition_resolver=_def_resolver({"additive": True}),
        emitters_source=_emitters("a", "b"),
    )
    assert unruled.target_mart is None
    assert unruled.reason is not None
    assert len(unruled.series) == 2


def test_invariant1_no_warehouse_read():
    """§24 (invariant 1): the module imports no fact_daily_kpi and opens no warehouse cursor.

    Grep the source: no reference to fact_daily_kpi or a marts .sql, and no metric value is
    computed -- the module ROUTES names only."""
    source = Path(mr.__file__).read_text(encoding="utf-8").lower()
    assert "fact_daily_kpi" not in source
    assert ".sql" not in source
    # The only DB access is the read of overlap_group_members (topology), never a fact read.
    assert "from app.overlap_group_members" in source
    assert "fact_" not in source


def test_known_marts_are_the_four_targets():
    """The KNOWN_MARTS allow-list is exactly the four routed marts (heads read in 27.3)."""
    assert mr.KNOWN_MARTS == frozenset({
        "cross_source_conversions", "cross_source_revenue",
        "transaction_reconciliation", "dedup_estimate",
    })


def test_derive_target_mart_declarative():
    """_derive_target_mart routes declaratively (A.5) and never invents a mart."""
    assert mr._derive_target_mart("PRIORITY", "conversions") == "cross_source_conversions"
    assert mr._derive_target_mart("PRIORITY", "revenue") == "cross_source_revenue"
    assert mr._derive_target_mart("PRIORITY", "installs") is None  # no such mart
    # D-1: DEDUP_ID only derives the mart when join_key == 'transaction_id'.
    assert mr._derive_target_mart(
        "DEDUP_ID", "anything", {"join_key": "transaction_id"}
    ) == "transaction_reconciliation"
    assert mr._derive_target_mart("DEDUP_ID", "anything", {"join_key": "order_id"}) is None
    assert mr._derive_target_mart("DEDUP_ID", "anything", {}) is None  # no join_key -> None
    assert mr._derive_target_mart("ESTIMATE", "anything") == "dedup_estimate"
    assert mr._derive_target_mart("KEEP_SEPARATE", "cost") is None


# ---------------------------------------------------------------------------
# Live Postgres -- the real socle after import_platform_defaults
# ---------------------------------------------------------------------------


def _ensure_set_updated_at(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS app")
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION app.set_updated_at() RETURNS trigger AS $$
            BEGIN NEW.updated_at = now(); RETURN NEW; END;
            $$ LANGUAGE plpgsql
            """
        )
    conn.commit()


def _apply_migration_049(conn) -> None:
    sql = _MIGRATION_049.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def _bootstrap_platform(conn) -> None:
    _ensure_set_updated_at(conn)
    _apply_migration_049(conn)


@pg_available
def test_live_route_conversions_to_mart():
    """§25: import then resolve_route(project, 'conversions') -> ROUTED_TO_MART, mart set."""
    from core.db import get_connection

    with get_connection() as conn:
        _bootstrap_platform(conn)
    ms.import_platform_defaults(seeds_dir=_SEEDS_DIR)

    d = mr.resolve_route("no_such_project", "conversions")
    assert d.status == mr.RouteStatus.ROUTED_TO_MART
    assert d.target_mart == "cross_source_conversions"
    assert d.method == "PRIORITY"


@pg_available
def test_live_route_revenue_to_mart():
    """§26: resolve_route(project, 'revenue') -> cross_source_revenue."""
    from core.db import get_connection

    with get_connection() as conn:
        _bootstrap_platform(conn)
    ms.import_platform_defaults(seeds_dir=_SEEDS_DIR)

    d = mr.resolve_route("no_such_project", "revenue")
    assert d.status == mr.RouteStatus.ROUTED_TO_MART
    assert d.target_mart == "cross_source_revenue"


@pg_available
def test_live_route_cost_keep_separate():
    """§27: resolve_route(project, 'cost') -> KEEP_SEPARATE, series {doubleverify, ias}."""
    from core.db import get_connection

    with get_connection() as conn:
        _bootstrap_platform(conn)
    ms.import_platform_defaults(seeds_dir=_SEEDS_DIR)

    d = mr.resolve_route("no_such_project", "cost")
    assert d.status == mr.RouteStatus.KEEP_SEPARATE
    assert d.target_mart is None
    assert {s.connector for s in d.series} == {"doubleverify", "ias"}


@pg_available
def test_live_org_override_shadows_platform():
    """§28: an ORG KEEP_SEPARATE group on a metric shadows the PLATFORM default (scope=ORG)."""
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_id = f"mr_org_{suffix}"
    project_id = f"mr_proj_{suffix}"
    with get_connection() as conn:
        _bootstrap_platform(conn)
    ms.import_platform_defaults(seeds_dir=_SEEDS_DIR)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, created_by) "
                "VALUES (%s, %s, %s, 'system')",
                (org_id, f"MROrg-{suffix}", f"mr-org-{suffix}"),
            )
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, created_by) "
                "VALUES (%s, %s, %s, 'system')",
                (project_id, org_id, f"MRProj-{suffix}"),
            )
        conn.commit()

    try:
        # An ORG KEEP_SEPARATE group on 'conversions' shadows the PLATFORM PRIORITY default.
        grp = ms.upsert_overlap_group(
            canonical_name="conversions", name=f"conv-org-{suffix}",
            scope_level=ms.SCOPE_ORG, org_id=org_id, created_by="system",
        )
        ms.set_overlap_group_members(
            overlap_group_id=grp["id"], connectors=["src_a", "src_b"],
            created_by="system", scope_level=ms.SCOPE_ORG, org_id=org_id,
        )
        ms.upsert_reconciliation_rule(
            overlap_group_id=grp["id"], method=ms.METHOD_KEEP_SEPARATE,
            scope_level=ms.SCOPE_ORG, org_id=org_id, created_by="system",
        )

        d = mr.resolve_route(project_id, "conversions")
        assert d.status == mr.RouteStatus.KEEP_SEPARATE
        assert d.scope_level == ms.SCOPE_ORG
        assert {s.connector for s in d.series} == {"src_a", "src_b"}
    finally:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM app.projects WHERE id = %s", (project_id,))
                cur.execute("DELETE FROM app.organizations WHERE id = %s", (org_id,))
            conn.commit()


@pg_available
def test_live_reimport_stable_decision():
    """§29: re-import_platform_defaults leaves the routing decision unchanged (idempotent)."""
    from core.db import get_connection

    with get_connection() as conn:
        _bootstrap_platform(conn)
    ms.import_platform_defaults(seeds_dir=_SEEDS_DIR)
    before = mr.resolve_route("no_such_project", "conversions")
    ms.import_platform_defaults(seeds_dir=_SEEDS_DIR)
    after = mr.resolve_route("no_such_project", "conversions")
    assert before == after
