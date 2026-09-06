"""Match discovery over a real schema (story 66.2).

What only a real database proves here: the JOIN that makes a cross EXECUTABLE.
A relationship inside a draft view version grants nothing, a relationship with no
pinned key grants nothing, and an unpublished Datastream must not appear anywhere
-- not in a match, not in a count, not in a refusal. Three of those four are
conditions on rows a mock would simply have been told.

It runs as the ordinary `connector` role. `live_postgres` rolls back on teardown.
"""

from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from core import datastream_matches as matches  # noqa: E402
from core import mdm_common_keys as keys  # noqa: E402

from tests.integration.epic66_fixtures import (  # noqa: E402
    make_canonical_field,
    make_datastream,
    make_project,
    make_semantic_view,
    pin_relationship,
    publish_output,
)


@pytest.fixture()
def world(live_postgres):
    """Two published Datastreams that both bind `day` and `campaign_id`."""
    org_id, project_id = make_project(live_postgres)
    day = make_canonical_field(live_postgres, project_id, "day", value_type="date")
    campaign = make_canonical_field(live_postgres, project_id, "campaign_id")
    left = make_datastream(
        live_postgres, org_id, project_id, "Campaign spend",
        bindings={day: ("date", "confirmed"), campaign: ("campaign", "confirmed")},
        measures={"spend": "confirmed", "impressions": "confirmed"},
    )
    right = make_datastream(
        live_postgres, org_id, project_id, "Conversions",
        bindings={day: ("event_date", "resolved"), campaign: ("campaign_key", "resolved")},
        measures={"conversions": "confirmed"},
    )
    publish_output(live_postgres, org_id, project_id, left, "match_left")
    publish_output(live_postgres, org_id, project_id, right, "match_right")
    key = keys.create_common_key(
        live_postgres,
        project_id=project_id,
        name="Day and Campaign",
        canonical_field_ids=[day, campaign],
        actor="tester",
    )
    return {
        "org_id": org_id,
        "project_id": project_id,
        "day": day,
        "campaign": campaign,
        "left": left,
        "right": right,
        "key": key,
    }


def test_a_key_with_no_approved_relationship_is_a_candidate(live_postgres, world):
    answer = matches.discover_matches(live_postgres, project_id=world["project_id"])
    assert answer["counts"]["governed"] == 0
    assert [m["kind"] for m in answer["matches"]] == ["candidate_key_missing"]
    assert answer["matches"][0]["explore_together"] is None


def test_a_published_relationship_makes_the_same_pair_governed(live_postgres, world):
    _view_id, version_id = make_semantic_view(live_postgres, world["project_id"])
    pin_relationship(live_postgres, version_id, world["key"]["current_version"]["id"])

    answer = matches.discover_matches(live_postgres, project_id=world["project_id"])
    assert answer["counts"]["governed"] == 1
    match = answer["matches"][0]
    assert match["kind"] == "governed"
    assert match["execution_safety"] == "ready"
    assert match["observed_coverage"] == "unavailable"
    # Both physical paths, read from the two published mappings.
    paths = {p["canonical_name"]: (p["left_field"], p["right_field"]) for p in match["key_paths"]}
    assert paths == {"day": ("date", "event_date"), "campaign_id": ("campaign", "campaign_key")}
    # Three measures across the two sources.
    assert match["unlocked_measures"] == 3


def test_approval_does_not_leak_to_another_pair_using_the_same_key(live_postgres, world):
    third = make_datastream(
        live_postgres,
        world["org_id"],
        world["project_id"],
        "A third implementation",
        bindings={
            world["day"]: ("day", "confirmed"),
            world["campaign"]: ("campaign", "confirmed"),
        },
    )
    publish_output(live_postgres, world["org_id"], world["project_id"], third, "third")
    _view_id, version_id = make_semantic_view(live_postgres, world["project_id"])
    pin_relationship(
        live_postgres,
        version_id,
        world["key"]["current_version"]["id"],
        left_datastream_id=world["left"],
        right_datastream_id=world["right"],
    )

    answer = matches.discover_matches(live_postgres, project_id=world["project_id"])
    governed_pairs = {
        frozenset((match["left"]["datastream_id"], match["right"]["datastream_id"]))
        for match in answer["matches"]
        if match["kind"] == "governed"
    }
    assert governed_pairs == {frozenset((world["left"], world["right"]))}
    assert all(third not in pair for pair in governed_pairs)


def test_a_relationship_inside_a_draft_version_grants_nothing(live_postgres, world):
    """Only `published` is authority. A draft is somebody's work in progress."""
    _view_id, version_id = make_semantic_view(
        live_postgres, world["project_id"], name="draft_view", status="draft"
    )
    pin_relationship(live_postgres, version_id, world["key"]["current_version"]["id"])

    answer = matches.discover_matches(live_postgres, project_id=world["project_id"])
    assert answer["counts"]["governed"] == 0


