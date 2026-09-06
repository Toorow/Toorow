"""Story 48.4: what TaxFeesCompiler concludes, and refuses to conclude (AC1, 4, 5, 8).

Offline. The compiler's `assess` reads only its `CompileContext.project_evidence`
and one Datastream dict, so every verdict below is exercised without a database --
which is the point: these are decisions about EVIDENCE, and a fixture that needs
Postgres to state "this Datastream's source type was never observed" would be
testing the fixture.

Each test names the defect it prevents. Before this story the compiler read mutable
rules, hashed eight of their columns, and reported one fabricated Governance owner
per rule; and it read an empty `datastream_source_types` as "no declared type",
which its scope filter treated as matching every source-scoped rule.
"""

from __future__ import annotations

import pytest
from core.capability_compilers import TaxFeesCompiler
from core.capability_proposals import NOT_APPLICABLE, CompileContext
from core.tax_evidence import TaxEvidence
from core.tax_fee_rule_set import TaxFeeLadder, validate_ladder_rules


class _Policy:
    """The two fields the compiler reads off a resolved Money Policy."""

    rule_set_id = "grs_MONEY"
    version_id = "grsv_MONEY"
    content_hash = "1" * 64
    reporting_currency = "EUR"


def _rule(**overrides):
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
        "source_evidence": {
            "issuer": "Agency",
            "reference": "Master services agreement",
            "reference_version": "v3",
            "document_ref": "MSA-2026-B",
        },
    }
    rule.update(overrides)
    return rule


def _ladder(rules=None):
    return TaxFeeLadder(
        rule_set_id="grs_LADDER",
        version_id="grsv_LADDER",
        version_number=4,
        content_hash="2" * 64,
        rounding="half_even",
        default_money_basis="native_source",
        rules=tuple(validate_ladder_rules(rules if rules is not None else [_rule()])),
    )


def _evidence(**overrides):
    fields = {
        "evidence_version_id": "dste_EXAMPLE",
        "datastream_id": "ds_EXAMPLE",
        "execution_id": "exec_EXAMPLE",
        "source_type": "PAID_MEDIA",
        "source_type_origin": "connector_contract",
        "source_type_confidence": "high",
        "tax_posture": "exclusive",
        "tax_posture_origin": "observed_field",
        "applicability": "applicable",
        "observed_inputs": {"native_amount_micros": 5_000_000, "native_currency": "EUR"},
        "geography_evidence": {"country_field": "country"},
        "gaps": (),
    }
    fields.update(overrides)
    return TaxEvidence(**fields)


def _context(
    *,
    ladder=None,
    policy=_Policy(),
    evidence=None,
    requested_state="enabled",
    preset_proposals=(),
):
    by_datastream = {"ds_EXAMPLE": evidence} if evidence is not None else {}
    return CompileContext(
        project_id="proj_EXAMPLE",
        org_id="org_EXAMPLE",
        capability_key="tax_fees",
        change_set_id="pcs_EXAMPLE",
        intent={"capabilities": {"tax_fees": requested_state}},
        actor="owner@example.com",
        requested_state=requested_state,
        project_evidence={
            "ladder": ladder,
            "money_policy": policy,
            "tax_evidence_by_datastream": by_datastream,
            "preset_proposals": list(preset_proposals),
            "drift_dimensions": {},
        },
    )


_DATASTREAM = {"id": "ds_EXAMPLE", "current_plan_version_id": "dpv_EXAMPLE"}


def _assess(**kwargs):
    return TaxFeesCompiler().assess(None, context=_context(**kwargs), datastream=_DATASTREAM)


# ---------------------------------------------------------------------------
# AC1: disabled is disabled, and the dependencies are hard.
# ---------------------------------------------------------------------------


def test_a_disabled_capability_has_no_applicable_scope():
    verdict = _assess(requested_state="disabled", ladder=_ladder(), evidence=_evidence())
    assert verdict.applicability == NOT_APPLICABLE
    assert verdict.detected_support_selection["state"] == "not_requested"


def test_no_published_ladder_is_unavailable_with_a_repair_route():
    verdict = _assess(ladder=None, evidence=_evidence())
    assert verdict.coverage_state == "unavailable"
    assert verdict.repair is not None
    assert [b.code for b in verdict.blockers] == ["missing_governance_evidence"]


def test_an_empty_published_ladder_is_not_a_complete_one():
    verdict = _assess(ladder=_ladder([]), evidence=_evidence())
    assert verdict.coverage_state == "unavailable"


def test_no_money_policy_blocks_because_currency_and_fx_is_required():
    verdict = _assess(ladder=_ladder(), policy=None, evidence=_evidence())
    assert verdict.coverage_state == "unavailable"
    assert "Money Policy" in verdict.reason


