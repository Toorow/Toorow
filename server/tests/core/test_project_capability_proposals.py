"""The common per-Datastream capability proposal contract (Story 48.1).

These tests assert the properties the story is about, not the plumbing around
them: the denominator invariant, deterministic replay, refusal of a fabricated or
cross-Project reference, and the pointer invariants activation must preserve.
"""

from __future__ import annotations

import pytest
from core.capability_proposals import (
    IMPACT_BLOCKS,
    CapabilityAssessment,
    CapabilityBlocker,
    CapabilityException,
    CapabilityProposalError,
    GovernanceOwner,
    aggregate_coverage,
    canonical_hash,
    governance_owner_reference,
    proposal_fingerprints,
    registered_compilers,
    required_blockers,
    serialize_proposal,
)


def _impact(**overrides) -> dict:
    impact = {key: {"state": "observed"} for key in IMPACT_BLOCKS}
    impact["grain_before_after"] = {
        "before": ["date"],
        "after": ["date"],
        "added": [],
        "removed": [],
        "changes_grain": False,
    }
    impact["backfill"] = {"required": False, "feasible": None}
    impact["fan_out"] = {"state": "observed", "downstream_consumers": 0}
    impact.update(overrides)
    return impact


def _assessment(**overrides) -> CapabilityAssessment:
    defaults = {
        "applicability": "applicable",
        "coverage_state": "complete",
        "reason": "Compiled from the active plan.",
        "detected_support_selection": {"state": "detected", "selected": True},
    }
    defaults.update(overrides)
    return CapabilityAssessment(**defaults)


def _serialize(**overrides) -> dict:
    kwargs = {
        "project_id": "proj_EXAMPLE",
        "org_id": "org_EXAMPLE",
        "datastream_id": "ds_EXAMPLE",
        "capability_key": "country",
        "change_set_id": "pcset_EXAMPLE",
        "base_configuration_version_id": "pcfg_EXAMPLE",
        "intended_configuration_content_hash": "a" * 64,
        "connector_contract": {"module_name": "example", "quota_cost": {}},
        "source_schema": {"source_schema_hash": "b" * 64, "field_count": 3},
        "current_plan_version_id": "dsp_EXAMPLE",
        "current_mapping_version_id": "dmap_EXAMPLE",
        "current_published_execution_id": None,
        "assessment": _assessment(),
        "impact": _impact(),
        "dependency_snapshot": {"datastream_id": "ds_EXAMPLE"},
    }
    kwargs.update(overrides)
    return serialize_proposal(**kwargs)


# ---------------------------------------------------------------------------
# Coverage: the denominator is the whole point.
# ---------------------------------------------------------------------------


def test_the_five_states_sum_to_the_applicable_denominator() -> None:
    coverage = aggregate_coverage(
        ["complete", "complete", "partial", "unavailable", "excluded", "pending"]
    )
    assert coverage["applicable"] == 6
    assert (
        coverage["complete"]
        + coverage["partial"]
        + coverage["unavailable"]
        + coverage["excluded"]
        + coverage["pending"]
        == coverage["applicable"]
    )


def test_excluded_stays_in_the_denominator_and_not_applicable_stays_out() -> None:
    # Two applicable Datastreams, one of them deliberately excluded: 50%, not 100%.
    # Dropping the exclusion from the denominator would inflate the promise.
    coverage = aggregate_coverage(["complete", "excluded", "not_applicable", "not_applicable"])
    assert coverage["applicable"] == 2
    assert coverage["not_applicable"] == 2
    assert coverage["percentage"] == 50.0


def test_zero_applicable_renders_not_applicable_never_full() -> None:
    coverage = aggregate_coverage(["not_applicable", "not_applicable"])
    assert coverage["applicable"] == 0
    assert coverage["label"] == "Not applicable"
    assert coverage["percentage"] is None


def test_an_unknown_coverage_state_is_refused() -> None:
    with pytest.raises(CapabilityProposalError):
        aggregate_coverage(["mostly_fine"])


# ---------------------------------------------------------------------------
# The assessment contract: an exclusion is a decision, not an absence.
# ---------------------------------------------------------------------------


