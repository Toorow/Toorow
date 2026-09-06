"""One exact plan, or a refusal by name (story 66.4), against the real schema.

Everything this compiler refuses is a condition on published rows -- what Data
publishes TODAY, which relationship a published view carries, which canonical
field a mapping actually binds. A mock would be told each of those, and the
compiler would pass every test while freezing pins that do not exist.
"""

from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

import json  # noqa: E402

from core import context_store, golden_questions  # noqa: E402
from core import mdm_common_keys as keys  # noqa: E402
from core import multi_source_execution as execution  # noqa: E402
from core import multi_source_plan as plans  # noqa: E402

from tests.integration.epic66_fixtures import (  # noqa: E402
    hash64,
    make_canonical_field,
    make_datastream,
    make_project,
    make_semantic_view,
    pin_relationship,
    publish_output,
    uid,
)
from tests.integration.taxonomy_fixtures import insert_domain_fixture  # noqa: E402
from tests.support.minted_identifiers import identifier_rendered_in  # noqa: E402


@pytest.fixture()
def world(live_postgres):
    org_id, project_id = make_project(live_postgres, "Plan")
    # A FIXTURE ROW since 2026-08-25: `create_domain` refuses, and what this
    # file measures is downstream of the domain existing, never of who made it.
    domain = insert_domain_fixture(
        live_postgres,
        org_id=org_id,
        name="Growth",
        slug=f"growth-{project_id.lower()}",
        actor="tester",
    )
    day = make_canonical_field(live_postgres, project_id, "day", value_type="date")
    campaign = make_canonical_field(live_postgres, project_id, "campaign_id")
    spend = make_canonical_field(
        live_postgres, project_id, "spend", kind="metric", value_type="money"
    )
    conversions = make_canonical_field(
        live_postgres, project_id, "conversions", kind="metric", value_type="integer"
    )
    left = make_datastream(
        live_postgres, org_id, project_id, "Campaign spend",
        bindings={
            day: ("date", "confirmed"),
            campaign: ("campaign", "confirmed"),
            spend: ("spend_micros", "confirmed"),
        },
    )
    right = make_datastream(
        live_postgres, org_id, project_id, "Conversions",
        bindings={
            day: ("event_date", "confirmed"),
            campaign: ("campaign_key", "confirmed"),
            conversions: ("conversion_count", "confirmed"),
        },
    )
    publish_output(live_postgres, org_id, project_id, left, "plan_spend")
    publish_output(live_postgres, org_id, project_id, right, "plan_conversions")
    key = keys.create_common_key(
        live_postgres,
        project_id=project_id,
        name="Day and Campaign",
        canonical_field_ids=[day, campaign],
        actor="tester",
    )
    view_id, view_version_id = make_semantic_view(
        live_postgres,
        project_id,
        business_domain_refs=(domain["id"],),
    )
    pin_relationship(
        live_postgres, view_version_id, key["current_version"]["id"], name="spend_to_conversions"
    )
    skill = context_store.create_procedure(
        live_postgres,
        project_id=project_id,
        frontmatter_yaml=(
            "name: Paid media investigation\n"
            "description: Reconcile paid media sources before interpreting the pivot.\n"
        ),
        body_md="Inspect matching quality, then compare governed measures.",
        created_by="tester",
    )
    validated_question = golden_questions.validate_golden_question_version(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        payload={
            "business_domain_id": domain["id"],
            "business_domain_version_number": 1,
            "semantic_view_id": view_id,
            "semantic_view_version_id": view_version_id,
            "semantic_view_version_role": "baseline",
            "question": "How do spend and conversions compare by campaign?",
            "time_boundary": {},
            "expected_result": [
                {"assertion_type": "value", "member_id": "spend", "tolerance": None}
            ],
            "required_provenance": [{"link_kind": "source", "required": True}],
            "expected_ai_path": {
                "required_nodes": [],
                "forbidden_tools": [],
                "order_constraints": [],
                "alternative_paths": [],
            },
            "result_type": "comparison",
            "capability_tags": ["multi_source_analysis"],
            "severity": "major",
        },
    )
    question = golden_questions.create_golden_question(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        title="Spend and conversions by campaign",
        owner="tester",
        validated=validated_question,
        actor="tester",
    )
    return {
        "org_id": org_id,
        "project_id": project_id,
        "left": left,
        "right": right,
        "day": day,
        "campaign": campaign,
        "spend": spend,
        "conversions": conversions,
        "key_version_id": key["current_version"]["id"],
        "view_id": view_id,
        "view_version_id": view_version_id,
        "domain_id": domain["id"],
        "golden_question_version_id": question["version_id"],
        "golden_question_content_hash": question["content_hash"],
        "skill_version_id": f"{skill['id']}@1",
    }


def _request(world, **overrides):
    request = {
        "members": [
            {
                "datastream_id": world["left"],
                "measures": [{"canonical_field_id": world["spend"]}],
            },
            {
                "datastream_id": world["right"],
                "measures": [{"canonical_field_id": world["conversions"]}],
            },
        ],
        "edges": [
            {
                "left": world["left"],
                "right": world["right"],
                "common_key_version_id": world["key_version_id"],
            }
        ],
        "dimensions": [{"canonical_field_id": world["campaign"]}],
        "inclusion_policy": "matched_only",
        "grain": "day",
    }
    request.update(overrides)
    return request


def _profile_evidence(conn, world, safety="ready", multiplication=None):
    published = plans._published_members(conn, world["project_id"], [world["left"], world["right"]])

    def snapshot(member):
        return {
            "datastream_id": member["datastream_id"],
            "mapping_version_id": member["mapping_version_id"],
            "execution_id": member["published_execution_id"],
            "output_version_id": member["output_version_id"],
            "output_id": member["output_id"],
            "publication_log_id": member["publication_log_id"],
            "plan_version_id": member["plan_version_id"],
            "schema_hash": member["schema_hash"],
        }

    return lambda *_args: {
        "execution_safety": safety,
        "multiplication": multiplication or {"state": "exact", "worst_case_rows_per_key": 1},
        "authority": {
            "left": snapshot(published[world["left"]]),
            "right": snapshot(published[world["right"]]),
        },
        "evidence": {"profiled_at": 1, "valid_until": 4_000_000_000},
    }


def test_a_complete_request_freezes_every_pin(live_postgres, world):
    compiled = plans.compile_plan(
        live_postgres,
        project_id=world["project_id"],
        request=_request(world),
        profile_lookup=_profile_evidence(live_postgres, world),
    )
    plan = compiled["plan"]

    assert plan["contract_version"] == plans.MULTI_SOURCE_PLAN_CONTRACT_VERSION
    assert plan["inclusion_policy"] == "matched_only"
    # The mapping version is the one Data publishes, resolved rather than trusted.
    assert all(member["mapping_version_id"] for member in plan["members"])
    assert all(member["output_version_id"] for member in plan["members"])
    assert all(member["published_execution_id"] for member in plan["members"])
    assert all(member["relation_ref"] for member in plan["members"])
    # Both physical key paths are frozen into the edge.
    paths = plan["edges"][0]["key_paths"]
    assert {p["left_field"] for p in paths} == {"date", "campaign"}
    assert {p["right_field"] for p in paths} == {"event_date", "campaign_key"}
    # The relationship, its cardinality and the view version it comes from.
    assert plan["edges"][0]["relationship"]["relationship_name"] == "spend_to_conversions"
    assert plan["semantic_view_version_ids"] == [world["view_version_id"]]
    assert compiled["content_hash"]


