"""The EVENTS route of an import, proven off base (Story 68.4).

WHAT THIS FILE PINS. The decisions the route makes before a single marker is
written, all pure:

  * WHICH files take this route -- a mapping declaring `date`, `type` and
    `label` roles -- and which do NOT: a mapping declaring nothing, a mapping
    missing a required role, a mapping where two columns claim one role, a
    file carrying measures (those are facts);
  * what a row that cannot be an event gets: a rejection naming the field, the
    rule and the reason -- never a drop. Including the one refusal that keeps
    the vocabulary honest, an unknown event type;
  * the delete-window is the FILE's own span, so a re-import replaces what it
    talks about and nothing else.

The storage half -- real markers, the arming of a Connector-less stream, the
ledger, `meta.context_events` -- lives in
`tests/integration/test_context_event_import_pg.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

from core import context_event_import as cei

SCHEMA = (
    Path(__file__).resolve().parents[2]
    / "core"
    / "schemas"
    / "datastream-field-mapping.schema.json"
)


def _field(field_id, target, *, role=None, status="confirmed", designation=None):
    binding = {"status": status, "canonical_target": target}
    if role is not None:
        binding["event_role"] = role
    if designation is not None:
        binding["designates_object_kind"] = designation
    return {"field_id": field_id, "binding": binding}


def _payload(fields=None):
    return {
        "grain": ["day", "headline"],
        "fields": list(
            fields
            if fields is not None
            else [
                _field("day", "day", role="date"),
                _field("kind", "kind", role="type"),
                _field("headline", "headline", role="label"),
            ]
        ),
    }


_PLAN = {"executable": True, "grain": ["day", "headline"]}


def _route():
    return cei.event_declaration(_payload(), _PLAN)


# ---------------------------------------------------------------------------
# The declaration, and the vocabulary that must not fork.
# ---------------------------------------------------------------------------


def test_the_schema_enum_and_the_module_agree():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    enum = schema["$defs"]["field_binding"]["properties"]["event_role"]["enum"]
    # Two lists holding one meaning drift the moment somebody extends one.
    assert [value for value in enum if value is not None] == list(cei.EVENT_ROLES)


def test_date_type_and_label_make_an_events_file():
    route = _route()
    assert route is not None
    assert route["targets"] == {"date": "day", "type": "kind", "label": "headline"}
    assert route["entity_kind"] is None


def test_a_mapping_declaring_no_role_is_not_an_events_file():
    payload = _payload([_field("day", "day"), _field("headline", "headline")])
    assert cei.event_declaration(payload, _PLAN) is None
    # ...and it is not a BROKEN events file either: a facts mapping must stay
    # executable.
    assert cei.validate_event_declaration(payload) == []


def test_a_missing_required_role_is_refused_and_names_it():
    payload = _payload(
        [_field("day", "day", role="date"), _field("headline", "headline", role="label")]
    )
    issues = cei.validate_event_declaration(payload)
    assert [issue.reason for issue in issues] == [cei.REASON_MISSING_ROLE]
    assert "type" in issues[0].message
    # A refused declaration never routes: half a calendar is worse than none.
    assert cei.event_declaration(payload, _PLAN) is None


def test_two_columns_claiming_one_role_are_refused_naming_both():
    payload = _payload(
        [
            _field("day", "day", role="date"),
            _field("kind", "kind", role="type"),
            _field("headline", "headline", role="label"),
            _field("subtitle", "subtitle", role="label"),
        ]
    )
    issues = cei.validate_event_declaration(payload)
    assert [issue.reason for issue in issues] == [cei.REASON_DUPLICATE_ROLE]
    assert "headline" in issues[0].message and "subtitle" in issues[0].message


def test_an_unknown_role_is_refused_rather_than_ignored():
    payload = _payload(
        [
            _field("day", "day", role="date"),
            _field("kind", "kind", role="type"),
            _field("headline", "headline", role="label"),
            _field("who", "who", role="owner"),
        ]
    )
    issues = cei.validate_event_declaration(payload)
    assert [issue.reason for issue in issues] == [cei.REASON_UNKNOWN_ROLE]


def test_a_role_on_a_column_the_mapping_will_not_land_is_refused():
    payload = _payload(
        [
            _field("day", "day", role="date"),
            _field("kind", "kind", role="type"),
            _field("headline", "headline", role="label"),
            _field("note", "note", role="description", status="excluded"),
        ]
    )
    issues = cei.validate_event_declaration(payload)
    assert [issue.reason for issue in issues] == [cei.REASON_ROLE_NOT_LANDED]


def test_a_file_carrying_measures_takes_the_warehouse_route():
    assert cei.event_declaration(_payload(), {**_PLAN, "additive_measures": ["clicks"]}) is None


def test_an_entity_column_composes_with_the_68_2_designation():
    payload = _payload(
        [
            _field("day", "day", role="date"),
            _field("kind", "kind", role="type"),
            _field("headline", "headline", role="label"),
            _field("video_id", "video_id", role="entity", designation="video"),
        ]
    )
    route = cei.event_declaration(payload, _PLAN)
    # An event ABOUT a governed entity: the two declarations compose, they never
    # fork a second identity model.
    assert route["entity_kind"] == "video"
    assert route["targets"]["entity"] == "video_id"


# ---------------------------------------------------------------------------
# Per-row refusals. Each names the field a person repairs.
# ---------------------------------------------------------------------------


def _known_type():
    from core.report_dictionary import _load_event_type_dictionary

    types = sorted(_load_event_type_dictionary())
    assert types, "the canonical event dictionary is empty -- this test proves nothing"
    return types[0]


def test_a_valid_row_passes():
    validate = cei.event_row_validator(_route())
    row = {"day": "2026-08-01", "kind": _known_type(), "headline": "Spring push"}
    assert validate(row, 2) is None


def test_a_row_without_a_usable_day_is_rejected():
    validate = cei.event_row_validator(_route())
    rejected = validate({"day": "01/08/2026", "kind": _known_type(), "headline": "x"}, 4)
    assert rejected.rule == cei.RULE_EVENT_VALIDATION_FAILED
    assert rejected.field_name == "day"
    assert rejected.row_number == 4


def test_a_row_without_a_label_is_rejected():
    validate = cei.event_row_validator(_route())
    rejected = validate({"day": "2026-08-01", "kind": _known_type(), "headline": "  "}, 5)
    assert rejected.field_name == "headline"


def test_an_over_long_label_is_rejected():
    validate = cei.event_row_validator(_route())
    rejected = validate(
        {"day": "2026-08-01", "kind": _known_type(), "headline": "x" * 121}, 6
    )
    assert rejected.field_name == "headline"
    assert "120" in rejected.reason


def test_an_unknown_event_type_is_rejected_never_drifted():
    validate = cei.event_row_validator(_route())
    rejected = validate({"day": "2026-08-01", "kind": "vibes", "headline": "x"}, 7)
    assert rejected.field_name == "kind"
    assert "drift" in rejected.reason


def test_a_non_numeric_magnitude_is_rejected():
    payload = _payload(
        [
            _field("day", "day", role="date"),
            _field("kind", "kind", role="type"),
            _field("headline", "headline", role="label"),
            _field("budget", "budget", role="value"),
        ]
    )
    validate = cei.event_row_validator(cei.event_declaration(payload, _PLAN))
    row = {"day": "2026-08-01", "kind": _known_type(), "headline": "x", "budget": "beaucoup"}
    assert validate(row, 8).field_name == "budget"
    # An EMPTY magnitude is not a bad one: the role is optional per row.
    assert validate({**row, "budget": ""}, 9) is None


# ---------------------------------------------------------------------------
# The delete-window is the file's own span.
# ---------------------------------------------------------------------------


def test_the_window_is_the_files_first_and_last_day():
    rows = [
        {"day": "2026-08-05", "kind": "x", "headline": "c"},
        {"day": "2026-08-01", "kind": "x", "headline": "a"},
        {"day": "2026-08-03", "kind": "x", "headline": "b"},
    ]
    assert cei.event_window(_route(), rows) == ("2026-08-01", "2026-08-05")


def test_a_file_with_no_usable_day_clears_nothing():
    # Erasing a window a file cannot even name would delete markers of days it
    # says nothing about.
    assert cei.event_window(_route(), [{"day": "", "kind": "x", "headline": "a"}]) is None
