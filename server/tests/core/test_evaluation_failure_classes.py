"""Story 75-7 -- the classification rules, one fixture per class.

WHAT THIS FILE IS FOR. `evaluation_failure_classes` decides, from evidence a run
already carries, WHICH gap would fill a failing case. Two properties matter more
than any single class:

  1. a case that matches TWO rules reports the earlier one, and the rule order is
     asserted here rather than left as a comment;
  2. a fact nobody read never fires a rule -- an absent `exemplars_active` or an
     unread settings source must not classify anything.

These are pure functions over dicts, so nothing here needs a database. The
pg-gated file beside it proves the LOADER hands them the shapes they expect.
"""

from __future__ import annotations

import pytest
from core.evaluation_failure_classes import (
    CLASS_FISCAL_OR_SCOPE,
    CLASS_JOIN_PATH,
    CLASS_MISSING_DEFINITION,
    CLASS_MISSING_EXEMPLAR,
    CLASS_NO_SUBJECT,
    CLASS_OTHER,
    CLASS_PASSED,
    CLASS_UNVERIFIABLE_BY_DESIGN,
    ENVIRONMENT_PINS,
    RULE_ORDER,
    EnvironmentDrift,
    FingerprintMismatch,
    RunNotFinalized,
    class_counts,
    classification_report,
    classify_case,
    compare_runs,
    dimension_counts,
    environment_drift,
    harvest_node_keys,
    object_type_of,
)

#: The seven environment pins, identical on both sides of a comparison. A real
#: run carries them from `app.evaluation_runs`; here they are a fixture, and the
#: tests that mean to move one move exactly one.
_ENVIRONMENT = {
    "semantic_view_version_id": "svv_EXAMPLE",
    "context_version_set_id": "ecvs_EXAMPLE",
    "model_ref": "model-example-1",
    "host_capability_profile": {"host": "example-host", "tools": ["get_exemplars"]},
    "tool_catalog_version": "c" * 64,
    "data_snapshot_hash": "d" * 64,
    "as_of": "2026-07-31",
}


def _environment(**overrides):
    pins = dict(_ENVIRONMENT)
    pins.update(overrides)
    return pins


_CONCEPT = "governance/semantic-concept/sc_EXAMPLE"
_VIEW = "governance/semantic-view/sv_EXAMPLE"
_NOTE = "context-hub/knowledge-note/kn_EXAMPLE"
_RESULT = "analyze/result/res_EXAMPLE"


def _verdicts(**overrides):
    """Six verdicts, the mechanical shape a run writes when nothing was measured."""
    base = {
        "semantic_correctness": {
            "verdict": "unverifiable",
            "reason_code": "dimension_evaluator_not_delivered",
            "evidence_refs": {},
        },
        "provenance_correctness": {
            "verdict": "unverifiable",
            "reason_code": "dimension_evaluator_not_delivered",
            "evidence_refs": {},
        },
        "dq_handling": {
            "verdict": "unverifiable",
            "reason_code": "dimension_evaluator_not_delivered",
            "evidence_refs": {},
        },
        "context_adherence": {
            "verdict": "unverifiable",
            "reason_code": "path_comparison_not_delivered",
            "evidence_refs": {},
        },
        "path_quality": {
            "verdict": "unverifiable",
            "reason_code": "path_comparison_not_delivered",
            "evidence_refs": {},
        },
        "mcp_app_behavior": {
            "verdict": "unverifiable",
            "reason_code": "render_not_pinned_for_this_subject",
            "evidence_refs": {},
        },
    }
    base.update(overrides)
    return base


def _case(**overrides):
    case = {
        "case_id": "ecase_EXAMPLE",
        "golden_question_version_id": "gqv_EXAMPLE",
        "question": "What was paid media spend last completed month, by market?",
        "severity": "critical",
        "result_id": "res_EXAMPLE",
        "ai_path_id": "aip_EXAMPLE",
        "ai_path_absent_literal": None,
        "verdicts": _verdicts(),
        "path_comparison": None,
        "assertion_results": [],
        "time_boundary": {"as_of": "2026-07-31", "grain": "month"},
        "exemplars_active": None,
        "fiscal_calendar_source": None,
        "query_scope_source": None,
    }
    case.update(overrides)
    return case


def _comparison(**overrides):
    comparison = {
        "evidence_state": "observed",
        "path_verdict": "fail",
        "required_missing": [],
        "forbidden_present": [],
        "order_violations": [],
        "version_mismatches": [],
    }
    comparison.update(overrides)
    return comparison


