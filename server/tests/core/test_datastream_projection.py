"""Unit tests for the safe KPI projection compiler (Story 12.4).

Pure domain logic — no DB, warehouse, or clock. Every gate is asserted with
exact values (not shape-only): additive acceptance, non-additive rejection,
ungoverned/second-dimension rejection, mixed-grain rejection, the governed
cardinality/scan gate (over/under/approved), mapping-not-executable /
mapping-drift gates, and plan determinism (identical mapping -> identical plan).
"""

from __future__ import annotations

import copy

from core.datastream_projection import (
    DEFAULT_MAX_GRAIN_CARDINALITY,
    REJECT_CARDINALITY_OVER_LIMIT,
    REJECT_GOVERNED_DIM_SHADOWS_CANONICAL,
    REJECT_MAPPING_DRIFT,
    REJECT_MAPPING_NOT_EXECUTABLE,
    REJECT_MIXED_GRAIN,
    REJECT_NON_ADDITIVE_MEASURE,
    REJECT_SCAN_OVER_LIMIT,
    REJECT_UNGOVERNED_DIMENSION,
    compile_projection,
    resolve_thresholds,
)


def _field(field_id, *, physical_type, semantic_role, aggregation, non_additive,
           cardinality_signal="low", canonical_target=None, mdm_target=None,
           status="confirmed"):
    return {
        "field_id": field_id,
        "physical_type": physical_type,
        "profile": {
            "nullable": False,
            "unique": False,
            "cardinality_signal": cardinality_signal,
            "sample_values": [],
            "confidence": 0.9,
        },
        "suggestion": {
            "semantic_role": semantic_role,
            "aggregation": aggregation,
            "non_additive": non_additive,
            "currency": "unknown",
            "sensitivity": "none",
            "status": "suggested",
            "evidence": [],
        },
        "binding": {
            "canonical_target": canonical_target,
            "mdm_target": mdm_target,
            "status": status,
            "blocking_reason": None,
            "confirmed_by": "u",
            "confirmed_reason": "r",
        },
    }


def _mapping_version(fields, grain, *, executable=True, drift=None):
    payload = {
        "mapping_contract_version": "1",
        "source_schema_hash": "a" * 64,
        "plan_version_id": "dsp_01",
        "capability_fingerprint": "b" * 64,
        "grain": grain,
        "fields": fields,
        "ambiguities": [],
    }
    mv = {
        "id": "dmap_01",
        "plan_version_id": "dsp_01",
        "source_schema_hash": "a" * 64,
        "capability_fingerprint": "b" * 64,
        "executable": executable,
        "mapping_payload": payload,
    }
    if drift is not None:
        mv["drift"] = drift
    return mv


def _base_mapping():
    fields = [
        _field("date", physical_type="date", semantic_role="primary_date",
               aggregation="none", non_additive=False, canonical_target="date"),
        _field("country", physical_type="string", semantic_role="dimension",
               aggregation="none", non_additive=False, canonical_target="country"),
        _field("sessions", physical_type="integer", semantic_role="measure",
               aggregation="sum", non_additive=False, canonical_target="sessions"),
    ]
    return _mapping_version(fields, ["country", "date"])


# --------------------------------------------------------------------------- #
# AC2: additive-measure acceptance
# --------------------------------------------------------------------------- #

def test_additive_measure_accepted():
    plan = compile_projection(_base_mapping())
    assert plan["executable"] is True
    assert plan["issues"] == []
    assert len(plan["additive_measures"]) == 1
    m = plan["additive_measures"][0]
    assert m["field_id"] == "sessions"
    assert m["metric"] == "sessions"
    assert m["aggregation"] == "sum"
    assert m["non_additive"] is False


def test_full_grain_relation_preserves_all_dimensions_and_provenance():
    plan = compile_projection(_base_mapping())
    rel = plan["full_grain_relation"]
    # Every grain dimension kept as its own typed column (order = declared grain).
    assert [c["field_id"] for c in rel["grain_columns"]] == ["country", "date"]
    assert rel["grain_key"] == "grain_key"
    # Full provenance queryable on the relation.
    assert rel["provenance"] == {
        "execution_id": "execution_id",
        "pull_id": "pull_id",
        "mapping_version_id": "mapping_version_id",
        "plan_version_id": "plan_version_id",
        "loaded_at": "loaded_at",
    }
    assert set(rel["scope_columns"]) == {"project_id", "connector"}


