"""Story 48.5, AC1/AC6/AC7: what the Competitors compiler is allowed to conclude.

These run against a fake connection because every rule here is about the VERDICT,
not about storage. The three the old adapter got wrong are stated first, because
each one produced a screen that lied in a different direction:

* every Datastream of a connector looked applicable, so the denominator was
  inflated and Not applicable was never reachable;
* one binding anywhere marked a Datastream Complete, so a Project could read 100%
  while nothing had been collected;
* the dependency fingerprint hashed the Project's role list, so an alias, an
  external identifier, an account or a local exception could change under a
  prepared proposal without invalidating it.
"""

from __future__ import annotations

import pytest
from core.capability_compilers import CompetitorsCompiler
from core.capability_proposals import CompileContext
from core.entity_bindings import Support

DECLARATION = {
    "declaration_version": "1",
    "reports": [
        {
            "report_id": "snapshot",
            "direction": "collect",
            "entity_kinds": ["source_entity"],
            "candidate_field_ids": ["entity_label"],
            "identity_field_id": "entity_ref",
            "label_field_id": "entity_label",
            "own_marker_field_id": None,
            "population": {"completeness": "declared_scope", "note": None},
            "query_driver": {
                "parameter": "entity_ids",
                "value_source": "source_identity",
                "cardinality": "one_request_per_value",
                "own_marker_parameter": None,
                "max_values_per_request": None,
            },
        },
        {
            "report_id": "terms",
            "direction": "observe",
            "entity_kinds": ["brand"],
            "candidate_field_ids": ["entity_label"],
            "identity_field_id": None,
            "label_field_id": "entity_label",
            "own_marker_field_id": None,
            "population": {
                "completeness": "reportable_subset",
                "note": "Low-volume values are withheld while their totals remain counted.",
            },
            "query_driver": None,
        },
    ],
}

NODE_A = "mdnode_AAAAAAAAAAAAAAAAAAAAAAAAAA"
NODE_B = "mdnode_BBBBBBBBBBBBBBBBBBBBBBBBBB"
DATASTREAM = "ds_EXAMPLE"


def _datastream(report_id: str | None = "snapshot", module: str = "example-source") -> dict:
    config = {"source": {"report_id": report_id}} if report_id else {}
    return {
        "id": DATASTREAM,
        "module_name": module,
        "config": config,
        "capability_fingerprint": "fp-1",
        "mapping_payload": {},
        "plan_payload": {},
    }


def _evidence(
    *,
    roles: list[dict] | None = None,
    identities: dict | None = None,
    bindings: list[dict] | None = None,
    observations: dict | None = None,
    unresolved: list[dict] | None = None,
) -> dict:
    return {
        "registry_id": "mdreg_EXAMPLE",
        "project_roles": roles if roles is not None else [_role(NODE_A)],
        "identities_by_node": identities if identities is not None else {},
        "bindings_by_datastream": {DATASTREAM: bindings} if bindings else {},
        "observations": observations or {},
        "unresolved_decisions": unresolved or [],
        "evidence_hash": "a" * 64,
    }


def _role(node_id: str, role: str = "competitor") -> dict:
    return {
        "association_id": f"mdass_{node_id[-6:]}",
        "node_id": node_id,
        "role": role,
        "pinned_version_id": None,
    }


def _identity(node_id: str, connector: str = "example-source") -> dict:
    return {"node_id": node_id, "connector_name": connector, "external_id": "42"}


def _binding(node_id: str, state: str, **extra) -> dict:
    return {
        "datastream_id": DATASTREAM,
        "node_id": node_id,
        "application_state": state,
        "content_hash": "b" * 64,
        "exception_reason_code": extra.get("exception_reason_code"),
        "exception_reason": extra.get("exception_reason"),
    }


_DEFAULT = object()


