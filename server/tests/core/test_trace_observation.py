"""Story 51.4 -- adversarial proofs for Observed Cohorts and Trace Observation.

These tests are written against the four ways an observed-evidence surface lies.

1. It stores an unqualified `latest` and calls it a pin.
2. It shows a percentage whose denominator quietly dropped the observations it
   could not read.
3. It reports `pass` where a pin has no owner at all.
4. It lets a production observation acquire the authority of a reproducible
   test.

Every one of them renders green. So the assertions below are mostly assertions
of ABSENCE -- no approval route, no baseline column word, no Golden Question
write, no render identifier -- because that is what these four defects have in
common: the thing that would have caught them is the thing that is missing.

The seam test at the end asserts the routes are MOUNTED in `build_asgi_app()`.
It fails until `server/core/admin_api.py` gains the two lines its owning session
adds; that failure is the test doing its job, not a broken fixture.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from core import trace_observation as to
from core.trace_observation import (
    AGGREGATE_REQUIRED_FIELDS,
    LENS_CONTEXT_SKILLS,
    LENS_LINKED_FEEDBACK,
    LENS_RESULT_RENDER,
    LENS_TIMELINE,
    LENS_TOOLS,
    OBSERVED_COHORT_LABEL,
    PATH_EVIDENCE_OBSERVED,
    PATH_EVIDENCE_UNVERIFIABLE,
    PROPOSAL_REASON_CODES,
    RENDER_EVIDENCE_UNVERIFIABLE,
    TRACE_OBSERVATION_LENSES,
    TraceObservationError,
    TraceObservationNotFound,
    TraceObservationRefused,
    assert_aggregate_is_complete,
    build_aggregates,
    classify_path_evidence,
    compose_lenses,
    context_skills_lens,
    derive_member_proposals,
    describe_pin_enforcement,
    load_trace_observation,
    observed_dimensions,
    resolve_observed_cohort,
    result_render_lens,
    timeline_lens,
    tools_lens,
    validate_cohort_pins,
)
from core.trace_observation_api import trace_observation_routes

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION = REPO_ROOT / "infra" / "nango" / "migrations" / "153_product_evaluation_evidence.sql"
MODULE_SOURCE = Path(to.__file__).read_text(encoding="utf-8")
API_SOURCE = Path(
    REPO_ROOT / "server" / "core" / "trace_observation_api.py"
).read_text(encoding="utf-8")

WINDOW_START = datetime(2026, 7, 1, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 7, 31, tzinfo=timezone.utc)

PROJECT = "proj_EXAMPLE"
OTHER_PROJECT = "proj_EXAMPLE_OTHER"
ORG = "org_EXAMPLE"


def _pins(**overrides):
    payload = {
        "window_start": WINDOW_START.isoformat(),
        "window_end": WINDOW_END.isoformat(),
    }
    payload.update(overrides)
    return payload


def _step(ordinal, **overrides):
    step = {
        "ordinal": ordinal,
        "observed_at": WINDOW_START + timedelta(minutes=ordinal),
        "step_kind": "semantic_query",
        "owner_workspace": "governance",
        "owner_object_type": "semantic-view",
        "owner_object_id": "sv_example",
        "owner_version_id": "svv_example",
        "skill_version_id": None,
        "skill_step_id": None,
        "tool_name": None,
        "outcome": "succeeded",
    }
    step.update(overrides)
    return step


def _member(**overrides):
    member = {
        "id": "ocm_EXAMPLE",
        "ai_path_id": "aip_EXAMPLE",
        "query_result_id": None,
        "policy_snapshot": {
            "required": [
                {
                    "owner_workspace": "governance",
                    "owner_object_type": "semantic-view",
                    "owner_object_id": "sv_example",
                    "owner_version_id": "svv_example",
                }
            ]
        },
        "steps": [_step(0)],
        "path_outcome": "succeeded",
        "result_outcome": None,
        "path_evidence_state": PATH_EVIDENCE_OBSERVED,
        "render_evidence_state": RENDER_EVIDENCE_UNVERIFIABLE,
        "observed_at": WINDOW_START,
    }
    member.update(overrides)
    return member


# ===========================================================================
# AC1 -- a pin is exact, or it is not a pin.
# ===========================================================================


@pytest.mark.parametrize("field", ["semantic_view_version_id", "model_ref", "tool_catalog_version"])
@pytest.mark.parametrize("value", ["latest", "current", "head", "LATEST", "  Latest  "])
def test_an_unqualified_latest_is_refused_on_every_version_pin(field, value):
    """`analyze-and-test.md:230`: the Context Version Set is "never an unqualified
    `latest`". A cohort resolved against `latest` describes an environment that
    changed after the observation it claims to pin."""
    payload = _pins(**{field: value})
    if field == "semantic_view_version_id":
        payload["semantic_view_id"] = "sv_example"
    with pytest.raises(TraceObservationRefused) as excinfo:
        validate_cohort_pins(payload)
    codes = {reason["code"] for reason in excinfo.value.reasons}
    assert "unqualified_version" in codes


def test_a_semantic_view_id_without_its_version_is_half_a_pin_and_is_refused():
    with pytest.raises(TraceObservationRefused) as excinfo:
        validate_cohort_pins(_pins(semantic_view_id="sv_example"))
    assert any(reason["code"] == "half_pin" for reason in excinfo.value.reasons)


def test_a_semantic_view_version_without_its_view_is_also_refused():
    with pytest.raises(TraceObservationRefused) as excinfo:
        validate_cohort_pins(_pins(semantic_view_version_id="svv_example"))
    assert any(reason["code"] == "half_pin" for reason in excinfo.value.reasons)


def test_a_business_domain_without_its_version_number_is_refused():
    """The pair `(domain_id, version_number)` IS the primary key of
    `app.mdm_business_domain_versions`. An id alone names no version at all."""
    with pytest.raises(TraceObservationRefused) as excinfo:
        validate_cohort_pins(_pins(business_domain_id="bd_example"))
    assert any(reason["field"] == "business_domain_version" for reason in excinfo.value.reasons)


def test_a_business_domain_version_without_its_domain_is_refused():
    with pytest.raises(TraceObservationRefused):
        validate_cohort_pins(_pins(business_domain_version=3))


def test_a_window_that_does_not_advance_is_refused():
    with pytest.raises(TraceObservationRefused) as excinfo:
        validate_cohort_pins(
            {"window_start": WINDOW_END.isoformat(), "window_end": WINDOW_START.isoformat()}
        )
    assert any(reason["code"] == "invalid_window" for reason in excinfo.value.reasons)


def test_a_cohort_without_a_time_window_is_refused():
    with pytest.raises(TraceObservationRefused) as excinfo:
        validate_cohort_pins({})
    fields = {reason["field"] for reason in excinfo.value.reasons}
    assert {"window_start", "window_end"} <= fields


def test_an_unknown_surface_is_refused_rather_than_stored_as_free_text():
    with pytest.raises(TraceObservationRefused):
        validate_cohort_pins(_pins(surface="slack"))


def test_a_complete_pin_set_normalizes_every_family():
    pins = validate_cohort_pins(
        _pins(
            label="July drift",
            surface="mcp-app",
            actor_class="analyst",
            business_domain_id="bd_example",
            business_domain_version=2,
            semantic_view_id="sv_example",
            semantic_view_version_id="svv_example",
            capability="metric_lookup",
            result_type="series",
            model_ref="model-x@2026-07",
            tool_catalog_version="a" * 64,
            context_version_set_hash="b" * 64,
            host_profile={"widgets": True},
        )
    )
    assert pins["window_start"] == WINDOW_START
    assert pins["window_end"] == WINDOW_END
    assert pins["business_domain_version"] == 2
    assert pins["host_profile"] == {"widgets": True}


def test_a_pin_that_cannot_narrow_membership_is_disclosed_not_hidden():
    """An unenforced filter that looks enforced is a denominator nobody can
    defend. `surface` has no column on `app.ai_paths` (migration 150), so it is
    stored as declared intent and REPORTED as such."""
    pins = validate_cohort_pins(
        _pins(surface="console", model_ref="model-x@2026-07")
    )
    described = {entry["pin"]: entry for entry in describe_pin_enforcement(pins)}
    assert described["model_ref"]["state"] == "applied"
    assert described["surface"]["state"] == "declared_not_enforced"
    assert described["surface"]["reason"]


# ===========================================================================
# AC2 -- membership is frozen evidence, and the DATABASE says so.
# ===========================================================================


def test_this_module_never_updates_or_deletes_a_cohort_or_a_member():
    """Immutability is a trigger, not a convention -- but a module that issued an
    UPDATE would be a documented way around it."""
    forbidden = re.findall(
        r"(?:UPDATE|DELETE\s+FROM)\s+app\.observed_cohorts?\w*", MODULE_SOURCE, re.IGNORECASE
    )
    assert forbidden == []


def test_the_database_owns_the_immutability_not_python():
    """The proof that Python is not the only rampart: the triggers exist in the
    applied migration, with the RGPD erasure hatch migrations 098/099/150 use."""
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "trg_observed_cohorts_immutable" in sql
    assert "trg_observed_cohort_members_immutable" in sql
    assert "app.reject_evaluation_evidence_mutation" in sql
    assert "app.rgpd_erasure" in sql


def test_resolving_twice_mints_two_cohorts_rather_than_refreshing_one():
    """A later execution matching the same filters does not join a historical
    cohort. Re-resolving produces a NEW identity with its own denominator."""
    first = resolve_observed_cohort(
        _Conn(_empty_selection()), org_id=ORG, project_id=PROJECT, actor="owner@example.com",
        payload=_pins(),
    )
    second = resolve_observed_cohort(
        _Conn(_empty_selection()), org_id=ORG, project_id=PROJECT, actor="owner@example.com",
        payload=_pins(),
    )
    assert first["id"] != second["id"]
    assert first["id"].startswith("ocoh_")
    # Identical filters, so the filter hash is stable -- that is what makes two
    # cohorts comparable without making them the same object.
    assert first["version_filters"] == second["version_filters"]


def test_a_resolved_cohort_stores_its_denominator():
    cohort = resolve_observed_cohort(
        _Conn(_empty_selection()), org_id=ORG, project_id=PROJECT, actor="owner@example.com",
        payload=_pins(),
    )
    assert cohort["member_count"] == 0
    assert cohort["blocking"] is False


def test_resolution_refuses_a_project_that_is_not_in_this_organization():
    """A bare project id from a client is never trusted."""
    with pytest.raises(TraceObservationNotFound):
        resolve_observed_cohort(
            _Conn(lambda sql, params: (None, [])),
            org_id=ORG,
            project_id=OTHER_PROJECT,
            actor="owner@example.com",
            payload=_pins(),
        )


# ===========================================================================
# AC3 -- observed evidence can never block and can never become a baseline.
# ===========================================================================


@pytest.mark.parametrize("word", ["approve", "baseline", "gate", "block"])
def test_no_route_in_the_exported_constant_carries_an_approval_word(word):
    """AC3 is proved by absence. A route named `.../approve` would be the
    promotion path `analyze-and-test.md:357-358` forbids."""
    # Whole words only: `aggregates` legitimately contains the letters of `gate`.
    for route in trace_observation_routes:
        assert re.search(rf"\b{word}\b", route.path.lower()) is None, route.path


@pytest.mark.parametrize("word", ["approve", "promote", "baseline", "gate_decision"])
def test_no_promotion_operation_is_exported_by_the_service(word):
    exported = [name for name in dir(to) if not name.startswith("_")]
    assert not [name for name in exported if word in name.lower()]


def test_the_non_blocking_label_is_the_exact_ratified_wording():
    """AC3 requires those exact terms. A softer wording is a different claim."""
    assert OBSERVED_COHORT_LABEL == "Observed Cohort - reference window, non-blocking"


def test_the_cohort_tables_carry_no_approval_column():
    """Re-proved from the schema: `information_schema` would find nothing, and so
    does reading the migration that creates the two tables."""
    sql = MIGRATION.read_text(encoding="utf-8")
    start = sql.index("CREATE TABLE IF NOT EXISTS app.observed_cohorts (")
    end = sql.index("CREATE TABLE IF NOT EXISTS app.golden_question_proposals (")
    cohort_ddl = sql[start:end].lower()
    for forbidden in ("is_baseline", "approved_by", "approved_at", "gate_decision_id", "blocking"):
        # A COLUMN definition, not the word: the table COMMENT deliberately says
        # "carries no approval, gate or blocking column by construction".
        assert re.search(rf"^\s+{forbidden}\s+\w", cohort_ddl, re.MULTILINE) is None, forbidden


def test_the_aggregate_payload_declares_itself_non_blocking():
    aggregates = build_aggregates(_cohort(member_count=1), [_member()])
    assert aggregates["blocking"] is False
    assert aggregates["label"] == OBSERVED_COHORT_LABEL


# ===========================================================================
# AC4 -- an observed failure proposes or prioritizes, and never authors.
# ===========================================================================


def test_an_unverifiable_member_proposes_path_unverifiable():
    proposals = derive_member_proposals(
        _member(steps=[], path_evidence_state=PATH_EVIDENCE_UNVERIFIABLE)
    )
    assert [p["reason_code"] for p in proposals] == ["path_unverifiable"]


def test_a_forbidden_node_proposes_a_critical_hint():
    member = _member(
        policy_snapshot={
            "required": [],
            "forbidden": [
                {
                    "owner_workspace": "governance",
                    "owner_object_type": "semantic-view",
                    "owner_object_id": "sv_example",
                }
            ],
        }
    )
    proposals = {p["reason_code"]: p["severity_hint"] for p in derive_member_proposals(member)}
    assert proposals["forbidden_node_used"] == "critical"


def test_a_missing_required_node_proposes_required_node_missing():
    member = _member(steps=[_step(0, owner_object_id="sv_other")])
    codes = {p["reason_code"] for p in derive_member_proposals(member)}
    assert "required_node_missing" in codes


def test_a_versionless_step_proposes_nothing_but_stays_unverifiable():
    """Missing evidence is not a deviation. It must not be dressed up as one by
    emitting a `version_mismatch` proposal."""
    member = _member(steps=[_step(0, owner_version_id=None)])
    codes = {p["reason_code"] for p in derive_member_proposals(member)}
    assert "version_mismatch" not in codes
    assert observed_dimensions(member)["path_quality"]["verdict"] == "unverifiable"


@pytest.mark.parametrize(
    "outcome,expected",
    [
        ("refused", "result_refused"),
        ("degraded", "result_degraded"),
        ("unavailable", "result_unavailable"),
    ],
)
def test_a_non_success_result_outcome_proposes_its_own_reason(outcome, expected):
    codes = {p["reason_code"] for p in derive_member_proposals(_member(result_outcome=outcome))}
    assert expected in codes


def test_a_clean_observation_proposes_nothing():
    assert derive_member_proposals(_member()) == []


def test_every_reason_code_this_module_can_emit_exists_in_the_migration_vocabulary():
    """A code the CHECK refuses would be a write that fails at the last possible
    moment, on a row nobody can inspect."""
    sql = MIGRATION.read_text(encoding="utf-8")
    for code in PROPOSAL_REASON_CODES:
        assert f"'{code}'" in sql


def test_no_code_path_in_this_story_writes_a_golden_question_table():
    """AC4: a proposal SUGGESTS. Story 51.1 owns authoring."""
    pattern = (
        r"(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+app\."
        r"(golden_questions|golden_question_versions|golden_question_reference_paths)\b"
    )
    assert re.findall(pattern, MODULE_SOURCE, re.IGNORECASE) == []
    assert re.findall(pattern, API_SOURCE, re.IGNORECASE) == []


# ===========================================================================
# AC5 -- the five ratified lenses, and no sixth.
# ===========================================================================


def test_the_lenses_are_exactly_the_five_of_analyze_and_test_293():
    assert TRACE_OBSERVATION_LENSES == (
        LENS_TIMELINE,
        LENS_CONTEXT_SKILLS,
        LENS_TOOLS,
        LENS_RESULT_RENDER,
        LENS_LINKED_FEEDBACK,
    )
    composed = compose_lenses(header={}, steps=[], result=None, project_id=PROJECT)
    assert set(composed) == set(TRACE_OBSERVATION_LENSES)


def test_the_timeline_orders_by_stored_ordinal_never_by_a_timestamp_that_can_tie():
    tied = WINDOW_START
    steps = [_step(0, observed_at=tied), _step(1, observed_at=tied)]
    lens = timeline_lens(steps)
    assert [entry["ordinal"] for entry in lens["steps"]] == [0, 1]
    assert lens["ordering"] == "stored ordinal"


def test_a_half_skill_pin_is_shown_as_nothing_rather_than_as_half_a_pin():
    """`ck_ai_path_steps_skill_pin` refuses to STORE half a pin; this lens refuses
    to display one, because half a pin resolves to the wrong step the first time
    a Skill is revised."""
    lens = context_skills_lens([_step(0, skill_version_id="skv_example", skill_step_id=None)])
    assert lens["steps"][0]["skill"] is None


def test_a_complete_skill_pin_is_shown_as_a_pair():
    """Le couple, ET l'etat de sa lecture -- Story 45.7.

    Sans resolution attachee par `ai_paths.load_path`, l'etat est
    `unavailable` : << on n'a pas lu ce pas >>. Rendre le couple nu laissait le
    lecteur croire qu'il n'y avait rien a lire.
    """
    lens = context_skills_lens(
        [_step(0, skill_version_id="skv_example", skill_step_id="sks_example")]
    )
    assert lens["steps"][0]["skill"] == {
        "skill_version_id": "skv_example",
        "skill_step_id": "sks_example",
        "state": "unavailable",
    }


def test_a_resolved_skill_pin_says_which_step_of_which_skill_ran():
    """REJECT 45.7 du 2026-08-05 : cette lentille rendait DEUX IDENTIFIANTS
    OPAQUES et rien d'autre, a l'une et l'autre version. `skill_steps.resolve()`
    n'avait aucun lecteur sur une surface de chemin, donc l'AC1 -- << le pas
    resout vers l'intitule et l'action que cette version declarait >> -- n'avait
    aucun code."""
    step = _step(0, skill_version_id="proc_EXAMPLE@7", skill_step_id="3")
    step["skill"] = {
        "skill_version_id": "proc_EXAMPLE@7",
        "skill_step_id": "3",
        "state": "resolved",
        "skill_name": "example-skill",
        "action": "run",
        "label": "Run the acceptance suite",
        "tool": "search_context",
    }
    lens = context_skills_lens([step])
    shown = lens["steps"][0]["skill"]
    assert shown["state"] == "resolved"
    assert shown["label"] == "Run the acceptance suite"
    assert shown["action"] == "run"
    assert shown["skill_version_id"] == "proc_EXAMPLE@7"


def test_the_tools_lens_shows_only_tool_calls_and_states_that_the_name_is_a_label():
    lens = tools_lens(
        [_step(0), _step(1, step_kind="tool_call", tool_name="search"), _step(2)]
    )
    assert [entry["tool_name"] for entry in lens["steps"]] == ["search"]
    assert lens["tool_name_is_a_label"] is True


# ===========================================================================
# AC6 -- the `Result & Render` lens states its missing half instead of filling it.
# ===========================================================================


def test_the_render_half_is_unverifiable_and_names_the_story_that_owns_it():
    lens = result_render_lens(None, project_id=PROJECT)
    assert lens["render"]["state"] == "unverifiable"
    assert lens["render"]["reason"] == "render_owner_not_delivered"
    assert lens["render"]["owner"] == "Stories 50.4 / 50.5 / 50.7"


@pytest.mark.parametrize("forbidden", ["pass", "fail"])
def test_the_render_half_is_never_pass_and_never_fail(forbidden):
    assert result_render_lens(None, project_id=PROJECT)["render"]["state"] != forbidden


def test_the_owner_is_a_named_story_never_a_placeholder():
    """Story 49.6 was rejected for a `future_owner` placeholder. Every absence
    here names an existing story number."""
    for absence in to.iter_absences():
        assert "future_owner" not in absence["owner"].lower()
        assert re.search(r"\d\d\.\d", absence["owner"])


def test_the_result_half_resolves_and_deep_links_to_its_story_50_1_owner():
    lens = result_render_lens(
        {"id": "qres_EXAMPLE", "outcome": "success", "content_hash": "a" * 64,
         "row_count": 3, "truncated": False},
        project_id=PROJECT,
    )
    assert lens["result"]["state"] == "resolved"
    assert lens["result"]["owner_href"].endswith("/analyze/results/qres_EXAMPLE")


def test_no_render_table_identifier_or_authority_is_created_by_this_story():
    """Creating a `renders` table or minting a `render_id` would be inventing a
    design decision and presenting it as a repair."""
    sql = MIGRATION.read_text(encoding="utf-8")
    assert not re.search(r"CREATE\s+TABLE[^;]*app\.renders?\b", sql, re.IGNORECASE)
    assert "_mint(\"render" not in MODULE_SOURCE
    assert "render_id" not in MODULE_SOURCE
    # The pin is declared and held NULL by the migration, which is how the
    # absence stays visible instead of being quietly filled later.
    assert "ck_observed_cohort_members_render_unpinned" in sql


# ===========================================================================
# AC7 -- every aggregate carries denominator, coverage and version filters.
# ===========================================================================


def _cohort(member_count: int):
    return {
        "id": "ocoh_EXAMPLE",
        "member_count": member_count,
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
        "semantic_view_id": "sv_example",
        "semantic_view_version_id": "svv_example",
        "model_ref": "model-x@2026-07",
    }


def _aggregate_entries(aggregates):
    for key, value in aggregates.items():
        if key.startswith("by_"):
            yield from value


def test_every_aggregate_entry_carries_the_four_mandatory_fields():
    """`analyze-and-test.md:310-313`. A percentage without its denominator is a
    defect, not a rounding choice."""
    aggregates = build_aggregates(
        _cohort(member_count=2),
        [
            _member(id="ocm_A", steps=[_step(0, tool_name="search", step_kind="tool_call")]),
            _member(id="ocm_B", result_outcome="refused"),
        ],
    )
    entries = list(_aggregate_entries(aggregates))
    assert entries
    for entry in entries:
        for field in AGGREGATE_REQUIRED_FIELDS:
            assert field in entry, (field, entry)


def test_an_entry_that_lost_any_of_the_four_is_refused():
    complete = {
        "numerator": 1,
        "denominator": 2,
        "coverage": {"denominator": 2},
        "version_filters": {},
    }
    assert_aggregate_is_complete(complete)
    for field in AGGREGATE_REQUIRED_FIELDS:
        partial = {k: v for k, v in complete.items() if k != field}
        with pytest.raises(TraceObservationError):
            assert_aggregate_is_complete(partial)


def test_coverage_states_its_own_denominator():
    with pytest.raises(TraceObservationError):
        assert_aggregate_is_complete(
            {"numerator": 1, "denominator": 2, "coverage": {}, "version_filters": {}}
        )


def test_the_denominator_is_the_stored_member_count_not_the_number_of_rows_read():
    """If an erasure removed a member row, the historical percentage must get
    worse, not silently better."""
    aggregates = build_aggregates(_cohort(member_count=5), [_member()])
    assert aggregates["denominator"] == 5
    assert aggregates["coverage"]["denominator"] == 5


def test_unverifiable_members_stay_in_the_denominator_and_out_of_the_numerator():
    """An aggregate that silently drops `unverifiable` members from the
    denominator is the same defect wearing a better number."""
    aggregates = build_aggregates(
        _cohort(member_count=2),
        [
            _member(id="ocm_A"),
            _member(id="ocm_B", steps=[], path_evidence_state=PATH_EVIDENCE_UNVERIFIABLE),
        ],
    )
    assert aggregates["coverage"]["members_with_path_evidence"] == 1
    assert aggregates["coverage"]["members_unverifiable"] == 1
    assert aggregates["coverage"]["denominator"] == 2


def test_every_aggregate_echoes_back_the_exact_version_filters_the_cohort_pinned():
    aggregates = build_aggregates(_cohort(member_count=1), [_member()])
    filters = aggregates["version_filters"]
    assert filters["semantic_view_version_id"] == "svv_example"
    assert filters["model_ref"] == "model-x@2026-07"
    assert any(entry["pin"] == "model_ref" for entry in filters["pin_enforcement"])
    for entry in _aggregate_entries(aggregates):
        assert entry["version_filters"] == filters


def test_a_required_node_divides_by_the_members_that_pinned_it_not_by_the_cohort():
    """Dividing a node required of two members by a cohort of a hundred would
    report a 2% adherence rate for a rule ninety-eight members never carried."""
    aggregates = build_aggregates(_cohort(member_count=100), [_member(), _member(id="ocm_B")])
    node = aggregates["by_required_node"][0]
    assert node["denominator"] == 2
    assert node["numerator"] == 2


def test_axes_with_no_observed_source_are_stated_absences_not_missing_keys():
    """`Business Domain`, `capability` and `result type` are mandatory
    classification axes an observed AI Path cannot supply. Omitting them would
    let a screen read "not aggregated" as "no findings"."""
    aggregates = build_aggregates(_cohort(member_count=1), [_member()])
    axes = {entry["axis"]: entry for entry in aggregates["unavailable_axes"]}
    assert set(axes) == {"business_domain", "capability", "result_type"}
    for entry in axes.values():
        assert entry["state"] == "unverifiable"
        assert entry["reason"]


# ===========================================================================
# AC8 -- missing, mixed or unavailable evidence is `Unverifiable`.
# ===========================================================================


def test_a_step_less_path_is_unverifiable_evidence():
    assert classify_path_evidence([], outcome="succeeded") == PATH_EVIDENCE_UNVERIFIABLE


def test_an_unavailable_outcome_is_unverifiable_even_with_steps():
    assert classify_path_evidence([_step(0)], outcome="unavailable") == PATH_EVIDENCE_UNVERIFIABLE


def test_an_observed_path_with_steps_is_observed_evidence():
    assert classify_path_evidence([_step(0)], outcome="succeeded") == PATH_EVIDENCE_OBSERVED


def test_an_unverifiable_member_is_never_a_pass_and_never_a_fail():
    """A pass over zero observations and a fail over missing evidence are both
    claims the evidence does not support."""
    dimensions = observed_dimensions(
        _member(steps=[], path_evidence_state=PATH_EVIDENCE_UNVERIFIABLE)
    )
    assert dimensions["path_quality"]["verdict"] == "unverifiable"


def test_mcp_app_behavior_can_never_be_pass_while_its_owner_is_undelivered():
    dimensions = observed_dimensions(_member())
    assert dimensions["mcp_app_behavior"]["state"] == "unverifiable"
    assert dimensions["mcp_app_behavior"]["owner"] == "Story 50.6"


def test_a_clean_observation_still_reports_the_two_unowned_dimensions_as_unverifiable():
    dimensions = observed_dimensions(_member())
    assert dimensions["path_quality"]["verdict"] == "pass"
    assert dimensions["render_behavior"]["state"] == "unverifiable"
    assert dimensions["blocking"] is False


def test_assessment_is_delegated_and_not_re_implemented():
    """`core.ai_paths.assess` already encodes why `unverifiable` and `fail` must
    not merge. A second assessor here would drift from it."""
    assert "from core.ai_paths import" in MODULE_SOURCE
    assert "assess" in MODULE_SOURCE
    assert "def assess(" not in MODULE_SOURCE


# ===========================================================================
# AC10 / AC12 -- Project isolation, non-disclosure, and foreign owners.
# ===========================================================================


class _Cursor:
    """Answers each SELECT by inspecting its text, and records what was asked."""

    def __init__(self, plan):
        self._plan = plan
        self.executed: list[tuple[str, tuple]] = []
        self._row = None
        self._rows: list = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, tuple(params or ())))
        self._row, self._rows = self._plan(sql, tuple(params or ()))

    def fetchone(self):
        return self._row

    def fetchall(self):
        return list(self._rows)


class _Conn:
    def __init__(self, plan):
        self.cur = _Cursor(plan)

    def cursor(self):
        return self.cur

    def commit(self):
        return None


def _empty_selection():
    """A live Project, no admissible path in the window."""

    def plan(sql, params):
        if "FROM app.projects" in sql:
            return (1,), []
        return None, []

    return plan


def _trace_plan(*, path_exists=True, steps=None, result=None):
    header = (
        "aip_EXAMPLE", ORG, PROJECT, "finalized", "succeeded", None, "owner@example.com",
        None, WINDOW_START, WINDOW_END, "model-x@2026-07", "c" * 64,
        {"required": []}, "d" * 64, "e" * 64,
    )

    def plan(sql, params):
        if "SELECT 1 FROM app.ai_paths" in sql:
            return ((1,) if path_exists else None), []
        if "FROM app.query_results" in sql:
            return result, []
        if "FROM app.ai_path_steps" in sql:
            return None, list(steps or [])
        if "FROM app.ai_paths" in sql:
            return header, []
        return None, []

    return plan


def _row_step(ordinal, **overrides):
    """A row in `_STEP_COLUMNS` order, as `core.ai_paths._load_steps` reads it."""
    values = {
        "id": f"aps_{ordinal}",
        "ordinal": ordinal,
        "step_kind": "tool_call",
        "owner_workspace": None,
        "owner_object_type": None,
        "owner_object_id": None,
        "owner_version_id": None,
        "skill_version_id": None,
        "skill_step_id": None,
        "tool_name": "search",
        "outcome": "succeeded",
        "evidence_record_id": None,
        "observed_at": WINDOW_START,
    }
    values.update(overrides)
    return tuple(values.values())


def test_a_foreign_or_absent_ai_path_is_the_same_not_found():
    """Distinguishing them would confirm that another Project's trace exists."""
    with pytest.raises(TraceObservationNotFound):
        load_trace_observation(
            _Conn(_trace_plan(path_exists=False)),
            org_id=ORG,
            project_id=OTHER_PROJECT,
            ai_path_id="aip_EXAMPLE",
        )