# ---------------------------------------------------------------------------
# One fixture per class.
# ---------------------------------------------------------------------------


def test_no_subject_when_nothing_was_walked():
    """The model never answered this question, so nothing else can be read off it."""
    verdict = classify_case(_case(result_id=None, ai_path_id=None))
    assert verdict["class"] == CLASS_NO_SUBJECT
    assert "no Result and no observed AI Path pinned on the case" in verdict["signals"]
    assert "subjects" in verdict["gesture"]


def test_a_declared_absent_path_is_not_a_missing_subject():
    """`No AI path` CLAIMS no AI was involved; it is not an absence of evidence."""
    verdict = classify_case(
        _case(result_id=None, ai_path_id=None, ai_path_absent_literal="No AI path")
    )
    assert verdict["class"] != CLASS_NO_SUBJECT


def test_missing_definition_from_a_required_concept_node():
    verdict = classify_case(
        _case(path_comparison=_comparison(required_missing=[{"key": _CONCEPT}]))
    )
    assert verdict["class"] == CLASS_MISSING_DEFINITION
    assert verdict["story"] == "75-1"
    assert "propose_calculated_field" in verdict["door"]


def test_missing_definition_from_an_assertion_that_named_an_absent_field():
    verdict = classify_case(
        _case(
            assertion_results=[
                {
                    "assertion_type": "value",
                    "verdict": "unverifiable",
                    "reason_code": "required_field_missing",
                    "evidence_refs": {"field": "media_spend_micros"},
                }
            ]
        )
    )
    assert verdict["class"] == CLASS_MISSING_DEFINITION
    assert any("media_spend_micros" in signal for signal in verdict["signals"])


def test_join_path_from_a_required_view_node():
    verdict = classify_case(_case(path_comparison=_comparison(required_missing=[{"key": _VIEW}])))
    assert verdict["class"] == CLASS_JOIN_PATH
    assert verdict["story"] == "75-5"
    assert "answerable-topics" in verdict["door"]


def test_join_path_reads_a_version_mismatch_too():
    """A node present at the WRONG version is a finding, not an absence."""
    verdict = classify_case(
        _case(
            path_comparison=_comparison(
                version_mismatches=[{"key": _VIEW, "expected": {}, "observed": {}}]
            )
        )
    )
    assert verdict["class"] == CLASS_JOIN_PATH


def test_fiscal_setting_when_the_question_asks_in_a_calendar_nobody_declared():
    verdict = classify_case(
        _case(
            time_boundary={"as_of": "2026-07-31", "grain": "fiscal_quarter"},
            fiscal_calendar_source="PLATFORM",
        )
    )
    assert verdict["class"] == CLASS_FISCAL_OR_SCOPE
    assert verdict["story"] == "75-4"
    assert "ai-settings" in verdict["door"]


def test_a_declared_fiscal_calendar_does_not_fire_the_setting_class():
    verdict = classify_case(
        _case(
            time_boundary={"as_of": "2026-07-31", "grain": "fiscal_quarter"},
            fiscal_calendar_source="PROJECT",
        )
    )
    assert verdict["class"] != CLASS_FISCAL_OR_SCOPE


def test_an_unread_settings_source_never_fires_the_setting_class():
    """A fact nobody read must not classify anything. `None` is not `PLATFORM`."""
    verdict = classify_case(
        _case(
            time_boundary={"grain": "fiscal_year"},
            fiscal_calendar_source=None,
            query_scope_source=None,
        )
    )
    assert verdict["class"] != CLASS_FISCAL_OR_SCOPE


def test_a_forbidden_node_crossed_with_no_declared_scope_is_a_setting_gap():
    verdict = classify_case(
        _case(
            path_comparison=_comparison(
                forbidden_present=[{"rule": {"tool_name": "run_raw_sql"}, "step_ordinal": 2}]
            ),
            query_scope_source="PLATFORM",
        )
    )
    assert verdict["class"] == CLASS_FISCAL_OR_SCOPE
    assert any("forbidden node" in signal for signal in verdict["signals"])


def test_missing_exemplar_from_a_context_node_never_crossed():
    verdict = classify_case(_case(path_comparison=_comparison(required_missing=[{"key": _NOTE}])))
    assert verdict["class"] == CLASS_MISSING_EXEMPLAR
    assert verdict["story"] == "75-3"


