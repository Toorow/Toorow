"""Tests for AI-32 nightly alert-check failure isolation (scheduler.py).

Covers:
  * per-step duration logged at INFO (AI-32 a)
  * soft-timeout WARNING logged when a step exceeds ALERT_TIMEOUT_SECONDS (AI-32 b)
  * degraded step tracked in _degraded list on failure and soft-timeout (AI-32 c)
  * _write_scheduler_step_degraded_alert called with alert_type='scheduler_step_degraded'
  * consolidated meta-alert written after run_nightly_steps when a step fails (AI-32 c)
  * no meta-alert written when all steps succeed

All tests are mock-based (no live DB, no network).  SCHEDULER_ENABLED is not
set so the daemon thread never starts (HG-5).
"""

from __future__ import annotations

import logging
import os
from datetime import date
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# AI-32 (a) -- duration logged for every step
# ---------------------------------------------------------------------------


def test_run_isolated_step_logs_duration_on_success(caplog):
    """A successful step emits 'scheduler: step=<name> duration_ms=<int>' at INFO."""
    from core.scheduler import _run_isolated_step

    with caplog.at_level(logging.INFO, logger="core.scheduler"):
        _run_isolated_step("my_step", lambda: None)

    duration_records = [
        r for r in caplog.records
        if "step=my_step" in r.getMessage() and "duration_ms=" in r.getMessage()
    ]
    assert duration_records, "Expected duration log for successful step"
    assert duration_records[0].levelno == logging.INFO


def test_run_isolated_step_logs_duration_on_failure(caplog):
    """A failing step still emits the duration log at INFO."""
    from core.scheduler import _run_isolated_step

    def _boom():
        raise RuntimeError("boom")

    with caplog.at_level(logging.INFO, logger="core.scheduler"):
        _run_isolated_step("bad_step", _boom)

    duration_records = [
        r for r in caplog.records
        if "step=bad_step" in r.getMessage() and "duration_ms=" in r.getMessage()
    ]
    assert duration_records, "Expected duration log even when step raises"
    assert duration_records[0].levelno == logging.INFO


# ---------------------------------------------------------------------------
# AI-32 (b) -- soft-timeout WARNING
# ---------------------------------------------------------------------------


def test_run_isolated_step_warns_when_soft_timeout_exceeded(caplog, monkeypatch):
    """Soft-timeout warning fires when elapsed > ALERT_TIMEOUT_SECONDS."""
    # Negative limit: a 0ms step still strictly exceeds it (0 > 0 would not).
    monkeypatch.setenv("ALERT_TIMEOUT_SECONDS", "-1")

    from core.scheduler import _run_isolated_step

    with caplog.at_level(logging.WARNING, logger="core.scheduler"):
        _run_isolated_step("slow_step", lambda: None)

    timeout_records = [
        r for r in caplog.records
        if "step=slow_step" in r.getMessage() and "ALERT_TIMEOUT_SECONDS" in r.getMessage()
    ]
    assert timeout_records, "Expected soft-timeout WARNING when limit is 0"
    assert timeout_records[0].levelno == logging.WARNING


def test_run_isolated_step_no_timeout_warning_when_fast(caplog, monkeypatch):
    """No timeout warning when step finishes well within the limit."""
    monkeypatch.setenv("ALERT_TIMEOUT_SECONDS", "9999")

    from core.scheduler import _run_isolated_step

    with caplog.at_level(logging.WARNING, logger="core.scheduler"):
        _run_isolated_step("fast_step", lambda: None)

    timeout_records = [
        r for r in caplog.records
        if "ALERT_TIMEOUT_SECONDS" in r.getMessage() and "fast_step" in r.getMessage()
    ]
    assert not timeout_records, "No timeout warning expected for fast step"


# ---------------------------------------------------------------------------
# AI-32 (c) -- _degraded list tracking
# ---------------------------------------------------------------------------


def test_degraded_step_tracked_on_failure():
    """A failing step is appended to _degraded."""
    from core.scheduler import _run_isolated_step

    degraded: list[str] = []

    def _fail():
        raise ValueError("oops")

    with patch("core.scheduler._insert_meta_alert"):  # avoid DB call
        _run_isolated_step("fail_step", _fail, _degraded=degraded)

    assert "fail_step" in degraded


def test_degraded_step_tracked_on_exception():
    """A step that raises RuntimeError is appended to _degraded."""
    from core.scheduler import _run_isolated_step

    degraded: list[str] = []

    def _fail():
        raise RuntimeError("fail")

    with patch("core.scheduler._insert_meta_alert"):  # avoid DB call
        _run_isolated_step("err_step", _fail, _degraded=degraded)

    assert "err_step" in degraded