def test_the_lookup_carries_org_and_project_scope_rather_than_filtering_afterwards():
    conn = _Conn(_trace_plan(path_exists=False))
    with pytest.raises(TraceObservationNotFound):
        load_trace_observation(
            conn, org_id=ORG, project_id=PROJECT, ai_path_id="aip_EXAMPLE"
        )
    sql, params = conn.cur.executed[0]
    assert "org_id = %s" in sql and "project_id = %s" in sql
    assert ORG in params and PROJECT in params


def test_a_trace_observation_composes_the_five_lenses_over_real_steps():
    observation = load_trace_observation(
        _Conn(
            _trace_plan(
                steps=[_row_step(0), _row_step(1, step_kind="semantic_query", tool_name=None)],
                result=("qres_EXAMPLE", "success", "a" * 64, 3, False),
            )
        ),
        org_id=ORG,
        project_id=PROJECT,
        ai_path_id="aip_EXAMPLE",
    )
    assert observation["lens_order"] == list(TRACE_OBSERVATION_LENSES)
    assert len(observation["lenses"][LENS_TIMELINE]["steps"]) == 2
    assert len(observation["lenses"][LENS_TOOLS]["steps"]) == 1
    assert observation["lenses"][LENS_RESULT_RENDER]["result"]["id"] == "qres_EXAMPLE"
    assert observation["lenses"][LENS_RESULT_RENDER]["render"]["state"] == "unverifiable"


