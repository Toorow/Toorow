"""Story 48.4: the Tax & Fee ladder's governance evidence, proved offline.

Each test names one line of the capability's own `Incomplete if` list, or one
defect Epic 41 shipped, and fails if it comes back. No database: the profile is a
pure normalizer, which is precisely what makes an immutable content hash mean
something.

The database-bound halves -- publication, the compiler, the Data evidence writes
-- are exercised by `test_tax_fees_governance_pg.py`, gated on a live PostgreSQL.
"""

from __future__ import annotations

import pytest
from core.governance_rule_sets import RuleSetError, get_profile, registered_profiles
from core.tax_fee_rule_set import (
    AUTHORITY_KINDS,
    FAMILY_TAX_FEE,
    PROFILE_TAX_FEE,
    REST_OF_WORLD_POSTURES,
    TaxFeeGap,
    TaxFeeLadder,
    validate_ladder_payload,
    validate_ladder_rule,
    validate_ladder_rules,
)


def _source_evidence(**overrides):
    base = {
        "issuer": "European Commission",
        "reference": "VAT Directive, standard rate table",
        "reference_version": "2026-01",
        "authoritative_url": "https://example.com/vat-rates",
        "published_on": "2026-01-01",
    }
    base.update(overrides)
    return base


def _agency_rule(**overrides):
    """A jurisdiction-independent agency fee: the simplest legal ladder rule."""
    rule = {
        "rule_key": "agency_fee",
        "label": "Agency fee",
        "scope_kind": "project",
        "scope_ref": None,
        "category": "AGENCY_FEE",
        "form": "PERCENTAGE",
        "rate": "0.15",
        "base_target": "NET_MEDIA",
        "cascade_phase": 5,
        "sequence_order": 10,
        "effective_from": "2026-01-01",
        "authority_kind": "agency_contract",
        "source_evidence": _source_evidence(
            issuer="Agency",
            reference="Master services agreement, schedule B",
            reference_version="v3",
            authoritative_url=None,
            document_ref="MSA-2026-B",
        ),
    }
    rule.update(overrides)
    return rule


def _country_rule(**overrides):
    rule = {
        "rule_key": "dst_fr",
        "label": "Digital services tax pass-through",
        "scope_kind": "project",
        "scope_ref": None,
        "category": "REGULATORY_TAX",
        "form": "PERCENTAGE",
        "rate": "0.03",
        "base_target": "NET_MEDIA",
        "cascade_phase": 3,
        "sequence_order": 10,
        "conditions": {"country": ["FR"]},
        "effective_from": "2026-01-01",
        "authority_kind": "statutory_reference",
        "source_evidence": _source_evidence(),
        "jurisdiction": {
            "kind": "country",
            "id": "ctry_EXAMPLE",
            "hierarchy_version_id": "mdv_EXAMPLE",
            "label": "France",
        },
        "rest_of_world_posture": "exclude",
        "unknown_posture": "exclude",
    }
    rule.update(overrides)
    return rule


# ---------------------------------------------------------------------------
# The profile is mounted on the GENERIC lifecycle, not on a Tax-shaped one.
# ---------------------------------------------------------------------------


def test_the_ladder_is_a_family_on_the_generic_rule_set_lifecycle():
    profile = get_profile(PROFILE_TAX_FEE)
    assert profile.family == FAMILY_TAX_FEE
    assert profile.validate_rules is not None, (
        "the ladder's content IS its ordered rules; a profile that does not "
        "normalize them publishes unchecked content into an immutable version"
    )
    assert "money_policy_version" in profile.required_reference_kinds
    keys = {item.key for item in registered_profiles()}
    assert PROFILE_TAX_FEE in keys


def test_a_family_with_no_rule_schema_cannot_carry_ordered_rules():
    """The generic fix, not the Tax one: Money must not silently store a ladder."""
    from core.money_policy import PROFILE_MONEY

    assert get_profile(PROFILE_MONEY).validate_rules is None


# ---------------------------------------------------------------------------
# "a rule lacks source, jurisdiction, effective date or version evidence"
# ---------------------------------------------------------------------------


def test_a_rule_with_no_source_evidence_is_refused():
    rule = _agency_rule()
    del rule["source_evidence"]
    with pytest.raises(RuleSetError, match="source_evidence is required"):
        validate_ladder_rule(rule)