def test_business_question_and_skill_are_exact_pins_in_the_stored_plan(
    live_postgres, world
):
    compiled = plans.compile_plan(
        live_postgres,
        project_id=world["project_id"],
        request=_request(
            world,
            analysis_context={
                "golden_question_version_id": world["golden_question_version_id"],
                "skill_version_ids": [world["skill_version_id"]],
            },
        ),
        profile_lookup=_profile_evidence(live_postgres, world),
    )
    frozen = compiled["plan"]["analysis_context"]
    assert frozen["business_domain"] == {
        "id": world["domain_id"],
        "version_number": 1,
        "version_id": f"{world['domain_id']}:1",
        "name": "Growth",
    }
    assert frozen["golden_question"]["version_id"] == world["golden_question_version_id"]
    assert frozen["golden_question"]["content_hash"] == world["golden_question_content_hash"]
    assert frozen["requested_skills"][0]["version_id"] == world["skill_version_id"]

    stored = plans.store_plan_version(
        live_postgres,
        org_id=world["org_id"],
        project_id=world["project_id"],
        compiled=compiled,
        actor="tester",
    )
    loaded = plans.load_plan_version(
        live_postgres,
        project_id=world["project_id"],
        query_spec_version_id=stored["query_spec_version_id"],
    )
    assert loaded["plan"]["analysis_context"] == frozen


def test_execution_resolves_the_output_frozen_before_a_later_publication(live_postgres, world):
    compiled = plans.compile_plan(
        live_postgres,
        project_id=world["project_id"],
        request=_request(world),
        profile_lookup=_profile_evidence(live_postgres, world),
    )
    before = {
        member["datastream_id"]: (member["output_version_id"], member["relation_ref"])
        for member in compiled["plan"]["members"]
    }

    publish_output(
        live_postgres, world["org_id"], world["project_id"], world["left"], "new_spend"
    )
    resolved = execution.resolve_relations(
        live_postgres, project_id=world["project_id"], plan=compiled["plan"]
    )

    assert resolved[world["left"]].endswith(f".{before[world['left']][1]}")
    assert not resolved[world["left"]].endswith(".new_spend")
    assert compiled["plan"]["members"][0]["output_version_id"] == before[world["left"]][0]


def test_the_hash_changes_with_the_inclusion_policy(live_postgres, world):
    """Two policies are two analyses, and their identity has to differ."""
    a = plans.compile_plan(
        live_postgres,
        project_id=world["project_id"],
        request=_request(world),
        profile_lookup=_profile_evidence(live_postgres, world),
    )
    b = plans.compile_plan(
        live_postgres,
        project_id=world["project_id"],
        request=_request(world, inclusion_policy="preserve_primary"),
        profile_lookup=_profile_evidence(live_postgres, world),
    )
    assert a["content_hash"] != b["content_hash"]


def test_no_inclusion_policy_is_refused_rather_than_defaulted(live_postgres, world):
    request = _request(world)
    del request["inclusion_policy"]
    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(live_postgres, project_id=world["project_id"], request=request)
    assert excinfo.value.code == "inclusion_policy_required"


def test_a_stale_mapping_pin_is_refused_and_names_both_versions(live_postgres, world):
    request = _request(world)
    request["members"][0]["mapping_version_id"] = "dmv_SOMETHING_ELSE"
    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(live_postgres, project_id=world["project_id"], request=request)
    assert excinfo.value.code == "stale_mapping_pin"
    assert excinfo.value.detail["claimed"] == "dmv_SOMETHING_ELSE"


def test_a_cross_with_no_common_key_is_refused(live_postgres, world):
    request = _request(world)
    del request["edges"][0]["common_key_version_id"]
    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(live_postgres, project_id=world["project_id"], request=request)
    assert excinfo.value.code == "undeclared_common_key"


def test_a_key_no_published_relationship_pins_is_refused(live_postgres, world):
    orphan = keys.create_common_key(
        live_postgres,
        project_id=world["project_id"],
        name="Campaign only",
        canonical_field_ids=[world["campaign"]],
        actor="tester",
    )
    request = _request(world)
    request["edges"][0]["common_key_version_id"] = orphan["current_version"]["id"]
    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(live_postgres, project_id=world["project_id"], request=request)
    assert excinfo.value.code == "no_approved_relationship"


def test_two_approved_relationships_are_named_and_left_to_the_person(live_postgres, world):
    pin_relationship(
        live_postgres, world["view_version_id"], world["key_version_id"],
        ordinal=1, name="an_equally_valid_path",
    )
    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(
            live_postgres, project_id=world["project_id"], request=_request(world)
        )
    assert excinfo.value.code == "ambiguous_relationship"
    assert sorted(excinfo.value.detail) == ["an_equally_valid_path", "spend_to_conversions"]


def test_naming_one_of_the_two_paths_compiles(live_postgres, world):
    pin_relationship(
        live_postgres, world["view_version_id"], world["key_version_id"],
        ordinal=1, name="an_equally_valid_path",
    )
    request = _request(world)
    request["edges"][0]["relationship_name"] = "an_equally_valid_path"
    compiled = plans.compile_plan(
        live_postgres,
        project_id=world["project_id"],
        request=request,
        profile_lookup=_profile_evidence(live_postgres, world),
    )
    assert compiled["plan"]["edges"][0]["relationship"]["relationship_name"] == (
        "an_equally_valid_path"
    )


def test_many_to_many_policy_is_not_silently_ignored(live_postgres, world):
    pin_relationship(
        live_postgres,
        world["view_version_id"],
        world["key_version_id"],
        ordinal=1,
        name="requires_bridge_execution",
        cardinality="many_to_many",
        fan_out_policy="bridge",
        bridge_dataset="campaign_bridge",
        left_datastream_id=world["left"],
        right_datastream_id=world["right"],
    )
    request = _request(world)
    request["edges"][0]["relationship_name"] = "requires_bridge_execution"

    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(
            live_postgres,
            project_id=world["project_id"],
            request=request,
            profile_lookup=lambda *_: {"execution_safety": "ready"},
        )
    assert excinfo.value.code == "bridge_execution_not_supported"


def test_review_required_profile_is_admitted_inside_the_bound(live_postgres, world):
    """AI-293, ratified 2026-08-17. This test asserted the opposite until then.

    `_request` shows `campaign` and asks for grain `day`, so the projection IS the
    whole merge key, and both measures are governed additive. Inside that bound
    the executor folds each source to the key before joining, so no measurement of
    duplication can change the totals -- and `review_required` stops being a reason
    to refuse. The plan says so itself rather than leaving the reader to infer it.
    """
    compiled = plans.compile_plan(
        live_postgres,
        project_id=world["project_id"],
        request=_request(world),
        profile_lookup=_profile_evidence(live_postgres, world, "review_required"),
    )
    bound = compiled["plan"]["merge_bound"]
    assert bound["contract_version"] == plans.MERGE_BOUND_CONTRACT_VERSION
    assert bound["refusals_lifted"] == ["profile_not_ready"]
    assert bound["merge_key_field_ids"] == sorted([world["day"], world["campaign"]])
    assert bound["output_field_ids"] == sorted([world["day"], world["campaign"]])
    # The mitigation is named on the edge, so execution can see the permission.
    assert compiled["plan"]["edges"][0]["safety_mitigation"] == (
        "aggregate_each_member_before_merge"
    )


