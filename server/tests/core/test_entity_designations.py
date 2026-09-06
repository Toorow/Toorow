"""A mapping column declares which entity type its values are keys OF (Story 68.2).

Proven WITHOUT a database, the discipline `test_master_data.py` states in its own
header: the validation is a pure function of the payload plus what the caller
already read, so a test that reached for Postgres would prove the rules for
whoever had a Postgres and for nobody else. What is schema -- the blocking
propagation through `save_field_mapping`, the publish refusals -- is proven on
real Postgres in `tests/integration/test_entity_binding_pg.py`.

The one rule these tests exist to hold: the binding lives IN the mapping
payload, and every refusal names where the repair belongs -- Master Data for
the type, the Datastream's Mapping workbench for the designation. Nothing is
invented and nothing is guessed.
"""

from __future__ import annotations

import pytest
from core.datastream_field_mapping import (
    DatastreamMappingStructuralError,
    compute_source_schema_hash,
    normalize_mapping,
)
from core.object_kind_registry import (
    REASON_CONTRADICTS_MDM,
    REASON_KEY_UNDECLARED,
    REASON_TYPE_ARCHIVED,
    REASON_TYPE_UNDECLARED,
    UNDECLARED_KEY_CANDIDATES,
    entity_designation_groups,
    entity_designations,
    validate_entity_designations,
)

DECLARED = {
    "video": {"canonical_key": "video_id", "lifecycle_state": "draft"},
    "restaurant": {"canonical_key": "restaurant_id", "lifecycle_state": "active"},
}


def _field(field_id, *, designation="__absent__", mdm_target=None, profile=None):
    binding = {"mdm_target": mdm_target, "status": "confirmed"}
    if designation != "__absent__":
        binding["designates_object_kind"] = designation
    return {
        "field_id": field_id,
        "physical_type": "string",
        "profile": profile
        or {
            "nullable": False,
            "unique": True,
            "cardinality_signal": "unique",
            "sample_values": [],
            "confidence": 0.9,
        },
        "binding": binding,
    }


def _payload(*fields):
    return {"grain": [f["field_id"] for f in fields], "fields": list(fields)}


# ---------------------------------------------------------------------------
# The happy path: a designation that resolves is not a refusal.
# ---------------------------------------------------------------------------


def test_a_designation_naming_a_declared_type_with_a_key_passes():
    payload = _payload(_field("video_id", designation="video"))
    assert validate_entity_designations(payload, declared_types=DECLARED) == ()


def test_a_payload_without_designations_validates_as_empty():
    """No designation means no lookup and no refusal -- the honest unbound."""
    for payload in ({}, None, _payload(_field("date"))):
        assert validate_entity_designations(payload, declared_types={}) == ()
        assert entity_designations(payload) == ()


# ---------------------------------------------------------------------------
# The refusals, each naming where the repair belongs.
# ---------------------------------------------------------------------------


def test_a_designation_naming_a_type_nobody_declared_is_refused():
    payload = _payload(_field("film_id", designation="film"))
    (issue,) = validate_entity_designations(payload, declared_types=DECLARED)
    assert issue.reason == REASON_TYPE_UNDECLARED
    assert issue.field_id == "film_id"
    assert issue.object_kind == "film"
    # The repair belongs to Master Data, and the message says so.
    assert "Master Data" in issue.message


def test_an_archived_type_is_not_a_live_type():
    payload = _payload(_field("video_id", designation="video"))
    archived = {"video": {"canonical_key": "video_id", "lifecycle_state": "disabled"}}
    (issue,) = validate_entity_designations(payload, declared_types=archived)
    assert issue.reason == REASON_TYPE_ARCHIVED
    assert "archived" in issue.message


def test_a_type_without_a_declared_key_shape_is_refused():
    """A registry that predates the 68.1 door carries no canonical_key."""
    payload = _payload(_field("video_id", designation="video"))
    keyless = {"video": {"canonical_key": None, "lifecycle_state": "active"}}
    (issue,) = validate_entity_designations(payload, declared_types=keyless)
    assert issue.reason == REASON_KEY_UNDECLARED
    assert "canonical key" in issue.message


def test_a_designation_contradicting_the_mdm_target_is_refused():
    """Two answers to 'which object' is one answer too many (AC5)."""
    payload = _payload(
        _field("video_id", designation="restaurant", mdm_target="mdm_0000000000000000000000000A")
    )
    (issue,) = validate_entity_designations(
        payload,
        declared_types=DECLARED,
        mdm_object_kinds={"mdm_0000000000000000000000000A": "video"},
    )
    assert issue.reason == REASON_CONTRADICTS_MDM
    assert "restaurant" in issue.message and "video" in issue.message


