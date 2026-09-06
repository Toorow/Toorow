"""Story 49.6 AC9: an Event is shown by REFERENCE, or not shown.

`resolve_event_observation_references` is the only read Context Hub is allowed
to make of an Event, and the whole risk is in one decision: when is a pointer
proven enough to publish, and when must it collapse to `unavailable`?

Every test below is a way that decision goes wrong quietly. A half-proven
binding published as a link sends a reader to a Datastream nobody demonstrated
owns the observation. A missing row distinguished from a foreign one turns the
overlay into a way to ask whether another Project's Event exists.

The SQL itself (the scope in the WHERE, the LEFT JOIN on the version) is not
exercised here — it needs a real Postgres. What is exercised is what the module
DECIDES about the rows it gets back, which is where the defect would be.
"""

from __future__ import annotations

import datetime as dt

from core.event_configurations import (
    EVENT_BINDING_LINKED,
    EVENT_BINDING_UNAVAILABLE,
    resolve_event_observation_references,
)


class _Cursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self.executed.append((sql, params))

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows):
        self.cursor_obj = _Cursor(rows)

    def cursor(self):
        return self.cursor_obj


def _row(
    event_id="evt_1",
    event_type="release",
    event_date=dt.date(2026, 8, 1),
    binding_state=EVENT_BINDING_LINKED,
    datastream_id="ds_1",
    execution_id="exec_1",
    version_id="ecv_1",
    configuration_id="ecfg_1",
    version_number=3,
):
    return (
        event_id, event_type, event_date, binding_state,
        datastream_id, execution_id, version_id, configuration_id, version_number,
    )


def test_a_fully_bound_observation_publishes_a_deep_link_to_its_owner():
    resolved = resolve_event_observation_references(
        _Conn([_row()]), project_id="p1", event_ids=["evt_1"]
    )
    reference = resolved["evt_1"]
    assert reference["binding_state"] == EVENT_BINDING_LINKED
    assert reference["datastream_id"] == "ds_1"
    assert reference["event_configuration_version_id"] == "ecv_1"
    # AC9: "The overlay deep-links to the canonical Data/Datastream Event
    # evidence." The object is the CONFIGURATION, in Data, on the `usage` tab —
    # which page-structure.md:844 maps to the very sentence authorising this.
    # (The citation read `:605` until 2026-09-01 and pointed at the Evidence
    # tabs of section C — a different subject. Re-read and re-anchored.)
    route = reference["owner_route"]
    assert route["workspace"] == "data"
    assert route["section"] == "events"
    assert route["object_type"] == "event-configuration"
    assert route["object_id"] == "ecfg_1"
    assert route["tab"] == "usage"
    assert route["version_id"] == "ecv_1"


def test_it_returns_the_pointer_and_never_the_events_content():
    """AC9: Context Hub "stores no Event definition, mapping, payload, source
    sample, timezone, run evidence or editable copy". A reader that returned
    those would have made the copy with an extra step, not with a table."""
    reference = resolve_event_observation_references(
        _Conn([_row()]), project_id="p1", event_ids=["evt_1"]
    )["evt_1"]
    forbidden = {
        "source_mapping", "collection_policy", "payload", "source_sample",
        "timezone", "run_evidence", "label", "description",
    }
    assert forbidden.isdisjoint(reference)


def test_a_binding_marked_linked_without_a_version_collapses_to_unavailable():
    """Migration 133 bound legacy rows only where exactly ONE owning Datastream
    could be proven, and a row can carry `linked` with its version row since
    deleted. Half a pointer sends a reader somewhere nobody proved."""
    resolved = resolve_event_observation_references(
        _Conn([_row(version_id=None, configuration_id=None)]),
        project_id="p1",
        event_ids=["evt_1"],
    )
    assert resolved["evt_1"] == {
        "event_id": "evt_1",
        "binding_state": EVENT_BINDING_UNAVAILABLE,
    }


def test_a_binding_marked_linked_without_a_datastream_collapses_to_unavailable():
    resolved = resolve_event_observation_references(
        _Conn([_row(datastream_id=None)]), project_id="p1", event_ids=["evt_1"]
    )
    assert resolved["evt_1"]["binding_state"] == EVENT_BINDING_UNAVAILABLE


def test_an_unbound_observation_discloses_nothing_beyond_unavailable():
    """Non-disclosing: the caller learns the evidence cannot be reached, and
    nothing about why — not its type, not its date, not a Datastream."""
    resolved = resolve_event_observation_references(
        _Conn([_row(binding_state=EVENT_BINDING_UNAVAILABLE)]),
        project_id="p1",
        event_ids=["evt_1"],
    )
    assert set(resolved["evt_1"]) == {"event_id", "binding_state"}


def test_an_absent_observation_is_simply_absent_so_foreign_and_missing_agree():
    """The scope lives in the WHERE, not in a filter applied after. An
    observation of another Project and one that never existed both come back
    as nothing — neither confirms the other, and the overlay cannot be used to
    probe for an Event's existence."""
    resolved = resolve_event_observation_references(
        _Conn([]), project_id="p1", event_ids=["evt_foreign"]
    )
    assert resolved == {}


def test_the_scope_is_a_query_parameter_not_a_post_hoc_filter():
    conn = _Conn([_row()])
    resolve_event_observation_references(conn, project_id="p1", event_ids=["evt_1", "evt_1", ""])
    sql, params = conn.cursor_obj.executed[0]
    assert params["project_id"] == "p1"
    assert "o.project_id = %(project_id)s" in sql
    # Deduplicated, and the empty id dropped before it reached the database.
    assert params["event_ids"] == ["evt_1"]


def test_no_ids_asks_the_database_nothing():
    conn = _Conn([_row()])
    assert resolve_event_observation_references(conn, project_id="p1", event_ids=[]) == {}
    assert conn.cursor_obj.executed == []
