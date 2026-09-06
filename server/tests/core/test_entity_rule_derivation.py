"""Entity rule derivation: the pure half, proven without a database (Story 68.6).

The payload validator is what the generic lifecycle calls at draft time, and
the evaluator is what publish runs per node -- both are pure by design, so
every refusal and every classification is exercised here, off base. What only
a live schema answers (stamped rows, RLS, re-derivation across versions) lives
in `tests/integration/test_entity_rule_derivation_pg.py`.
"""

from __future__ import annotations

import pytest
from core import entity_rule_derivation as erd
from core.governance_rule_sets import RuleSetError, get_profile


def _attribute(**overrides):
    base = {
        "name": "content_type",
        "rules": [
            {"when": {"field": "duration_seconds", "op": "<", "value": 60}, "then": "short"},
            {"when": {"field": "duration_seconds", "op": "<", "value": 600}, "then": "medium"},
        ],
        "otherwise": "long",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# The profile is mounted on the generic lifecycle -- no parallel engine.
# ---------------------------------------------------------------------------


def test_the_profile_is_registered_under_the_generic_loader():
    profile = get_profile(erd.PROFILE_ENTITY_DERIVATION)
    assert profile.family == erd.FAMILY_ENTITY_DERIVATION
    assert profile.validate is erd._validate_payload


# ---------------------------------------------------------------------------
# Payload validation (draft-time refusals).
# ---------------------------------------------------------------------------


def test_a_valid_payload_is_normalized():
    normalized = erd._validate_payload(
        {"object_kind": "video", "derived_attributes": [_attribute()]}
    )
    assert normalized["object_kind"] == "video"
    assert normalized["derived_attributes"][0]["name"] == "content_type"
    # Rule order is content: first match wins, so normalization never sorts.
    assert [rule["then"] for rule in normalized["derived_attributes"][0]["rules"]] == [
        "short",
        "medium",
    ]


def test_normalization_makes_logically_identical_payloads_hash_identical():
    from core.governance_rule_sets import content_hash

    first = erd._validate_payload({"object_kind": "video", "derived_attributes": [_attribute()]})
    second = erd._validate_payload(
        # Same content, different key order inside every object.
        {"derived_attributes": [_attribute()], "object_kind": "video"}
    )
    assert content_hash(first) == content_hash(second)


def test_an_unknown_operator_is_refused_with_the_whitelist():
    with pytest.raises(RuleSetError, match="unknown operator"):
        erd._validate_payload(
            {
                "object_kind": "video",
                "derived_attributes": [
                    _attribute(
                        rules=[{"when": {"field": "duration_seconds", "op": "~", "value": 1},
                                "then": "short"}]
                    )
                ],
            }
        )


def test_an_attribute_without_rules_is_refused():
    with pytest.raises(RuleSetError, match="at least one rule"):
        erd._validate_payload(
            {"object_kind": "video", "derived_attributes": [_attribute(rules=[])]}
        )


def test_two_attributes_of_the_same_name_are_refused():
    with pytest.raises(RuleSetError, match="same name"):
        erd._validate_payload(
            {
                "object_kind": "video",
                "derived_attributes": [_attribute(), _attribute()],
            }
        )


def test_an_in_rule_requires_a_non_empty_list():
    with pytest.raises(RuleSetError, match="non-empty list"):
        erd._validate_payload(
            {
                "object_kind": "video",
                "derived_attributes": [
                    _attribute(
                        rules=[{"when": {"field": "lang", "op": "in", "value": "fr"},
                                "then": "local"}]
                    )
                ],
            }
        )


def test_a_compared_value_must_be_a_scalar():
    with pytest.raises(RuleSetError, match="string, a number or a boolean"):
        erd._validate_payload(
            {
                "object_kind": "video",
                "derived_attributes": [
                    _attribute(
                        rules=[{"when": {"field": "lang", "op": "=", "value": None},
                                "then": "local"}]
                    )
                ],
            }
        )


def test_the_output_value_must_be_a_scalar():
    with pytest.raises(RuleSetError, match="string, a number or a boolean"):
        erd._validate_payload(
            {
                "object_kind": "video",
                "derived_attributes": [
                    _attribute(
                        rules=[{"when": {"field": "lang", "op": "=", "value": "fr"},
                                "then": {"nested": "no"}}]
                    )
                ],
            }
        )


def test_a_rule_set_without_derived_attributes_is_refused():
    with pytest.raises(RuleSetError, match="at least one derived attribute"):
        erd._validate_payload({"object_kind": "video", "derived_attributes": []})


def test_the_object_kind_shape_is_enforced():
    with pytest.raises(RuleSetError, match="object_kind"):
        erd._validate_payload(
            {"object_kind": "Video Ads", "derived_attributes": [_attribute()]}
        )


def test_a_malformed_kind_is_refused_before_any_db_write():
    with pytest.raises(RuleSetError):
        erd.draft_entity_rule_set(
            None,
            org_id="org",
            project_id="proj",
            object_kind="NOT A KIND",
            derived_attributes=[_attribute()],
            label="Videos",
            actor="alice@example.com",
        )


# ---------------------------------------------------------------------------
# Evaluation (publish-time, per node). Facts in, value out, never SQL.
# ---------------------------------------------------------------------------


def test_first_matching_rule_wins():
    attribute = erd.validate_derived_attribute(_attribute())
    assert erd.evaluate_attribute(attribute, {"duration_seconds": 42}) == "short"
    assert erd.evaluate_attribute(attribute, {"duration_seconds": 120}) == "medium"
    assert erd.evaluate_attribute(attribute, {"duration_seconds": 3600}) == "long"


def test_a_missing_fact_matches_nothing_not_even_not_equal():
    attribute = erd.validate_derived_attribute(
        {
            "name": "lang_flag",
            "rules": [{"when": {"field": "lang", "op": "!=", "value": "fr"}, "then": "foreign"}],
        }
    )
    # "Unknown" is not "different": without the fact the rule must not fire,
    # and with no otherwise the attribute derives NOTHING.
    assert erd.evaluate_attribute(attribute, {}) is erd.NO_MATCH
    assert erd.evaluate_attribute(attribute, {"lang": "fr"}) is erd.NO_MATCH
    assert erd.evaluate_attribute(attribute, {"lang": "en"}) == "foreign"


def test_an_incomparable_fact_matches_nothing():
    attribute = erd.validate_derived_attribute(_attribute())
    # A string duration against numeric bounds: a data-quality fact, not a
    # classification -- and never a crash that refuses the whole publish.
    assert erd.evaluate_attribute(attribute, {"duration_seconds": "42"}) == "long"


def test_the_in_operator_matches_membership():
    attribute = erd.validate_derived_attribute(
        {
            "name": "market",
            "rules": [
                {"when": {"field": "country", "op": "in", "value": ["fr", "be", "ch"]},
                 "then": "francophone"}
            ],
            "otherwise": "other",
        }
    )
    assert erd.evaluate_attribute(attribute, {"country": "be"}) == "francophone"
    assert erd.evaluate_attribute(attribute, {"country": "de"}) == "other"


def test_no_match_is_not_a_value():
    attribute = erd.validate_derived_attribute(
        {
            "name": "flag",
            "rules": [{"when": {"field": "x", "op": "=", "value": True}, "then": "yes"}],
        }
    )
    result = erd.evaluate_attribute(attribute, {"x": False})
    assert result is erd.NO_MATCH
    assert result is not None  # a stored NULL would read as a value
