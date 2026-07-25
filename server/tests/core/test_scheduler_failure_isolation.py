"""AI-32 (Story 6.1, AC10) -- nightly piggyback failure-isolation tests.

Asserts that when one nightly step raises:
  (a) a per-step duration log is emitted for every step,
  (b) subsequent steps still run (failure isolation),
  (c) a meta-alert row is inserted for the failing step.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from datetime import date  # noqa: E402

from core import scheduler  # noqa: E402


def test_one_step_failure_does_not_block_others(caplog):
    """A raising step is isolated; later steps still run + meta-alert inserted.

    The nightly pipeline currently has eleven isolated steps, ending with briefings.
    Step 4b (_run_mediaplan_alert_check) was added by Story 22.6 (FR9); the
    per-org dbt step (_run_dbt_per_org) was added by Story 24.4 after dispatch.
    """
    ran: list[str] = []

    def _dispatch(as_of_date=None):
        ran.append("dispatch")
        raise RuntimeError("boom in dispatch")

    def _rebuild():
        ran.append("rebuild")

    def _schema_context():
        ran.append("schema_context")

    def _alerts():
        ran.append("alerts")

    def _business():
        ran.append("business")

    def _anomaly():
        ran.append("anomaly")

    def _mediaplan():
        ran.append("mediaplan")

    def _dq():
        ran.append("dq")

    def _notebooks():
        ran.append("notebooks")

    def _briefings(nightly_run_id):
        ran.append("briefings")

    meta_alerts: list[tuple[str, str]] = []

    with (
        patch("core.scheduler._try_advisory_lock", return_value=None),
        patch("core.scheduler.dispatch_nightly", side_effect=_dispatch),
        # Story 24.4: per-org dbt step (default-off no-op; patched to keep the
        # step-count deterministic and record its run like the others).
        patch("core.scheduler._run_dbt_per_org", side_effect=lambda: ran.append("dbt_per_org")),
        patch("core.scheduler._run_rebuild_cache", side_effect=_rebuild),
        patch("core.scheduler._run_schema_context_gen", side_effect=_schema_context),
        patch("core.scheduler._run_alert_check", side_effect=_alerts),
        patch("core.scheduler._run_business_alert_check", side_effect=_business),
        patch("core.scheduler._run_anomaly_alert_check", side_effect=_anomaly),
        patch("core.scheduler._run_mediaplan_alert_check", side_effect=_mediaplan),
        patch("core.scheduler._run_dq_monitors_check", side_effect=_dq),
        patch("core.scheduler._run_due_notebooks", side_effect=_notebooks),
        patch("core.scheduler._run_due_briefings", side_effect=_briefings),
        patch(
            "core.scheduler._insert_meta_alert",
            side_effect=lambda step, reason: meta_alerts.append((step, reason)),
        ),
        # AI-32 (c): keep the consolidated post-run firing out of the live DB.
        patch("core.scheduler._write_scheduler_step_degraded_alert"),
        caplog.at_level("INFO"),
    ):
        scheduler.run_nightly_steps(date(2026, 7, 11))

    # (b) All eleven steps ran despite dispatch raising first (Story 24.4 added
    #     dbt_per_org right after dispatch, before rebuild_cache).
    assert ran == [
        "dispatch", "dbt_per_org", "rebuild", "schema_context", "alerts", "business",
        "anomaly", "mediaplan", "dq", "notebooks", "briefings"
    ]

    # (c) Exactly one meta-alert inserted, for the failing dispatch step.
    assert len(meta_alerts) == 1
    assert meta_alerts[0][0] == "dispatch_nightly"
    assert "boom in dispatch" in meta_alerts[0][1]

    # (a) Duration log emitted for every step
    #     (11: dispatch + dbt_per_org + rebuild + schema_context + 3 alerts + mediaplan
    #      + dq + notebooks + briefings). AI-32 (a): "scheduler: step=<name> duration_ms=<int>".
    duration_logs = [r for r in caplog.records if "duration_ms=" in r.getMessage()]
    assert len(duration_logs) == 11
    failed_logs = [r for r in caplog.records if "nightly_step_failed" in r.message]
    assert any("dispatch_nightly" in r.message for r in failed_logs)


def test_meta_alert_insert_writes_row():
    """_insert_meta_alert writes a type='meta_alert' scheduler_health row."""
    cursor_mock = MagicMock()
    cursor_cm = MagicMock()
    cursor_cm.__enter__ = MagicMock(return_value=cursor_mock)
    cursor_cm.__exit__ = MagicMock(return_value=False)
    conn_mock = MagicMock()
    conn_mock.__enter__ = MagicMock(return_value=conn_mock)
    conn_mock.__exit__ = MagicMock(return_value=False)
    conn_mock.cursor = MagicMock(return_value=cursor_cm)

    with patch("core.db.get_connection", return_value=conn_mock):
        scheduler._insert_meta_alert("business_alert_check", "kaboom")

    cursor_mock.execute.assert_called_once()
    sql = cursor_mock.execute.call_args[0][0]
    assert "app.alert_firings" in sql
    assert "meta_alert" in sql
    assert "scheduler_health" in sql
    params = cursor_mock.execute.call_args[0][1]
    # message param carries the failing step + reason.
    assert any("business_alert_check" in str(p) and "kaboom" in str(p) for p in params)


def test_all_steps_succeed_no_meta_alert(caplog):
    """When all steps succeed, no meta-alert is inserted.

    All eleven isolated steps are patched; briefings remains the final step.
    Story 22.6: _run_mediaplan_alert_check added as step 4b.
    Story 24.4: _run_dbt_per_org added right after dispatch.
    """
    meta_alerts: list = []
    with (
        patch("core.scheduler._try_advisory_lock", return_value=None),
        patch("core.scheduler.dispatch_nightly", return_value=[]),
        patch("core.scheduler._run_dbt_per_org"),
        patch("core.scheduler._run_rebuild_cache"),
        patch("core.scheduler._run_schema_context_gen"),
        patch("core.scheduler._run_alert_check"),
        patch("core.scheduler._run_business_alert_check"),
        patch("core.scheduler._run_anomaly_alert_check"),
        patch("core.scheduler._run_mediaplan_alert_check"),
        patch("core.scheduler._run_dq_monitors_check"),
        patch("core.scheduler._run_due_notebooks"),
        patch("core.scheduler._run_due_briefings"),
        patch(
            "core.scheduler._insert_meta_alert",
            side_effect=lambda step, reason: meta_alerts.append((step, reason)),
        ),
    ):
        scheduler.run_nightly_steps(date(2026, 7, 11))
    assert meta_alerts == []
