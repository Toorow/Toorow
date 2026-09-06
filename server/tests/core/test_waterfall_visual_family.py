"""Story 41.6 -- waterfall is selectable only for the closed waterfall_v1 Result."""

from __future__ import annotations

from pathlib import Path

from core.result_shapes import WATERFALL_FIELDS
from core.visualization_families import (
    DEFERRED_FAMILIES,
    SPEC_SELECTABLE_FAMILY_IDS,
    WATERFALL_RESULT_BINDINGS,
    get_family,
)
from core.visualization_specs import (
    PinnedMembers,
    check_shape_compatibility,
    evaluate_result_disclosures,
    normalize_document,
)

ROOT = Path(__file__).resolve().parents[3]


def _document(bindings=None):
    selected = bindings or {
        well: list(fields) for well, fields in WATERFALL_RESULT_BINDINGS.items()
    }
    normalized, refusals = normalize_document(
        {
            "spec_contract_version": "visualization-spec.v1",
            "schema_version": 1,
            "family": "waterfall",
            "bindings": selected,
        }
    )
    assert refusals == []
    return normalized


def _pinned(shape: str | None = "waterfall_v1") -> PinnedMembers:
    return PinnedMembers(
        roles={},
        labels={},
        grain="project",
        comparison="none",
        row_limit=1000,
        query_spec_id="qs_waterfall",
        semantic_view_id="sv_tax_fees",
        semantic_view_version_id="svv_tax_fees_1",
        result_shape=shape,
    )


def test_waterfall_is_declared_selectable_and_not_deferred():
    family = get_family("waterfall")
    assert family is not None
    assert "waterfall" in SPEC_SELECTABLE_FAMILY_IDS
    assert "waterfall" not in {item["id"] for item in DEFERRED_FAMILIES}
    assert family.result_shape == "waterfall_v1"


def test_exact_result_field_bindings_are_compatible_without_semantic_role_overload():
    family = get_family("waterfall")
    assert family is not None
    assert check_shape_compatibility(_document(), family, _pinned()) == []


def test_tabular_query_or_changed_result_binding_is_refused():
    family = get_family("waterfall")
    assert family is not None
    wrong_shape = check_shape_compatibility(_document(), family, _pinned(None))
    assert {item.code for item in wrong_shape} == {"incompatible_result_shape"}

    changed = {well: list(fields) for well, fields in WATERFALL_RESULT_BINDINGS.items()}
    changed["measure"][0] = "wf_attacker_value"
    wrong_binding = check_shape_compatibility(_document(changed), family, _pinned())
    assert {item.code for item in wrong_binding} == {"incompatible_result_binding"}


def test_result_disclosure_checks_manifest_and_stable_schema_ids():
    family = get_family("waterfall")
    assert family is not None
    schema = {
        "contract": "waterfall_v1",
        "fields": [{"id": field_id, "name": name} for field_id, name in WATERFALL_FIELDS],
    }
    manifest = {
        "result_shape": "waterfall_v1",
        "field_bindings": {name: field_id for field_id, name in WATERFALL_FIELDS},
    }
    compatible = evaluate_result_disclosures(
        _document(),
        family,
        rows=[],
        row_count=0,
        truncated=False,
        outcome="empty",
        result_schema=schema,
        result_manifest=manifest,
    )
    assert compatible["compatible"] is True

    incompatible = evaluate_result_disclosures(
        _document(),
        family,
        rows=[],
        row_count=0,
        truncated=False,
        outcome="empty",
        result_schema=schema,
        result_manifest={**manifest, "field_bindings": {}},
    )
    assert incompatible["compatible"] is False
    assert [item["code"] for item in incompatible["refusals"]] == ["incompatible_result_shape"]


def test_forward_migration_opens_both_closed_family_constraints_and_registers_build():
    sql = (ROOT / "infra" / "nango" / "migrations" / "191_waterfall_visual_family.sql").read_text(
        encoding="utf-8"
    )
    assert "DROP CONSTRAINT IF EXISTS ck_visualization_spec_versions_family" in sql
    assert "DROP CONSTRAINT IF EXISTS ck_renderer_runtime_builds_family" in sql
    assert sql.count("'waterfall'") >= 3
    assert "waterfall/toorow-echarts-waterfall@1.0.0" in sql
    assert "ON CONFLICT (id) DO NOTHING" in sql
