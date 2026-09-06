"""Story 48.4: qualified presets and observed Data evidence, proved offline.

Two halves of the same claim -- that Tax & Fees never concludes from an absence:

* a preset narrows candidates and proves nothing on its own (AC3);
* a publication, not a mapping, states what a Datastream showed (AC4).

Pure functions only. The persistence halves are exercised by the pg-gated suite.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from core.tax_evidence import (
    GAP_NO_NATIVE_MONEY,
    GAP_SOURCE_TYPE_UNRESOLVED,
    GAP_TAX_POSTURE_UNKNOWN,
    TaxEvidence,
    TaxEvidenceError,
    classify_gaps,
    derive_applicability,
    record_tax_evidence,
)
from core.tax_fee_presets import (
    QUALIFICATION_CODES,
    PresetError,
    PresetVersion,
    propose_from_presets,
    seed_rows_as_preset_payloads,
    validate_preset,
    validate_qualifications,
)


def _preset_payload(**overrides):
    payload = {
        "preset_key": "dst_fr",
        "label": "France DST pass-through",
        "issuer": "Direction generale des Finances publiques",
        "source_reference": "Digital services tax, headline rate table",
        "source_reference_version": "2026-01",
        "authoritative_url": "https://example.com/dst",
        "jurisdiction_kind": "country",
        "jurisdiction_code": "FR",
        "taxable_subject": "Digital advertising services supplied in France",
        "category": "REGULATORY_TAX",
        "form": "PERCENTAGE",
        "rate": "0.030000",
        "base_target": "NET_MEDIA",
        "qualifications": [
            {
                "code": "provider_passes_through",
                "question": "Does the platform pass this through on its invoice?",
            }
        ],
        "effective_from": "2026-01-01",
    }
    payload.update(overrides)
    return payload


def _preset(**overrides) -> PresetVersion:
    normalized = validate_preset(_preset_payload(**overrides))
    return PresetVersion(
        id="tfpv_EXAMPLE",
        version_number=1,
        status=overrides.pop("status", "published"),
        content_hash="0" * 64,
        **{
            key: normalized[key]
            for key in (
                "preset_key",
                "label",
                "issuer",
                "source_reference",
                "source_reference_version",
                "authoritative_url",
                "document_ref",
                "jurisdiction_kind",
                "jurisdiction_code",
                "taxable_subject",
                "service_scope",
                "category",
                "form",
                "rate",
                "amount_micros",
                "cpm_micros",
                "currency",
                "base_target",
                "published_on",
                "effective_from",
                "effective_to",
                "last_verified_on",
                "verification_status",
            )
        },
        thresholds=tuple(normalized["thresholds"]),
        qualifications=tuple(normalized["qualifications"]),
        assumptions=tuple(normalized["assumptions"]),
    )


# ---------------------------------------------------------------------------
# AC3: a country and a rate are not a qualification.
# ---------------------------------------------------------------------------


def test_a_country_and_a_rate_are_not_enough_to_ship_a_preset():
    with pytest.raises(PresetError, match="not a qualification"):
        validate_preset(_preset_payload(qualifications=[]))


def test_a_jurisdiction_free_preset_may_carry_no_qualification():
    """An agency fee is proven by a contract, not by a jurisdiction test."""
    normalized = validate_preset(
        _preset_payload(
            preset_key="agency_fee",
            jurisdiction_kind="none",
            jurisdiction_code=None,
            category="AGENCY_FEE",
            qualifications=[],
        )
    )
    assert normalized["qualifications"] == []


def test_a_preset_must_pin_the_version_of_its_source():
    with pytest.raises(PresetError, match="source_reference_version"):
        validate_preset(_preset_payload(source_reference_version=""))


def test_a_preset_cannot_propose_a_pair_no_evaluator_routes():
    with pytest.raises(PresetError, match="does not support form"):
        validate_preset(_preset_payload(category="AGENCY_FEE", form="GROSS_UP"))


def test_a_jurisdiction_kind_and_code_must_agree():
    with pytest.raises(PresetError, match="jurisdiction_code"):
        validate_preset(_preset_payload(jurisdiction_kind="none"))


def test_qualification_codes_are_a_closed_vocabulary():
    with pytest.raises(PresetError, match="not one of"):
        validate_qualifications([{"code": "vibes", "question": "?"}])
    assert "provider_passes_through" in QUALIFICATION_CODES


def test_a_qualification_must_be_a_question_a_human_can_answer():
    with pytest.raises(PresetError, match="question"):
        validate_qualifications([{"code": "threshold_met"}])


# ---------------------------------------------------------------------------
# AC3: a proposal shows why it may apply and what is still unproven.
# ---------------------------------------------------------------------------


def test_a_proposal_narrows_by_jurisdiction_without_proving_anything():
    proposals = propose_from_presets(
        [_preset()], jurisdiction_codes=["FR"], hierarchy_version_id="mdv_EXAMPLE"
    )
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.matched_on == ("jurisdiction:FR",)
    assert proposal.confidence == "unqualified", (
        "a matched jurisdiction with nothing proven is a candidate, not a rate to "
        "start adding to an invoice"
    )
    assert [item["code"] for item in proposal.unproven] == ["provider_passes_through"]
    assert proposal.as_payload()["operator_must_confirm"] == [
        "Does the platform pass this through on its invoice?"
    ]


def test_confidence_counts_answered_questions_and_never_rounds_up():
    """Partly proven is `low`, fully proven is `high`, nothing proven is `unqualified`."""
    two = [
        {
            "code": "provider_passes_through",
            "question": "Does the platform pass this through?",
            "proven": True,
            "evidence": "invoice line 4, March 2026",
        },
        {"code": "threshold_met", "question": "Are the thresholds met?"},
    ]
    partly = propose_from_presets([_preset(qualifications=two)], jurisdiction_codes=["FR"])[0]
    assert partly.confidence == "low"

    all_proven = [{**item, "proven": True, "evidence": "checked"} for item in two]
    proven = propose_from_presets(
        [_preset(qualifications=all_proven)], jurisdiction_codes=["FR"]
    )[0]
    assert proven.confidence == "high"
    assert proven.unproven == ()


def test_a_proposal_for_another_country_is_not_offered():
    assert propose_from_presets([_preset()], jurisdiction_codes=["DE"]) == []


def test_a_jurisdiction_independent_preset_is_always_a_candidate():
    preset = _preset(
        preset_key="agency_fee",
        jurisdiction_kind="none",
        jurisdiction_code=None,
        category="AGENCY_FEE",
        qualifications=[],
    )
    proposals = propose_from_presets([preset], jurisdiction_codes=[])
    assert proposals[0].matched_on == ("jurisdiction_independent",)


def test_a_draft_rule_leaves_rest_of_world_and_unknown_unresolved():
    """Shared knowledge cannot know one Project's posture, so it declares neither."""
    proposal = propose_from_presets(
        [_preset()], jurisdiction_codes=["FR"], hierarchy_version_id="mdv_EXAMPLE"
    )[0]
    assert proposal.draft_rule["rest_of_world_posture"] == "unresolved"
    assert proposal.draft_rule["unknown_posture"] == "unresolved"


