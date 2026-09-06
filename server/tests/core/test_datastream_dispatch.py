"""The gates a run passes, whoever asked for it -- 2026-08-12.

WHAT THIS FILE IS ABOUT. A run had two entrances that did not agree. The
dispatchers refused a Datastream that was stopped, still a draft, archived, hung
off an inactive connection, registered read-only, or whose module the project had
turned off -- and gave every window they queued an execution row, so the
collection was visible while it happened. `POST /api/datastreams/{id}/run` did
none of it.

Measured on the live base the day this was repaired:

  * 89 Datastreams; exactly ONE active, enabled and mapped -- cadence `manual`,
    so no clock would ever select it (the dispatchers take `nightly`, `weekly`,
    `hourly`);
  * both manual doors answered `422 dispatch_not_available` to every Datastream
    carrying a plan version, which is every published one;
  * `app.pull_jobs` rows carrying an `execution_id`: **0**.

Nothing in that build could collect anything. Every case below is offline: the
gates read a row and nothing else, deliberately, so they can be proven without a
database and asked before one is opened.
"""

from __future__ import annotations

import pytest
from core import datastream_dispatch as dispatch
from core.run_origins import SCHEDULER_NIGHTLY as _NIGHTLY

from tests.support.dispatch_rows import dispatch_row


def test_a_row_that_passes_every_gate_is_refused_by_none_of_them():
    assert dispatch.gate_refusal(dispatch_row()) is None


@pytest.mark.parametrize(
    ("override", "code", "must_name"),
    [
        ({"connection_ref_id": None}, "not_configured", "Link a connection"),
        ({"enabled": False, "lifecycle_state": "paused"}, "not_armed", "Schedule"),
        ({"lifecycle_state": "draft", "enabled": False}, "not_armed", "Publish"),
        ({"lifecycle_state": "archived"}, "not_armed", "Restore"),
        ({"cr_status": "revoked"}, "connection_inactive", "Reconnect"),
        ({"cr_enabled": False}, "connection_inactive", "Reconnect"),
        ({"source_kind": "external_bq"}, "read_only_source", "BigQuery"),
        ({"source_kind": "managed_feed"}, "pushed_source", "pushed to it"),
        ({"current_plan_version_id": None}, "not_published", "Publish"),
        ({"current_mapping_version_id": None}, "not_published", "Publish"),
        ({"module_enabled": False}, "module_disabled", "project settings"),
        ({"project_status": "suspended"}, "project_inactive", "project"),
    ],
)
def test_each_gate_refuses_by_name_and_names_the_gesture(override, code, must_name):
    """A refusal that only says no leaves the person on a dead screen."""
    refusal = dispatch.gate_refusal(dispatch_row(**override))

    assert refusal is not None, f"{override} must be refused"
    assert refusal.code == code
    assert must_name.lower() in refusal.message.lower(), refusal.message


@pytest.mark.parametrize("lifecycle", ["archived", "draft"])
def test_the_two_states_no_door_relaxes(lifecycle):
    """`require_armed=False` relaxes STOPPED, and nothing else.

    An archived Datastream is restored, not collected into; a draft has never
    published a day, so there is no day to re-collect either. Both were measured
    on the live base -- the draft carried a plan AND a mapping, so every other
    gate let it through.
    """
    row = dispatch_row(lifecycle_state=lifecycle, enabled=True)

    for require_armed in (True, False):
        refusal = dispatch.gate_refusal(row, require_armed=require_armed)
        assert refusal is not None and refusal.code == "not_armed"


def test_a_stopped_stream_is_the_one_thing_the_re_collection_door_relaxes():
    row = dispatch_row(enabled=False, lifecycle_state="paused")

    assert dispatch.gate_refusal(row).code == "not_armed"
    assert dispatch.gate_refusal(row, require_armed=False) is None