def test_a_trace_without_a_result_says_so_instead_of_inventing_one():
    observation = load_trace_observation(
        _Conn(_trace_plan(steps=[_row_step(0)], result=None)),
        org_id=ORG,
        project_id=PROJECT,
        ai_path_id="aip_EXAMPLE",
    )
    assert observation["lenses"][LENS_RESULT_RENDER]["result"]["state"] == "absent"


def test_the_linked_feedback_lens_refuses_to_present_correlation_as_identity():
    """`app.feedback` (migration 012) carries no Result, render or path reference.
    Matching its `trace_id` against `w3c_trace_id` would be exactly the identity
    claim migration 150:99-104 refuses in the schema itself."""
    observation = load_trace_observation(
        _Conn(_trace_plan(steps=[_row_step(0)])),
        org_id=ORG,
        project_id=PROJECT,
        ai_path_id="aip_EXAMPLE",
    )
    feedback = observation["lenses"][LENS_LINKED_FEEDBACK]["feedback"]
    assert feedback["state"] == "unverifiable"
    assert feedback["owner"] == "Story 51.5"
    assert observation["lenses"][LENS_LINKED_FEEDBACK]["records"] == []
    assert observation["w3c_trace_id_is_correlation_not_identity"] is True
    # No query joins `app.feedback` to a trace id. The absence IS the deliverable.
    assert re.search(r"FROM\s+app\.feedback\b", MODULE_SOURCE, re.IGNORECASE) is None
    assert re.search(r"w3c_trace_id\s*=\s*\w*\.?trace_id", MODULE_SOURCE) is None