def test_excluded_coverage_requires_an_explicit_exclusion_decision() -> None:
    with pytest.raises(CapabilityProposalError, match="exclusion decision"):
        _assessment(coverage_state="excluded", reason="Left out.")
    # With the governed decision attached it is accepted.
    accepted = _assessment(
        coverage_state="excluded",
        reason="Excluded by the data owner.",
        exceptions=(
            CapabilityException(
                kind="exclusion",
                severity="informational",
                reason_code="owner_excluded",
                reason="This source is out of scope for this Project.",
                owner_kind="governance",
            ),
        ),
    )
    assert accepted.coverage_state == "excluded"


def test_applicability_and_coverage_state_must_agree() -> None:
    with pytest.raises(CapabilityProposalError, match="disagree"):
        _assessment(applicability="not_applicable", coverage_state="complete")
    with pytest.raises(CapabilityProposalError, match="disagree"):
        _assessment(applicability="applicable", coverage_state="not_applicable")


def test_a_coverage_state_must_carry_its_reason() -> None:
    with pytest.raises(CapabilityProposalError, match="reason"):
        _assessment(reason="   ")


# ---------------------------------------------------------------------------
# The serializer: deterministic, exact, and one shape for both callers.
# ---------------------------------------------------------------------------


def test_the_same_inputs_always_produce_the_same_content_hash() -> None:
    one = _serialize()
    two = _serialize()
    assert one["content_hash"] == two["content_hash"]
    assert one == two


def test_the_content_hash_moves_when_any_dependency_pin_moves() -> None:
    baseline = _serialize()["content_hash"]
    for field, value in (
        ("current_plan_version_id", "dsp_OTHER"),
        ("current_mapping_version_id", "dmap_OTHER"),
        ("source_schema", {"source_schema_hash": "c" * 64, "field_count": 3}),
        ("connector_contract", {"module_name": "other", "quota_cost": {}}),
        ("intended_configuration_content_hash", "d" * 64),
    ):
        assert _serialize(**{field: value})["content_hash"] != baseline, field


def test_an_inexact_impact_shape_is_refused() -> None:
    incomplete = _impact()
    incomplete.pop("fan_out")
    with pytest.raises(CapabilityProposalError, match="inexact"):
        _serialize(impact=incomplete)
    with pytest.raises(CapabilityProposalError, match="inexact"):
        _serialize(impact={**_impact(), "invented_block": {}})


def test_the_proposal_is_scoped_to_one_project_and_one_change_set() -> None:
    proposal = _serialize()
    assert proposal["project_id"] == "proj_EXAMPLE"
    assert proposal["change_set_id"] == "pcset_EXAMPLE"
    # A cross-Project reference cannot be smuggled in: the scope is part of the
    # hashed body, so a swapped project yields a different proposal identity.
    other = _serialize(project_id="proj_OTHER")
    assert other["content_hash"] != proposal["content_hash"]


def test_governance_owners_are_pinned_by_version_with_a_semantic_route() -> None:
    proposal = _serialize(
        assessment=_assessment(
            governance_owners=(
                GovernanceOwner(
                    object_type="registry",
                    object_id="project-geography",
                    version_id="e" * 64,
                    evidence_hash="e" * 64,
                ),
            )
        )
    )
    reference = proposal["governance_owner_references"][0]
    assert reference["version_id"] == "e" * 64
    owner = reference["owner_reference"]
    # A semantic reference, never a URL: the console resolves it through the
    # canonical registry, so an Epic 49 rename cannot be frozen into a version.
    assert owner["surface"] == "project"
    assert owner["workspace"] == "governance"
    assert owner["section"] == "master-data"
    assert "/org/" not in canonical_hash(owner)
    assert not any(isinstance(value, str) and value.startswith("/") for value in owner.values())


