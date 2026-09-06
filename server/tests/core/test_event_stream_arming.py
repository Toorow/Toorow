"""Chantier C -- arming an event stream is one gesture, on any domain.

THE MEASUREMENT THESE PIN. Before this module, the four operations that create a
governed Event Configuration were four HTTP routes and zero console callers, so
the object could only exist if someone knew the four addresses and could compose
their payloads. The three that exist in production got there exactly that way.

And nothing about the payload was domain-specific: every field of it is on the
Datastream or in the Connector manifest it was built from. So the tests that
matter are the ones that prove the derivation, prove it on a NON-video domain,
and prove that the refusals name a gesture rather than a table.
"""

from __future__ import annotations

import core.event_stream_arming as arming
import pytest
from core.event_stream_arming import (
    EventStreamRefused,
    declared_events,
    existing_configuration,
    plan_event_stream,
)


class _Cursor:
    def __init__(self, script):
        self._script = script
        self._last = ""
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self._last = sql
        self.executed.append((sql, params))

    def fetchone(self):
        for pattern, value in self._script:
            if pattern in self._last:
                return value
        return None


class _Conn:
    def __init__(self, script=()):
        self._script = list(script)
        self.cursors = []

    def cursor(self):
        cur = _Cursor(self._script)
        self.cursors.append(cur)
        return cur


def _datastream(name, module, profile, schedule="nightly"):
    return [("FROM app.datastreams", (name, module, profile, schedule))]


# ---------------------------------------------------------------------------
# 1. What a Connector declares, read from the registry -- never a list of names.
# ---------------------------------------------------------------------------


#: The five domains the repository actually ships an event profile for, measured
#: 2026-08-14 by scanning `server/modules/*/manifest.json` for a report profile
#: carrying `events`. They are parameters of ONE test on purpose: the day a sixth
#: connector declares one, this list is what has to grow, and nothing else.
_EVENT_PROFILES = [
    ("google-business-profile", "social_post", "social_post"),
    ("meta-ads", "campaign_launch", "campaign_launch"),
    ("monday", "board_events", "monday_change"),
    ("shopify", "product_launch", "product_launch"),
    ("youtube-analytics", "video_upload", "video_upload"),
]


@pytest.mark.parametrize("module,profile,event", _EVENT_PROFILES)
def test_the_declared_events_come_from_the_manifest_for_every_domain(module, profile, event):
    assert declared_events(module, profile) == [event]


def test_a_measurement_report_declares_no_event_and_says_so_rather_than_guessing():
    assert declared_events("youtube-analytics", "channel_performance_daily") == []


def test_an_unknown_connector_yields_no_event_instead_of_raising():
    """A Datastream on a Connector this build does not load must refuse with a
    gesture, not with a stack trace on the way to one."""
    assert declared_events("a-connector-that-is-not-loaded", "anything") == []


# ---------------------------------------------------------------------------
# 2. The plan is DERIVED. No question is asked, on any domain.
# ---------------------------------------------------------------------------


def test_the_plan_is_derived_from_the_datastream_and_its_manifest():
    plan = plan_event_stream(
        _Conn(_datastream("Shop - product launches", "shopify", "product_launch", "nightly")),
        project_id="proj_EXAMPLE",
        datastream_id="ds_EXAMPLE",
    )
    assert plan["source_mapping"] == {
        "module": "shopify",
        "report_id": "product_launch",
        "events": ["product_launch"],
    }
    assert plan["collection_policy"] == {"cadence": "nightly", "timezone": "UTC"}
    assert plan["configuration_name"] == "product_launch"


def test_the_same_code_path_plans_a_non_video_domain_and_a_video_one():
    """Jean's acceptance measure for chantier C, as a test: the gesture must work
    on a domain that is not video, without a line of code being written for it."""
    video = plan_event_stream(
        _Conn(_datastream("v", "youtube-analytics", "video_upload")),
        project_id="proj_EXAMPLE",
        datastream_id="ds_video",
    )
    launch = plan_event_stream(
        _Conn(_datastream("c", "meta-ads", "campaign_launch")),
        project_id="proj_EXAMPLE",
        datastream_id="ds_campaign",
    )
    assert video["source_mapping"]["events"] == ["video_upload"]
    assert launch["source_mapping"]["events"] == ["campaign_launch"]
    # Same shape, same keys, nothing branched on the domain.
    assert set(video["source_mapping"]) == set(launch["source_mapping"])


def test_a_stream_that_runs_hourly_is_not_described_as_nightly():
    """A policy that says `nightly` about an hourly stream is a wrong statement,
    and it is the schedule the policy is supposed to be reporting."""
    plan = plan_event_stream(
        _Conn(_datastream("h", "shopify", "product_launch", "hourly")),
        project_id="proj_EXAMPLE",
        datastream_id="ds_EXAMPLE",
    )
    assert plan["collection_policy"]["cadence"] == "hourly"


# ---------------------------------------------------------------------------
# 3. Every refusal names a gesture, and arming twice is refused.
# ---------------------------------------------------------------------------