def test_a_pushed_source_is_never_told_to_link_a_connection():
    """The order of the gates, and why it is not alphabetical.

    Measured on the live base: the single ACTIVE Datastream is a `managed_feed`
    with no `connection_ref_id` -- by design, because files are pushed to it.
    Asked in the wrong order it read `not_configured`, "link a connection",
    which is not a gesture that Datastream has.
    """
    refusal = dispatch.gate_refusal(
        dispatch_row(source_kind="managed_feed", connection_ref_id=None)
    )

    assert refusal.code == "pushed_source"
    assert "connection" not in refusal.message.lower()


def test_a_column_nobody_projected_is_refused_rather_than_waved_through():
    """The trap this design exists to avoid.

    `row.get(key)` on an absent key answers None, and None reads as "no" for
    `enabled` and as "yes" for `module_enabled`. A caller whose SELECT forgot a
    column would therefore have half the gates silently pass -- a guard covering
    part of the fleet reads exactly like one covering all of it.
    """
    partial = dispatch_row()
    del partial["lifecycle_state"]

    refusal = dispatch.gate_refusal(partial)

    assert refusal is not None
    assert refusal.code == "row_incomplete"
    assert "lifecycle_state" in refusal.message


def test_the_dispatchers_project_every_column_the_gates_read():
    """The other half of the trap: the SELECTs and the gates, in step.

    Both dispatchers filter these columns in SQL; they PROJECT them so the gate
    reads a value rather than an absence -- and so the manual door, which cannot
    filter in SQL because it must explain itself, asks the identical question.
    """
    import inspect

    from core import scheduler

    for owner in (
        scheduler._dispatch_nightly_datastreams,
        scheduler._dispatch_hourly_datastreams,
    ):
        source = inspect.getsource(owner)
        for column in dispatch.DISPATCH_ROW_COLUMNS:
            assert column in source, f"{owner.__name__} does not project {column}"


def test_the_manual_door_and_the_clock_read_the_same_column_list():
    """`load_dispatch_row` answers every gate, or the door cannot explain itself."""
    for column in dispatch.DISPATCH_ROW_COLUMNS:
        assert f"AS {column}" in dispatch.DISPATCH_ROW_SELECT, column


def test_the_manual_row_select_is_LEFT_joined_so_a_gap_gets_a_SENTENCE():
    """An INNER JOIN would answer "introuvable" to a Datastream that exists.

    The dispatchers may inner-join: they are asking "who is due", and a row that
    fails a condition is simply not in the set. The manual door is asking about
    ONE Datastream a person is looking at, and "not found" is a lie when the real
    answer is "it has no published mapping yet".
    """
    assert "INNER JOIN" not in dispatch.DISPATCH_ROW_SELECT.upper()
    assert dispatch.DISPATCH_ROW_SELECT.count("LEFT JOIN") == 3


# ---------------------------------------------------------------------------
# The effect: one execution, one queue call per window, one close.
# ---------------------------------------------------------------------------


class _Queue:
    """A queue double that records what a dispatch asked it to do."""

    def __init__(self, fail_on: str | None = None):
        self.calls: list[dict] = []
        self.fail_on = fail_on

    def enqueue_pull(self, connection_ref_id, date_from, date_to, **kwargs):
        if date_from == self.fail_on:
            raise RuntimeError("queue refused this window")
        self.calls.append(
            {
                "connection_ref_id": connection_ref_id,
                "date_from": date_from,
                "date_to": date_to,
                **kwargs,
            }
        )
        return {"job_id": f"job_{len(self.calls)}", "state": "queued"}


@pytest.fixture()
def _instrumented(monkeypatch):
    """`_open_collection_run` / `_close_collection_run`, recorded rather than run."""
    opened: list[dict] = []
    closed: list[str | None] = []

    monkeypatch.setattr(
        "core.scheduler._open_collection_run",
        lambda _get_conn, **kw: (opened.append(kw), "dse_run")[1],
    )
    monkeypatch.setattr(
        "core.scheduler._close_collection_run",
        lambda _get_conn, execution_id, _actor: closed.append(execution_id),
    )
    return opened, closed