def test_a_merge_key_finer_than_the_grain_names_that_condition(live_postgres, world):
    """Outside the bound the refusal remains, and it says WHICH condition failed.

    Showing `campaign` alone drops `day` from the projection while the two sources
    are still matched on it -- and the executor's merged SELECT carries no GROUP BY,
    so folding to that coarser grain would not roll up, it would repeat.
    """
    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(
            live_postgres,
            project_id=world["project_id"],
            request=_request(
                world, dimensions=[{"canonical_field_id": world["campaign"]}], grain=None
            ),
            profile_lookup=_profile_evidence(live_postgres, world, "review_required"),
        )
    assert excinfo.value.code == "merge_key_finer_than_grain"
    assert excinfo.value.detail["components"] == ["day"]
    assert excinfo.value.detail["conditions_failed"] == ["merge_key_finer_than_grain"]
    # It names the gesture, never only the cause.
    assert "Show it as a dimension" in excinfo.value.message
    assert "cannot combine" not in excinfo.value.message.lower()


def test_a_non_additive_metric_names_itself_in_the_refusal(live_postgres, world):
    """The other bounded refusal: the metric is named, not merely counted."""
    with live_postgres.cursor() as cur:
        cur.execute(
            "UPDATE app.mdm_canonical_fields SET non_additive = TRUE, aggregation = NULL "
            "WHERE id = %s",
            (world["spend"],),
        )
    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(
            live_postgres,
            project_id=world["project_id"],
            request=_request(world),
            profile_lookup=_profile_evidence(live_postgres, world, "review_required"),
        )
    assert excinfo.value.code == "metric_not_additive"
    assert [entry["metric"] for entry in excinfo.value.detail["metrics"]] == ["spend"]
    assert excinfo.value.detail["metrics"][0]["source"] == "Campaign spend"
    assert "spend" in excinfo.value.message
    assert "cannot combine" not in excinfo.value.message.lower()


def _one_sided_duplicate_profile(conn, world):
    """The pre-AI-293 concession, exercised on its own terms.

    `exact_one_sided_duplicates` (`multi_source_plan.py`) needs a `review_required`
    safety whose two sides are measured `exact`, a multiplication measured `exact`
    above one row per key, and a duplicate on the declared many side ALONE. It
    grants its own mitigation on the edge and appends NOTHING to `deferred`, which
    is precisely how it used to cross `compile_plan` without the bound.
    """
    base = _profile_evidence(
        conn,
        world,
        "review_required",
        multiplication={"state": "exact", "worst_case_rows_per_key": 3},
    )

    def lookup(*args):
        profile = dict(base(*args))
        profile["left"] = {"state": "exact", "duplicated_keys": 4}
        profile["right"] = {"state": "exact", "duplicated_keys": 0}
        return profile

    return lookup


def test_the_bound_is_read_on_a_ready_profile_too(live_postgres, world):
    """The bound is a property of the merge, not of the profile that measured it.

    `compile_plan` used to evaluate `_merge_bound` behind `if deferred_safety:`, so
    an edge measured `ready` reached execution without either ratified condition
    ever being read -- while `multi_source_execution` folds every member to the
    FULL merge key and then projects only the selected dimensions, whatever the
    profile said. This request shows `campaign` and drops `day` from the grain, so
    the two sources are matched on a key the Result does not show.

    THIS TEST REDDENS IF THE BOUND BECOMES CONDITIONAL AGAIN: a `ready` profile
    defers nothing, so a bound read only on a deferral would compile this plan.
    """
    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(
            live_postgres,
            project_id=world["project_id"],
            request=_request(
                world, dimensions=[{"canonical_field_id": world["campaign"]}], grain=None
            ),
            profile_lookup=_profile_evidence(live_postgres, world, "ready"),
        )
    assert excinfo.value.code == "merge_key_finer_than_grain"
    assert excinfo.value.detail["components"] == ["day"]
    assert "Show it as a dimension" in excinfo.value.message


def test_a_ready_profile_does_not_admit_a_non_additive_metric(live_postgres, world):
    """The other half of the bound, on the same path, for the same reason."""
    with live_postgres.cursor() as cur:
        cur.execute(
            "UPDATE app.mdm_canonical_fields SET non_additive = TRUE, aggregation = NULL "
            "WHERE id = %s",
            (world["spend"],),
        )
    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(
            live_postgres,
            project_id=world["project_id"],
            request=_request(world),
            profile_lookup=_profile_evidence(live_postgres, world, "ready"),
        )
    assert excinfo.value.code == "metric_not_additive"
    assert [entry["metric"] for entry in excinfo.value.detail["metrics"]] == ["spend"]


def test_the_bound_is_read_on_the_one_sided_duplicate_concession_too(live_postgres, world):
    """The third path, and the oldest: a concession that predates the bound.

    First it is proved that this profile really takes the concession -- the edge
    carries the mitigation and NO refusal was lifted, which is only true of the
    `exact_one_sided_duplicates` branch. Then the same profile is compiled at a
    grain coarser than the merge key, and the bound must refuse it by name.

    THIS TEST REDDENS IF THE BOUND BECOMES CONDITIONAL AGAIN: this branch appends
    nothing to `deferred`, so a bound read only on a deferral would compile it.
    """
    profile = _one_sided_duplicate_profile(live_postgres, world)
    inside = plans.compile_plan(
        live_postgres,
        project_id=world["project_id"],
        request=_request(world),
        profile_lookup=profile,
    )
    edge = inside["plan"]["edges"][0]
    assert edge["relationship"]["cardinality"] == "many_to_one"
    assert edge["measured_safety"] == "review_required"
    assert edge["safety_mitigation"] == "aggregate_each_member_before_merge"
    # No refusal was lifted: the concession granted the mitigation by itself.
    assert "merge_bound" not in inside["plan"]

    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(
            live_postgres,
            project_id=world["project_id"],
            request=_request(
                world, dimensions=[{"canonical_field_id": world["campaign"]}], grain=None
            ),
            profile_lookup=profile,
        )
    assert excinfo.value.code == "merge_key_finer_than_grain"
    assert excinfo.value.detail["components"] == ["day"]


def test_an_unreadable_vocabulary_refuses_the_merge_instead_of_admitting_it(
    live_postgres, world, monkeypatch
):
    """`unavailable` never reads as a pass (`analyze-and-test.md:647`).

    The additivity matrix is built from the governed vocabulary; when that read
    failed the matrix was empty and every metric was taken for additive, so the
    ratified condition passed EMPTY. An unreachable catalogue is not a permission.
    """
    from core import canonical_field_registry

    def unreadable(*_args, **_kwargs):
        raise RuntimeError("catalogue unreachable")

    monkeypatch.setattr(
        canonical_field_registry, "list_visible_canonical_fields", unreadable
    )
    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(
            live_postgres,
            project_id=world["project_id"],
            request=_request(world),
            profile_lookup=_profile_evidence(live_postgres, world, "ready"),
        )
    assert excinfo.value.code == "metric_additivity_unknown"
    assert {entry["reason"] for entry in excinfo.value.detail["metrics"]} == {
        "vocabulary_unreadable"
    }
    # Named in the reader's words: the SOURCES, never a canonical id.
    assert "Campaign spend, Conversions" in excinfo.value.message
    assert "could not be read at all" in excinfo.value.message
    assert not identifier_rendered_in(excinfo.value.message)
    assert "cannot combine" not in excinfo.value.message.lower()