# --------------------------------------------------------------------------- #
# AC2: non-additive rejection (NEVER summed; routed to semantic_*)
# --------------------------------------------------------------------------- #

def test_non_additive_measure_rejected_and_routed_to_semantic():
    fields = [
        _field("date", physical_type="date", semantic_role="primary_date",
               aggregation="none", non_additive=False),
        _field("average_position", physical_type="float", semantic_role="measure",
               aggregation="avg", non_additive=True, canonical_target="average_position"),
    ]
    plan = compile_projection(_mapping_version(fields, ["date"]))
    assert plan["executable"] is False
    # NEVER emitted as an additive measure.
    assert plan["additive_measures"] == []
    issue = next(i for i in plan["issues"] if i["code"] == REJECT_NON_ADDITIVE_MEASURE)
    assert issue["field_ids"] == ["average_position"]
    assert issue["repair"] == {"route_to_semantic": "semantic_view"}


def test_sum_but_flagged_non_additive_is_rejected():
    # aggregation='sum' is NOT sufficient: non_additive=true still rejects.
    fields = [
        _field("date", physical_type="date", semantic_role="primary_date",
               aggregation="none", non_additive=False),
        _field("ratio", physical_type="float", semantic_role="measure",
               aggregation="sum", non_additive=True),
    ]
    plan = compile_projection(_mapping_version(fields, ["date"]))
    assert plan["additive_measures"] == []
    assert any(i["code"] == REJECT_NON_ADDITIVE_MEASURE for i in plan["issues"])


# --------------------------------------------------------------------------- #
# AC2: governed dimension projection (exactly one)
# --------------------------------------------------------------------------- #

def test_one_governed_dimension_projection_accepted():
    # 'country' projected as a PARALLEL series: the connector's current canonical
    # ('brand') sorts strictly BEFORE 'country', so MIN(breakdown_dimension) still
    # pins 'brand' and the projected series never re-pins canonical -> accepted.
    plan = compile_projection(
        _base_mapping(),
        dimension_projection="country",
        connector_canonical_breakdown="brand",
    )
    assert plan["executable"] is True
    proj = plan["governed_dimension_projection"]
    assert proj["field_id"] == "country"
    assert proj["breakdown_dimension"] == "country"


def test_arbitrary_second_dimension_rejected():
    # 'campaign_id' is not a declared governed dimension of this mapping.
    plan = compile_projection(_base_mapping(), dimension_projection="campaign_id")
    assert plan["executable"] is False
    issue = next(i for i in plan["issues"] if i["code"] == REJECT_UNGOVERNED_DIMENSION)
    assert issue["field_ids"] == ["campaign_id"]
    # The repair names the governed choices available.
    assert "country" in issue["repair"]["choose_governed_dimension"]


def test_projecting_a_measure_as_dimension_rejected():
    plan = compile_projection(_base_mapping(), dimension_projection="sessions")
    assert plan["executable"] is False
    assert any(i["code"] == REJECT_UNGOVERNED_DIMENSION for i in plan["issues"])


# --------------------------------------------------------------------------- #
# HIGH (fresh-context review): a governed dimension whose breakdown name sorts
# AT/BEFORE the connector's canonical would be RE-PINNED as canonical by
# rollup.canonical_breakdown_per_connector / MIN(breakdown_dimension), shifting
# the additive hero KPI total (the Epic-10 xN failure mode). The compiler must
# REJECT it (governed_dim_shadows_canonical) and fail closed.
# --------------------------------------------------------------------------- #

def _mapping_with_dim(field_id, canonical_target):
    """A base mapping whose governed dimension projects to *canonical_target*."""
    fields = [
        _field("date", physical_type="date", semantic_role="primary_date",
               aggregation="none", non_additive=False, canonical_target="date"),
        _field(field_id, physical_type="string", semantic_role="dimension",
               aggregation="none", non_additive=False,
               canonical_target=canonical_target),
        _field("sessions", physical_type="integer", semantic_role="measure",
               aggregation="sum", non_additive=False, canonical_target="sessions"),
    ]
    return _mapping_version(fields, [field_id, "date"])