def test_every_window_is_bound_to_the_run_that_declared_it(_instrumented):
    opened, closed = _instrumented
    queue = _Queue()

    outcome = dispatch.dispatch_windows(
        lambda: None,
        row=dispatch_row(),
        windows=[
            {"date_from": "2026-08-01", "date_to": "2026-08-02"},
            {"date_from": "2026-08-03", "date_to": "2026-08-04"},
        ],
        queue=queue,
        actor="owner@example.com",
        origin=dispatch.ORIGIN_MANUAL,
        run_key="k",
    )

    assert outcome.execution_id == "dse_run"
    assert len(queue.calls) == 2
    assert {call["execution_id"] for call in queue.calls} == {"dse_run"}
    assert opened[0]["windows"] == list(outcome.windows), "the run declares its own days"
    assert closed == ["dse_run"], "a run left open answers 409 to every later publish"


def test_a_window_the_queue_refused_does_not_take_the_others_with_it(_instrumented):
    _opened, closed = _instrumented
    queue = _Queue(fail_on="2026-08-01")

    outcome = dispatch.dispatch_windows(
        lambda: None,
        row=dispatch_row(),
        windows=[
            {"date_from": "2026-08-01", "date_to": "2026-08-02"},
            {"date_from": "2026-08-03", "date_to": "2026-08-04"},
        ],
        queue=queue,
        actor="owner@example.com",
        origin=dispatch.ORIGIN_MANUAL,
        run_key="k",
    )

    assert len(outcome.jobs) == 1
    assert queue.calls[0]["date_from"] == "2026-08-03"
    assert closed == ["dse_run"], "the run still closes when a window fell over"


def test_a_manual_run_does_not_move_the_schedule_a_person_set(_instrumented):
    """`on_enqueued` is the nightly path's, and only its.

    An advance fired by a manual run would push `next_run_at` forward every time
    someone pressed the button, so the cadence a person chose would drift out of
    their hands one click at a time.
    """
    advanced: list[dict] = []
    queue = _Queue()

    dispatch.dispatch_windows(
        lambda: None,
        row=dispatch_row(),
        windows=[{"date_from": "2026-08-01", "date_to": "2026-08-02"}],
        queue=queue,
        actor="owner@example.com",
        origin=dispatch.ORIGIN_MANUAL,
        run_key="k",
    )
    assert advanced == []

    dispatch.dispatch_windows(
        lambda: None,
        row=dispatch_row(),
        windows=[{"date_from": "2026-08-01", "date_to": "2026-08-02"}],
        queue=queue,
        actor="scheduler",
        origin="scheduler_nightly",
        run_key="k",
        on_enqueued=advanced.append,
    )
    assert len(advanced) == 1


def test_a_manual_run_is_an_origin_the_registry_can_actually_stamp():
    """An origin with no engine mints nothing, and this one carries real windows."""
    from core import run_origins

    assert dispatch.ORIGIN_MANUAL in run_origins.MINTABLE_ORIGINS
    assert run_origins.reads_provider_windows(dispatch.ORIGIN_MANUAL)


# ---------------------------------------------------------------------------
# AI-301 -- a refusal is not a window, and a module is not re-derived blind.
#
# Measured on production 2026-08-17: from 2026-08-12 19:27 to that morning, EVERY
# pull of the platform was refused at enqueue with `access_denied`, and the
# nightly dispatcher advanced `next_run_at` on each one. `missed_run_count` stayed
# at 0, nothing above DEBUG was written, and the executions closed `collected`
# (declared success/Healthy) with `days=0/30` -- so the 30-day success rate ROSE
# while nothing landed. Five days.
# ---------------------------------------------------------------------------


class _RefusingQueue:
    """`enqueue_pull` as it really answers: a refusal is a VALUE, not a raise."""

    def __init__(self, refuse_on: str | None = None, code: str = "access_denied"):
        self.calls: list[dict] = []
        self.refuse_on = refuse_on
        self.code = code

    def enqueue_pull(self, connection_ref_id, date_from, date_to, **kwargs):
        self.calls.append({"date_from": date_from, **kwargs})
        if self.refuse_on in (date_from, "*"):
            return {
                "state": "refused",
                "code": self.code,
                "message": "connection/account scope is not authorized for this resource",
            }
        return {"job_id": f"job_{len(self.calls)}", "pull_id": "p", "state": "queued"}