def test_this_story_writes_none_of_the_tables_it_reads():
    """AC12: Story 49.6 owns the AI Path, Story 50.1 owns the Result. This module
    is a reader of both."""
    pattern = (
        r"(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+app\."
        r"(ai_paths|ai_path_steps|query_results|query_result_payloads)\b"
    )
    assert re.findall(pattern, MODULE_SOURCE, re.IGNORECASE) == []
    assert re.findall(pattern, API_SOURCE, re.IGNORECASE) == []


# ===========================================================================
# AC11 -- routes are mounted by constant, in the right order.
# ===========================================================================


def test_the_module_exports_one_route_constant():
    assert isinstance(trace_observation_routes, list)
    assert trace_observation_routes
    assert all(hasattr(route, "path") for route in trace_observation_routes)


@pytest.mark.parametrize("literal", ["aggregates", "proposals", "decision"])
def test_a_literal_segment_is_declared_before_the_bare_id_route(literal):
    """Route order is load-bearing: `/aggregates` must never be captured as a
    cohort id."""
    paths = [route.path for route in trace_observation_routes]
    bare = paths.index("/api/projects/{project_id}/test/observed-cohorts/{cohort_id}")
    literal_index = min(i for i, path in enumerate(paths) if literal in path)
    assert literal_index < bare