def test_source_evidence_must_name_the_version_of_its_source():
    rule = _agency_rule(source_evidence=_source_evidence(reference_version=""))
    with pytest.raises(RuleSetError, match="reference_version"):
        validate_ladder_rule(rule)


def test_a_statutory_reference_must_cite_something_citable():
    rule = _country_rule(
        source_evidence=_source_evidence(authoritative_url=None, document_ref=None)
    )
    with pytest.raises(RuleSetError, match="authoritative_url"):
        validate_ladder_rule(rule)


def test_an_operator_declared_rule_still_needs_a_stated_basis():
    """No exemption: the criterion says 'a rule', with no carve-out."""
    rule = _agency_rule(authority_kind="operator_declared")
    del rule["source_evidence"]
    with pytest.raises(RuleSetError, match="source_evidence is required"):
        validate_ladder_rule(rule)


def test_the_four_authorities_stay_distinguishable():
    assert "statutory_reference" in AUTHORITY_KINDS
    assert "provider_invoice_practice" in AUTHORITY_KINDS
    assert "agency_contract" in AUTHORITY_KINDS
    assert "client_defined_markup" in AUTHORITY_KINDS
    with pytest.raises(RuleSetError, match="authority_kind"):
        validate_ladder_rule(_agency_rule(authority_kind="whatever"))


def test_an_effective_date_is_required():
    rule = _agency_rule()
    del rule["effective_from"]
    with pytest.raises(RuleSetError, match="effective_from"):
        validate_ladder_rule(rule)


# ---------------------------------------------------------------------------
# "Rest of World and Unknown applicability are silent"
# ---------------------------------------------------------------------------


def test_a_geographic_rule_must_declare_rest_of_world_and_unknown():
    rule = _country_rule()
    del rule["rest_of_world_posture"]
    with pytest.raises(RuleSetError, match="rest_of_world_posture"):
        validate_ladder_rule(rule)


def test_a_condition_only_rule_is_geographic_too():
    """No jurisdiction object, but `conditions.country` still reads geography."""
    rule = _agency_rule(conditions={"country": ["FR"]})
    with pytest.raises(RuleSetError, match="geography-dependent"):
        validate_ladder_rule(rule)


def test_unresolved_is_a_declared_posture_not_an_error():
    rule = validate_ladder_rule(
        _country_rule(rest_of_world_posture="unresolved", unknown_posture="unresolved")
    )
    assert rule["rest_of_world_posture"] == "unresolved"
    assert "unresolved" in REST_OF_WORLD_POSTURES


def test_rest_of_world_and_unknown_are_separate_declarations():
    rule = validate_ladder_rule(
        _country_rule(rest_of_world_posture="include", unknown_posture="exclude")
    )
    assert rule["rest_of_world_posture"] == "include"
    assert rule["unknown_posture"] == "exclude", (
        "Rest of World is a known place outside the named set; Unknown is no place "
        "at all -- collapsing them silently matches or excludes rows nobody decided on"
    )


def test_a_non_geographic_rule_may_not_claim_a_geographic_posture():
    with pytest.raises(RuleSetError, match="names no country"):
        validate_ladder_rule(_agency_rule(rest_of_world_posture="include"))


def test_a_jurisdiction_must_pin_its_hierarchy_version():
    rule = _country_rule(
        jurisdiction={"kind": "country", "id": "ctry_EXAMPLE", "hierarchy_version_id": ""}
    )
    with pytest.raises(RuleSetError, match="hierarchy_version_id"):
        validate_ladder_rule(rule)


def test_a_jurisdiction_label_is_display_only():
    rule = validate_ladder_rule(_country_rule())
    assert rule["jurisdiction"]["label"] == "France"
    assert rule["jurisdiction"]["id"] == "ctry_EXAMPLE", "identity is the id, never the label"


def test_a_jurisdiction_independent_rule_needs_no_geography_at_all():
    rule = validate_ladder_rule(_agency_rule())
    assert rule["geography_dependent"] is False
    assert rule["jurisdiction"]["kind"] == "none"


# ---------------------------------------------------------------------------
# Money basis (AC6) and ordering.
# ---------------------------------------------------------------------------