def test_missing_exemplar_from_a_violated_context_before_query_prerequisite():
    verdict = classify_case(
        _case(
            path_comparison=_comparison(
                order_violations=[{"before": _NOTE, "after": _VIEW, "before_ordinal": 3}]
            )
        )
    )
    assert verdict["class"] == CLASS_MISSING_EXEMPLAR
    assert any("prerequisite violated" in signal for signal in verdict["signals"])


def test_the_gesture_changes_when_the_subject_already_has_an_exemplar():
    """READ, never guessed: an exemplar that exists was not missing, it was unread."""
    without = classify_case(
        _case(path_comparison=_comparison(required_missing=[{"key": _NOTE}]), exemplars_active=0)
    )
    with_one = classify_case(
        _case(path_comparison=_comparison(required_missing=[{"key": _NOTE}]), exemplars_active=2)
    )
    assert without["class"] == with_one["class"] == CLASS_MISSING_EXEMPLAR
    assert "Publish and activate" in without["gesture"]
    assert "already has 2 active exemplar" in with_one["gesture"]


def test_passed_when_something_was_measured_and_nothing_failed():
    verdict = classify_case(
        _case(
            verdicts=_verdicts(
                path_quality={
                    "verdict": "pass",
                    "reason_code": "path_matches_expected_pattern",
                    "evidence_refs": {},
                }
            )
        )
    )
    assert verdict["class"] == CLASS_PASSED
    assert verdict["passed_dimensions"] == ["path_quality"]


def test_unverifiable_by_design_offers_no_gesture_of_this_epic():
    verdict = classify_case(_case())
    assert verdict["class"] == CLASS_UNVERIFIABLE_BY_DESIGN
    assert verdict["story"] == "-"
    assert "None in this epic" in verdict["gesture"]


def test_other_names_the_failing_dimension_instead_of_inventing_a_class():
    verdict = classify_case(
        _case(
            verdicts=_verdicts(
                semantic_correctness={
                    "verdict": "fail",
                    "reason_code": "assertion_failed",
                    "evidence_refs": {},
                }
            )
        )
    )
    assert verdict["class"] == CLASS_OTHER
    assert any("assertion_failed" in signal for signal in verdict["signals"])
    assert verdict["gesture"].startswith("None invented")


def test_other_names_an_unrouted_node_key():
    """An object type no rail owns is printed, not squeezed into a class."""
    verdict = classify_case(_case(path_comparison=_comparison(required_missing=[{"key": _RESULT}])))
    assert verdict["class"] == CLASS_OTHER
    assert any(_RESULT in signal for signal in verdict["signals"])


# ---------------------------------------------------------------------------
# The rule order, asserted rather than commented.
# ---------------------------------------------------------------------------


def test_the_rule_order_is_the_ratified_one():
    assert RULE_ORDER == (
        CLASS_NO_SUBJECT,
        CLASS_MISSING_DEFINITION,
        CLASS_JOIN_PATH,
        CLASS_FISCAL_OR_SCOPE,
        CLASS_MISSING_EXEMPLAR,
        CLASS_PASSED,
        CLASS_UNVERIFIABLE_BY_DESIGN,
        CLASS_OTHER,
    )


def test_a_case_matching_two_rules_reports_the_earlier_one():
    """A definition AND a view are both missing: the definition is the prerequisite.

    Binding the view first would move a number without making the question
    answerable, which is why `missing_definition` is ahead of `join_path`.
    """
    verdict = classify_case(
        _case(path_comparison=_comparison(required_missing=[{"key": _VIEW}, {"key": _CONCEPT}]))
    )
    assert verdict["class"] == CLASS_MISSING_DEFINITION


def test_the_signals_of_both_matched_rules_travel_in_the_report():
    """The ratified sentence, asserted: "the signals of both travel in the report".

    The earlier class decides; the later one's evidence is still printed, and
    named as the later class, so a reader who fills the definition already knows
    the view is missing too instead of discovering it on the next run.
    """
    verdict = classify_case(
        _case(path_comparison=_comparison(required_missing=[{"key": _VIEW}, {"key": _CONCEPT}]))
    )
    assert verdict["class"] == CLASS_MISSING_DEFINITION
    assert any(_CONCEPT in signal for signal in verdict["signals"])
    view_signals = [signal for signal in verdict["signals"] if _VIEW in signal]
    assert view_signals, verdict["signals"]
    # Named as the class that saw it, never as the reason this class was chosen.
    assert all(signal.startswith(f"also `{CLASS_JOIN_PATH}`") for signal in view_signals)