def test_degraded_step_tracked_on_soft_timeout(monkeypatch):
    """A step that exceeds ALERT_TIMEOUT_SECONDS is appended to _degraded."""
    # Negative limit: a 0ms step still strictly exceeds it (0 > 0 would not).
    monkeypatch.setenv("ALERT_TIMEOUT_SECONDS", "-1")

    from core.scheduler import _run_isolated_step

    degraded: list[str] = []
    _run_isolated_step("tardy_step", lambda: None, _degraded=degraded)
    assert "tardy_step" in degraded, (
        f"Expected 'tardy_step' in degraded list when the limit is negative, got {degraded}"
    )


def test_degraded_accumulates_multiple():
    """Multiple failing steps all appear in _degraded."""
    from core.scheduler import _run_isolated_step

    degraded: list[str] = []

    def _fail_a():
        raise ValueError("a")

    def _fail_b():
        raise ValueError("b")

    with patch("core.scheduler._insert_meta_alert"):
        _run_isolated_step("step_a", _fail_a, _degraded=degraded)
        _run_isolated_step("step_b", _fail_b, _degraded=degraded)

    assert "step_a" in degraded
    assert "step_b" in degraded


def test_no_degraded_entry_for_success(monkeypatch):
    """A successful, fast step does not appear in _degraded."""
    monkeypatch.setenv("ALERT_TIMEOUT_SECONDS", "9999")

    from core.scheduler import _run_isolated_step

    degraded: list[str] = []
    _run_isolated_step("ok_step", lambda: "done", _degraded=degraded)
    assert degraded == [], f"Expected empty degraded list for success, got {degraded}"


# ---------------------------------------------------------------------------
# AI-32 (c) -- _write_scheduler_step_degraded_alert calls write_infra_firing
# ---------------------------------------------------------------------------


def test_write_scheduler_step_degraded_alert_calls_infra_firing():
    """_write_scheduler_step_degraded_alert calls write_infra_firing with correct alert_type."""
    mock_write = MagicMock()

    with patch("core.infra_alerts.write_infra_firing", mock_write):
        from core.scheduler import _write_scheduler_step_degraded_alert
        _write_scheduler_step_degraded_alert(["dispatch_nightly", "alert_check"])

    mock_write.assert_called_once()
    call = mock_write.call_args
    alert_type = call.kwargs.get("alert_type") or (call.args[0] if call.args else None)
    assert alert_type == "scheduler_step_degraded", (
        f"Expected alert_type='scheduler_step_degraded', got {alert_type!r}"
    )


def test_write_scheduler_step_degraded_never_raises():
    """_write_scheduler_step_degraded_alert swallows all exceptions (best-effort)."""
    with patch("core.infra_alerts.write_infra_firing", side_effect=Exception("infra down")):
        from core.scheduler import _write_scheduler_step_degraded_alert
        # Must not raise.
        _write_scheduler_step_degraded_alert(["some_step"])


# ---------------------------------------------------------------------------
# AI-32 (c) -- run_nightly_steps integration: meta-alert after failure
# ---------------------------------------------------------------------------


def _make_ulid_mock():
    return MagicMock(ULID=MagicMock(return_value="TESTULIDVAL"))


def test_run_nightly_steps_emits_meta_alert_when_step_fails():
    """run_nightly_steps calls _write_scheduler_step_degraded_alert when dispatch fails."""
    import core.scheduler as sched

    captured: list[list[str]] = []
    real_write = sched._write_scheduler_step_degraded_alert
    sched._write_scheduler_step_degraded_alert = lambda steps: captured.append(list(steps))
    try:
        with (
            patch("core.scheduler.dispatch_nightly", side_effect=RuntimeError("dispatch boom")),
            patch("core.scheduler._run_alert_check"),
            patch("core.scheduler._run_business_alert_check"),
            patch("core.scheduler._run_anomaly_alert_check"),
            patch("core.scheduler._run_due_notebooks"),
            patch("core.scheduler._run_due_briefings"),
            patch("core.scheduler._insert_meta_alert"),
            patch.dict("sys.modules", {"ulid": _make_ulid_mock()}),
        ):
            sched.run_nightly_steps(date.today())
    finally:
        sched._write_scheduler_step_degraded_alert = real_write

    assert captured, "Expected _write_scheduler_step_degraded_alert to be called"
    assert "dispatch_nightly" in captured[0], (
        f"Expected 'dispatch_nightly' in degraded steps, got {captured[0]}"
    )


# ---------------------------------------------------------------------------
# G-scheduler -- advisory lock (double-fire protection)
# ---------------------------------------------------------------------------


def test_run_nightly_steps_skips_when_advisory_lock_not_acquired(caplog):
    """run_nightly_steps returns early with INFO log when pg_try_advisory_lock is False."""
    import logging

    with (
        patch("core.scheduler._try_advisory_lock", return_value=False),
        patch("core.scheduler.dispatch_nightly") as mock_dispatch,
    ):
        from core.scheduler import run_nightly_steps
        with caplog.at_level(logging.INFO, logger="core.scheduler"):
            run_nightly_steps(date.today())

    mock_dispatch.assert_not_called()
    skip_records = [r for r in caplog.records if "nightly_skipped" in r.getMessage()]
    assert skip_records, "Expected 'nightly_skipped' log when advisory lock is not acquired"