def test_a_draft_rule_carries_the_preset_version_as_its_source():
    proposal = propose_from_presets(
        [_preset()], jurisdiction_codes=["FR"], hierarchy_version_id="mdv_EXAMPLE"
    )[0]
    evidence = proposal.draft_rule["source_evidence"]
    assert evidence["preset_version_id"] == "tfpv_EXAMPLE"
    assert evidence["reference_version"] == "2026-01"


def test_a_draft_rule_pins_the_hierarchy_version_it_was_proposed_under():
    proposal = propose_from_presets(
        [_preset()], jurisdiction_codes=["FR"], hierarchy_version_id="mdv_EXAMPLE"
    )[0]
    assert proposal.draft_rule["jurisdiction"]["hierarchy_version_id"] == "mdv_EXAMPLE"


def test_a_draft_rule_carries_no_project_owned_jurisdiction_id():
    """A preset is shared, so it names a CODE. Resolving it to one Project's own
    Country id is the operator's step, and the profile refuses until it happens."""
    from core.governance_rule_sets import RuleSetError
    from core.tax_fee_rule_set import validate_ladder_rule

    proposal = propose_from_presets(
        [_preset()], jurisdiction_codes=["FR"], hierarchy_version_id="mdv_EXAMPLE"
    )[0]
    assert proposal.draft_rule["jurisdiction"]["id"] is None
    with pytest.raises(RuleSetError, match="jurisdiction.id"):
        validate_ladder_rule(proposal.draft_rule)