def test_a_rule_declares_which_money_its_base_is():
    native = validate_ladder_rule(_agency_rule(money_basis="native_source"))
    reporting = validate_ladder_rule(_agency_rule(money_basis="project_reporting"))
    assert native["money_basis"] == "native_source"
    assert reporting["money_basis"] == "project_reporting"
    with pytest.raises(RuleSetError, match="money_basis"):
        validate_ladder_rule(_agency_rule(money_basis="whatever"))


def test_two_rules_cannot_share_a_ladder_position():
    with pytest.raises(RuleSetError, match="both sit at phase"):
        validate_ladder_rules(
            [
                _agency_rule(rule_key="fee_a", cascade_phase=5, sequence_order=10),
                _agency_rule(rule_key="fee_b", cascade_phase=5, sequence_order=10),
            ]
        )


def test_rule_keys_are_unique_within_a_version():
    with pytest.raises(RuleSetError, match="unique within a version"):
        validate_ladder_rules(
            [
                _agency_rule(sequence_order=10),
                _agency_rule(sequence_order=20),
            ]
        )


def test_the_ladder_is_returned_in_composition_order():
    rules = validate_ladder_rules(
        [
            _agency_rule(rule_key="last", cascade_phase=5, sequence_order=10),
            _country_rule(rule_key="first", cascade_phase=3, sequence_order=10),
        ]
    )
    assert [rule["rule_key"] for rule in rules] == ["first", "last"]


def test_normalization_is_idempotent_so_the_content_hash_is_meaningful():
    once = validate_ladder_rules([_country_rule(), _agency_rule()])
    twice = validate_ladder_rules(once)
    assert once == twice


def test_an_unroutable_pair_is_still_refused():
    """Epic 41's guard, reused: a pair no evaluator routes contributes a silent 0."""
    with pytest.raises(RuleSetError, match="does not support form"):
        validate_ladder_rule(_agency_rule(form="GROSS_UP"))


def test_a_rule_carries_no_status_of_its_own():
    rule = validate_ladder_rule(_agency_rule())
    assert "status" not in rule, (
        "a rule inside a published version is active by virtue of the version; a "
        "per-rule status would be a second activation authority"
    )


# ---------------------------------------------------------------------------
# The ladder payload owns exactly one rounding boundary.
# ---------------------------------------------------------------------------


def test_the_ladder_declares_one_rounding_policy():
    payload = validate_ladder_payload({"rounding": "half_up"})
    assert payload["rounding"] == "half_up"
    with pytest.raises(RuleSetError, match="rounding"):
        validate_ladder_payload({"rounding": "banker"})


def test_the_ladder_payload_refuses_unknown_keys():
    with pytest.raises(RuleSetError, match="unsupported keys"):
        validate_ladder_payload({"rate": "0.20"})


# ---------------------------------------------------------------------------
# The resolved ladder exposes what blocks a complete total.
# ---------------------------------------------------------------------------


def _ladder(rules):
    return TaxFeeLadder(
        rule_set_id="grs_EXAMPLE",
        version_id="grsv_EXAMPLE",
        version_number=1,
        content_hash="0" * 64,
        rounding="half_even",
        default_money_basis="native_source",
        rules=tuple(rules),
    )


def test_unresolved_geography_is_surfaced_separately_from_completeness():
    ladder = _ladder(
        validate_ladder_rules(
            [
                _agency_rule(),
                _country_rule(rest_of_world_posture="unresolved", unknown_posture="exclude"),
            ]
        )
    )
    assert len(ladder.geography_dependent_rules) == 1
    assert [rule["rule_key"] for rule in ladder.unresolved_geography_rules] == ["dst_fr"]


def test_the_ladder_reports_every_hierarchy_version_it_pins():
    ladder = _ladder(validate_ladder_rules([_country_rule(), _agency_rule()]))
    assert ladder.pinned_hierarchy_versions() == ("mdv_EXAMPLE",)


def test_a_missing_ladder_is_a_typed_gap_not_an_empty_one():
    gap = TaxFeeGap("tax_fee_ladder_unpublished", "no ladder")
    assert gap.code == "tax_fee_ladder_unpublished"
    assert isinstance(gap, RuntimeError) and not isinstance(gap, ValueError), (
        "a caller catching bad input must not swallow 'no ladder' and report gross "
        "equal to net as a working answer"
    )
