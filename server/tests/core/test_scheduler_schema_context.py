"""Scheduler hook tests for the schema-context nightly step (Story 11.2, fix pass).

Asserts:
  - the _run_schema_context_gen step is present in run_nightly_steps,
  - it is ordered AFTER rebuild_cache and BEFORE the alert checks,
  - it is wrapped in _run_isolated_step (failure isolation),
  - it is guarded by SCHEMA_CONTEXT_ENABLED (default off; no DB touched),
  - a raising generation does NOT abort the remaining nightly steps.
"""

from __future__ import annotations

import inspect
import os
from datetime import date
from unittest.mock import patch

os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import scheduler  # noqa: E402


def test_schema_context_step_present_and_ordered():
    src = inspect.getsource(scheduler.run_nightly_steps)
    assert "_run_schema_context_gen" in src, (
        "_run_schema_context_gen must appear in run_nightly_steps"
    )
    # AFTER rebuild_cache, BEFORE alert_check.
    rebuild_idx = src.index("rebuild_cache")
    schema_idx = src.index("_run_schema_context_gen")
    alert_idx = src.index("alert_check")
    assert rebuild_idx < schema_idx < alert_idx, (
        "schema_context step must run after rebuild_cache and before alert_check"
    )


def test_schema_context_step_is_isolated():
    """The step is dispatched via _run_isolated_step (failure isolation)."""
    src = inspect.getsource(scheduler.run_nightly_steps)
    # The call must be wrapped:
    # _run_isolated_step("schema_context_gen", _run_schema_context_gen, ...)
    assert '_run_isolated_step("schema_context_gen", _run_schema_context_gen' in src


def test_schema_context_gen_skipped_by_default(caplog):
    """SCHEMA_CONTEXT_ENABLED defaults to false -> the step is a no-op (no DB)."""
    with patch.dict(os.environ, {"SCHEMA_CONTEXT_ENABLED": "false"}):
        with patch("core.db.get_connection") as mock_conn:
            with caplog.at_level("DEBUG", logger="core.scheduler"):
                scheduler._run_schema_context_gen()
    mock_conn.assert_not_called()
    assert any("schema_context_gen_skipped" in r.getMessage() for r in caplog.records)


def test_schema_context_gen_failure_is_isolated():
    """A raising schema-context step does not abort subsequent nightly steps."""
    ran: list[str] = []

    with (
        patch("core.scheduler._try_advisory_lock", return_value=None),
        patch("core.scheduler.dispatch_nightly", side_effect=lambda **kw: ran.append("dispatch")),
        patch("core.scheduler._run_rebuild_cache", side_effect=lambda: ran.append("rebuild")),
        patch(
            "core.scheduler._run_schema_context_gen",
            side_effect=RuntimeError("schema boom"),
        ),
        patch("core.scheduler._run_alert_check", side_effect=lambda: ran.append("alerts")),
        patch(
            "core.scheduler._run_business_alert_check",
            side_effect=lambda: ran.append("business"),
        ),
        patch("core.scheduler._run_anomaly_alert_check", side_effect=lambda: ran.append("anomaly")),
        patch("core.scheduler._run_dq_monitors_check", side_effect=lambda: ran.append("dq")),
        patch("core.scheduler._run_due_notebooks", side_effect=lambda: ran.append("notebooks")),
        patch("core.scheduler._run_due_briefings", side_effect=lambda rid: ran.append("briefings")),
        patch("core.scheduler._insert_meta_alert") as meta,
        patch("core.scheduler._write_scheduler_step_degraded_alert"),
    ):
        scheduler.run_nightly_steps(date(2026, 7, 20))

    # Every step after the raising schema_context step still ran.
    assert ran == [
        "dispatch", "rebuild", "alerts", "business", "anomaly", "dq", "notebooks", "briefings"
    ]
    # A meta-alert was recorded for the failing schema_context_gen step.
    assert any(call.args[0] == "schema_context_gen" for call in meta.call_args_list)