def _assess(
    datastream: dict, evidence: dict, *, requested_state: str = "enabled", support=_DEFAULT
):
    compiler = CompetitorsCompiler()
    context = CompileContext(
        org_id="org_EXAMPLE",
        project_id="proj_EXAMPLE",
        capability_key="competitors",
        change_set_id="pcs_EXAMPLE",
        intent={},
        requested_state=requested_state,
        project_evidence=evidence,
        actor="operator@example.com",
    )
    if support is _DEFAULT:
        support = DECLARATION
    import core.entity_bindings as bindings_module

    original = bindings_module.tracked_entity_declaration
    bindings_module.tracked_entity_declaration = lambda name: support
    try:
        return compiler.assess(None, context=context, datastream=datastream)
    finally:
        bindings_module.tracked_entity_declaration = original


# ---------------------------------------------------------------------------
# The denominator. Not applicable must be reachable, with a reason.
# ---------------------------------------------------------------------------


def test_a_connector_with_no_declaration_is_not_applicable_not_unavailable():
    assessment = _assess(_datastream(), _evidence(), support=None)
    assert assessment.applicability == "not_applicable"
    assert assessment.coverage_state == "not_applicable"
    assert "declares no tracked-entity contract" in assessment.reason


def test_a_declared_connector_whose_selected_report_is_not_covered_is_not_applicable():
    assessment = _assess(_datastream(report_id="unrelated_report"), _evidence())
    assert assessment.coverage_state == "not_applicable"
    assert "not through the report this Datastream selected" in assessment.reason
    # A different absence from the one above: a different repair, so a different
    # sentence. Collapsing them is what made the old screen unactionable.
    assert (
        assessment.detected_support_selection["reason_code"]
        == "selected_report_declares_no_tracked_entity_support"
    )


def test_a_datastream_with_no_selected_report_is_not_applicable():
    assessment = _assess(_datastream(report_id=None), _evidence())
    assert assessment.coverage_state == "not_applicable"
    assert (
        assessment.detected_support_selection["reason_code"]
        == "datastream_has_no_selected_report"
    )


def test_a_disabled_capability_touches_nothing():
    assessment = _assess(_datastream(), _evidence(), requested_state="disabled")
    assert assessment.coverage_state == "not_applicable"
    assert assessment.detected_support_selection == {"state": "not_requested"}


# ---------------------------------------------------------------------------
# Complete. The state the old adapter handed out for free.
# ---------------------------------------------------------------------------


def test_a_candidate_binding_is_not_complete():
    """A confirmed Project edit produces candidates. Collecting needs publication."""
    assessment = _assess(
        _datastream(),
        _evidence(
            identities={NODE_A: [_identity(NODE_A)]},
            bindings=[_binding(NODE_A, "candidate")],
        ),
    )
    assert assessment.coverage_state == "unavailable"
    assert "0 of 1" in assessment.reason


def test_a_published_binding_with_a_governed_identity_is_complete():
    assessment = _assess(
        _datastream(),
        _evidence(
            identities={NODE_A: [_identity(NODE_A)]},
            bindings=[_binding(NODE_A, "published")],
        ),
    )
    assert assessment.coverage_state == "complete"
    assert assessment.detected_support_selection["covered"] == 1


def test_one_bound_entity_out_of_two_is_partial_never_complete():
    assessment = _assess(
        _datastream(),
        _evidence(
            roles=[_role(NODE_A), _role(NODE_B)],
            identities={NODE_A: [_identity(NODE_A)], NODE_B: [_identity(NODE_B)]},
            bindings=[_binding(NODE_A, "published")],
        ),
    )
    assert assessment.coverage_state == "partial"
    assert assessment.detected_support_selection["covered"] == 1
    assert assessment.detected_support_selection["intended"] == 2


def test_a_governed_identity_for_another_connector_does_not_count_here():
    """The identity exists, but not in this source's vocabulary."""
    assessment = _assess(
        _datastream(),
        _evidence(
            identities={NODE_A: [_identity(NODE_A, connector="another-source")]},
            bindings=[_binding(NODE_A, "published")],
        ),
    )
    assert assessment.coverage_state == "unavailable"
    assert any(item.code == "source_identity_absent" for item in assessment.blockers)


def test_no_project_role_is_missing_governance_not_a_broken_datastream():
    assessment = _assess(_datastream(), _evidence(roles=[]))
    assert assessment.coverage_state == "unavailable"
    assert any(item.code == "missing_governance_evidence" for item in assessment.blockers)
    assert assessment.repair is not None