def test_governed_dim_sorting_before_canonical_is_rejected():
    """Discriminating NEGATIVE case: 'aaa_dim' sorts BEFORE the canonical 'country'.

    If projected it would win the lexicographic-MIN tie in
    canonical_breakdown_per_connector and re-pin the connector's canonical
    partition, inflating/shifting the additive hero total. Must be rejected.
    """
    mv = _mapping_with_dim("brk", "aaa_dim")
    plan = compile_projection(
        mv,
        dimension_projection="brk",
        connector_canonical_breakdown="country",
    )
    assert plan["executable"] is False
    assert plan["governed_dimension_projection"] is None
    issue = next(
        i for i in plan["issues"]
        if i["code"] == REJECT_GOVERNED_DIM_SHADOWS_CANONICAL
    )
    assert issue["field_ids"] == ["brk"]


def test_governed_dim_equal_to_canonical_is_rejected():
    """A breakdown name EQUAL to the canonical collides with the same slot."""
    mv = _mapping_with_dim("brk", "country")
    plan = compile_projection(
        mv,
        dimension_projection="brk",
        connector_canonical_breakdown="country",
    )
    assert plan["executable"] is False
    assert any(
        i["code"] == REJECT_GOVERNED_DIM_SHADOWS_CANONICAL for i in plan["issues"]
    )


def test_governed_dim_sorting_after_canonical_is_accepted():
    """The benign case: 'zzz_dim' sorts AFTER 'country' -> parallel series, accepted."""
    mv = _mapping_with_dim("brk", "zzz_dim")
    plan = compile_projection(
        mv,
        dimension_projection="brk",
        connector_canonical_breakdown="country",
    )
    assert plan["executable"] is True
    assert plan["governed_dimension_projection"]["breakdown_dimension"] == "zzz_dim"


def test_governed_dim_unknown_canonical_fails_closed():
    """Canonical unknown at compile time -> cannot prove parallel -> rejected."""
    mv = _mapping_with_dim("brk", "zzz_dim")
    plan = compile_projection(
        mv,
        dimension_projection="brk",
        connector_canonical_breakdown=None,
    )
    assert plan["executable"] is False
    assert any(
        i["code"] == REJECT_GOVERNED_DIM_SHADOWS_CANONICAL for i in plan["issues"]
    )


# --------------------------------------------------------------------------- #
# AC2: mixed-grain rejection (grain-bleed)
# --------------------------------------------------------------------------- #

def test_mixed_grain_projection_rejected():
    fields = [
        _field("day", physical_type="date", semantic_role="primary_date",
               aggregation="none", non_additive=False),
        _field("event_ts", physical_type="timestamp", semantic_role="primary_date",
               aggregation="none", non_additive=False),
        _field("sessions", physical_type="integer", semantic_role="measure",
               aggregation="sum", non_additive=False),
    ]
    plan = compile_projection(_mapping_version(fields, ["day", "event_ts"]))
    assert plan["executable"] is False
    issue = next(i for i in plan["issues"] if i["code"] == REJECT_MIXED_GRAIN)
    assert issue["field_ids"] == ["day", "event_ts"]


# --------------------------------------------------------------------------- #
# AC4: cardinality / scan gate
# --------------------------------------------------------------------------- #

def test_cardinality_over_limit_blocks_and_names_expensive_field():
    fields = [
        _field("date", physical_type="date", semantic_role="primary_date",
               aggregation="none", non_additive=False),
        _field("user_id", physical_type="string", semantic_role="dimension",
               aggregation="none", non_additive=False, cardinality_signal="unique"),
        _field("sessions", physical_type="integer", semantic_role="measure",
               aggregation="sum", non_additive=False),
    ]
    # A tiny governed threshold forces over-limit.
    prefs = {"max_projection_grain_cardinality": 10, "max_projection_scan_bytes": 10**18}
    plan = compile_projection(
        _mapping_version(fields, ["date", "user_id"]), project_preferences=prefs
    )
    assert plan["executable"] is False
    issue = next(i for i in plan["issues"] if i["code"] == REJECT_CARDINALITY_OVER_LIMIT)
    # The concrete expensive field is named -- never silently dropped.
    assert "user_id" in issue["field_ids"]
    est = plan["estimate"]
    assert est["over_limit"] is True
    assert est["threshold_source"] == "project_preference"
    assert est["estimated_grain_cardinality"] > est["max_grain_cardinality"]