def test_a_refused_window_does_not_advance_the_schedule(_instrumented):
    """The exact line that cost five days of collection.

    `on_enqueued` is what moves `next_run_at`. Calling it for a window the queue
    REFUSED records that a day was collected when none was -- and the next night
    starts after it, so the refused day is never retried by the clock.
    """
    queue = _RefusingQueue(refuse_on="*")
    advanced: list[dict] = []

    outcome = dispatch.dispatch_windows(
        lambda: None,
        row=dispatch_row(),
        windows=[{"date_from": "2026-08-14", "date_to": "2026-08-15"}],
        queue=queue,
        actor="scheduler",
        origin=_NIGHTLY,
        run_key="k",
        on_enqueued=advanced.append,
    )

    assert advanced == [], "a refusal must not move the clock"
    assert len(outcome.jobs) == 1, "the refusal is still REPORTED, not swallowed"
    assert outcome.jobs[0]["code"] == "access_denied"


def test_a_refused_window_is_said_at_warning_not_swallowed(_instrumented, caplog):
    """Four nights of silent refusal are indistinguishable from a healthy system."""
    import logging

    queue = _RefusingQueue(refuse_on="*")
    with caplog.at_level(logging.WARNING, logger="core.datastream_dispatch"):
        dispatch.dispatch_windows(
            lambda: None,
            row=dispatch_row(),
            windows=[{"date_from": "2026-08-14", "date_to": "2026-08-15"}],
            queue=queue,
            actor="scheduler",
            origin=_NIGHTLY,
            run_key="k",
            on_enqueued=lambda _job: None,
        )

    said = "\n".join(r.getMessage() for r in caplog.records)
    assert "enqueue_refused" in said
    assert "access_denied" in said
    assert "2026-08-14" in said, "which window was lost is part of the sentence"


def test_a_deduplicated_window_is_not_mistaken_for_a_refusal(_instrumented):
    """`running` is a REAL row someone else already made. It still counts."""
    class _Deduplicating:
        calls: list = []

        def enqueue_pull(self, connection_ref_id, date_from, date_to, **kwargs):
            return {"job_id": "job_1", "pull_id": "p", "state": "running"}

    advanced: list[dict] = []
    dispatch.dispatch_windows(
        lambda: None,
        row=dispatch_row(),
        windows=[{"date_from": "2026-08-14", "date_to": "2026-08-15"}],
        queue=_Deduplicating(),
        actor="scheduler",
        origin=_NIGHTLY,
        run_key="k",
        on_enqueued=advanced.append,
    )

    assert len(advanced) == 1, "a window already in flight was queued -- the clock moves"


def test_a_refusal_spelt_a_new_way_still_reads_as_a_refusal():
    """Positive test on real states, never a blacklist of codes."""
    assert dispatch._is_refusal({"state": "refused"})
    assert dispatch._is_refusal({"state": "window_before_backfill_ceiling"})
    assert dispatch._is_refusal({"state": "some_word_invented_next_year"})
    assert not dispatch._is_refusal({"state": "queued"})
    assert not dispatch._is_refusal({"state": "running"})


def test_the_module_travels_from_the_row_instead_of_being_re_read(_instrumented):
    """AI-301/AI-96: the authorization gate re-read `app.datastreams` on an
    RLS-armed connection with no organisation context. It saw 0 rows, fell back
    to the credential's `google` -- the name of no module -- and failed closed.
    The caller already selected the module; it must hand it over.
    """
    queue = _RefusingQueue()

    dispatch.dispatch_windows(
        lambda: None,
        row=dispatch_row(module_name="youtube-analytics"),
        windows=[{"date_from": "2026-08-14", "date_to": "2026-08-15"}],
        queue=queue,
        actor="scheduler",
        origin=_NIGHTLY,
        run_key="k",
    )

    assert queue.calls[0]["module_name"] == "youtube-analytics"
