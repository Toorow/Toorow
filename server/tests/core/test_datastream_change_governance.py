"""The governed mapping append: audited, and appending is not activating.

WHY THIS FILE EXISTS. `datastream_change.confirm_change` is the writer that
replaced the retired `POST /api/datastreams/{id}/mapping/versions` (commit
26695dc). `test_datastream_mapping_api.py` marks the retired route
`xfail(strict=True)`, so a remount raises an alarm — but nothing asserted what
the REPLACEMENT guarantees. The retirement was covered; the successor was not.

Three properties, each of which failed silently in the retired handler:

  1. the append runs inside `execute_operation`, so it leaves an audit row and an
     outbox event. The orphaned handler called `save_field_mapping` directly and
     left `app.operations` untouched;
  2. it appends WITHOUT moving `datastreams.current_mapping_version_id`.
     `save_field_mapping` defaults `advance_pointer` to True, so a caller that
     simply omits the argument silently ACTIVATES the version it just wrote;
  3. it commits with its operation, not before it, so the mapping row, the audit
     row and the outbox event are atomic.

These read the source rather than exercising a transaction because that is what
makes them cheap enough to run on every change, and because the defect they guard
is a missing argument at a call site — visible in the source, invisible in a
mocked round trip that never checks which value was passed.
"""

from __future__ import annotations

import pathlib

import pytest

_CORE = pathlib.Path(__file__).resolve().parents[2] / "core"


def _confirm_change_source() -> str:
    body = (_CORE / "datastream_change.py").read_text(encoding="utf-8")
    start = body.find("def confirm_change")
    assert start != -1, "confirm_change has been renamed; this contract needs re-pointing"
    return body[start:]


def test_the_governed_append_runs_inside_the_audited_operation_seam():
    confirm = _confirm_change_source()
    assert "execute_operation" in confirm, (
        "confirm_change writes a mapping version outside execute_operation. "
        "app.operations and app.operation_outbox would carry no trace of an "
        "append to a governed append-only table."
    )
    assert "save_field_mapping" in confirm


@pytest.mark.parametrize(
    ("argument", "why"),
    [
        (
            "advance_pointer=False",
            "appending a version must not activate it; activation belongs to the "
            "publication step. save_field_mapping defaults this to True, so "
            "omitting it silently activates.",
        ),
        (
            "commit=False",
            "the operation owns the transaction: mapping row, audit row and outbox "
            "event commit together or not at all.",
        ),
    ],
)
def test_the_governed_append_passes_its_safety_arguments_explicitly(argument, why):
    assert argument in _confirm_change_source(), why


def test_no_writer_of_mapping_versions_advances_the_pointer_by_default():
    """The class, not the instance: every in-operation caller must be explicit.

    Two governed callers exist — `datastream_change.confirm_change` and
    `datastream_activation.materialize_draft_mutation`. Both must pass
    `advance_pointer` rather than inherit the permissive default, or the next
    writer added beside them will inherit it too.
    """
    for module in ("datastream_change.py", "datastream_activation.py"):
        body = (_CORE / module).read_text(encoding="utf-8")
        if "save_field_mapping(" not in body:
            continue
        assert "advance_pointer=" in body, (
            f"{module} calls save_field_mapping without naming advance_pointer, so it "
            "inherits True and activates the version it appends"
        )
        assert "advance_pointer=True" not in body, (
            f"{module} activates on append. Only the publication step may move "
            "datastreams.current_mapping_version_id."
        )