# ---------------------------------------------------------------------------
# AC8: one real owner, at a version a workbench can open.
# ---------------------------------------------------------------------------


def test_the_owner_is_the_rule_set_not_one_fabricated_owner_per_rule():
    ladder = _ladder(
        [_rule(rule_key="fee_a", sequence_order=10), _rule(rule_key="fee_b", sequence_order=20)]
    )
    verdict = _assess(ladder=ladder, evidence=_evidence())
    rule_set_owners = [o for o in verdict.governance_owners if o.object_id == "grs_LADDER"]
    assert len(rule_set_owners) == 1, (
        "two rules must not produce two owners: the object an operator edits is the "
        "Rule Set, and its version is what a confirmation rechecks"
    )
    assert rule_set_owners[0].version_id == "grsv_LADDER"
    assert rule_set_owners[0].evidence_hash == "2" * 64


def test_the_money_policy_version_is_pinned_beside_the_ladder():
    verdict = _assess(ladder=_ladder(), evidence=_evidence())
    assert {o.object_id for o in verdict.governance_owners} == {"grs_LADDER", "grs_MONEY"}


def test_the_drift_hash_is_the_versions_own_content_hash():
    """Not a digest of eight selected columns: rate, jurisdiction, source and
    effective dates all sat outside the old hash and could change unseen."""
    verdict = _assess(ladder=_ladder(), evidence=_evidence())
    assert verdict.detected_support_selection["rule_set_version_id"] == "grsv_LADDER"


# ---------------------------------------------------------------------------
# AC4: no verdict is drawn from an absence.
# ---------------------------------------------------------------------------


def test_an_unobserved_datastream_is_unavailable_not_complete():
    verdict = _assess(ladder=_ladder(), evidence=None)
    assert verdict.coverage_state == "unavailable"
    assert [b.code for b in verdict.blockers] == ["tax_evidence_unobserved"]


def test_an_unknown_source_type_is_unresolved_never_not_applicable():
    evidence = _evidence(source_type="UNKNOWN", source_type_origin="unresolved")
    ladder = _ladder([_rule(source_type_scope=["PAID_MEDIA"])])
    verdict = _assess(ladder=ladder, evidence=evidence)
    assert verdict.coverage_state == "unavailable"
    assert verdict.applicability == "applicable", (
        "Not applicable is a verdict; an unresolved source type proves nothing, and "
        "the old compiler read the empty source-type table as a match for everything"
    )


def test_a_proved_source_type_outside_scope_does_conclude_not_applicable():
    evidence = _evidence(source_type="COMMERCE_REVENUE")
    ladder = _ladder([_rule(source_type_scope=["PAID_MEDIA"])])
    verdict = _assess(ladder=ladder, evidence=evidence)
    assert verdict.applicability == NOT_APPLICABLE


def test_refused_rules_are_reported_not_silently_dropped():
    ladder = _ladder(
        [
            _rule(rule_key="in_scope", sequence_order=10),
            _rule(rule_key="other_stream", sequence_order=20, scope_kind="datastream",
                  scope_ref="ds_SOMEONE_ELSE"),
        ]
    )
    verdict = _assess(ladder=ladder, evidence=_evidence())
    support = verdict.detected_support_selection
    assert support["matched_rule_keys"] == ["in_scope"]
    assert [item["code"] for item in support["refused_rules"]] == [
        "scoped_to_another_datastream"
    ], "'does not apply here' and 'was never considered' need different repairs"


def test_a_cpm_rule_without_measured_impressions_is_refused_not_zero():
    ladder = _ladder(
        [
            _rule(
                rule_key="verification",
                category="VERIFICATION",
                form="CPM",
                base_target="MEASURED_IMPRESSIONS",
                cascade_phase=2,
                rate=None,
                cpm_micros=250_000,
                currency="EUR",
            )
        ]
    )
    verdict = _assess(ladder=ladder, evidence=_evidence())
    refused = verdict.detected_support_selection["refused_rules"]
    assert [item["code"] for item in refused] == ["required_input_unavailable"]
    assert "measured_impressions" in refused[0]["reason"]


def test_a_cpm_rule_with_measured_impressions_matches():
    ladder = _ladder(
        [
            _rule(
                rule_key="verification",
                category="VERIFICATION",
                form="CPM",
                base_target="MEASURED_IMPRESSIONS",
                cascade_phase=2,
                rate=None,
                cpm_micros=250_000,
                currency="EUR",
            )
        ]
    )
    evidence = _evidence(
        observed_inputs={"native_amount_micros": 1, "measured_impressions": 120_000}
    )
    verdict = _assess(ladder=ladder, evidence=evidence)
    assert verdict.coverage_state == "complete"


