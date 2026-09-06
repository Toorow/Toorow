"""The nightly step ledger, without a database. `Incomplete if` 2, locus 3.

The pg-gated sibling (`tests/integration/test_nightly_step_ledger_pg.py`) proves
the rows. This file guards the three things that are wrong long before a row is
written, and that a green pg run would not notice:

  1. THE DECLARED SEQUENCE IS THE ONE THAT IS CALLED. `NIGHTLY_STEPS` is what the
     dispatch writes; the call sites are what actually runs. A name in one and
     not the other is the exact defect the ledger exists to catch, so it must not
     be possible to introduce it BY EDITING THE LEDGER. The call sites are read
     out of `run_nightly_steps`'s own source -- the independent artefact -- and
     not out of a second copy of the list.
  2. THE ORDER OF THE TWO LEDGER WRITES. `started_at` is stamped BEFORE the step
     runs and the row is closed AFTER; the whole guarantee ("a crash leaves the
     open row") is that order and nothing else. Asserted against a recording
     stub, so it holds without a Postgres.
  3. NO PAYLOAD REACHES `error_class`. The CHECK is the last line of defence;
     `_error_class` is the first, and a violated write is a write that does not
     happen -- on a table whose whole purpose is to be written.
"""

from __future__ import annotations

import inspect
import re
from datetime import datetime, timedelta, timezone

import pytest
from core import platform_clocks, scheduler

_CALL_SITE = re.compile(r'_run_isolated_step\(\s*\n?\s*"([a-z0-9_]+)"')


# ---------------------------------------------------------------------------
# 1. The list and the call sites
# ---------------------------------------------------------------------------


def test_the_declared_sequence_is_the_one_that_is_called():
    called = tuple(_CALL_SITE.findall(inspect.getsource(scheduler.run_nightly_steps)))
    assert called == scheduler.NIGHTLY_STEPS, (
        "NIGHTLY_STEPS is written to the ledger at dispatch; the call sites are "
        "what runs. A name declared with no call site leaves an open row every "
        "night; a call site missing from the declaration runs unrecorded."
    )


def test_every_call_site_passes_the_ledger():
    source = inspect.getsource(scheduler.run_nightly_steps)
    assert source.count("_ledger=_ledger") == len(scheduler.NIGHTLY_STEPS)


def test_the_declaration_is_not_empty_and_has_no_duplicate():
    """A vacuous list would make every assertion above pass by blindness."""
    assert len(scheduler.NIGHTLY_STEPS) >= 5
    assert len(set(scheduler.NIGHTLY_STEPS)) == len(scheduler.NIGHTLY_STEPS)


def test_the_hourly_steps_do_not_write_the_nightly_ledger():
    """`_run_isolated_step` is shared. Only the nightly declares a sequence, so
    an hourly step writing into it would produce rows no dispatch ever opened."""
    source = inspect.getsource(scheduler.run_hourly_steps)
    assert "_ledger" not in source


# ---------------------------------------------------------------------------
# 2. The order of the two writes
# ---------------------------------------------------------------------------


class _RecordingLedger:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def started(self, step_name):
        self.events.append(("started", step_name))
        return True

    def closed(self, step_name, error):
        self.events.append(
            ("closed", step_name, None if error is None else type(error).__name__)
        )
        return True


def test_started_is_written_before_the_step_runs():
    ledger = _RecordingLedger()

    def step():
        ledger.events.append(("ran", "alert_check"))

    scheduler._run_isolated_step("alert_check", step, _ledger=ledger)
    assert ledger.events == [
        ("started", "alert_check"),
        ("ran", "alert_check"),
        ("closed", "alert_check", None),
    ]


def test_a_raising_step_is_closed_failed_with_its_class(monkeypatch):
    monkeypatch.setattr(scheduler, "_insert_meta_alert", lambda *a, **k: None)
    ledger = _RecordingLedger()

    def step():
        raise TimeoutError("upstream")

    scheduler._run_isolated_step("dq_monitors", step, _ledger=ledger)
    assert ledger.events == [
        ("started", "dq_monitors"),
        ("closed", "dq_monitors", "TimeoutError"),
    ]