def test_a_draft_rule_proposed_with_no_hierarchy_cannot_be_published():
    """It fails the ladder profile rather than pinning nothing (AC5)."""
    from core.governance_rule_sets import RuleSetError
    from core.tax_fee_rule_set import validate_ladder_rule

    proposal = propose_from_presets([_preset()], jurisdiction_codes=["FR"])[0]
    rule = dict(proposal.draft_rule)
    rule["jurisdiction"] = {**rule["jurisdiction"], "id": "ctry_EXAMPLE"}
    with pytest.raises(RuleSetError, match="hierarchy_version_id"):
        validate_ladder_rule(rule)


def test_a_fully_proven_draft_rule_does_publish_once_postures_are_declared():
    """The round trip: preset -> proposal -> a rule the ladder profile accepts."""
    from core.tax_fee_rule_set import validate_ladder_rule

    proposal = propose_from_presets(
        [_preset()], jurisdiction_codes=["FR"], hierarchy_version_id="mdv_EXAMPLE"
    )[0]
    rule = dict(proposal.draft_rule)
    rule["jurisdiction"] = {**rule["jurisdiction"], "id": "ctry_EXAMPLE"}
    rule["rest_of_world_posture"] = "exclude"
    rule["unknown_posture"] = "exclude"
    normalized = validate_ladder_rule(rule)
    assert normalized["authority_kind"] == "statutory_reference"
    assert normalized["rate"] == "0.030000"


def test_an_effective_date_window_excludes_a_preset_that_has_not_started():
    preset = _preset(effective_from="2027-01-01")
    assert propose_from_presets([preset], jurisdiction_codes=["FR"], on_date=date(2026, 6, 1)) == []


def test_a_withdrawn_preset_is_never_proposed():
    withdrawn = replace(_preset(), status="withdrawn")
    assert propose_from_presets([withdrawn], jurisdiction_codes=["FR"]) == []
    draft = replace(_preset(), status="draft")
    assert propose_from_presets([draft], jurisdiction_codes=["FR"]) == [], (
        "an imported seed row is a draft candidate; it must not compile until "
        "somebody has read it and published it"
    )


# ---------------------------------------------------------------------------
# The seed importer keeps the caveat Story 41.2 dropped.
# ---------------------------------------------------------------------------


class _SeedRow:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_the_seed_import_preserves_the_source_note_verbatim():
    note = "Default starting point: confirm against your platform invoice."
    payloads = seed_rows_as_preset_payloads(
        [
            _SeedRow(
                iso_code="FR",
                tax_category="REGULATORY_TAX",
                form="PERCENTAGE",
                rate=Decimal("0.030000"),
                label="France DST 3%",
                source_note=note,
                effective_from=date(2019, 1, 1),
            )
        ],
        source_reference_version="2026-07-30",
    )
    assert payloads[0]["assumptions"] == [note]


def test_the_seed_import_refuses_a_row_with_no_note_to_preserve():
    with pytest.raises(PresetError, match="source_note"):
        seed_rows_as_preset_payloads(
            [
                _SeedRow(
                    iso_code="FR",
                    tax_category="REGULATORY_TAX",
                    form="PERCENTAGE",
                    rate=Decimal("0.030000"),
                    label="x",
                    source_note="   ",
                    effective_from=date(2019, 1, 1),
                )
            ],
            source_reference_version="2026-07-30",
        )