_SEAM_PROBES = (
    ("GET", "/api/projects/proj_EXAMPLE/test/observed-cohorts"),
    ("GET", "/api/projects/proj_EXAMPLE/test/observed-cohorts/ocoh_X/aggregates"),
    ("GET", "/api/projects/proj_EXAMPLE/test/observed-cohorts/ocoh_X/proposals"),
    ("GET", "/api/projects/proj_EXAMPLE/test/observed-cohorts/ocoh_X"),
    ("GET", "/api/projects/proj_EXAMPLE/test/trace-observations/aip_X"),
)


def test_each_route_resolves_to_its_own_handler_in_a_standalone_app():
    """Proves the constant is well-formed independently of the mounting step.

    The assertion reads the ROUTER, not a status code. `status_code != 404` was
    the first form and it cannot tell the two 404s apart: Starlette answers a
    plain `Not Found` for a path no route declares, and this application answers
    its own `{"code": "not_found"}` envelope for a Project that does not exist.
    A correctly mounted route probed with a Project id that is absent from the
    database therefore returns 404 and the old assertion failed on working code.
    """
    from starlette.applications import Starlette

    app = Starlette(routes=list(trace_observation_routes))
    declared = {route.path for route in app.routes}
    for method, probe in _SEAM_PROBES:
        assert any(
            _matches(pattern, probe) for pattern in declared
        ), f"{method} {probe} resolves to no route in the standalone app"