def test_a_designation_agreeing_with_the_mdm_target_composes():
    """Same answer twice is composition, not contradiction (CAP-29)."""
    payload = _payload(
        _field("video_id", designation="video", mdm_target="mdm_0000000000000000000000000A")
    )
    assert (
        validate_entity_designations(
            payload,
            declared_types=DECLARED,
            mdm_object_kinds={"mdm_0000000000000000000000000A": "video"},
        )
        == ()
    )


def test_a_mdm_target_without_an_object_kind_contradicts_nothing():
    """`object_kind` is nullable (migration 241): no answer is not a second answer."""
    payload = _payload(
        _field("video_id", designation="video", mdm_target="mdm_0000000000000000000000000A")
    )
    assert (
        validate_entity_designations(
            payload,
            declared_types=DECLARED,
            mdm_object_kinds={"mdm_0000000000000000000000000A": None},
        )
        == ()
    )


def test_every_designation_is_checked_and_every_issue_names_its_column():
    payload = _payload(
        _field("video_id", designation="video"),
        _field("film_id", designation="film"),
        _field("meal_id", designation="meal"),
    )
    issues = validate_entity_designations(payload, declared_types=DECLARED)
    assert [issue.field_id for issue in issues] == ["film_id", "meal_id"]


# ---------------------------------------------------------------------------
# The honest unbound (AC4): absent vs declared-untyped, and the named group.
# ---------------------------------------------------------------------------


def test_absent_and_declared_untyped_are_distinguishable():
    payload = _payload(
        _field("video_id", designation="video"),
        _field("restaurant_id", designation=None),
        _field("date"),
    )
    groups = entity_designation_groups(payload)
    assert groups["designated"] == {"video_id": "video"}
    assert groups["declared_untyped"] == ["restaurant_id"]
    # `date` declares nothing and profiles unique -- the named group counts it.
    assert groups[UNDECLARED_KEY_CANDIDATES] == ["date"]


def test_only_profiled_key_candidates_are_counted_never_guessed():
    """A column is a candidate because the payload's OWN profile says so."""
    payload = _payload(
        _field("date", profile={"unique": False, "cardinality_signal": "low"}),
        _field("channel_id"),
    )
    groups = entity_designation_groups(payload)
    # `date` is undeclared but not a key candidate: nobody counted it.
    assert groups[UNDECLARED_KEY_CANDIDATES] == ["channel_id"]


def test_a_designated_column_is_not_an_undeclared_candidate():
    payload = _payload(_field("video_id", designation="video"))
    assert entity_designation_groups(payload)[UNDECLARED_KEY_CANDIDATES] == []


# ---------------------------------------------------------------------------
# The payload contract (AC1): the key round-trips, and the hash does not move.
# ---------------------------------------------------------------------------


def _full_payload(designation):
    return {
        "mapping_contract_version": "1",
        "source_schema_hash": "a" * 64,
        "plan_version_id": "dsp_1",
        "grain": ["video_id"],
        "fields": [
            {
                "field_id": "video_id",
                "physical_type": "string",
                "profile": {
                    "nullable": False,
                    "unique": True,
                    "cardinality_signal": "unique",
                    "sample_values": [],
                    "confidence": 0.9,
                },
                "suggestion": {
                    "semantic_role": "dimension",
                    "aggregation": "none",
                    "non_additive": False,
                    "currency": "unknown",
                    "sensitivity": "none",
                    "status": "suggested",
                    "evidence": ["kind:dimension"],
                },
                "binding": {
                    "canonical_target": None,
                    "mdm_target": None,
                    "status": "confirmed",
                    "blocking_reason": None,
                    "confirmed_by": "alice@example.com",
                    "confirmed_reason": "key column",
                    **(
                        {"designates_object_kind": designation}
                        if designation != "__absent__"
                        else {}
                    ),
                },
            }
        ],
        "ambiguities": [],
    }


def test_the_designation_round_trips_through_normalize_mapping():
    normalized, _ = normalize_mapping(_full_payload("video"))
    binding = normalized["fields"][0]["binding"]
    assert binding["designates_object_kind"] == "video"


def test_an_explicit_null_round_trips_and_stays_distinguishable_from_absent():
    normalized, _ = normalize_mapping(_full_payload(None))
    binding = normalized["fields"][0]["binding"]
    assert "designates_object_kind" in binding
    assert binding["designates_object_kind"] is None


def test_a_malformed_designation_is_a_structural_refusal():
    """The kind's own snake_case grammar (68.1) is the schema's, not a lookup's."""
    with pytest.raises(DatastreamMappingStructuralError):
        normalize_mapping(_full_payload("Video"))


def test_source_schema_hash_ignores_the_designation():
    """No false drift: the hash is field_id/physical_type/kind and nothing else."""
    base = [{"field_id": "video_id", "physical_type": "string", "kind": "dimension"}]
    designated = [
        {
            **base[0],
            "binding": {"designates_object_kind": "video"},
            "designates_object_kind": "video",
        }
    ]
    assert compute_source_schema_hash(base) == compute_source_schema_hash(designated)
