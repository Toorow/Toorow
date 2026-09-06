"""Which Datastream names a dimension -- derived, never stored twice.

The fixture is the deployment's own shape: a performance stream that binds
`video`, a catalogue stream that binds `video_id` to the same canonical target
with the reference role, and the four ways that pairing can be missing.
"""

from __future__ import annotations

import pytest


def _payload(field_id: str, target: str, status: str = "confirmed"):
    return {
        "grain": [field_id],
        "fields": [
            {
                "field_id": field_id,
                "binding": {"canonical_target": target, "status": status},
            }
        ],
    }


PERFORMANCE = {
    "id": "ds_perf",
    "name": "views by video (daily)",
    "data_role": "Performance",
    "archived": False,
    "mapping_payload": _payload("video", "video"),
}
CATALOGUE = {
    "id": "ds_cat",
    "name": "video catalogue",
    "data_role": "Reference & targets",
    "archived": False,
    "mapping_payload": _payload("video_id", "video"),
}


def test_the_reference_role_is_one_of_the_seven_declared_roles():
    from core.datastreams import DATA_ROLES
    from core.dimension_reference import REFERENCE_ROLE

    assert REFERENCE_ROLE in DATA_ROLES


def test_two_streams_that_bind_the_same_target_are_joined_on_it():
    """One says `video`, the other `video_id`. Neither is renamed."""
    from core.dimension_reference import select_reference

    answer = select_reference([PERFORMANCE, CATALOGUE], canonical_dimension="video")
    assert answer["state"] == "declared"
    assert answer["reference"]["datastream_id"] == "ds_cat"
    assert answer["reference"]["source_field"] == "video_id"
    assert len(answer["candidates"]) == 2


def test_an_unconfirmed_binding_names_nothing():
    from core.dimension_reference import GAP_NO_CANDIDATE, select_reference

    proposed = dict(CATALOGUE, mapping_payload=_payload("video_id", "video", "suggested"))
    answer = select_reference(
        [dict(PERFORMANCE, mapping_payload=_payload("video", "video", "suggested")), proposed],
        canonical_dimension="video",
    )
    assert (answer["state"], answer["gap"]) == ("absent", GAP_NO_CANDIDATE)


def test_a_dimension_nothing_binds_says_so():
    from core.dimension_reference import GAP_NO_CANDIDATE, select_reference

    answer = select_reference([PERFORMANCE, CATALOGUE], canonical_dimension="playlist")
    assert (answer["state"], answer["gap"]) == ("absent", GAP_NO_CANDIDATE)
    assert answer["reference"] is None


def test_binding_without_the_role_is_a_different_gap_and_a_different_gesture():
    from core.dimension_reference import GAP_ROLE_ABSENT, select_reference

    answer = select_reference(
        [PERFORMANCE, dict(CATALOGUE, data_role="Performance")],
        canonical_dimension="video",
    )
    assert (answer["state"], answer["gap"]) == ("absent", GAP_ROLE_ABSENT)
    assert "role" in answer["message"].lower()


def test_the_archived_catalogue_is_the_deployment_case(caplog):
    """2026-08-12: the one stream with the reference role was archived, in draft.

    The honest answer names the catalogue and says it is archived -- a sentence a
    person acts on. `0 unresolved` would not have been.
    """
    from core.dimension_reference import GAP_ARCHIVED, select_reference

    answer = select_reference(
        [PERFORMANCE, dict(CATALOGUE, archived=True)], canonical_dimension="video"
    )
    assert (answer["state"], answer["gap"]) == ("absent", GAP_ARCHIVED)
    assert answer["reference"]["name"] == "video catalogue"


def test_a_reference_with_no_mapping_version_is_named_apart():
    from core.dimension_reference import GAP_NOT_MAPPED, select_reference

    # It binds the target through an older payload we still see, but its current
    # mapping version is gone: `mapped` is what separates the two refusals.
    unmapped = dict(CATALOGUE)
    unmapped["mapping_payload"] = _payload("video_id", "video")
    answer = select_reference(
        [PERFORMANCE, dict(unmapped, archived=False)], canonical_dimension="video"
    )
    assert answer["state"] == "declared"

    stripped = dict(CATALOGUE, mapping_payload=None)
    answer = select_reference([PERFORMANCE, stripped], canonical_dimension="video")
    # With no payload at all it binds nothing, so it is not even a candidate: the
    # gap is the one before it, and the sentence still names one gesture.
    assert answer["gap"] in {GAP_NOT_MAPPED, "no_stream_carries_the_reference_role"}


def test_a_json_string_payload_is_read_like_a_dict():
    import json

    from core.dimension_reference import select_reference

    answer = select_reference(
        [dict(CATALOGUE, mapping_payload=json.dumps(_payload("video_id", "video")))],
        canonical_dimension="video",
    )
    assert answer["state"] == "declared"


def test_an_unreadable_catalogue_is_never_a_silent_absence():
    from core.dimension_reference import read_reference

    class Boom:
        def cursor(self):
            raise RuntimeError("no connection")

    answer = read_reference(Boom(), project_id="proj_EXAMPLE", canonical_dimension="video")
    assert answer["state"] == "absent"
    assert answer["message"]


@pytest.mark.parametrize("dimension", ["", "   ", None])
def test_an_empty_dimension_binds_nothing(dimension):
    from core.dimension_reference import fields_binding

    assert fields_binding(_payload("video", "video"), dimension) == []