# ---------------------------------------------------------------------------
# A local exception is its own answer, and survives a global edit.
# ---------------------------------------------------------------------------


def test_an_excluded_datastream_is_excluded_not_incomplete():
    assessment = _assess(
        _datastream(),
        _evidence(
            bindings=[
                _binding(
                    NODE_A,
                    "excluded",
                    exception_reason_code="not_measured_here",
                    exception_reason="This account does not run competitor campaigns.",
                )
            ]
        ),
    )
    assert assessment.coverage_state == "excluded"
    assert [item.reason_code for item in assessment.exceptions] == ["not_measured_here"]


def test_an_exclusion_removes_that_entity_from_the_denominator():
    assessment = _assess(
        _datastream(),
        _evidence(
            roles=[_role(NODE_A), _role(NODE_B)],
            identities={NODE_A: [_identity(NODE_A)]},
            bindings=[
                _binding(NODE_A, "published"),
                _binding(
                    NODE_B,
                    "excluded",
                    exception_reason_code="not_measured_here",
                    exception_reason="Out of scope for this account.",
                ),
            ],
        ),
    )
    assert assessment.coverage_state == "complete"
    assert assessment.detected_support_selection["intended"] == 1
    assert "not_measured_here" in {item.reason_code for item in assessment.exceptions}


# ---------------------------------------------------------------------------
# What the coverage number may never quietly absorb.
# ---------------------------------------------------------------------------


def test_identity_alignment_always_says_it_is_not_commensurability():
    assessment = _assess(
        _datastream(),
        _evidence(
            identities={NODE_A: [_identity(NODE_A)]},
            bindings=[_binding(NODE_A, "published")],
        ),
    )
    assert "identity_is_not_commensurability" in {
        item.reason_code for item in assessment.exceptions
    }


def test_a_truncatable_population_is_a_degrading_exception_not_a_silent_pass():
    assessment = _assess(
        _datastream(report_id="terms"),
        _evidence(
            identities={NODE_A: [_identity(NODE_A)]},
            bindings=[_binding(NODE_A, "published")],
        ),
    )
    subset = next(
        item
        for item in assessment.exceptions
        if item.reason_code == "observed_population_is_a_subset"
    )
    assert subset.severity == "degrading"
    assert "withheld" in subset.reason


def test_an_unresolved_candidate_is_surfaced_rather_than_ignored():
    assessment = _assess(
        _datastream(),
        _evidence(
            identities={NODE_A: [_identity(NODE_A)]},
            bindings=[_binding(NODE_A, "published")],
            unresolved=[{"id": "emd_1", "value": "unknown brand", "state": "proposed"}],
        ),
    )
    codes = {item.code for item in assessment.blockers}
    assert "candidate_decision_unresolved" in codes
    # It does not block Complete: an unresolved candidate is a question about a
    # value nobody has claimed, not a gap in what WAS claimed.
    assert assessment.coverage_state == "complete"


@pytest.mark.parametrize(
    "state,expected",
    [
        ("published", "complete"),
        ("candidate", "unavailable"),
        ("superseded", "unavailable"),
    ],
)
def test_only_a_published_binding_counts(state, expected):
    bindings = [_binding(NODE_A, state)] if state != "superseded" else []
    assessment = _assess(
        _datastream(),
        _evidence(identities={NODE_A: [_identity(NODE_A)]}, bindings=bindings),
    )
    assert assessment.coverage_state == expected


# ---------------------------------------------------------------------------
# Detection is read, never guessed. This is the AC3 boundary seen from here.
# ---------------------------------------------------------------------------


def test_support_carries_the_declared_contract_verbatim():
    support = Support(
        applicable=True,
        reason_code="declared",
        connector_name="example-source",
        report_id="snapshot",
        direction="collect",
        entity_kinds=("source_entity",),
        candidate_field_ids=("entity_label",),
        identity_field_id="entity_ref",
        population={"completeness": "declared_scope"},
        query_driver={"parameter": "entity_ids"},
    )
    projected = support.as_dict()
    assert projected["state"] == "detected"
    assert projected["query_driver"]["parameter"] == "entity_ids"
    assert projected["identity_field_id"] == "entity_ref"