def test_scan_over_limit_blocks():
    fields = [
        _field("date", physical_type="date", semantic_role="primary_date",
               aggregation="none", non_additive=False),
        _field("page", physical_type="string", semantic_role="dimension",
               aggregation="none", non_additive=False, cardinality_signal="high"),
    ]
    prefs = {"max_projection_grain_cardinality": 10**18, "max_projection_scan_bytes": 1}
    plan = compile_projection(
        _mapping_version(fields, ["date", "page"]), project_preferences=prefs
    )
    assert any(i["code"] == REJECT_SCAN_OVER_LIMIT for i in plan["issues"])


def test_over_limit_with_explicit_approval_passes_gate():
    fields = [
        _field("date", physical_type="date", semantic_role="primary_date",
               aggregation="none", non_additive=False),
        _field("user_id", physical_type="string", semantic_role="dimension",
               aggregation="none", non_additive=False, cardinality_signal="unique"),
    ]
    prefs = {"max_projection_grain_cardinality": 10, "max_projection_scan_bytes": 10}
    plan = compile_projection(
        _mapping_version(fields, ["date", "user_id"]),
        project_preferences=prefs,
        approved=True,
    )
    # Over-limit is surfaced honestly, but explicit approval clears the block.
    assert plan["estimate"]["over_limit"] is True
    assert plan["estimate"]["approved"] is True
    assert not any(
        i["code"] in (REJECT_CARDINALITY_OVER_LIMIT, REJECT_SCAN_OVER_LIMIT)
        for i in plan["issues"]
    )


def test_under_threshold_passes():
    plan = compile_projection(_base_mapping())
    assert plan["estimate"]["over_limit"] is False
    assert plan["estimate"]["threshold_source"] == "documented_default"
    assert plan["estimate"]["max_grain_cardinality"] == DEFAULT_MAX_GRAIN_CARDINALITY


def test_resolve_thresholds_documents_default_source():
    max_card, max_scan, source = resolve_thresholds(None)
    assert source == "documented_default"
    assert max_card == DEFAULT_MAX_GRAIN_CARDINALITY
    max_card2, _, source2 = resolve_thresholds(
        {"max_projection_grain_cardinality": 42}
    )
    assert source2 == "project_preference"
    assert max_card2 == 42


# --------------------------------------------------------------------------- #
# Gate 0: mapping executability / drift
# --------------------------------------------------------------------------- #

def test_non_executable_mapping_rejected():
    mv = _base_mapping()
    mv["executable"] = False
    plan = compile_projection(mv)
    assert plan["executable"] is False
    assert any(i["code"] == REJECT_MAPPING_NOT_EXECUTABLE for i in plan["issues"])


def test_drifted_mapping_rejected():
    mv = _base_mapping()
    mv["drift"] = {"source_schema_hash_changed": True, "capability_changed": False}
    plan = compile_projection(mv)
    assert any(i["code"] == REJECT_MAPPING_DRIFT for i in plan["issues"])


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #

def test_projection_is_deterministic_for_identical_mapping():
    mv1 = _base_mapping()
    mv2 = copy.deepcopy(_base_mapping())
    plan1 = compile_projection(
        mv1, dimension_projection="country", connector_canonical_breakdown="brand"
    )
    plan2 = compile_projection(
        mv2, dimension_projection="country", connector_canonical_breakdown="brand"
    )
    assert plan1 == plan2


def test_plan_validates_against_strict_schema():
    # A fully-valid compile returns a schema-valid plan (no ProjectionCompileError).
    plan = compile_projection(
        _base_mapping(),
        dimension_projection="country",
        connector_canonical_breakdown="brand",
    )
    assert plan["projection_contract_version"] == "1"
    assert plan["mapping_version_id"] == "dmap_01"