def test_required_blockers_carry_their_exact_owner() -> None:
    proposal = _serialize(
        assessment=_assessment(
            coverage_state="unavailable",
            reason="No compatible country grain.",
            blockers=(
                CapabilityBlocker(code="country_grain_unavailable", message="Select a report."),
                CapabilityBlocker(code="advisory", message="Nice to have", required=False),
            ),
        )
    )
    blockers = required_blockers([proposal])
    assert [item["code"] for item in blockers] == ["country_grain_unavailable"]
    assert blockers[0]["datastream_id"] == "ds_EXAMPLE"
    assert blockers[0]["owner_reference"]["object_id"] == "ds_EXAMPLE"


def test_proposal_fingerprints_freeze_the_exact_reviewed_identities() -> None:
    proposal = {**_serialize(), "id": "dscp_EXAMPLE"}
    frozen = proposal_fingerprints([proposal])
    assert frozen == [
        {
            "proposal_id": "dscp_EXAMPLE",
            "datastream_id": "ds_EXAMPLE",
            "capability_key": "country",
            "content_hash": proposal["content_hash"],
            "dependency_fingerprint": proposal["dependency_fingerprint"],
        }
    ]


# ---------------------------------------------------------------------------
# The registry: exactly six capabilities plug into the common contract.
# ---------------------------------------------------------------------------


def test_every_declared_capability_registers_a_compiler_and_no_other() -> None:
    """One compiler per declared key, and the set is compared to the DECLARATION.

    `compiler_for` RAISES `no compiler registered` the moment an activation is
    requested for a key it does not know, so a key declared without its compiler
    would show `Off` on every screen and could never be turned on -- delivered
    and unreachable. That was measured on `placement_mapping` (61.5) and holds
    for `analytics_alignment` (70.3).

    The expectation is `CAPABILITY_SPECS` and not a written-out list: a literal
    passes for exactly as long as somebody remembers to edit it, and the failure
    it is supposed to catch is somebody forgetting.
    """
    import core.capability_compilers  # noqa: F401 -- registers on import
    from core.project_settings import CAPABILITY_SPECS

    assert sorted(registered_compilers()) == sorted(CAPABILITY_SPECS)


def test_every_capability_resolves_a_canonical_governance_section() -> None:
    for capability_key in registered_compilers():
        owner = governance_owner_reference(capability_key)
        assert owner["workspace"] == "governance"
        # The four Epic 49 Level 2 screens, and nothing invented alongside them.
        assert owner["section"] in {
            "master-data",
            "semantic-model",
            "controls-quality",
            "evidence",
        }


def test_every_declared_capability_has_a_governance_section_not_only_the_compiled_ones() -> None:
    """The registry that decides this is `CAPABILITY_SPECS`, not the compilers.

    `read_datastream_capabilities` loops over `CAPABILITY_SPECS` and calls
    `governance_owner_reference` for every capability with no proposal yet -- and
    that function RAISES on a key it does not know. A sixth key added to
    `CAPABILITY_SPECS` in one commit and to `GOVERNANCE_SECTION_BY_CAPABILITY` in
    the next would take down the Workbench projection of every Datastream, on
    every Project, in between. The two maps move together or not at all.
    """
    from core.project_settings import CAPABILITY_SPECS

    for capability_key in CAPABILITY_SPECS:
        owner = governance_owner_reference(capability_key)
        assert owner["workspace"] == "governance"
        assert owner["section"]