def test_a_metric_absent_from_the_vocabulary_is_unknown_and_never_additive(
    live_postgres, world, monkeypatch
):
    """Unknown is not additive, one metric at a time as well as wholesale.

    BOTH REGISTRY READS ARE HIDDEN, and that is not belt and braces. "Never
    declared" and "declared then archived" are two situations with two gestures
    (`analyze-and-test.md`, amendment of 2026-08-21), and the compiler tells them
    apart by the `status` on the row. Hiding only the visible catalog would leave
    the row there and simulate the ARCHIVED case while claiming to test the
    absent one -- which is how this test used to pass while asserting the wrong
    sentence.
    """
    from core import canonical_field_registry

    real = canonical_field_registry.list_visible_canonical_fields
    real_identities = canonical_field_registry.load_canonical_field_identities

    def without_spend(conn, **kwargs):
        return [row for row in real(conn, **kwargs) if str(row["id"]) != world["spend"]]

    def identities_without_spend(conn, **kwargs):
        return {
            field_id: identity
            for field_id, identity in real_identities(conn, **kwargs).items()
            if field_id != world["spend"]
        }

    monkeypatch.setattr(
        canonical_field_registry, "list_visible_canonical_fields", without_spend
    )
    monkeypatch.setattr(
        canonical_field_registry,
        "load_canonical_field_identities",
        identities_without_spend,
    )
    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(
            live_postgres,
            project_id=world["project_id"],
            request=_request(world),
            profile_lookup=_profile_evidence(live_postgres, world, "ready"),
        )
    assert excinfo.value.code == "metric_additivity_unknown"
    assert [entry["reason"] for entry in excinfo.value.detail["metrics"]] == [
        "metric_not_in_vocabulary"
    ]
    assert excinfo.value.detail["metrics"][0]["source"] == "Campaign spend"
    # The column the person mapped, not the canonical id nobody can read.
    assert excinfo.value.detail["metrics"][0]["metric"] == "spend_micros"
    assert "spend_micros is not in the governed vocabulary" in excinfo.value.message
    assert "Declare it in Governance" in excinfo.value.message
    assert not identifier_rendered_in(excinfo.value.message)


def test_an_archived_metric_is_asked_to_be_restored_and_never_to_be_declared(
    live_postgres, world
):
    """A field archived while a published mapping still binds it (AI-293 review).

    `list_visible_canonical_fields` is `status = 'active'`, so an archived field
    fell into `metric_not_in_vocabulary` and the person was told to "Declare it in
    Governance with an aggregation" -- about a field they had declared themselves
    and then archived. A refusal that names an impossible gesture is worse than
    one that names none.
    """
    from core import canonical_field_registry

    canonical_field_registry.archive_project_field(
        live_postgres, project_id=world["project_id"], field_id=world["spend"], actor="tester"
    )
    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(
            live_postgres,
            project_id=world["project_id"],
            request=_request(world),
            profile_lookup=_profile_evidence(live_postgres, world, "ready"),
        )
    assert excinfo.value.code == "metric_additivity_unknown"
    assert [entry["reason"] for entry in excinfo.value.detail["metrics"]] == [
        "metric_archived"
    ]
    # Named by the word the registry still holds, which `load_canonical_names`
    # keeps precisely because archiving does not rename what a mapping pinned.
    assert excinfo.value.detail["metrics"][0]["metric"] == "spend"
    assert "spend was archived" in excinfo.value.message
    assert "Restore it in Governance" in excinfo.value.message
    # The gesture that cannot be performed is NOT offered.
    assert "Declare it in Governance" not in excinfo.value.message
    assert not identifier_rendered_in(excinfo.value.message)


@pytest.fixture()
def blank_column_world(live_postgres):
    """The same world, except one measure is bound to NO column at all."""
    org_id, project_id = make_project(live_postgres, "Blank")
    day = make_canonical_field(live_postgres, project_id, "day", value_type="date")
    campaign = make_canonical_field(live_postgres, project_id, "campaign_id")
    spend = make_canonical_field(
        live_postgres, project_id, "spend", kind="metric", value_type="money"
    )
    conversions = make_canonical_field(
        live_postgres, project_id, "conversions", kind="metric", value_type="integer"
    )
    left = make_datastream(
        live_postgres, org_id, project_id, "Campaign spend",
        bindings={
            day: ("date", "confirmed"),
            campaign: ("campaign", "confirmed"),
            spend: ("", "confirmed"),
        },
    )
    right = make_datastream(
        live_postgres, org_id, project_id, "Conversions",
        bindings={
            day: ("event_date", "confirmed"),
            campaign: ("campaign_key", "confirmed"),
            conversions: ("conversion_count", "confirmed"),
        },
    )
    publish_output(live_postgres, org_id, project_id, left, "blank_spend")
    publish_output(live_postgres, org_id, project_id, right, "blank_conversions")
    key = keys.create_common_key(
        live_postgres,
        project_id=project_id,
        name="Day and Campaign",
        canonical_field_ids=[day, campaign],
        actor="tester",
    )
    _view_id, view_version_id = make_semantic_view(live_postgres, project_id)
    pin_relationship(
        live_postgres, view_version_id, key["current_version"]["id"], name="blank_cross"
    )
    return {
        "org_id": org_id, "project_id": project_id, "left": left, "right": right,
        "day": day, "campaign": campaign, "spend": spend, "conversions": conversions,
        "key_version_id": key["current_version"]["id"], "view_version_id": view_version_id,
    }


def test_a_measure_bound_to_no_column_is_refused_before_it_can_be_nameless(
    live_postgres, blank_column_world
):
    """The root of the id that reached the screen (AI-293 review, finding 1).

    `_bindings` reads `physical_field` as `str(field.get("field_id") or "")`, and
    nothing refused the empty string. Downstream the merge bound had no word left
    for that measure -- no governed name once archived, no column -- so it fell
    back on the canonical id and printed it. A binding with no column contributes
    no numbers either, so the honest place to stop is here.
    """
    world = blank_column_world
    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(
            live_postgres,
            project_id=world["project_id"],
            request=_request(world),
            profile_lookup=_profile_evidence(live_postgres, world, "ready"),
        )
    assert excinfo.value.code == "measure_binding_has_no_column"
    assert "Campaign spend binds spend to no column" in excinfo.value.message
    assert "Map it to a column" in excinfo.value.message
    assert not identifier_rendered_in(excinfo.value.message)


