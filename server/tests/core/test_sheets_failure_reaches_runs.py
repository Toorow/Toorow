"""A failed sync must be visible on the screen that exists to show failures.

`_record_fetch_failure` opened a ledger row, marked the LEDGER failed, and left
the `datastream_executions` row it had just created sitting in `created`
forever. The Runs tab reads that table, so it printed **"No error was recorded"**
on a run that had failed.

That is worse than showing nothing: a person checking whether their sync broke
was told it had not. Three review lenses converged on the write path; a fourth
found this sub-class none of them had named.

This is not the open question of WHERE a durable failure record should live for
the channels that create no execution at all. It is the guard for a write that
was already started and never finished.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def _conn() -> MagicMock:
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    cursor.fetchone.return_value = None
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    return conn


def _call(execution: dict | None):
    from core import google_sheets_sync

    opened = {
        "ledger": {"id": "mfil_EXAMPLE"},
        "execution": execution,
    }
    with (
        patch("core.managed_feed_ledger.open_import", return_value=opened),
        patch("core.managed_feed_ledger.mark_outcome") as mark_outcome,
        patch("core.datastream_publication.advance_state") as advance_state,
    ):
        result = google_sheets_sync._record_fetch_failure(
            datastream_id="ds_EXAMPLE",
            project_id="proj_EXAMPLE",
            plan_version_id="dplan_EXAMPLE",
            mapping_version_id="dmap_EXAMPLE",
            source_metadata={},
            idempotency_key="key_EXAMPLE",
            projection_plan={},
            actor="person_EXAMPLE",
            error_code="sheet_unreadable",
            error_detail="The spreadsheet could not be read.",
            conn=_conn(),
        )
    return result, mark_outcome, advance_state


def test_a_failed_fetch_marks_the_EXECUTION_failed_not_only_the_ledger() -> None:
    _, mark_outcome, advance_state = _call({"id": "dse_EXAMPLE"})

    assert mark_outcome.called, "the ledger must still record the failure"
    assert advance_state.called, (
        "the execution row was created and left in `created`: Runs reads that "
        "table and would print 'No error was recorded' on a failed run"
    )
    args = advance_state.call_args
    assert args.args[0] == "dse_EXAMPLE"
    assert args.args[2] == "failed"
    # The reason travels with it. A failed run with no error code is the same
    # silence one layer down.
    assert args.kwargs["error_code"] == "sheet_unreadable"


def test_a_no_op_import_creates_no_execution_and_advances_nothing() -> None:
    """`open_import` returns `execution: None` on a no-op — there is nothing to
    advance, and inventing a failed execution would report a run that never ran."""
    _, mark_outcome, advance_state = _call(None)

    assert mark_outcome.called
    assert not advance_state.called