def test_a_context_signal_travels_under_a_definition_class():
    """Three rules fire; the report still names one class and loses no evidence."""
    verdict = classify_case(
        _case(
            path_comparison=_comparison(
                required_missing=[{"key": _CONCEPT}, {"key": _VIEW}, {"key": _NOTE}]
            )
        )
    )
    assert verdict["class"] == CLASS_MISSING_DEFINITION
    assert [signal for signal in verdict["signals"] if _VIEW in signal]
    assert [signal for signal in verdict["signals"] if _NOTE in signal]


def test_no_subject_wins_over_unverifiable_by_design():
    """The case nobody walked also has six `unverifiable` verdicts.

    Reporting it as `unverifiable_by_design` would blame an undelivered evaluator
    for an execution that never happened.
    """
    verdict = classify_case(_case(result_id=None, ai_path_id=None, verdicts=_verdicts()))
    assert verdict["class"] == CLASS_NO_SUBJECT


def test_a_context_gap_loses_to_a_definition_gap():
    verdict = classify_case(
        _case(path_comparison=_comparison(required_missing=[{"key": _NOTE}, {"key": _CONCEPT}]))
    )
    assert verdict["class"] == CLASS_MISSING_DEFINITION


# ---------------------------------------------------------------------------
# Reading the evidence shapes the database actually stores.
# ---------------------------------------------------------------------------


def test_a_node_key_is_read_as_the_triple_the_steps_record():
    assert object_type_of(_CONCEPT) == "semantic-concept"
    assert object_type_of("not-a-key") is None
    assert object_type_of(None) is None


def test_an_unsatisfied_alternative_group_yields_its_nested_keys():
    """`required_missing` holds two shapes at once; one reader reads both."""
    keys = harvest_node_keys(
        [
            {"key": _CONCEPT, "step_kind": "semantic_query"},
            {
                "alternative": "either door",
                "branches": [
                    {"name": "a", "missing": [{"key": _VIEW}], "mismatches": []},
                    {"name": "b", "missing": [{"key": _NOTE}], "mismatches": []},
                ],
            },
        ]
    )
    assert keys == [_CONCEPT, _VIEW, _NOTE]


def test_counts_list_every_class_and_every_verdict_so_the_denominator_travels():
    cases = [_case(result_id=None, ai_path_id=None), _case()]
    report = classification_report(
        run_id="erun_EXAMPLE",
        project_id="proj_EXAMPLE",
        lifecycle="finalized",
        question_set_fingerprint="f" * 64,
        cases=cases,
        environment=_environment(),
    )
    assert set(report["class_counts"]) == set(RULE_ORDER)
    assert report["class_counts"][CLASS_NO_SUBJECT] == 1
    assert report["class_counts"][CLASS_UNVERIFIABLE_BY_DESIGN] == 1
    assert set(report["dimension_counts"]) == {
        "semantic_correctness",
        "provenance_correctness",
        "context_adherence",
        "path_quality",
        "dq_handling",
        "mcp_app_behavior",
    }
    assert report["dimension_counts"]["path_quality"]["unverifiable"] == 2
    assert report["dimension_counts"]["path_quality"]["pass"] == 0
    assert class_counts(report["cases"]) == report["class_counts"]
    assert dimension_counts(cases) == report["dimension_counts"]


def test_the_six_dimensions_are_the_ones_the_run_writer_states():
    """Copied, not imported -- so this asserts the two copies still agree."""
    from core.evaluation_failure_classes import DIMENSIONS, VERDICTS
    from core.evaluation_runs import DIMENSIONS as RUN_DIMENSIONS
    from core.evaluation_runs import VERDICTS as RUN_VERDICTS

    assert DIMENSIONS == RUN_DIMENSIONS
    assert VERDICTS == RUN_VERDICTS


# ---------------------------------------------------------------------------
# The before/after delta.
# ---------------------------------------------------------------------------


def _report(run_id, fingerprint, cases, environment=None):
    return classification_report(
        run_id=run_id,
        project_id="proj_EXAMPLE",
        lifecycle="finalized",
        question_set_fingerprint=fingerprint,
        cases=cases,
        environment=environment if environment is not None else _environment(),
    )


