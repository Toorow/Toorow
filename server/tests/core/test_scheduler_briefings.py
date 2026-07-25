"""Tests for briefing step ordering in run_nightly_steps (Story 6.7, AC7).

Tests:
  - test_briefings_step_is_last
  - test_briefings_step_runs_after_notebooks
"""

from __future__ import annotations

import inspect
from datetime import date
from unittest.mock import patch

# ---------------------------------------------------------------------------
# test_briefings_step_is_last
# ---------------------------------------------------------------------------

def test_briefings_step_is_last():
    """Inspect run_nightly_steps source: briefings step index > notebooks step index."""
    import core.scheduler as sched

    src = inspect.getsource(sched.run_nightly_steps)

    # Both steps must appear in the function
    assert "run_due_notebooks" in src, (
        "run_due_notebooks must appear in run_nightly_steps"
    )
    assert "run_due_briefings" in src, (
        "run_due_briefings must appear in run_nightly_steps"
    )

    # Briefings step must appear AFTER notebooks step
    notebooks_idx = src.index("run_due_notebooks")
    briefings_idx = src.index("run_due_briefings")

    assert briefings_idx > notebooks_idx, (
        f"run_due_briefings (idx={briefings_idx}) must come AFTER "
        f"run_due_notebooks (idx={notebooks_idx}) in run_nightly_steps"
    )


# ---------------------------------------------------------------------------
# test_briefings_step_runs_after_notebooks
# ---------------------------------------------------------------------------

def test_briefings_step_runs_after_notebooks():
    """Mock each step; assert execution order: notebooks before briefings."""
    import core.scheduler as sched

    call_order: list[str] = []

    def make_step_mock(name: str):
        def mock_fn(*args, **kwargs):
            call_order.append(name)
        return mock_fn

    with patch.object(sched, "_run_isolated_step") as mock_isolated:
        # Capture calls to _run_isolated_step in order
        def record_isolated(step_name, fn, *args, **kwargs):
            call_order.append(step_name)

        mock_isolated.side_effect = record_isolated

        sched.run_nightly_steps(as_of_date=date(2026, 7, 12))

    # Verify notebooks comes before briefings in call order
    assert "run_due_notebooks" in call_order, "run_due_notebooks must be called"
    assert "run_due_briefings" in call_order, "run_due_briefings must be called"

    notebooks_pos = call_order.index("run_due_notebooks")
    briefings_pos = call_order.index("run_due_briefings")

    assert briefings_pos > notebooks_pos, (
        f"run_due_briefings (pos={briefings_pos}) must execute AFTER "
        f"run_due_notebooks (pos={notebooks_pos}). "
        f"Full order: {call_order}"
    )

    # Verify briefings is the very last step
    assert call_order[-1] == "run_due_briefings", (
        f"run_due_briefings must be the last step. "
        f"Got last step: {call_order[-1]}. Full order: {call_order}"
    )
