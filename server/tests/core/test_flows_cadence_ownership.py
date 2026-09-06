"""`flows_upsert` accepted a field it does not write, and said nothing.

Found by tracing `datastream :: the whole surface editable automatically by MCP`
across the four planes (`capability-trace`).

Measured: `flow.datastream.schema.json` declares `cadence_mode` and
`next_run_at` and sets `additionalProperties: false`, so both are ACCEPTED.
`_upsert_datastream` builds its scalar map with `schedule_mode`,
`refetch_days`, `date_window_days` — and neither of those two. They are read
back by `_datastream_row_to_flow` and written by nobody.

So a model that set a cadence through `flows_upsert` got a validated, audited
write with `changed:true` on the OTHER fields, and its own edit dropped. The
door reported a success it had not performed — the failure this repository keeps
finding, this time on the model's side of the wire.

The repair refuses rather than writes: `set_datastream_schedule` owns those
fields, and `flows.py:8` states the module's contract — "via those modules,
never inline SQL, never bypassing their audit". Writing here would create the
second cadence path that contract exists to prevent.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from core.flows import FlowValidationError, _refuse_fields_owned_elsewhere

from tests.conftest import REPO_ROOT

SCHEMA = pathlib.Path(REPO_ROOT / "server/core/schemas/flow.datastream.schema.json")


def test_the_schema_accepts_the_two_fields_which_is_why_silence_was_wrong():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert schema.get("additionalProperties") is False
    for field in ("cadence_mode", "next_run_at"):
        assert field in schema["properties"], (
            f"{field} left the schema; the refusal below is then unreachable and "
            f"should go with it"
        )


def _refuse(field: str, sent, stored):
    """Call the rule with a doc that changes one owned field."""
    with pytest.raises(FlowValidationError) as excinfo:
        _refuse_fields_owned_elsewhere({field: sent}, {field: stored})
    return excinfo.value


def test_changing_the_cadence_here_is_refused_and_names_its_owner():
    error = _refuse("cadence_mode", "hourly", "nightly")
    message = json.dumps(error.errors)
    assert "set_datastream_schedule" in message
    assert "cadence_mode" in message
    # The current value is stated, so the caller can tell "already right" from
    # "wrong door".
    assert "nightly" in message


def test_changing_the_next_run_here_is_refused_too():
    error = _refuse("next_run_at", "2026-09-01T00:00:00Z", "2026-08-01T00:00:00Z")
    assert "set_datastream_schedule" in json.dumps(error.errors)


def test_resending_the_stored_value_unchanged_is_not_refused():
    # The read projection returns both fields, so get -> edit something else ->
    # upsert resends them identical. Refusing that would break the honest loop.
    _refuse_fields_owned_elsewhere({"cadence_mode": "nightly"}, {"cadence_mode": "nightly"})


def test_a_creation_has_nothing_to_compare_and_is_not_refused():
    # No existing row: there is no stored value to differ from, and the create
    # path is not this rule's business.
    _refuse_fields_owned_elsewhere({"cadence_mode": "hourly"}, None)


def test_the_flow_document_can_carry_the_weekly_cadence():
    """AI-217. The read projection now RETURNS `weekly` for a weekly Datastream.

    `cadence_mode` is derived from `schedule_mode` and used to fall through to
    `manual` for anything but `nightly`/`hourly`. Now that a weekly row reads as
    `weekly`, an enum stuck on three values would refuse the very document this
    schema describes -- and `additionalProperties: false` means the refusal would
    be total, not partial.
    """
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert "weekly" in schema["properties"]["cadence_mode"]["enum"]
