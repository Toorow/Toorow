"""Unit tests for the dq_monitors nightly scheduler step (Story 8.6).

Extends the scheduler test pattern from test_scheduler.py WITHOUT editing it.
Verifies:
  - _run_dq_monitors_check is registered in run_nightly_steps (step order).
  - DQ_MONITORS_ENABLED=false causes skip.
  - DQ_MONITORS_ENABLED=true (default) triggers run_dq_monitors call.
  - Failure in _run_dq_monitors_check is isolated (other steps still run).

Strategy:
  - All DB/infra calls mocked.
  - SCHEDULER_ENABLED=false (daemon thread never starts).
"""

from __future__ import annotations

import os
from unittest.mock import patch

os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")


# ---------------------------------------------------------------------------
# _run_dq_monitors_check
# ---------------------------------------------------------------------------


def test_dq_monitors_check_skipped_when_disabled():
    """_run_dq_monitors_check logs skip when DQ_MONITORS_ENABLED=false."""
    from core.scheduler import _run_dq_monitors_check

    with patch.dict(os.environ, {"DQ_MONITORS_ENABLED": "false"}):
        # Should return without calling dq_monitors.run_dq_monitors.
        with patch("core.dq_monitors.run_dq_monitors") as mock_run:
            _run_dq_monitors_check()
        mock_run.assert_not_called()


def test_dq_monitors_check_called_when_enabled():
    """_run_dq_monitors_check calls run_dq_monitors when DQ_MONITORS_ENABLED=true."""
    from core.scheduler import _run_dq_monitors_check

    mock_summary = {
        "evaluated": 2, "total_issues": 1, "errors": 0,
        "volume_issues": 1, "timeliness_issues": 0,
        "duplication_issues": 0, "schema_issues": 0,
    }

    with patch.dict(os.environ, {"DQ_MONITORS_ENABLED": "true"}):
        with patch("core.dq_monitors.run_dq_monitors", return_value=mock_summary) as mock_run:
            _run_dq_monitors_check()
        mock_run.assert_called_once()


def test_dq_monitors_check_handles_exception():
    """_run_dq_monitors_check catches exceptions and does not propagate."""
    from core.scheduler import _run_dq_monitors_check

    with patch.dict(os.environ, {"DQ_MONITORS_ENABLED": "true"}):
        with patch("core.dq_monitors.run_dq_monitors", side_effect=RuntimeError("boom")):
            # Must not raise.
            _run_dq_monitors_check()


# ---------------------------------------------------------------------------
# Step registration in run_nightly_steps
# ---------------------------------------------------------------------------


def test_dq_monitors_step_registered_in_run_nightly_steps():
    """run_nightly_steps includes the 'dq_monitors' step."""

    steps_seen: list[str] = []

    def fake_isolated_step(step_name, fn, *args, _degraded=None, **kwargs):
        steps_seen.append(step_name)
        # Simulate the fn call (don't let it actually run).
        return None

    import datetime

    with (
        patch("core.scheduler._run_isolated_step", side_effect=fake_isolated_step),
        patch("core.scheduler._try_advisory_lock", return_value=True),
        patch("core.scheduler._release_advisory_lock"),
    ):
        from core.scheduler import run_nightly_steps

        run_nightly_steps(datetime.date(2026, 7, 13))

    assert "dq_monitors" in steps_seen


def test_dq_monitors_step_comes_after_anomaly_check():
    """dq_monitors step is registered after anomaly_alert_check."""
    steps_seen: list[str] = []

    def fake_isolated_step(step_name, fn, *args, _degraded=None, **kwargs):
        steps_seen.append(step_name)
        return None

    import datetime

    with (
        patch("core.scheduler._run_isolated_step", side_effect=fake_isolated_step),
        patch("core.scheduler._try_advisory_lock", return_value=True),
        patch("core.scheduler._release_advisory_lock"),
    ):
        from core.scheduler import run_nightly_steps

        run_nightly_steps(datetime.date(2026, 7, 13))

    anomaly_idx = steps_seen.index("anomaly_alert_check")
    dq_idx = steps_seen.index("dq_monitors")
    notebooks_idx = steps_seen.index("run_due_notebooks")
    assert anomaly_idx < dq_idx < notebooks_idx, (
        f"Expected anomaly_alert_check < dq_monitors < run_due_notebooks; "
        f"got {anomaly_idx} < {dq_idx} < {notebooks_idx}"
    )


def test_dq_monitors_failure_does_not_block_notebooks():
    """Failure in dq_monitors step does not prevent run_due_notebooks from running."""

    steps_called: list[str] = []

    def fake_isolated_step(step_name, fn, *args, _degraded=None, **kwargs):
        steps_called.append(step_name)
        if step_name == "dq_monitors":
            if _degraded is not None:
                _degraded.append(step_name)
        return None

    import datetime

    with (
        patch("core.scheduler._run_isolated_step", side_effect=fake_isolated_step),
        patch("core.scheduler._try_advisory_lock", return_value=True),
        patch("core.scheduler._release_advisory_lock"),
        patch("core.scheduler._write_scheduler_step_degraded_alert"),
    ):
        from core.scheduler import run_nightly_steps

        run_nightly_steps(datetime.date(2026, 7, 13))

    assert "run_due_notebooks" in steps_called
    assert "run_due_briefings" in steps_called
