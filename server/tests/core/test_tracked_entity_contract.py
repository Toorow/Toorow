"""Story 48.5, AC3: a Connector says whether it can see tracked entities, or nothing is assumed.

The failure this file exists to prevent is the cheap one: deciding that a source
supports competitors because it has a field called ``brand``, or because its name
appears in a list somewhere in ``server/core/``. Support is a declaration, it names
the exact report and the exact fields, and those must exist in the same validated
descriptor -- otherwise the contract is broken at conformance time rather than
discovered when a pull returns nothing.

The second half of the file is the other direction: a Connector that declares
nothing produces ``not_applicable`` **with a reason**, never a guess and never a
silent absence a caller has to interpret.
"""

from __future__ import annotations

import copy

import pytest
from core.source_capabilities import (
    normalize_capabilities,
    validate_manifest_capabilities,
)


def _field(field_id: str, kind: str = "dimension") -> dict:
    return {
        "field_id": field_id,
        "source_field": field_id,
        "kind": kind,
        "physical_type": "string" if kind == "dimension" else "integer",
        "description": f"{field_id} description",
        "semantic_hints": [field_id],
        "canonical_target": None,
        "aggregation": "none" if kind == "dimension" else "sum",
        "non_additive": False,
    }


def _report(report_id: str, dimensions: list[str], metrics: list[str]) -> dict:
    return {
        "id": report_id,
        "selection_mode": "exact_bundle",
        "availability": {"status": "selectable"},
        "dispatch": {"callable": f"pull_{report_id}"},
        "metrics": metrics,
        "dimensions": dimensions,
        "supported_grains": [sorted({"date", *dimensions})],
        "compatibility": [],
        "filters": [],
        "pagination": {
            "mode": "none",
            "completeness": "complete",
            "max_pages": None,
            "row_limit": None,
            "truncation_signal": "none",
        },
        "quota_cost": {"read_points": 1, "unit": "request"},
        "incremental": {"mode": "full_refresh", "cursor_field": None},
        "cadence": {"minimum_interval_minutes": 1440, "supported_modes": ["daily"]},
    }


def _manifest(tracked_entity: dict | None = None) -> dict:
    descriptor = {
        "contract_version": "1",
        "field_discovery": {"mode": "static", "allowed_targets": []},
        "fields": [
            _field("date"),
            _field("entity_ref"),
            _field("entity_label"),
            _field("own_flag"),
            _field("volume", kind="metric"),
        ],
        "reports": [
            _report("snapshot", ["date", "entity_ref", "entity_label", "own_flag"], ["volume"]),
            _report("terms", ["date", "entity_label"], ["volume"]),
        ],
    }
    if tracked_entity is not None:
        descriptor["tracked_entity"] = tracked_entity
    return {
        "name": "example-source",
        "display_name": "Example Source",
        "module_kind": "kpi",
        "report_profiles": [
            {
                "id": "snapshot",
                "display_name": "Snapshot",
                "metrics": ["volume"],
                "dimensions": ["date", "entity_ref", "entity_label", "own_flag"],
                "extraction_capabilities": {
                    "row_limit": None,
                    "filters_supported": False,
                    "realtime": False,
                },
            },
            {
                "id": "terms",
                "display_name": "Terms",
                "metrics": ["volume"],
                "dimensions": ["date", "entity_label"],
                "extraction_capabilities": {
                    "row_limit": None,
                    "filters_supported": False,
                    "realtime": False,
                },
            },
        ],
        "source_capabilities": descriptor,
    }


_COLLECT = {
    "declaration_version": "1",
    "reports": [
        {
            "report_id": "snapshot",
            "direction": "collect",
            "entity_kinds": ["source_entity"],
            "candidate_field_ids": ["entity_label"],
            "identity_field_id": "entity_ref",
            "label_field_id": "entity_label",
            "own_marker_field_id": "own_flag",
            "population": {"completeness": "declared_scope"},
            "query_driver": {
                "parameter": "entity_ids",
                "value_source": "source_identity",
                "cardinality": "one_request_per_value",
                "own_marker_parameter": "own_entity_ids",
            },
        }
    ],
}

_OBSERVE = {
    "declaration_version": "1",
    "reports": [
        {
            "report_id": "terms",
            "direction": "observe",
            "entity_kinds": ["brand"],
            "candidate_field_ids": ["entity_label"],
            "population": {
                "completeness": "reportable_subset",
                "note": "Low-volume values are withheld while their totals remain counted.",
            },
        }
    ],
}


def _codes(manifest: dict) -> set[str]:
    return {issue.code for issue in validate_manifest_capabilities(manifest)}


# ---------------------------------------------------------------------------
# A valid declaration validates, and both directions are expressible.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("declaration", [_COLLECT, _OBSERVE])
def test_a_complete_declaration_passes_conformance(declaration):
    assert validate_manifest_capabilities(_manifest(declaration)) == []


