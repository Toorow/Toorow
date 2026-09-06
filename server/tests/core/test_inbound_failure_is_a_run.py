"""An inbound failure must be visible in the run universe, which is not the ledger.

`spec-43-17b:26` — **Never** "use the managed-feed ledger as the run universe".
`app.datastream_executions` is that universe and the Runs tab reads it. But a
delivery that aborted BEFORE `open_import` wrote only to
`app.inbound_raw_imports`, so a person who came to check whether their file had
failed was shown an empty run list.

Three review lenses traced this path independently and read it three different
ways. The spec had already settled it: this is a reconciliation, not an
arbitration.

The limit is asserted too. An execution carries the plan and mapping version
that produced it; a Datastream that has never had an executable pair has no run
to record, and inventing one would report a run that never existed.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def _conn(datastream_row):
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    cursor.fetchone.return_value = datastream_row
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    return conn


def test_a_pre_ledger_failure_becomes_a_FAILED_run() -> None:
    from core.datastream_publication import record_failed_execution

    with (
        patch(
            "core.datastream_publication.create_execution",
            return_value={"id": "dse_EXAMPLE"},
        ) as create,
        patch("core.datastream_publication.advance_state") as advance,
    ):
        execution_id = record_failed_execution(
            _conn(("dplan_EXAMPLE", "dmap_EXAMPLE")),
            project_id="proj_EXAMPLE",
            datastream_id="ds_EXAMPLE",
            actor="person_EXAMPLE",
            error_code="attachment_processing_error",
            error_detail="Inbound delivery failed before any import opened.",
            idempotency_key="inbound-failure:rcpt_EXAMPLE:1",
        )

    assert execution_id == "dse_EXAMPLE"
    assert create.called
    # It carries the versions that were current, because a run that names no
    # version cannot be compared with anything.
    assert create.call_args.args[2] == "dplan_EXAMPLE"
    assert create.call_args.args[3] == "dmap_EXAMPLE"
    # And it is FAILED, with the reason. A run in `created` reads as a success
    # on the Runs tab -- that exact defect was repaired in `e2cd2106`.
    assert advance.call_args.args[2] == "failed"
    assert advance.call_args.kwargs["error_code"] == "attachment_processing_error"


def test_it_records_NOTHING_when_there_is_no_executable_pair_to_bind_to() -> None:
    """The honest limit: no plan version means no run ever happened.

    The raw-import row remains the trace, and it is the truthful one."""
    from core.datastream_publication import record_failed_execution

    with (
        patch("core.datastream_publication.create_execution") as create,
        patch("core.datastream_publication.advance_state") as advance,
    ):
        assert record_failed_execution(
            _conn((None, None)),
            project_id="proj_EXAMPLE",
            datastream_id="ds_EXAMPLE",
            actor="person_EXAMPLE",
            error_code="x",
            error_detail="y",
            idempotency_key="k",
        ) is None

    assert not create.called
    assert not advance.called


def test_an_unknown_datastream_records_nothing_rather_than_guessing() -> None:
    from core.datastream_publication import record_failed_execution

    with patch("core.datastream_publication.create_execution") as create:
        assert record_failed_execution(
            _conn(None),
            project_id="proj_EXAMPLE",
            datastream_id="ds_MISSING",
            actor="person_EXAMPLE",
            error_code="x",
            error_detail="y",
            idempotency_key="k",
        ) is None
    assert not create.called