def test_the_delta_reports_both_directions_and_no_rate():
    before = _report("erun_BEFORE", "a" * 64, [_case(result_id=None, ai_path_id=None)])
    after = _report(
        "erun_AFTER",
        "a" * 64,
        [
            _case(
                verdicts=_verdicts(
                    path_quality={
                        "verdict": "pass",
                        "reason_code": "path_matches_expected_pattern",
                        "evidence_refs": {},
                    }
                )
            )
        ],
    )
    delta = compare_runs(before, after)

    assert delta["dimensions"]["path_quality"]["pass_delta"] == 1
    assert delta["dimensions"]["path_quality"]["unverifiable_delta"] == -1
    assert delta["classes"][CLASS_NO_SUBJECT]["delta"] == -1
    assert delta["classes"][CLASS_PASSED]["delta"] == 1
    # No rate, no score, no precision percentage -- anywhere in the payload.
    assert not any("rate" in key or "pct" in key for key in delta)


def test_two_runs_over_different_question_sets_are_refused_by_name():
    before = _report("erun_BEFORE", "a" * 64, [_case()])
    after = _report("erun_AFTER", "b" * 64, [_case()])
    with pytest.raises(FingerprintMismatch) as refusal:
        compare_runs(before, after)
    assert refusal.value.before == "a" * 64
    assert refusal.value.after == "b" * 64
    assert "different question sets" in str(refusal.value)


def test_an_absent_fingerprint_is_not_proof_of_agreement():
    before = _report("erun_BEFORE", None, [_case()])
    after = _report("erun_AFTER", None, [_case()])
    with pytest.raises(FingerprintMismatch):
        compare_runs(before, after)


# ---------------------------------------------------------------------------
# The two guards a caller must not be able to forget.
# ---------------------------------------------------------------------------


def test_a_run_that_is_not_finalized_is_refused_where_both_callers_inherit_it():
    """The guard lives in the report builder, so no caller can be the one without it.

    The script refuses a `recording` run at load time; the gate never loaded
    anything, it reads the lifecycle off a GET door. Both build their report
    here, so both refuse.
    """
    with pytest.raises(RunNotFinalized) as refusal:
        classification_report(
            run_id="erun_EXAMPLE",
            project_id="proj_EXAMPLE",
            lifecycle="recording",
            question_set_fingerprint="f" * 64,
            cases=[_case()],
            environment=_environment(),
        )
    assert refusal.value.lifecycle == "recording"
    assert "finalize" in str(refusal.value)


def test_the_report_carries_the_seven_environment_pins():
    report = _report("erun_EXAMPLE", "a" * 64, [_case()])
    assert set(report["environment"]) == set(ENVIRONMENT_PINS)
    assert report["environment"]["model_ref"] == "model-example-1"
    # A JSONB pin is canonicalised, so a re-serialisation is not read as a change.
    assert environment_drift(
        _environment(host_capability_profile={"tools": ["get_exemplars"], "host": "example-host"}),
        _environment(),
    ) == []


def test_two_runs_whose_environment_moved_are_refused_by_name():
    """A delta over two Semantic View versions measures the environment, not the repair."""
    before = _report("erun_BEFORE", "a" * 64, [_case()])
    after = _report(
        "erun_AFTER",
        "a" * 64,
        [_case()],
        environment=_environment(semantic_view_version_id="svv_OTHER"),
    )
    with pytest.raises(EnvironmentDrift) as refusal:
        compare_runs(before, after)
    assert [item["pin"] for item in refusal.value.differences] == ["semantic_view_version_id"]
    assert "semantic_view_version_id" in str(refusal.value)


def test_an_unread_environment_pin_is_not_proof_of_agreement():
    """The same rule the fingerprint already holds: unread is not identical."""
    before = _report("erun_BEFORE", "a" * 64, [_case()])
    after = _report("erun_AFTER", "a" * 64, [_case()], environment=_environment(model_ref=None))
    with pytest.raises(EnvironmentDrift) as refusal:
        compare_runs(before, after)
    assert [item["pin"] for item in refusal.value.differences] == ["model_ref"]


def test_the_delta_prints_the_environment_both_runs_held():
    before = _report("erun_BEFORE", "a" * 64, [_case()])
    after = _report("erun_AFTER", "a" * 64, [_case()])
    delta = compare_runs(before, after)
    assert set(delta["environment"]) == set(ENVIRONMENT_PINS)
    assert delta["environment"]["as_of"] == "2026-07-31"