def test_a_manifest_without_the_block_stays_valid():
    assert validate_manifest_capabilities(_manifest()) == []


# ---------------------------------------------------------------------------
# Every reference must exist in the SAME descriptor.
# ---------------------------------------------------------------------------


def test_a_report_the_descriptor_does_not_declare_is_refused():
    declaration = copy.deepcopy(_COLLECT)
    declaration["reports"][0]["report_id"] = "not_a_report"
    assert "tracked_entity_unknown_report" in _codes(_manifest(declaration))


def test_a_field_the_descriptor_does_not_declare_is_refused():
    declaration = copy.deepcopy(_COLLECT)
    declaration["reports"][0]["identity_field_id"] = "not_a_field"
    assert "tracked_entity_unknown_field" in _codes(_manifest(declaration))


def test_a_field_the_named_report_cannot_return_is_refused():
    """`own_flag` exists, but the `terms` report does not select it."""
    declaration = copy.deepcopy(_OBSERVE)
    declaration["reports"][0]["own_marker_field_id"] = "own_flag"
    assert "tracked_entity_field_not_in_report" in _codes(_manifest(declaration))


def test_declaring_the_same_report_twice_is_refused():
    declaration = copy.deepcopy(_COLLECT)
    declaration["reports"].append(copy.deepcopy(declaration["reports"][0]))
    assert "duplicate_tracked_entity_report" in _codes(_manifest(declaration))


# ---------------------------------------------------------------------------
# Collect and observe are different contracts, and neither borrows the other's.
# ---------------------------------------------------------------------------


def test_collect_without_a_source_identity_is_refused():
    declaration = copy.deepcopy(_COLLECT)
    declaration["reports"][0]["identity_field_id"] = None
    assert "tracked_entity_collect_incomplete" in _codes(_manifest(declaration))


def test_collect_without_a_query_driver_is_refused():
    declaration = copy.deepcopy(_COLLECT)
    declaration["reports"][0]["query_driver"] = None
    assert "tracked_entity_collect_incomplete" in _codes(_manifest(declaration))


def test_observe_may_not_declare_a_query_driver():
    declaration = copy.deepcopy(_OBSERVE)
    declaration["reports"][0]["query_driver"] = {
        "parameter": "entity_ids",
        "value_source": "source_identity",
        "cardinality": "one_request_per_value",
    }
    assert "tracked_entity_observe_declares_driver" in _codes(_manifest(declaration))


def test_a_query_driver_parameter_the_callable_cannot_accept_is_refused():
    """The declared parameter is a keyword argument; a dispatch that cannot take it
    would fail at pull time, which is the wrong moment to learn about it."""
    declaration = copy.deepcopy(_COLLECT)
    declaration["reports"][0]["query_driver"]["parameter"] = "own_entity_ids"
    declaration["reports"][0]["query_driver"]["own_marker_parameter"] = "own_entity_ids"
    assert "tracked_entity_driver_parameter_collision" in _codes(_manifest(declaration))


# ---------------------------------------------------------------------------
# The public response says `not_applicable` with a reason, never nothing.
# ---------------------------------------------------------------------------


def test_an_absent_declaration_normalizes_to_not_applicable_with_a_reason():
    response = normalize_capabilities(
        _manifest(), project_id="proj_EXAMPLE", connection_ref_id="conn_EXAMPLE"
    )
    tracked = response["tracked_entity"]
    assert tracked["support"] == "not_applicable"
    assert tracked["reports"] == []
    assert tracked["reason"]


def test_a_declared_source_projects_its_directions_and_kinds():
    response = normalize_capabilities(
        _manifest(_COLLECT), project_id="proj_EXAMPLE", connection_ref_id="conn_EXAMPLE"
    )
    tracked = response["tracked_entity"]
    assert tracked["support"] == "declared"
    assert tracked["directions"] == ["collect"]
    assert tracked["entity_kinds"] == ["source_entity"]
    report = tracked["reports"][0]
    # Optional keys are PRESENT and null rather than absent: a caller reads one
    # shape whatever the connector declared.
    assert report["label_field_id"] == "entity_label"
    assert report["query_driver"]["parameter"] == "entity_ids"


def test_an_observe_only_source_keeps_its_population_caveat_verbatim():
    response = normalize_capabilities(
        _manifest(_OBSERVE), project_id="proj_EXAMPLE", connection_ref_id="conn_EXAMPLE"
    )
    report = response["tracked_entity"]["reports"][0]
    assert report["population"]["completeness"] == "reportable_subset"
    assert "withheld" in report["population"]["note"]
    assert report["identity_field_id"] is None
    assert report["query_driver"] is None