def test_a_relationship_pinning_no_key_grants_nothing(live_postgres, world):
    """The column is nullable, and a NULL means 'joins, but names no shared identity'."""
    _view_id, version_id = make_semantic_view(live_postgres, world["project_id"])
    with live_postgres.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.semantic_view_version_relationships
                (view_version_id, ordinal, name, from_dataset, to_dataset, from_columns,
                 to_columns, cardinality_type, fan_out_policy)
            VALUES (%s, 0, 'unpinned', 'left', 'right', ARRAY['a'], ARRAY['b'],
                    'many_to_one', 'forbid')
            """,
            (version_id,),
        )
    answer = matches.discover_matches(live_postgres, project_id=world["project_id"])
    assert answer["counts"]["governed"] == 0


def test_an_unpublished_datastream_appears_nowhere(live_postgres, world):
    make_datastream(live_postgres, world["org_id"], world["project_id"], "Never published")
    answer = matches.discover_matches(live_postgres, project_id=world["project_id"])
    assert answer["counts"]["datastreams_published"] == 2
    named = str(answer["matches"])
    assert "Never published" not in named


def test_same_named_columns_with_no_binding_are_the_other_candidate(live_postgres):
    org_id, project_id = make_project(live_postgres, "Same names")
    left = make_datastream(
        live_postgres, org_id, project_id, "A", unbound_fields=("campaign_id", "spend")
    )
    right = make_datastream(
        live_postgres, org_id, project_id, "B", unbound_fields=("campaign_id", "clicks")
    )
    publish_output(live_postgres, org_id, project_id, left, "same_name_left")
    publish_output(live_postgres, org_id, project_id, right, "same_name_right")
    answer = matches.discover_matches(live_postgres, project_id=project_id)
    assert [m["kind"] for m in answer["matches"]] == ["candidate_binding_missing"]
    assert "campaign_id" in answer["matches"][0]["next_action"]


def test_a_project_with_one_published_source_says_so(live_postgres):
    org_id, project_id = make_project(live_postgres, "Lonely")
    datastream_id = make_datastream(
        live_postgres, org_id, project_id, "Only one", unbound_fields=("x",)
    )
    publish_output(live_postgres, org_id, project_id, datastream_id, "lonely")
    answer = matches.discover_matches(live_postgres, project_id=project_id)
    assert answer["matches"] == []
    assert answer["empty_reason"]["code"] == "one_published_datastream"


def test_an_empty_project_names_the_gesture_that_fills_it(live_postgres):
    _org_id, project_id = make_project(live_postgres, "Empty")
    answer = matches.discover_matches(live_postgres, project_id=project_id)
    assert answer["empty_reason"]["code"] == "no_published_datastream"
    assert "publish" in answer["empty_reason"]["message"].lower()


def test_another_project_sees_none_of_it(live_postgres, world):
    _view_id, version_id = make_semantic_view(live_postgres, world["project_id"])
    pin_relationship(live_postgres, version_id, world["key"]["current_version"]["id"])
    _other_org, other_project = make_project(live_postgres, "Other")

    answer = matches.discover_matches(live_postgres, project_id=other_project)
    assert answer["matches"] == []
    assert answer["counts"]["datastreams_published"] == 0


def test_the_datastream_door_is_the_catalog_filtered(live_postgres, world):
    _view_id, version_id = make_semantic_view(live_postgres, world["project_id"])
    pin_relationship(live_postgres, version_id, world["key"]["current_version"]["id"])

    catalog = matches.discover_matches(live_postgres, project_id=world["project_id"])
    per_source = matches.matches_for_datastream(
        live_postgres, project_id=world["project_id"], datastream_id=world["left"]
    )
    assert per_source["matches"] == catalog["matches"]

    third = make_datastream(
        live_postgres, world["org_id"], world["project_id"], "Unrelated",
        unbound_fields=("something_else",),
    )
    isolated = matches.matches_for_datastream(
        live_postgres, project_id=world["project_id"], datastream_id=third
    )
    assert isolated["matches"] == []
    assert isolated["empty_reason"]["code"] == "no_match_for_datastream"


def test_a_datastream_of_another_project_is_not_found(live_postgres, world):
    _other_org, other_project = make_project(live_postgres, "Elsewhere")
    with pytest.raises(matches.DatastreamNotFound):
        matches.matches_for_datastream(
            live_postgres, project_id=other_project, datastream_id=world["left"]
        )


def test_the_answer_states_its_bounds(live_postgres, world):
    answer = matches.discover_matches(live_postgres, project_id=world["project_id"])
    assert answer["bounds"]["max_matches"] == matches.MAX_MATCHES
    assert answer["bounds"]["response_bytes"] <= matches.MAX_RESPONSE_BYTES
    assert answer["bounds"]["max_measures_per_datastream"] == (
        matches.MAX_MEASURES_PER_DATASTREAM
    )
    assert answer["bounds"]["truncated"] is False