def test_placement_mapping_is_not_applicable_and_says_why() -> None:
    """`not_applicable`, with its reason -- and that is the RIGHT answer today.

    No table of observed placements exists yet (story 61.1 measured it), so this
    compiler has nothing to read. `unavailable` would keep every Datastream in
    the applicable denominator -- `aggregate_coverage` takes only
    `not_applicable` out of it -- and 37 of 39 connectors declaring no placement
    dimension would drag the coverage of EVERY Project down. A percentage that
    counts Datastreams the capability does not apply to is a number that lies.
    """
    from core.capability_compilers import PlacementMappingCompiler
    from core.capability_proposals import NOT_APPLICABLE, CompileContext

    compiler = PlacementMappingCompiler()
    evidence = compiler.project_evidence(None, project_id="proj_EXAMPLE", org_id="org_EXAMPLE")
    context = CompileContext(
        org_id="org_EXAMPLE",
        project_id="proj_EXAMPLE",
        capability_key="placement_mapping",
        change_set_id="pcset_EXAMPLE",
        intent={"capabilities": {"placement_mapping": "enabled"}},
        requested_state="enabled",
        project_evidence=evidence,
        actor="person_EXAMPLE",
    )

    assessment = compiler.assess(None, context=context, datastream={"id": "ds_EXAMPLE"})

    assert assessment.applicability == NOT_APPLICABLE
    assert assessment.coverage_state == NOT_APPLICABLE
    assert "placement" in assessment.reason.lower()

    # NOT A ZERO, ON EITHER PATH. Zero is a count taken over a table that exists
    # and held no row; no placement table exists, so nobody counted. The
    # Project-wide read and the per-Datastream verdict answer the same absence
    # the same way -- they are built from the same dict for that reason.
    for block in (evidence, assessment.detected_support_selection):
        assert block["observed_placements"] is None
        assert block["mapped_plan_lines"] is None
        assert 0 not in block.values()
    # Out of the denominator, and out of it for a stated reason rather than by
    # being dropped from the fan-out.
    assert aggregate_coverage([assessment.coverage_state])["applicable"] == 0
    assert aggregate_coverage([assessment.coverage_state])["not_applicable"] == 1


def test_an_unknown_capability_is_refused_rather_than_defaulted() -> None:
    with pytest.raises(CapabilityProposalError):
        governance_owner_reference("invented_capability")

def test_country_compiler_previews_the_future_plan_not_only_the_current_one(monkeypatch):
    import core.source_capabilities as source_capabilities
    from core.capability_compilers import CountryCompiler
    from core.capability_proposals import CompileContext

    from tests.core.test_geographic_plan_compilation import (
        _capabilities,
        _country_evidence,
        _intent,
    )

    monkeypatch.setattr(
        source_capabilities,
        "get_scoped_source_capabilities",
        lambda **_kwargs: _capabilities(),
    )
    context = CompileContext(
        org_id="org_EXAMPLE",
        project_id="proj_EXAMPLE",
        capability_key="country",
        change_set_id="pcset_EXAMPLE",
        intent={"capabilities": {"country": "enabled"}},
        requested_state="enabled",
        project_evidence=_country_evidence(),
        actor="person_EXAMPLE",
        loaded_modules=[object()],
    )
    datastream = {
        "id": "ds_EXAMPLE",
        "source_kind": "connector_pull",
        "module_name": "provider-x",
        "connection_ref_id": "conn_01",
        "plan_payload": _intent(),
        "current_mapping_version_id": "dmap_EXAMPLE",
        "mapping_payload": {"grain": ["date", "campaign_id"]},
        # A published global execution does not evidence retained Country rows.
        "current_published_execution_id": "dsexec_global_1",
    }

    assessment = CountryCompiler().assess(
        object(), context=context, datastream=datastream
    )

    assert assessment.coverage_state == "partial"
    assert assessment.detected_support_selection["compilation_status"] == "country_complete"
    assert len(assessment.detected_support_selection["proposed_plan_content_hash"]) == 64
    assert assessment.grain_after == ["campaign_id", "date", "geo_country"]

    monkeypatch.setattr(
        source_capabilities,
        "get_scoped_source_capabilities",
        lambda **_kwargs: _capabilities(compatible=False),
    )
    incompatible = CountryCompiler().assess(
        object(), context=context, datastream=datastream
    )
    assert incompatible.coverage_state == "unavailable"
    assert [item.code for item in incompatible.blockers] == [
        "country_grain_unavailable"
    ]

def test_country_compiler_blocks_an_active_datastream_without_a_current_mapping():
    from core.capability_compilers import CountryCompiler
    from core.capability_proposals import CompileContext

    from tests.core.test_geographic_plan_compilation import _country_evidence, _intent

    context = CompileContext(
        org_id="org_EXAMPLE",
        project_id="proj_EXAMPLE",
        capability_key="country",
        change_set_id="pcset_EXAMPLE",
        intent={"capabilities": {"country": "enabled"}},
        requested_state="enabled",
        project_evidence=_country_evidence(),
        actor="person_EXAMPLE",
        loaded_modules=[object()],
    )
    assessment = CountryCompiler().assess(
        object(),
        context=context,
        datastream={"id": "ds_EXAMPLE", "plan_payload": _intent()},
    )

    assert assessment.coverage_state == "unavailable"
    assert [blocker.code for blocker in assessment.blockers] == [
        "country_mapping_unavailable"
    ]