# ---------------------------------------------------------------------------
# AC5: an undecided geography posture is not a complete answer.
# ---------------------------------------------------------------------------


def _geo_rule(**overrides):
    rule = _rule(
        rule_key="dst_fr",
        category="REGULATORY_TAX",
        cascade_phase=3,
        rate="0.03",
        authority_kind="statutory_reference",
        conditions={"country": ["FR"]},
        jurisdiction={
            "kind": "country",
            "id": "ctry_EXAMPLE",
            "hierarchy_version_id": "mdv_EXAMPLE",
        },
        rest_of_world_posture="exclude",
        unknown_posture="exclude",
        source_evidence={
            "issuer": "Tax authority",
            "reference": "DST rate table",
            "reference_version": "2026-01",
            "authoritative_url": "https://example.com/dst",
        },
    )
    rule.update(overrides)
    return rule


def test_a_decided_geography_rule_completes():
    evidence = _evidence(
        observed_inputs={"native_amount_micros": 1, "country": "FR"},
    )
    verdict = _assess(ladder=_ladder([_geo_rule()]), evidence=evidence)
    assert verdict.coverage_state == "complete"


def test_an_unresolved_rest_of_world_posture_degrades_to_partial():
    evidence = _evidence(observed_inputs={"native_amount_micros": 1, "country": "FR"})
    ladder = _ladder([_geo_rule(rest_of_world_posture="unresolved")])
    verdict = _assess(ladder=ladder, evidence=evidence)
    assert verdict.coverage_state == "partial"
    assert [e.reason_code for e in verdict.exceptions] == ["geography_posture_unresolved"]
    assert verdict.repair is not None


def test_a_geography_rule_with_no_observed_country_is_refused():
    """The publication landed no country, so the rule reads Unknown for every row."""
    evidence = _evidence(observed_inputs={"native_amount_micros": 1})
    verdict = _assess(ladder=_ladder([_geo_rule()]), evidence=evidence)
    refused = verdict.detected_support_selection["refused_rules"]
    assert [item["code"] for item in refused] == ["required_input_unavailable"]


def test_a_jurisdiction_independent_rule_needs_no_country_at_all():
    """AC5: an agency fee may remain applicable when Country is Disabled."""
    evidence = _evidence(observed_inputs={"native_amount_micros": 1}, geography_evidence={})
    verdict = _assess(ladder=_ladder(), evidence=evidence)
    assert verdict.coverage_state == "complete"


@pytest.mark.parametrize("posture", ["unresolved"])
def test_an_unresolved_unknown_posture_also_degrades(posture):
    evidence = _evidence(observed_inputs={"native_amount_micros": 1, "country": "FR"})
    ladder = _ladder([_geo_rule(unknown_posture=posture)])
    assert _assess(ladder=ladder, evidence=evidence).coverage_state == "partial"


# ---------------------------------------------------------------------------
# Completeness criterion [0]: "activation opens a blank rule editor when a
# qualified proposal is possible."
#
# The criterion is a CONDITIONAL, and both halves have to be tested. Before this
# change the compiler answered `_missing_governance("a published Tax & Fee Rule
# Ladder version")` whatever the Project held -- so an operator with fifteen
# published presets matching their own countries was sent to the same empty
# workbench as an operator with none, and told only that something was missing.
# `propose_from_presets` had existed since the first commit of this story and was
# reachable ONLY from `fee_tax_mcp`, never from the activation path the criterion
# names.
# ---------------------------------------------------------------------------


def _proposal(**overrides):
    """The payload shape `PresetProposal.as_payload()` produces, trimmed."""
    payload = {
        "preset": {
            "preset_version_id": "tfp_FR_DST",
            "issuer": "Direction generale des Finances publiques",
            "jurisdiction_code": "FR",
            "taxable_subject": "Digital services turnover",
        },
        "matched_on": ["jurisdiction:FR"],
        "why_it_may_apply": "DGFiP states this for FR (Digital services turnover).",
        "unproven_qualifications": [
            {"question": "Does your platform invoice pass this tax through to you?"}
        ],
        "confidence": "low",
        "operator_must_confirm": [
            "Does your platform invoice pass this tax through to you?"
        ],
        "draft_rule": {"rule_key": "fr_dst", "category": "REGULATORY_TAX"},
    }
    payload.update(overrides)
    return payload


def test_a_qualified_proposal_replaces_the_blank_editor():
    verdict = _assess(ladder=None, evidence=_evidence(), preset_proposals=[_proposal()])
    assert verdict.coverage_state == "unavailable"
    # The distinguishing fact: a DIFFERENT blocker code, so a surface can tell
    # "there is something to adopt" from "there is nothing here".
    assert [b.code for b in verdict.blockers] == ["qualified_proposal_available"]
    assert verdict.repair is not None