def _matches(pattern: str, concrete: str) -> bool:
    """Does a Starlette path pattern (`/a/{id}/b`) match a concrete path?"""
    expected = pattern.split("/")
    actual = concrete.split("/")
    if len(expected) != len(actual):
        return False
    return all(
        segment.startswith("{") or segment == given
        for segment, given in zip(expected, actual)
    )


@pytest.mark.parametrize(
    "path",
    [
        "/api/projects/proj_EXAMPLE/test/observed-cohorts",
        "/api/projects/proj_EXAMPLE/test/observed-cohorts/ocoh_X/aggregates",
        "/api/projects/proj_EXAMPLE/test/observed-cohorts/ocoh_X/proposals",
        "/api/projects/proj_EXAMPLE/test/observed-cohorts/ocoh_X",
        "/api/projects/proj_EXAMPLE/test/trace-observations/aip_X",
    ],
)
def test_the_routes_are_spliced_into_the_asgi_app(path):
    """The seam: these routes must be reachable through the REAL ASGI stack.

    `admin_api.py` spreads `*trace_observation_routes`; without those two lines a
    route constant nobody mounted answers 404 to every user.

    The probe forces an UNAUTHENTICATED caller, which is what separates the two
    404s. Authentication runs before the handler looks anything up, so a mounted
    route answers 401 whatever the Project id; an undeclared path never reaches
    auth and answers Starlette's own 404. Asserting `!= 404` instead let a
    correctly mounted route fail merely because `proj_EXAMPLE` is not in the
    database -- the test reported a defect that did not exist.
    """
    from unittest.mock import AsyncMock, patch

    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    with TestClient(build_asgi_app(), raise_server_exceptions=False) as client:
        with patch("core.admin_api._check_auth", new=AsyncMock(return_value=(False, None))):
            control = client.get("/api/projects/proj_EXAMPLE/test/does-not-exist")
            assert control.status_code == 404, (
                "the probe itself is broken: an undeclared path must 404 at the router, "
                f"got {control.status_code}"
            )
            assert client.get(path).status_code == 401, (
                f"{path} is not mounted: splice trace_observation_routes into admin_api"
            )


def test_the_cohort_counts_a_required_node_written_in_the_detail_vocabulary(monkeypatch):
    """AI-376 (Opus N2): the cohort said 0/1 for a node `assess` called pass -- both keys now speak
    the column's word."""
    member = _member(
        policy_snapshot={"required": [{"owner_workspace": "context-hub", "owner_object_type": "schema_doc", "owner_object_id": "doc_1"}]},
        steps=[{"ordinal": 0, "step_kind": "knowledge_read", "outcome": "succeeded", "owner_workspace": "context-hub",
                "owner_object_type": "schema-doc", "owner_object_id": "doc_1", "tool_name": "search_context"}],
    )
    aggregates = build_aggregates(_cohort(member_count=1), [member])
    rows = [row for row in aggregates["by_required_node"] if row["key"][2] == "doc_1"]
    assert rows and rows[0]["key"][1] == "schema-doc" and rows[0]["numerator"] == 1 and rows[0]["denominator"] == 1
