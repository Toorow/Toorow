"""Unit tests for datastream field mapping domain service (Story 12.3)."""

import pytest
from core.datastream_field_mapping import (
    DatastreamMappingStructuralError,
    compute_source_schema_hash,
    confirm_binding,
    normalize_mapping,
    profile_fields,
)


def test_compute_source_schema_hash_is_deterministic_and_order_independent():
    fields_a = [
        {"field_id": "clicks", "physical_type": "integer", "kind": "metric"},
        {"field_id": "date", "physical_type": "date", "kind": "date"},
    ]
    fields_b = [
        {"field_id": "date", "physical_type": "date", "kind": "date"},
        {"field_id": "clicks", "physical_type": "integer", "kind": "metric"},
    ]
    hash_a = compute_source_schema_hash(fields_a)
    hash_b = compute_source_schema_hash(fields_b)
    assert hash_a == hash_b
    assert len(hash_a) == 64

    # Drift in type changes hash
    fields_c = [
        {"field_id": "clicks", "physical_type": "decimal", "kind": "metric"},
        {"field_id": "date", "physical_type": "date", "kind": "date"},
    ]
    assert compute_source_schema_hash(fields_c) != hash_a


def test_profile_fields_proposes_suggestions_without_deciding():
    field_records = [
        {"field_id": "date", "physical_type": "date", "kind": "date", "canonical_target": "date"},
        {
            "field_id": "cost",
            "physical_type": "decimal",
            "kind": "metric",
            "canonical_target": "cost",
            "semantic_hints": ["spend"],
        },
    ]
    sample_data = [{"date": "2026-07-20", "cost": "100.50"}]
    res = profile_fields(field_records=field_records, sample_data=sample_data)

    assert res["mapping_contract_version"] == "1"
    assert res["grain"] == ["date"]
    assert len(res["fields"]) == 2

    cost_f = next(f for f in res["fields"] if f["field_id"] == "cost")
    assert cost_f["suggestion"]["status"] == "suggested"
    assert cost_f["suggestion"]["semantic_role"] == "measure_spend"
    assert cost_f["binding"]["status"] == "suggested"
    assert cost_f["binding"]["confirmed_by"] is None


def test_detect_ambiguities_multiple_dates_and_non_additive():
    field_records = [
        {"field_id": "date_a", "physical_type": "date", "kind": "date"},
        {"field_id": "date_b", "physical_type": "date", "kind": "date"},
        {
            "field_id": "ctr",
            "physical_type": "decimal",
            "kind": "metric",
            "aggregation": "sum",
            "non_additive": True,
        },
    ]
    res = profile_fields(field_records=field_records)
    codes = [a["code"] for a in res["ambiguities"]]
    assert "multiple_date_candidates" in codes
    # Non-additive-bound-additively reuses the datamodel MEASURE_NULL code.
    assert "MEASURE_NULL" in codes

    ctr_f = next(f for f in res["fields"] if f["field_id"] == "ctr")
    assert ctr_f["binding"]["status"] == "blocking"
    assert ctr_f["binding"]["blocking_reason"] == "MEASURE_NULL"


def test_confirm_binding_explicit_actor_reason():
    field_records = [
        {
            "field_id": "cost",
            "physical_type": "decimal",
            "kind": "metric",
            "canonical_target": "cost",
        }
    ]
    mapping = profile_fields(field_records=field_records)
    cost_f = mapping["fields"][0]
    assert cost_f["binding"]["status"] == "suggested"

    updated = confirm_binding(
        mapping,
        "cost",
        actor="user_jean",
        reason="Verified cost target with finance",
        canonical_target="cost",
    )
    confirmed_f = updated["fields"][0]
    assert confirmed_f["binding"]["status"] == "confirmed"
    assert confirmed_f["binding"]["confirmed_by"] == "user_jean"
    assert confirmed_f["binding"]["confirmed_reason"] == "Verified cost target with finance"
    assert confirmed_f["binding"]["blocking_reason"] is None


def test_unknown_target_field_becomes_blocking():
    field_records = [
        {
            "field_id": "custom_metric",
            "physical_type": "decimal",
            "kind": "metric",
            "canonical_target": "unregistered_target",
        }
    ]
    res = profile_fields(
        field_records=field_records, known_target_fields={"cost", "impressions", "date"}
    )
    custom_f = res["fields"][0]
    assert custom_f["binding"]["status"] == "blocking"
    assert custom_f["binding"]["blocking_reason"] == "target_field_not_found:unregistered_target"


def test_normalize_mapping_validation():
    invalid_payload = {"mapping_contract_version": "1"}
    with pytest.raises(DatastreamMappingStructuralError):
        normalize_mapping(invalid_payload)


def test_profile_fields_multiple_dates_marks_date_bindings_as_blocking():
    field_records = [
        {"field_id": "created_at", "physical_type": "timestamp", "kind": "date"},
        {"field_id": "updated_at", "physical_type": "timestamp", "kind": "date"},
    ]
    res = profile_fields(field_records=field_records)
    for f in res["fields"]:
        assert f["binding"]["status"] == "blocking"
        assert f["binding"]["blocking_reason"] == "multiple_date_candidates"


def test_profile_fields_redacts_email_in_sample_values():
    field_records = [{"field_id": "email", "physical_type": "varchar", "kind": "dimension"}]
    sample_data = [{"email": "user@example.com"}]
    res = profile_fields(field_records=field_records, sample_data=sample_data)
    f = res["fields"][0]
    assert "[REDACTED_EMAIL]" in f["profile"]["sample_values"][0]