def test_the_proposal_itself_reaches_the_operator_not_just_its_count():
    """A count is not a proposal: the operator must see WHY and WHAT is unproven."""
    verdict = _assess(ladder=None, evidence=_evidence(), preset_proposals=[_proposal()])
    support = verdict.detected_support_selection
    assert support["state"] == "proposed"
    [proposal] = support["preset_proposals"]
    assert proposal["why_it_may_apply"]
    assert proposal["operator_must_confirm"] == [
        "Does your platform invoice pass this tax through to you?"
    ]
    assert proposal["draft_rule"]["rule_key"] == "fr_dst"


def test_a_proposal_is_never_a_decision():
    """AC3: narrowing by Country does not prove VAT, DST or a pass-through.

    `selected` stays False and the coverage stays `unavailable`: nothing composes
    until an operator adopts and publishes a ladder. A proposal that flipped either
    would be the "silently becomes Project policy" this story forbids.
    """
    verdict = _assess(ladder=None, evidence=_evidence(), preset_proposals=[_proposal()])
    assert verdict.detected_support_selection["selected"] is False
    assert verdict.coverage_state == "unavailable"


def test_with_no_qualified_proposal_the_honest_answer_is_still_what_is_missing():
    """The other half of the conditional. A blank editor is CORRECT when nothing
    can be proposed -- inventing a proposal to avoid one would be worse than the
    defect. So this asserts the old behaviour survives, unchanged."""
    verdict = _assess(ladder=None, evidence=_evidence(), preset_proposals=[])
    assert [b.code for b in verdict.blockers] == ["missing_governance_evidence"]
    assert verdict.detected_support_selection["state"] == "unavailable"


def test_a_published_ladder_wins_over_any_proposal():
    """Once a ladder exists the proposals are noise: the Project has decided."""
    verdict = _assess(
        ladder=_ladder(), evidence=_evidence(), preset_proposals=[_proposal()]
    )
    assert verdict.coverage_state == "complete"
    assert "preset_proposals" not in verdict.detected_support_selection


def test_an_empty_ladder_still_offers_the_proposal():
    """A published-but-empty ladder is the blank editor with extra steps."""
    verdict = _assess(
        ladder=_ladder([]), evidence=_evidence(), preset_proposals=[_proposal()]
    )
    assert [b.code for b in verdict.blockers] == ["qualified_proposal_available"]


# ---------------------------------------------------------------------------
# Task 3, second bullet: the physical inputs a rule needs must be VISIBLE in the
# Datastream tabs, not only implied by a refusal.
#
# `WorkbenchCapabilityPanel` (Story 41.7) already renders the source type, the
# posture, the matched and the refused rules. It could not render what the
# publication actually landed, because the server never sent it -- so a reader saw
# "this CPM rule was refused: no measured impressions" without being able to see
# which inputs DID land, which is the difference between a diagnosis and a verdict.
# ---------------------------------------------------------------------------


def test_the_observed_physical_inputs_reach_the_datastream_panel():
    evidence = _evidence(
        observed_inputs={
            "native_amount_micros": 1_000_000,
            "measured_impressions": 25_000,
            "transaction_count": 0,
        }
    )
    support = _assess(ladder=_ladder(), evidence=evidence).detected_support_selection
    assert support["observed_inputs"] == {
        "native_amount_micros": 1_000_000,
        "measured_impressions": 25_000,
        "transaction_count": 0,
    }
    # A key present with a falsy value is ABSENT for a rule, and the panel must be
    # able to say so rather than print "0" as if it were a measurement.
    assert "transaction_count" not in support["available_inputs"]
    assert sorted(support["available_inputs"]) == [
        "measured_impressions",
        "native_amount_micros",
    ]


def test_the_geography_evidence_reaches_the_panel_too():
    """AC4 names Country/Market evidence among the required inputs."""
    evidence = _evidence(
        observed_inputs={"native_amount_micros": 1},
        geography_evidence={"country_field": "country", "observed_country": "FR"},
    )
    support = _assess(ladder=_ladder(), evidence=evidence).detected_support_selection
    assert support["geography_evidence"]["observed_country"] == "FR"


def test_the_typed_gaps_travel_with_the_inputs():
    """A gap is what turns an absent input into a reason, so it rides along."""
    evidence = _evidence(
        observed_inputs={"native_amount_micros": 1},
        gaps=({"code": "tax_posture_unknown", "detail": "no posture observed"},),
    )
    support = _assess(ladder=_ladder(), evidence=evidence).detected_support_selection
    assert support["observed_gaps"] == [
        {"code": "tax_posture_unknown", "detail": "no posture observed"}
    ]