def test_a_measure_with_neither_name_nor_column_still_never_prints_its_id():
    """The last resort of the naming chain, on the pure function (finding 1).

    Compilation now stops a column-less binding at the door, but a plan FROZEN
    before that refusal existed is immutable and still carries one -- and the
    bound is re-read on every execution. So the chain has to end on a word even
    when the vocabulary, the registry and the mapping all have nothing: it names
    the SOURCE, which is what the person typed when they created it.
    """
    verdict = plans._merge_bound(
        [
            {
                "name": "Campaign spend",
                "measures": [
                    {
                        "canonical_field_id": "mdm_01KZAAAAAAAAAAAAAAAAAAAAAA",
                        "physical_field": "",
                    }
                ],
            }
        ],
        [
            {
                "components": [
                    {
                        "canonical_field_id": "mdm_01KZBBBBBBBBBBBBBBBBBBBBBB",
                        "canonical_name": "day",
                    }
                ]
            }
        ],
        [],
        None,
        {},
        {},
        additivity_readable=True,
        names={},
        archived=set(),
    )
    assert not verdict["within"]
    message = verdict["failures"][0]["message"]
    assert "a measure of Campaign spend" in message
    assert not identifier_rendered_in(message)


def test_no_refusal_of_this_module_names_a_field_by_its_canonical_id(live_postgres, world):
    """FIVE REFUSALS PROVOKED FOR REAL -- and no longer the guard of the class.

    This test used to end on a promise it could not keep: "they are walked in one
    place so the next one added is measured against the same rule". Five
    scenarios in a dict walk five scenarios. Nothing here measured the sixth, and
    nothing here measured the ELEVEN refusals of `multi_source_execution`, one of
    which -- `ratio_component_not_selected` at `:481` -- rendered
    `mdm_<ULID>` into `manifest.refusal.message` of every Result it refused,
    for as long as this file claimed to hold the class.

    The class is held by `tests/core/test_refusals_never_name_an_identifier.py`,
    which LOCATES every `raise ...Refused(...)` of `server/core/` by walking the
    AST and needs no scenario at all. What is left here is what only a live
    database can give: these five sentences, provoked against real published
    mappings and a real governed vocabulary, so the rule is checked on the words
    the compiler actually produced and not on the words it was written to
    produce.

    The identifier check is derived too: `identifier_rendered_in` carries the
    shape of EVERY family the product mints, where this file used to grep for
    `mdm_` and would have let `ds_01KZ...` or `qsv_01KZ...` straight through.
    """
    scenarios = {
        "measure_not_bound": _request(
            world,
            members=[
                {"datastream_id": world["left"],
                 "measures": [{"canonical_field_id": world["conversions"]}]},
                {"datastream_id": world["right"],
                 "measures": [{"canonical_field_id": world["conversions"]}]},
            ],
        ),
        "field_is_not_a_measure": _request(
            world,
            members=[
                {"datastream_id": world["left"],
                 "measures": [{"canonical_field_id": world["campaign"]}]},
                {"datastream_id": world["right"],
                 "measures": [{"canonical_field_id": world["conversions"]}]},
            ],
        ),
        "duplicate_measure": _request(
            world,
            members=[
                {"datastream_id": world["left"],
                 "measures": [{"canonical_field_id": world["spend"]},
                              {"canonical_field_id": world["spend"]}]},
                {"datastream_id": world["right"],
                 "measures": [{"canonical_field_id": world["conversions"]}]},
            ],
        ),
        "duplicate_pivot_dimension": _request(
            world,
            pivot={
                "rows": [{"canonical_field_id": world["campaign"]},
                         {"canonical_field_id": world["campaign"]}],
                "columns": [],
            },
        ),
        "ratio_component_not_selected": _request(
            world,
            derived_measures=[
                {
                    "canonical_field_id": world["spend"],
                    "numerator_field_id": world["spend"],
                    "denominator_field_id": world["campaign"],
                }
            ],
        ),
    }
    seen = {}
    for expected, request in scenarios.items():
        with pytest.raises(plans.PlanRefused) as excinfo:
            plans.compile_plan(
                live_postgres,
                project_id=world["project_id"],
                request=request,
                profile_lookup=_profile_evidence(live_postgres, world, "ready"),
            )
        seen[expected] = excinfo.value.code
        assert not identifier_rendered_in(excinfo.value.message), (expected, excinfo.value.message)
        # A refusal names a gesture, never only a cause.
        assert excinfo.value.message.rstrip().endswith("."), expected
    assert seen == {code: code for code in scenarios}


def test_a_precomputed_ratio_is_refused_toward_its_two_sided_components(live_postgres, world):
    """The third cause the generic refusal used to hide (AI-293).

    `cpa` is spend over conversions -- one component on each side of the merge. A
    source that exports the ratio pre-computed cannot contribute it: the executor
    folds every member to the shared key before joining, and a folded row ratio
    states a number nobody measured (sum of ratios != ratio of sums). The refusal
    is the non-additive condition NAMING THE RATIO, and its gesture -- ask for
    the components and let the Result recompute it after the merge -- is proven
    real right below, on components that live on the two different sides.
    """
    cpa = make_canonical_field(
        live_postgres, world["project_id"], "cpa", kind="metric", value_type="ratio"
    )
    with live_postgres.cursor() as cur:
        # The registry classifies a ratio name as non-additive (`is_ratio_name`);
        # the fixture helper writes additive metrics only, so state it here.
        cur.execute(
            "UPDATE app.mdm_canonical_fields SET non_additive = TRUE, aggregation = NULL "
            "WHERE id = %s",
            (cpa,),
        )
    exporter = make_datastream(
        live_postgres, world["org_id"], world["project_id"], "CPA export",
        bindings={
            world["day"]: ("date", "confirmed"),
            world["campaign"]: ("campaign", "confirmed"),
            cpa: ("cpa_value", "confirmed"),
        },
    )
    publish_output(live_postgres, world["org_id"], world["project_id"], exporter, "plan_cpa")
    pin_relationship(
        live_postgres,
        world["view_version_id"],
        world["key_version_id"],
        ordinal=1,
        name="cpa_to_conversions",
        left_datastream_id=exporter,
        right_datastream_id=world["right"],
    )
    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(
            live_postgres,
            project_id=world["project_id"],
            request=_request(
                world,
                members=[
                    {"datastream_id": exporter, "measures": [{"canonical_field_id": cpa}]},
                    {
                        "datastream_id": world["right"],
                        "measures": [{"canonical_field_id": world["conversions"]}],
                    },
                ],
                edges=[
                    {
                        "left": exporter,
                        "right": world["right"],
                        "common_key_version_id": world["key_version_id"],
                        "relationship_name": "cpa_to_conversions",
                    }
                ],
            ),
            profile_lookup=None,
        )
    assert excinfo.value.code == "metric_not_additive"
    assert [entry["metric"] for entry in excinfo.value.detail["metrics"]] == ["cpa"]
    assert "measures it is computed from" in excinfo.value.message

    # The named gesture compiles: the same ratio, declared as a derived measure
    # over its two governed components -- spend from one source, conversions from
    # the other -- is admitted inside the bound and recomputed after the merge.
    admitted = plans.compile_plan(
        live_postgres,
        project_id=world["project_id"],
        request=_request(
            world,
            derived_measures=[
                {
                    "canonical_field_id": cpa,
                    "numerator_field_id": world["spend"],
                    "denominator_field_id": world["conversions"],
                }
            ],
        ),
        profile_lookup=None,
    )
    assert admitted["plan"]["merge_bound"]["refusals_lifted"] == ["profile_required"]
    derived = admitted["plan"]["derived_measures"][0]
    assert derived["numerator_field_id"] == world["spend"]
    assert derived["denominator_field_id"] == world["conversions"]