def test_run_nightly_steps_runs_when_lock_acquired():
    """run_nightly_steps executes steps when pg_try_advisory_lock is True."""
    step_ran = []

    with (
        patch("core.scheduler._try_advisory_lock", return_value=True),
        patch("core.scheduler._release_advisory_lock"),
        patch("core.scheduler.dispatch_nightly", side_effect=lambda **kw: step_ran.append("ok")),
        patch("core.scheduler._run_alert_check"),
        patch("core.scheduler._run_business_alert_check"),
        patch("core.scheduler._run_anomaly_alert_check"),
        patch("core.scheduler._run_due_notebooks"),
        patch("core.scheduler._run_due_briefings"),
        patch.dict("sys.modules", {"ulid": _make_ulid_mock()}),
        patch.dict(os.environ, {"ALERT_TIMEOUT_SECONDS": "9999"}),
    ):
        from core.scheduler import run_nightly_steps
        run_nightly_steps(date.today())

    assert step_ran, "Expected dispatch_nightly to run when lock is acquired"


def test_run_nightly_steps_releases_lock_in_finally_after_failure():
    """Advisory lock is released (finally) even when a step raises."""
    release_calls = []

    with (
        patch("core.scheduler._try_advisory_lock", return_value=True),
        patch("core.scheduler._release_advisory_lock", side_effect=lambda: release_calls.append(1)),
        patch("core.scheduler.dispatch_nightly", side_effect=RuntimeError("boom")),
        patch("core.scheduler._run_alert_check"),
        patch("core.scheduler._run_business_alert_check"),
        patch("core.scheduler._run_anomaly_alert_check"),
        patch("core.scheduler._run_due_notebooks"),
        patch("core.scheduler._run_due_briefings"),
        patch("core.scheduler._insert_meta_alert"),
        patch("core.scheduler._write_scheduler_step_degraded_alert"),
        patch.dict("sys.modules", {"ulid": _make_ulid_mock()}),
    ):
        from core.scheduler import run_nightly_steps
        run_nightly_steps(date.today())

    assert release_calls, "Expected _release_advisory_lock to be called in finally"


def test_run_nightly_steps_proceeds_without_lock_when_postgres_down():
    """When _try_advisory_lock returns None (Postgres down), steps still execute."""
    step_ran = []

    with (
        patch("core.scheduler._try_advisory_lock", return_value=None),
        patch("core.scheduler._release_advisory_lock"),
        patch("core.scheduler.dispatch_nightly", side_effect=lambda **kw: step_ran.append("ok")),
        patch("core.scheduler._run_alert_check"),
        patch("core.scheduler._run_business_alert_check"),
        patch("core.scheduler._run_anomaly_alert_check"),
        patch("core.scheduler._run_due_notebooks"),
        patch("core.scheduler._run_due_briefings"),
        patch.dict("sys.modules", {"ulid": _make_ulid_mock()}),
        patch.dict(os.environ, {"ALERT_TIMEOUT_SECONDS": "9999"}),
    ):
        from core.scheduler import run_nightly_steps
        run_nightly_steps(date.today())

    assert step_ran, "Steps must run even when advisory lock is unavailable (Postgres down)"


def test_run_nightly_steps_no_meta_alert_when_all_succeed():
    """run_nightly_steps does NOT call _write_scheduler_step_degraded_alert when all succeed."""
    import core.scheduler as sched

    called: list = []
    real_write = sched._write_scheduler_step_degraded_alert
    sched._write_scheduler_step_degraded_alert = lambda steps: called.append(steps)
    try:
        with (
            patch("core.scheduler.dispatch_nightly"),
            patch("core.scheduler._run_alert_check"),
            patch("core.scheduler._run_business_alert_check"),
            patch("core.scheduler._run_anomaly_alert_check"),
            patch("core.scheduler._run_due_notebooks"),
            patch("core.scheduler._run_due_briefings"),
            patch.dict("sys.modules", {"ulid": _make_ulid_mock()}),
            # Make sure timeout won't trip (steps are instant).
        ):
            import os
            old_timeout = os.environ.get("ALERT_TIMEOUT_SECONDS")
            os.environ["ALERT_TIMEOUT_SECONDS"] = "9999"
            try:
                sched.run_nightly_steps(date.today())
            finally:
                if old_timeout is None:
                    os.environ.pop("ALERT_TIMEOUT_SECONDS", None)
                else:
                    os.environ["ALERT_TIMEOUT_SECONDS"] = old_timeout
    finally:
        sched._write_scheduler_step_degraded_alert = real_write

    assert not called, "Expected no degraded alert when all steps succeed"
