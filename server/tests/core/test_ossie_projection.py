"""Unit tests for Apache Ossie 0.1.1 projection generator (Story 12.3)."""

import json

from core.datastream_field_mapping import (
    confirm_binding,
    profile_fields,
    project_ossie,
)


def test_ossie_projection_validates_against_pinned_profile():
    field_records = [
        {"field_id": "date", "physical_type": "date", "kind": "date", "canonical_target": "date"},
        {
            "field_id": "cost",
            "physical_type": "decimal",
            "kind": "metric",
            "canonical_target": "cost",
            "semantic_hints": ["spend"],
            "aggregation": "sum",
        },
    ]
    mapping = profile_fields(field_records=field_records)
    mapping = confirm_binding(
        mapping, "cost", actor="jean", reason="approved", canonical_target="cost"
    )

    proj = project_ossie(mapping, dataset_name="campaign_daily")

    assert proj["ossie_spec_version"] == "0.1.1"
    model = proj["semantic_model"]
    assert "datasets" in model
    assert len(model["datasets"]) == 1
    ds = model["datasets"][0]
    assert ds["name"] == "campaign_daily"

    cost_field = next(f for f in ds["fields"] if f["name"] == "cost")
    exts = cost_field["custom_extensions"]
    assert len(exts) == 1
    assert exts[0]["vendor_name"] == "toorow"
    ext_data = json.loads(exts[0]["data"])
    assert ext_data["physical_type"] == "decimal"
    assert ext_data["aggregation"] == "sum"
    assert ext_data["canonical_target"] == "cost"
    assert ext_data["binding_status"] == "confirmed"

    # Confirmed additive metric produces an Ossie metric
    assert len(model["metrics"]) == 1
    assert model["metrics"][0]["name"] == "cost_sum"
    assert model["metrics"][0]["expression"] == "SUM(cost)"


def test_ossie_projection_is_deterministic():
    field_records = [
        {"field_id": "date", "physical_type": "date", "kind": "date"},
        {"field_id": "clicks", "physical_type": "integer", "kind": "metric"},
    ]
    mapping = profile_fields(field_records=field_records)
    proj_a = project_ossie(mapping)
    proj_b = project_ossie(mapping)
    assert json.dumps(proj_a, sort_keys=True) == json.dumps(proj_b, sort_keys=True)
