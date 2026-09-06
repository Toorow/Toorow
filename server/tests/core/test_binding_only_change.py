"""What counts as a binding-only mapping change (governance.md, 2026-09-05)."""

from __future__ import annotations

from copy import deepcopy

from core.datastream_change import binding_only_change


def _field(field_id, *, target, signal="unknown", role="dimension"):
    return {
        "field_id": field_id,
        "physical_type": "string",
        "profile": {"nullable": False, "unique": False, "cardinality_signal": signal, "sample_values": [], "confidence": 0.9},
        "suggestion": {"semantic_role": role, "aggregation": "none"},
        "binding": {"status": "confirmed", "mdm_target": target, "canonical_target": field_id},
    }


BASE = {
    "grain": ["channel_id", "date"],
    "capability_fingerprint": "a" * 64,
    "fields": [_field("channel_id", target=None), _field("date", target=None, role="primary_date")],
}


def test_a_pin_alone_is_a_binding_only_change():
    after = deepcopy(BASE)
    after["fields"][0]["binding"]["mdm_target"] = "mdm_CHANNEL"
    assert binding_only_change(BASE, after) is True


def test_the_measured_cardinality_and_the_derived_fingerprint_do_not_make_it_more_than_that():
    after = deepcopy(BASE)
    after["fields"][0]["binding"]["mdm_target"] = "mdm_CHANNEL"
    after["fields"][0]["profile"]["cardinality_signal"] = "low"
    after["fields"][0]["profile"]["allowed_values"] = ["UC_one"]
    after["fields"][0]["binding"]["confirmed_by"] = "someone@example.com"
    after["fields"][0]["binding"]["confirmed_reason"] = "names the shared identity"
    after["capability_fingerprint"] = "b" * 64
    assert binding_only_change(BASE, after) is True


def test_a_column_that_moves_is_not_a_binding_only_change():
    renamed = deepcopy(BASE)
    renamed["fields"][0]["field_id"] = "channel"
    renamed["fields"][0]["binding"]["mdm_target"] = "mdm_CHANNEL"
    assert binding_only_change(BASE, renamed) is False

    regrained = deepcopy(BASE)
    regrained["grain"] = ["date"]
    regrained["fields"][0]["binding"]["mdm_target"] = "mdm_CHANNEL"
    assert binding_only_change(BASE, regrained) is False


def test_nothing_changed_is_not_a_binding_only_change_either():
    assert binding_only_change(BASE, deepcopy(BASE)) is False