_DATABASE_WORDS = ("app.", "table", "column", "event_configurations", "report_profile_id")


def _refusal(script, **kwargs):
    with pytest.raises(EventStreamRefused) as caught:
        plan_event_stream(_Conn(script), project_id="proj_EXAMPLE", datastream_id="ds_EXAMPLE")
    return caught.value


def test_a_datastream_with_no_report_names_the_gesture_that_gives_it_one():
    refusal = _refusal(_datastream("x", "shopify", "", "nightly"))
    assert refusal.code == "no_report_profile"
    assert "Finish its setup" in refusal.message


def test_a_measurement_datastream_is_refused_by_naming_what_to_do_instead():
    refusal = _refusal(_datastream("x", "youtube-analytics", "channel_performance_daily"))
    assert refusal.code == "profile_declares_no_event"
    assert "declares events" in refusal.message


@pytest.mark.parametrize(
    "script,code",
    [
        ([("FROM app.datastreams", None)], "datastream_not_found"),
        (_datastream("x", "shopify", ""), "no_report_profile"),
        (_datastream("x", "youtube-analytics", "channel_performance_daily"),
         "profile_declares_no_event"),
    ],
)
def test_no_refusal_of_this_door_hands_a_person_a_table_name(script, code):
    refusal = _refusal(script)
    assert refusal.code == code
    lowered = refusal.message.lower()
    for word in _DATABASE_WORDS:
        assert word not in lowered, f"`{code}` names `{word}` instead of a gesture"


def test_arming_an_already_armed_stream_is_refused_and_names_the_one_that_exists():
    """A second configuration would collect the same events under a second
    identity, and every later count of them would be double."""
    conn = _Conn(
        [
            (
                "FROM app.event_configurations c",
                ("ecfg_1", "product_launch", "active", "ecv_1", 1, "active"),
            )
        ]
    )
    with pytest.raises(EventStreamRefused) as caught:
        arming.arm_event_stream(
            conn,
            project_id="proj_EXAMPLE",
            datastream_id="ds_EXAMPLE",
            org_id="org_EXAMPLE",
            actor="owner@example.com",
        )
    assert caught.value.code == "already_armed"
    assert "product_launch" in caught.value.message


def test_a_draft_configuration_does_not_block_arming():
    """`already_armed` is about an ACTIVE stream. A configuration left in draft is
    an unfinished attempt, and refusing on it would strand the Datastream with no
    way forward from the screen."""
    conn = _Conn(
        [
            (
                "FROM app.event_configurations c",
                ("ecfg_1", "product_launch", "draft", None, None, None),
            ),
            ("FROM app.datastreams", ("x", "shopify", "product_launch", "nightly")),
        ]
    )
    live = existing_configuration(conn, project_id="proj_EXAMPLE", datastream_id="ds_EXAMPLE")
    assert live["lifecycle_state"] == "draft"
    # It plans, rather than raising `already_armed`.
    assert plan_event_stream(
        conn, project_id="proj_EXAMPLE", datastream_id="ds_EXAMPLE"
    )["source_mapping"]["module"] == "shopify"


# ---------------------------------------------------------------------------
# 4. The four operations are walked, in order, on one idempotency stem.
# ---------------------------------------------------------------------------


def test_the_ladder_is_walked_in_order_and_a_retry_replays_the_same_operations(monkeypatch):
    calls: list[tuple[str, str]] = []

    def _record(name, result):
        def _fn(conn, **kwargs):
            calls.append((name, kwargs["idempotency_key"]))
            return {"result": result}

        return _fn

    from core import event_configurations

    monkeypatch.setattr(
        event_configurations, "create_event_configuration",
        _record("create", {"event_configuration_id": "ecfg_1"}),
    )
    monkeypatch.setattr(
        event_configurations, "create_event_configuration_version",
        _record("version", {"version_id": "ecv_1"}),
    )
    monkeypatch.setattr(
        event_configurations, "confirm_event_configuration_version",
        _record("confirm", {"review_state": "confirmed"}),
    )
    monkeypatch.setattr(
        event_configurations, "activate_event_configuration_version",
        _record("activate", {"lifecycle_state": "active"}),
    )

    conn = _Conn(
        [
            ("FROM app.event_configurations c", None),
            ("FROM app.datastreams", ("x", "shopify", "product_launch", "nightly")),
        ]
    )
    armed = arming.arm_event_stream(
        conn,
        project_id="proj_EXAMPLE",
        datastream_id="ds_EXAMPLE",
        org_id="org_EXAMPLE",
        actor="owner@example.com",
    )

    assert [name for name, _ in calls] == ["create", "version", "confirm", "activate"]
    # One stem, four suffixes: a retried gesture replays the same four operations
    # rather than minting a second configuration.
    stems = {key.rsplit("-", 1)[0] for _, key in calls}
    assert stems == {"arm-events-ds_EXAMPLE"}
    assert armed["event_configuration_id"] == "ecfg_1"
    assert armed["version_id"] == "ecv_1"
    assert armed["source_mapping"]["events"] == ["product_launch"]