def test_relationship_version_pin_is_part_of_authority(live_postgres, world):
    request = _request(world)
    request["edges"][0]["view_version_id"] = "svv_NOT_THE_APPROVED_VERSION"
    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(
            live_postgres,
            project_id=world["project_id"],
            request=request,
            profile_lookup=lambda *_: {"execution_safety": "ready"},
        )
    assert excinfo.value.code == "relationship_not_found"


def test_a_measure_the_source_does_not_bind_is_refused(live_postgres, world):
    request = _request(world)
    request["members"][0]["measures"] = [{"canonical_field_id": world["conversions"]}]
    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(live_postgres, project_id=world["project_id"], request=request)
    assert excinfo.value.code == "measure_not_bound"


def test_a_client_expression_never_becomes_a_measure(live_postgres, world):
    request = _request(world)
    request["members"][0]["measures"] = [
        {"canonical_field_id": world["spend"], "expression": "SUM(spend) * 2"}
    ]
    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(live_postgres, project_id=world["project_id"], request=request)
    assert excinfo.value.code == "client_expression_refused"


def test_a_filter_without_its_stage_is_refused(live_postgres, world):
    request = _request(world)
    request["filters"] = [{"canonical_field_id": world["campaign"], "operator": "eq", "value": "A"}]
    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(live_postgres, project_id=world["project_id"], request=request)
    assert excinfo.value.code == "filter_stage_required"


def test_a_pre_aggregation_filter_names_the_source_it_narrows(live_postgres, world):
    request = _request(world)
    request["filters"] = [
        {
            "stage": "pre_aggregation",
            "canonical_field_id": world["campaign"],
            "operator": "eq",
            "value": "A",
        }
    ]
    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(live_postgres, project_id=world["project_id"], request=request)
    assert excinfo.value.code == "filter_names_no_source"


def test_a_measured_unsafe_cross_is_refused_outside_the_bound(live_postgres, world):
    """A six-fold measured fan-out no longer decides alone -- the bound does.

    Inside the bound the same `unsafe` measurement is admitted, because folding
    each source to the merge key first is what makes the six-fold irrelevant. The
    refusal survives exactly where the bound does not hold, and it carries what
    was measured so the reader is not asked to take the verdict on trust.
    """
    unsafe = _profile_evidence(
        live_postgres, world, "unsafe", {"state": "exact", "worst_case_rows_per_key": 6}
    )
    admitted = plans.compile_plan(
        live_postgres,
        project_id=world["project_id"],
        request=_request(world),
        profile_lookup=unsafe,
    )
    assert admitted["plan"]["merge_bound"]["refusals_lifted"] == ["unsafe_fan_out"]

    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(
            live_postgres,
            project_id=world["project_id"],
            request=_request(
                world, dimensions=[{"canonical_field_id": world["campaign"]}], grain=None
            ),
            profile_lookup=unsafe,
        )
    assert excinfo.value.code == "merge_key_finer_than_grain"
    assert excinfo.value.detail["measured"][0]["worst_case_rows_per_key"] == 6


def test_a_plan_without_evidence_is_admitted_only_inside_the_bound(live_postgres, world):
    """Evidence bounds a fan-out; inside the bound there is no fan-out to bound.

    Outside it, the absence of a measurement is still refused -- and the refusal
    names the bound condition that failed rather than asking for evidence that
    would not have changed the answer.
    """
    compiled = plans.compile_plan(
        live_postgres,
        project_id=world["project_id"],
        request=_request(world),
        profile_lookup=None,
    )
    assert compiled["plan"]["merge_bound"]["refusals_lifted"] == ["profile_required"]

    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(
            live_postgres,
            project_id=world["project_id"],
            request=_request(
                world, dimensions=[{"canonical_field_id": world["campaign"]}], grain=None
            ),
            profile_lookup=None,
        )
    assert excinfo.value.code == "merge_key_finer_than_grain"


def test_one_source_alone_is_not_a_cross(live_postgres, world):
    request = _request(world)
    request["members"] = request["members"][:1]
    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(live_postgres, project_id=world["project_id"], request=request)
    assert excinfo.value.code == "member_count_out_of_bounds"


def test_two_sources_with_no_declared_cross_are_refused(live_postgres, world):
    request = _request(world)
    request["edges"] = []
    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(live_postgres, project_id=world["project_id"], request=request)
    assert excinfo.value.code == "join_graph_not_a_tree"


def test_a_source_of_another_project_is_one_answer(live_postgres, world):
    _other_org, other_project = make_project(live_postgres, "Elsewhere")
    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(
            live_postgres, project_id=other_project, request=_request(world)
        )
    assert excinfo.value.code == "member_not_available"


# ---------------------------------------------------------------------------
# Storage: immutable, beside the single-source specs, never confused with them
# ---------------------------------------------------------------------------


def test_a_stored_plan_reads_back_with_every_pin(live_postgres, world):
    compiled = plans.compile_plan(
        live_postgres,
        project_id=world["project_id"],
        request=_request(world),
        profile_lookup=_profile_evidence(live_postgres, world),
    )
    stored = plans.store_plan_version(
        live_postgres,
        org_id=world["org_id"],
        project_id=world["project_id"],
        compiled=compiled,
        actor="tester",
        name="Spend and conversions",
    )
    loaded = plans.load_plan_version(
        live_postgres,
        project_id=world["project_id"],
        query_spec_version_id=stored["query_spec_version_id"],
    )
    assert loaded["content_hash"] == compiled["content_hash"]
    assert loaded["plan"]["inclusion_policy"] == "matched_only"
    assert loaded["semantic_view_version_id"] == world["view_version_id"]


def test_a_single_source_spec_is_refused_rather_than_read_as_a_plan(live_postgres, world):
    """Reading a v1 spec as a plan would invent members it never had."""
    from ulid import ULID

    spec_id, version_id = f"qs_{ULID()}", f"qsv_{ULID()}"
    with live_postgres.cursor() as cur:
        cur.execute(
            "INSERT INTO app.query_specs (id, org_id, project_id, semantic_view_id, created_by) "
            "VALUES (%s,%s,%s,%s,'tester')",
            (spec_id, world["org_id"], world["project_id"], world["view_id"]),
        )
        cur.execute(
            """
            INSERT INTO app.query_spec_versions
                (id, query_spec_id, org_id, project_id, version_number, semantic_view_id,
                 semantic_view_version_id, spec, content_hash, created_by)
            VALUES (%s,%s,%s,%s,1,%s,%s,'{"contract_version":"query-spec.v1"}'::jsonb,%s,'tester')
            """,
            (
                version_id,
                spec_id,
                world["org_id"],
                world["project_id"],
                world["view_id"],
                world["view_version_id"],
                "a" * 64,
            ),
        )
    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.load_plan_version(
            live_postgres,
            project_id=world["project_id"],
            query_spec_version_id=version_id,
        )
    assert excinfo.value.code == "not_a_multi_source_plan"