def test_a_base_exception_leaves_the_row_open():
    """No `finally` closes the row. The open row IS the record of the crash."""
    ledger = _RecordingLedger()

    def step():
        raise SystemExit(1)

    with pytest.raises(SystemExit):
        scheduler._run_isolated_step("rebuild_cache", step, _ledger=ledger)
    assert ledger.events == [("started", "rebuild_cache")]


def test_a_step_run_without_a_ledger_still_runs():
    """The hourly loop and every existing caller pass no ledger."""
    seen = []
    scheduler._run_isolated_step("alert_check", lambda: seen.append(1))
    assert seen == [1]


# ---------------------------------------------------------------------------
# 3. No payload reaches the table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        ValueError("token AKIA1234 refused for owner@example.com"),
        RuntimeError("multi\nline\nmessage"),
        KeyError("'a quoted key'"),
    ],
)
def test_error_class_is_the_class_and_never_the_message(error):
    rendered = scheduler._error_class(error)
    assert rendered == type(error).__name__
    assert scheduler._ERROR_CLASS_RE.match(rendered)


def test_an_unrenderable_class_name_falls_back_rather_than_violating_the_check():
    """A dynamically built class can carry a name the CHECK refuses. The write
    must still land: a refused write is a step with no record, which is the
    silence this table exists to end."""
    weird = type("not a name!", (Exception,), {})
    assert scheduler._error_class(weird()) == "Exception"
    assert scheduler._ERROR_CLASS_RE.match(scheduler._error_class(weird()))


# ---------------------------------------------------------------------------
# 4. The reader's derivations
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 8, 31, 2, 0, tzinfo=timezone.utc)


def _row(**overrides):
    row = {
        "started_at": None,
        "ended_at": None,
        "outcome": None,
        "error_class": None,
    }
    row.update(overrides)
    return row


def test_never_started_and_unfinished_are_not_the_same_state():
    assert platform_clocks._step_state(_row()) == platform_clocks.NEVER_STARTED
    assert (
        platform_clocks._step_state(_row(started_at=_T0)) == platform_clocks.UNFINISHED
    )


def test_a_closed_row_reads_as_its_outcome():
    closed = _row(started_at=_T0, ended_at=_T0 + timedelta(seconds=3), outcome="failed")
    assert platform_clocks._step_state(closed) == "failed"


def test_duration_is_derived_and_absent_while_the_row_is_open():
    assert platform_clocks._duration_ms(_row(started_at=_T0)) is None
    assert platform_clocks._duration_ms(_row()) is None
    closed = _row(started_at=_T0, ended_at=_T0 + timedelta(milliseconds=1500))
    assert platform_clocks._duration_ms(closed) == 1500


def test_the_state_vocabulary_covers_every_state_the_reader_can_emit():
    assert set(platform_clocks.STEP_STATES) == {
        "succeeded",
        "failed",
        platform_clocks.UNFINISHED,
        platform_clocks.NEVER_STARTED,
        platform_clocks.UNRECORDED,
    }


def test_the_read_model_bounds_the_number_of_nights(monkeypatch):
    from core import platform_clocks_read_model as read_model

    asked: list[int] = []

    def _fake(_conn, *, nights):
        asked.append(nights)
        return {"runs": [], "declared_steps": [], "has_run": False}

    monkeypatch.setattr(platform_clocks, "list_nightly_step_runs", _fake)
    read_model.collect_nightly_step_runs(object(), nights=999)
    read_model.collect_nightly_step_runs(object(), nights=0)
    read_model.collect_nightly_step_runs(object())
    assert asked == [read_model.MAX_NIGHTS, 1, read_model.DEFAULT_NIGHTS]


def test_the_read_model_serialises_dates_and_timestamps(monkeypatch):
    """A `date` in a JSONResponse is a 500 on the one surface that must be
    readable when something is already wrong."""
    from core import platform_clocks_read_model as read_model

    monkeypatch.setattr(
        platform_clocks,
        "list_nightly_step_runs",
        lambda _conn, *, nights: {
            "runs": [{"as_of_date": _T0.date(), "steps": [{"started_at": _T0}]}],
            "declared_steps": ["alert_check"],
            "has_run": True,
        },
    )
    payload = read_model.collect_nightly_step_runs(object())
    assert payload["runs"][0]["as_of_date"] == "2026-08-31"
    assert payload["runs"][0]["steps"][0]["started_at"] == _T0.isoformat()