def test_confirm_binding_rejects_empty_or_whitespace_actor_and_reason():
    field_records = [{"field_id": "cost", "physical_type": "decimal", "kind": "metric"}]
    mapping = profile_fields(field_records=field_records)
    with pytest.raises(ValueError, match="actor and reason are required"):
        confirm_binding(mapping, "cost", actor="   ", reason="valid reason")
    with pytest.raises(ValueError, match="actor and reason are required"):
        confirm_binding(mapping, "cost", actor="user", reason="   ")


def test_unknown_field_is_surfaced_and_blocks():
    """A field whose physical_type/kind is outside the recognized universe is an
    `unknown_field` ambiguity with a blocking binding (never a silent dimension)."""
    field_records = [
        {"field_id": "weird", "physical_type": "geography", "kind": "spatial"},
        {"field_id": "date", "physical_type": "date", "kind": "date"},
    ]
    res = profile_fields(field_records=field_records)
    codes = [a["code"] for a in res["ambiguities"]]
    assert "unknown_field" in codes
    unknown_amb = next(a for a in res["ambiguities"] if a["code"] == "unknown_field")
    assert unknown_amb["field_ids"] == ["weird"]

    weird_f = next(f for f in res["fields"] if f["field_id"] == "weird")
    assert weird_f["binding"]["status"] == "blocking"
    assert weird_f["binding"]["blocking_reason"] == "unknown_field"
    # An unknown field is never folded into the joint grain.
    assert "weird" not in res["grain"]


def test_mixed_grain_detected_across_date_granularities():
    """Date candidates at differing granularity raise a mixed_grain ambiguity."""
    field_records = [
        {"field_id": "day", "physical_type": "date", "kind": "date"},
        {"field_id": "event_ts", "physical_type": "timestamp", "kind": "date"},
    ]
    res = profile_fields(field_records=field_records)
    codes = [a["code"] for a in res["ambiguities"]]
    assert "mixed_grain" in codes
    grain_amb = next(a for a in res["ambiguities"] if a["code"] == "mixed_grain")
    assert grain_amb["field_ids"] == ["day", "event_ts"]
    assert set(grain_amb["candidates"]) == {"day", "instant"}


def test_confidence_is_derived_from_signals_not_hardcoded():
    """A field with a recognized type + hint + sample evidence scores higher than a
    bare metadata-only field, and confidence is never the old constant 0.85."""
    rich = profile_fields(
        field_records=[
            {
                "field_id": "cost",
                "physical_type": "decimal",
                "kind": "metric",
                "semantic_hints": ["spend"],
            }
        ],
        sample_data=[{"cost": "1"}, {"cost": "2"}, {"cost": "3"}],
    )
    lean = profile_fields(
        field_records=[{"field_id": "cost", "physical_type": "decimal", "kind": "metric"}]
    )
    rich_conf = rich["fields"][0]["profile"]["confidence"]
    lean_conf = lean["fields"][0]["profile"]["confidence"]
    assert rich_conf > lean_conf
    assert rich_conf != 0.85
    assert lean_conf != 0.85


def test_low_confidence_field_stays_blocking_low_confidence():
    """A field below the configured threshold blocks with `low_confidence`."""
    field_records = [{"field_id": "amt", "physical_type": "decimal", "kind": "metric"}]
    # Raise the threshold above what a bare field can score so it must block.
    res = profile_fields(field_records=field_records, confidence_threshold=0.99)
    binding = res["fields"][0]["binding"]
    assert binding["status"] == "blocking"
    assert binding["blocking_reason"] == "low_confidence"


def test_currency_conflict_reuses_datamodel_code():
    """Currency ambiguity reuses the exact CURRENCY_CONFLICT code from datamodel."""
    from core.datastream_field_mapping import CONFLICT_CURRENCY

    field_records = [
        {"field_id": "cost", "physical_type": "decimal", "kind": "metric", "currency": "USD"},
        {"field_id": "spend", "physical_type": "decimal", "kind": "metric", "currency": "EUR"},
    ]
    res = profile_fields(field_records=field_records)
    codes = [a["code"] for a in res["ambiguities"]]
    assert CONFLICT_CURRENCY == "CURRENCY_CONFLICT"
    assert "CURRENCY_CONFLICT" in codes
    for f in res["fields"]:
        assert f["binding"]["blocking_reason"] == "CURRENCY_CONFLICT"


def test_non_additive_bound_additively_reuses_measure_null_code():
    """A non-additive measure bound with sum reuses the MEASURE_NULL code."""
    from core.datastream_field_mapping import CONFLICT_MEASURE_NULL

    field_records = [
        {
            "field_id": "ctr",
            "physical_type": "decimal",
            "kind": "metric",
            "aggregation": "sum",
            "non_additive": True,
        }
    ]
    res = profile_fields(field_records=field_records)
    codes = [a["code"] for a in res["ambiguities"]]
    assert CONFLICT_MEASURE_NULL == "MEASURE_NULL"
    assert "MEASURE_NULL" in codes
    assert res["fields"][0]["binding"]["blocking_reason"] == "MEASURE_NULL"


def test_sensitivity_defaults_to_unknown_not_none():
    field_records = [{"field_id": "note", "physical_type": "text", "kind": "dimension"}]
    res = profile_fields(field_records=field_records)
    assert res["fields"][0]["suggestion"]["sensitivity"] == "unknown"