def test_an_imported_seed_row_says_it_has_no_taxable_subject():
    payloads = seed_rows_as_preset_payloads(
        [
            _SeedRow(
                iso_code="GB",
                tax_category="REGULATORY_TAX",
                form="PERCENTAGE",
                rate=Decimal("0.020000"),
                label="UK DST 2%",
                source_note="Confirm before use.",
                effective_from=date(2020, 4, 1),
            )
        ],
        source_reference_version="2026-07-30",
    )
    normalized = validate_preset(payloads[0])
    assert "not a taxable subject" in normalized["taxable_subject"]
    assert normalized["verification_status"] == "unverified"
    assert len(normalized["qualifications"]) == 3, (
        "a headline rate with a country raises three questions it cannot answer"
    )


# ---------------------------------------------------------------------------
# AC4: observed evidence, typed gaps, and no verdict drawn from an absence.
# ---------------------------------------------------------------------------


def test_an_unresolved_source_type_is_a_typed_gap():
    gaps = classify_gaps(
        source_type="UNKNOWN",
        source_type_origin="unresolved",
        tax_posture="exclusive",
        observed_inputs={"native_amount_micros": 1, "native_currency": "EUR"},
        geography_evidence={"country_field": "country"},
    )
    assert [gap["code"] for gap in gaps] == [GAP_SOURCE_TYPE_UNRESOLVED]


def test_an_unknown_tax_posture_is_a_gap_not_a_default():
    gaps = classify_gaps(
        source_type="COMMERCE_REVENUE",
        source_type_origin="operator_override",
        tax_posture="unknown",
        observed_inputs={"native_amount_micros": 1},
        geography_evidence={"country_field": "country"},
    )
    assert GAP_TAX_POSTURE_UNKNOWN in {gap["code"] for gap in gaps}


def test_a_publication_with_no_money_cannot_compose_a_component():
    gaps = classify_gaps(
        source_type="PAID_MEDIA",
        source_type_origin="connector_contract",
        tax_posture="exclusive",
        observed_inputs={},
        geography_evidence={"country_field": "country"},
    )
    assert GAP_NO_NATIVE_MONEY in {gap["code"] for gap in gaps}


def test_unknown_source_type_never_concludes_not_applicable():
    verdict = derive_applicability(
        source_type="UNKNOWN", source_type_origin="unresolved", gaps=[]
    )
    assert verdict == "unresolved", (
        "a missing fact proves nothing; Not applicable is a verdict, not a default"
    )


def test_an_inferred_source_type_does_not_settle_not_applicable_either():
    evidence = TaxEvidence(
        evidence_version_id="dste_EXAMPLE",
        datastream_id="ds_EXAMPLE",
        execution_id=None,
        source_type="ORGANIC_ANALYTICS",
        source_type_origin="observed_mapping",
        source_type_confidence="low",
        tax_posture="not_applicable",
        tax_posture_origin="observed_field",
        applicability="unresolved",
        observed_inputs={},
        geography_evidence={},
        gaps=(),
    )
    assert evidence.source_type_is_proved is False


def test_a_contracted_organic_stream_does_conclude_not_applicable():
    verdict = derive_applicability(
        source_type="ORGANIC_ANALYTICS", source_type_origin="connector_contract", gaps=[]
    )
    assert verdict == "not_applicable"


def test_a_zero_input_counts_as_absent_for_a_cpm_rule():
    evidence = TaxEvidence(
        evidence_version_id="dste_EXAMPLE",
        datastream_id="ds_EXAMPLE",
        execution_id=None,
        source_type="PAID_MEDIA",
        source_type_origin="connector_contract",
        source_type_confidence="high",
        tax_posture="exclusive",
        tax_posture_origin="observed_field",
        applicability="applicable",
        observed_inputs={"measured_impressions": None, "native_amount_micros": 5},
        geography_evidence={},
        gaps=(),
    )
    assert "measured_impressions" not in evidence.available_inputs
    assert "native_amount_micros" in evidence.available_inputs


def test_a_recorded_observation_refuses_an_unknown_vocabulary_value():
    with pytest.raises(TaxEvidenceError, match="source_type"):
        record_tax_evidence(
            conn=None,
            project_id="proj_EXAMPLE",
            datastream_id="ds_EXAMPLE",
            execution_id=None,
            source_type="WHATEVER",
        )