def test_every_compiled_measure_carries_the_word_a_reader_needs(live_postgres, world):
    """THE LEGEND IS FROZEN WITH THE PLAN, not recomposed by whoever reads it.

    The Explorer used to build each Result column's legend from the match
    catalogue and end on `?? measure.canonical_field_id`, so a measure that
    catalogue does not carry was legended `mdm_01KZ...` -- a legend nobody can
    use, which `visualization-and-rendering.md` forbids in the same words. The
    word is a governed fact of the plan, like the unit beside it, so it travels
    with the measure and no reader has to look it up.

    Asserted on EVERY measure of EVERY member and on every derived measure, not
    on one: a plan that names three of its four columns is a plan with a ULID on
    a screen.
    """
    compiled = plans.compile_plan(
        live_postgres,
        project_id=world["project_id"],
        request=_request(
            world,
            derived_measures=[
                {
                    "canonical_field_id": world["spend"],
                    "numerator_field_id": world["spend"],
                    "denominator_field_id": world["conversions"],
                }
            ],
        ),
        profile_lookup=_profile_evidence(live_postgres, world),
    )
    plan = compiled["plan"]
    named = [
        *(
            measure
            for member in plan["members"]
            for measure in member["measures"]
        ),
        *plan["derived_measures"],
    ]
    assert named, "a rule asserted over an empty list proves nothing"
    for measure in named:
        word = measure.get("canonical_name")
        assert word, f"{measure['canonical_field_id']} is frozen with no word at all"
        assert not identifier_rendered_in(word), f"{word!r} is an identifier, not a name"


def test_the_golden_question_list_serves_the_name_of_the_domain_it_pins(live_postgres, world):
    """THE PIN'S NAME IS SERVED, so no screen has to compose it.

    The Explorer looked each pinned Business Domain up in the org's domain list
    and fell back to `version.business_domain_id`, which put `bd_01KZ...` in a
    picker whenever the pin named a domain that list does not carry -- a
    Golden Question pinning a domain outside the current selection, or one
    archived since. A fallback the browser takes is a name the server did not
    serve, so the server serves it.
    """
    questions = golden_questions.list_golden_questions(
        live_postgres, org_id=world["org_id"], project_id=world["project_id"]
    )
    pinned = [
        question["current_version"]
        for question in questions
        if question["current_version"]
        and question["current_version"]["business_domain_id"] == world["domain_id"]
    ]
    assert pinned, "the fixture pins a Business Domain: the read lost it"
    for version in pinned:
        assert version["business_domain_name"] == "Growth"


# ---------------------------------------------------------------------------
# The cross-source temporal compatibility gate (story 66.4).
#
# The key VERSION is immutable; the mappings implementing it are not. A key
# declared while both sources stored a calendar DATE keeps its content hash after
# one of them republishes the same column as a TIMESTAMP -- and both spellings
# classify as `date`, so the DECLARATION-time refusal of `mdm_common_keys` lets
# the pair through. The plan is where the two columns finally meet.
# ---------------------------------------------------------------------------


def _temporal_world(
    conn,
    left_day_type: str,
    right_day_type: str,
    *,
    left_campaign_type: str | None = None,
    right_campaign_type: str | None = None,
) -> dict[str, str]:
    """Le monde a deux flux, et les deux composantes de la cle sont typables.

    Les types de CAMPAGNE sont arrives le 2026-08-23 (66.4) : sans eux, le refus
    `incompatible_key_types` ne pouvait etre prouve qu'en appelant
    `_refuse_incompatible_time` a la main -- un instrument qui mesure sa propre
    copie. Il fallait pouvoir donner deux CLASSES differentes a une composante
    qui n'est PAS le jour, parce que sur le jour c'est `false_day_equivalence`
    qui tombe d'abord.
    """
    org_id, project_id = make_project(conn, "Temporal")
    day = make_canonical_field(conn, project_id, "day", value_type="date")
    campaign = make_canonical_field(conn, project_id, "campaign_id")
    spend = make_canonical_field(conn, project_id, "spend", kind="metric", value_type="money")
    conversions = make_canonical_field(
        conn, project_id, "conversions", kind="metric", value_type="integer"
    )
    left = make_datastream(
        conn, org_id, project_id, "Campaign spend",
        bindings={
            day: ("date", "confirmed", left_day_type),
            campaign: (
                ("campaign", "confirmed", left_campaign_type)
                if left_campaign_type
                else ("campaign", "confirmed")
            ),
            spend: ("spend_micros", "confirmed"),
        },
    )
    right = make_datastream(
        conn, org_id, project_id, "Conversions",
        bindings={
            day: ("event_date", "confirmed", right_day_type),
            campaign: (
                ("campaign_key", "confirmed", right_campaign_type)
                if right_campaign_type
                else ("campaign_key", "confirmed")
            ),
            conversions: ("conversion_count", "confirmed"),
        },
    )
    publish_output(conn, org_id, project_id, left, "temporal_spend")
    publish_output(conn, org_id, project_id, right, "temporal_conversions")
    key = keys.create_common_key(
        conn,
        project_id=project_id,
        name="Day and Campaign",
        canonical_field_ids=[day, campaign],
        actor="tester",
    )
    _view_id, view_version_id = make_semantic_view(conn, project_id)
    pin_relationship(
        conn, view_version_id, key["current_version"]["id"], name="spend_to_conversions"
    )
    return {
        "org_id": org_id,
        "project_id": project_id,
        "left": left,
        "right": right,
        "campaign": campaign,
        "spend": spend,
        "conversions": conversions,
        "key_version_id": key["current_version"]["id"],
    }


def _temporal_request(world: dict[str, str]) -> dict:
    return {
        "members": [
            {"datastream_id": world["left"], "measures": [{"canonical_field_id": world["spend"]}]},
            {
                "datastream_id": world["right"],
                "measures": [{"canonical_field_id": world["conversions"]}],
            },
        ],
        "edges": [
            {
                "left": world["left"],
                "right": world["right"],
                "common_key_version_id": world["key_version_id"],
            }
        ],
        "dimensions": [{"canonical_field_id": world["campaign"]}],
        "inclusion_policy": "matched_only",
        "grain": "day",
    }


def test_two_sources_agreeing_on_the_calendar_day_compile(live_postgres):
    """The gate is a refusal, not a tax: agreeing columns pass it unremarked."""
    world = _temporal_world(live_postgres, "date", "date")
    compiled = plans.compile_plan(
        live_postgres, project_id=world["project_id"], request=_temporal_request(world)
    )
    key_path = compiled["plan"]["edges"][0]["key_paths"][0]
    # The two types are frozen INTO the plan: a pin nobody can read is a pin
    # nobody can check.
    assert key_path["left_physical_type"] == "date"
    assert key_path["right_physical_type"] == "date"


def test_a_day_matched_against_an_instant_is_a_false_day_equivalence(live_postgres):
    """`mdm_common_keys` let this pair through, and it was right to.

    `date` and `timestamp` are the SAME class -- both temporal -- so the
    declaration-time refusal has nothing to say. The granularity is where they
    part, and matching them keeps only the rows stamped at midnight.
    """
    world = _temporal_world(live_postgres, "date", "timestamp")
    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(
            live_postgres, project_id=world["project_id"], request=_temporal_request(world)
        )
    assert excinfo.value.code == "false_day_equivalence"
    # The sentence names the gesture, on the side that has to make it.
    assert "Conversions" in excinfo.value.message
    assert "Campaign spend" in excinfo.value.message
    assert "midnight" in excinfo.value.message