def test_capability_proposals_are_scoped_to_enabled_active_datastreams():
    from core.capability_proposals import _DATASTREAM_QUERY

    normalized = " ".join(_DATASTREAM_QUERY.split())
    assert "d.enabled = TRUE" in normalized
    assert "d.lifecycle_state = 'active'" in normalized
    assert "d.archived_at IS NULL" in normalized


def test_a_capability_that_is_off_is_absent_from_the_datastream_projection(monkeypatch) -> None:
    """« Éteinte, la capacité n'apparaît nulle part » -- amendment 2 of 2026-08-05.

    Held open for six days by `read_datastream_capabilities`, which looped over
    `CAPABILITY_SPECS` -- the six DECLARED keys -- and never opened
    `app.project_capabilities` at all. Measured on the Project of the 2026-08-11
    review: four `disabled`, two `draft`, none `ready` -- six of six should have
    been absent, and the Workbench drew six of six on three tabs each.

    The threshold is `CAPABILITY_ACTIVE_STATES`, the same one `capabilityTabs.ts`
    uses to open `Cost` and `Placements`. Two thresholds would let the tab and
    the panel disagree about one capability, which is the defect one level up.
    """
    from core import capability_proposals
    from core.project_settings import CAPABILITY_SPECS

    states = {
        "country": {"state": "disabled", "availability": "optional"},
        "currency_fx": {"state": "draft", "availability": "always_present"},
        "reporting_timezone": {"state": "ready", "availability": "always_present"},
        "tax_fees": {"state": "unset", "availability": "optional"},
        "competitors": {"state": "blocked", "availability": "optional"},
        "placement_mapping": {"state": "degraded", "availability": "optional"},
        # The seventh, story 70.3, and `disabled` on purpose: the assertion below
        # is about which states let a capability through, so the newest key is
        # added on the OFF side rather than quietly widening the expected result.
        "analytics_alignment": {"state": "disabled", "availability": "optional"},
    }
    assert set(states) == set(CAPABILITY_SPECS), "the fixture must cover every declared key"
    monkeypatch.setattr(
        capability_proposals,
        "_latest_proposals",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "core.project_capability_states.read_capability_states",
        lambda *_a, **_k: states,
    )

    projection = capability_proposals.read_datastream_capabilities(
        object(), project_id="proj_EXAMPLE", datastream_id="ds_EXAMPLE"
    )

    # `ready` and `degraded`, and nothing else. `unset` -- no row at all, meaning
    # the seed never ran -- is off too: it is a fact about the control plane, not
    # a default this read may paper over.
    assert [item["capability_key"] for item in projection["capabilities"]] == [
        "reporting_timezone",
        "placement_mapping",
    ]


def test_a_project_with_no_capability_on_projects_nothing_at_all(monkeypatch) -> None:
    """Zero capabilities, so the panel renders NOTHING -- not an empty panel.

    `capabilityProjection` in the console returns `null` on an empty list and
    `WorkbenchCapabilityPanel` returns `null` on that, so the whole block is
    absent -- « ni onglet, ni panneau, ni colonne ». A projection that returned
    six `pending` rows was the reason the panel was never absent for anybody.
    """
    from core import capability_proposals
    from core.project_settings import CAPABILITY_SPECS

    monkeypatch.setattr(capability_proposals, "_latest_proposals", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "core.project_capability_states.read_capability_states",
        lambda *_a, **_k: {
            key: {"state": "disabled", "availability": "optional"} for key in CAPABILITY_SPECS
        },
    )

    projection = capability_proposals.read_datastream_capabilities(
        object(), project_id="proj_EXAMPLE", datastream_id="ds_EXAMPLE"
    )

    assert projection["capabilities"] == []
    assert projection["primary_action"] is None