def test_the_refusal_names_the_finer_side_whichever_way_the_edge_runs(live_postgres):
    world = _temporal_world(live_postgres, "timestamp", "date")
    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(
            live_postgres, project_id=world["project_id"], request=_temporal_request(world)
        )
    assert excinfo.value.code == "false_day_equivalence"
    assert excinfo.value.message.startswith("Campaign spend stamps day at an instant")


def _retype_binding(conn, *, datastream_id: str, project_id: str, physical: str, new_type: str):
    """Publier une SECONDE version de mapping ou une colonne a change de type.

    Pas un UPDATE : `app.reject_datastream_mapping_version_mutation` rend une
    version publiee IMMUABLE. C'est aussi ce qui rend le scenario reel -- un
    mapping ne se corrige pas, il se republie, et la cle a ete gelee avant.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT mapping_payload, plan_version_id, version_number "
            "FROM app.datastream_mapping_versions "
            "WHERE datastream_id = %s ORDER BY version_number DESC LIMIT 1",
            (datastream_id,),
        )
        payload, plan_version_id, version_number = cur.fetchone()
        fields = [dict(f) for f in payload["fields"]]
        # La cle est `field_id` -- `epic66_fixtures:146`. Un nom devine ici ne
        # leverait pas : il ne trouverait simplement rien, et le test passerait en
        # mesurant une compilation reussie. Le compte ci-dessous est ce qui
        # empeche ce faux vert.
        touched = 0
        for field in fields:
            if field.get("field_id") == physical:
                field["physical_type"] = new_type
                touched += 1
        assert touched == 1, (
            f"aucun champ `{physical}` retype : le mapping ne porte pas cette cle, "
            "et le test mesurerait une compilation reussie au lieu d'un refus"
        )
        new_id = uid("dmv")
        cur.execute(
            """
            INSERT INTO app.datastream_mapping_versions
                (id, datastream_id, project_id, version_number, mapping_contract_version,
                 source_schema_hash, plan_version_id, content_hash, ossie_spec_version,
                 toorow_extension_version, mapping_payload, ossie_projection,
                 idempotency_key_hash, created_by)
            VALUES (%s,%s,%s,%s,'mapping.v1',%s,%s,%s,'0.1.1','2',%s::jsonb,'{}'::jsonb,%s,'tester')
            """,
            (
                new_id,
                datastream_id,
                project_id,
                version_number + 1,
                hash64(),
                plan_version_id,
                hash64(),
                json.dumps({**payload, "fields": fields}),
                hash64(),
            ),
        )
        cur.execute(
            "UPDATE app.datastreams SET current_mapping_version_id = %s WHERE id = %s",
            (new_id, datastream_id),
        )


def test_compile_plan_ITSELF_refuses_two_key_kinds_against_a_real_base(live_postgres):
    """La meme propriete, prouvee par le COMPILATEUR et non par le refuseur.

    Le test plus bas appelle `_refuse_incompatible_time` directement. C'est utile
    -- la comparaison est pure -- mais ca ne prouve pas que `compile_plan`
    l'ATTEINT : une branche jamais executee rendrait ce test vert et le refus
    mort. C'est << un instrument ne mesure pas sa propre copie >>.

    LE SCENARIO EST CELUI QUE LA BRANCHE GARDE, et il n'y en a pas d'autre : la
    cle est declaree sur des types QUI S'ACCORDENT -- `mdm_common_keys` refuse
    net a la declaration, sinon -- puis un des deux flux republie son mapping
    avec une autre classe. Une version publiee est immuable, donc c'est bien une
    SECONDE version, exactement comme en production.

    La composante retypee est CAMPAGNE et pas le jour : sur le jour,
    `false_day_equivalence` tombe d'abord et ce test mesurerait l'autre refus.
    """
    world = _temporal_world(
        live_postgres,
        "date",
        "date",
        left_campaign_type="string",
        right_campaign_type="varchar",
    )
    # La cle est gelee. MAINTENANT le mapping bouge.
    _retype_binding(
        live_postgres,
        datastream_id=world["right"],
        project_id=world["project_id"],
        physical="campaign_key",
        new_type="bigint",
    )
    # LA SORTIE SE REPUBLIE AVEC LE MAPPING. Elle epingle la version de mapping,
    # donc republier l'un sans l'autre casse la chaine et le compilateur refuse
    # `member_publishes_no_output` AVANT d'atteindre les types -- le test
    # mesurerait alors un refus qui n'est pas le sien. C'est aussi ce que fait la
    # production : un mapping republie produit une nouvelle sortie.
    publish_output(
        live_postgres,
        world["org_id"],
        world["project_id"],
        world["right"],
        "temporal_conversions_v2",
    )
    with pytest.raises(plans.PlanRefused) as excinfo:
        plans.compile_plan(
            live_postgres, project_id=world["project_id"], request=_temporal_request(world)
        )
    assert excinfo.value.code == "incompatible_key_types"
    # Le refus NOMME les deux cotes : sans eux, la personne sait qu'une cle est
    # mal typee et pas laquelle des deux corriger.
    assert "Campaign spend" in excinfo.value.message
    assert "Conversions" in excinfo.value.message


def test_two_key_components_of_the_SAME_kind_compile(live_postgres):
    """La borne de l'autre cote : le refus ci-dessus doit pouvoir NE PAS tomber.

    Sans elle, un `compile_plan` qui refuserait tout rendrait le test precedent
    vert -- il mesurerait alors l'indulgence de rien du tout. Deux orthographes
    de la MEME classe (`string` et `varchar`) doivent compiler.
    """
    world = _temporal_world(
        live_postgres,
        "date",
        "date",
        left_campaign_type="string",
        right_campaign_type="varchar",
    )
    plan = plans.compile_plan(
        live_postgres, project_id=world["project_id"], request=_temporal_request(world)
    )
    assert plan is not None


def test_a_key_component_of_two_different_kinds_is_refused_by_its_own_name():
    """Not `false_day_equivalence`: a string against an integer is not a day at all.

    Proven without a database because it is a pure comparison of two mapping
    entries. The DECLARATION-time twin of this refusal lives in
    `mdm_common_keys` and is proven there; this one exists because the mappings
    can change after the key version is frozen.
    """
    with pytest.raises(plans.PlanRefused) as excinfo:
        plans._refuse_incompatible_time(
            component={"canonical_name": "campaign_id"},
            left_name="Campaign spend",
            right_name="Conversions",
            left_binding={"physical_type": "string"},
            right_binding={"physical_type": "bigint"},
        )
    assert excinfo.value.code == "incompatible_key_types"
    assert "Campaign spend" in excinfo.value.message
    assert "Conversions" in excinfo.value.message


def test_a_type_the_classifier_does_not_recognize_refuses_nothing():
    """An unrecognized type is the mapping's `unknown_field` ambiguity, not ours."""
    assert (
        plans._refuse_incompatible_time(
            component={"canonical_name": "campaign_id"},
            left_name="A",
            right_name="B",
            left_binding={"physical_type": "st_geography"},
            right_binding={"physical_type": "string"},
        )
        is None
    )
