"""Tests for server/core/daily_insights_recipe.py (Epic 35, Story 35.5).

Proves the copyable task recipe (deterministic, no secret/scheduler, real tool names +
prompt ref) and the run-journal observability formatter (distinct states, provenance,
rejection reasons). Pure/offline. ASCII-only stdout (L-3).
"""

from __future__ import annotations

import pytest
from core.daily_insights_recipe import (
    TASK_RECIPE_VERSION,
    build_task_recipe,
    render_recipe_text,
    run_journal,
)


def _recipe(**over):
    kwargs = dict(
        project_id="proj_a",
        timezone="Europe/Paris",
        hour_local=7,
        contract_version="1",
    )
    kwargs.update(over)
    return build_task_recipe(**kwargs)


def test_recipe_shape_and_determinism():
    r1 = _recipe()
    r2 = _recipe()
    assert r1 == r2  # deterministic
    assert r1["recipeVersion"] == TASK_RECIPE_VERSION
    assert r1["schedule"]["target"] == "J-1"
    assert r1["hostOwnsSchedule"] is True
    assert r1["backendHostsModel"] is False  # no model in toorow backend


def test_recipe_tool_sequence_uses_real_35_2_tool_names():
    seq = _recipe()["toolSequence"]
    names = {e.get("tool") for e in seq if "tool" in e}
    assert "get_daily_insight_readiness" in names
    assert "get_card_capabilities" in names
    assert "preview_daily_insight" in names
    assert "publish_daily_insights" in names
    # readiness step must stop when blocked
    readiness = next(e for e in seq if e.get("tool") == "get_daily_insight_readiness")
    assert readiness["stopIf"] == "blocked"


def test_recipe_rejects_bad_hour():
    with pytest.raises(ValueError, match="hour_local"):
        _recipe(hour_local=24)


def test_render_recipe_text_is_copyable_and_references_prompt():
    text = render_recipe_text(_recipe(priority_domains=["pacing breaks"]))
    assert "toorow daily-insight task" in text
    assert "35-0-agent-prompt.v1.md" in text
    assert "publish_daily_insights" in text
    assert "pacing breaks" in text
    assert text.isascii()


# ---------------------------------------------------------------------------
# run_journal -- observability over distinct states
# ---------------------------------------------------------------------------


def test_journal_absent_run():
    j = run_journal(None)
    assert j["state"] == "absent"


def test_journal_published_run_with_items():
    run = {
        "status": "published",
        "insight_date": "2026-07-21",
        "period_from": "2026-07-21",
        "period_to": "2026-07-21",
        "host": "claude-cowork",
        "prompt_version": "v1",
        "contract_version": "1",
        "coverage": {"datastreams": 3},
        "insights": [{"slot": 0}, {"slot": 1}],
        "created_at": "2026-07-22T06:00:00+00:00",
    }
    j = run_journal(run)
    assert j["state"] == "published"
    assert j["itemCount"] == 2 and j["slots"] == [0, 1]
    assert j["provenance"]["host"] == "claude-cowork"
    assert j["rejection"] is None


def test_journal_blocked_run_surfaces_rejection_reason():
    run = {
        "status": "blocked",
        "insight_date": "2026-07-21",
        "coverage": {"reason": "J-1 freshness incomplete", "reason_code": "stale_data"},
        "insights": [],
    }
    j = run_journal(run)
    assert j["state"] == "blocked"
    assert j["itemCount"] == 0
    assert j["rejection"]["reasonCode"] == "stale_data"


def test_journal_no_insight_distinct_from_blocked():
    j = run_journal({"status": "no_insight", "insights": [], "coverage": {}})
    assert j["state"] == "no_insight"
    assert j["rejection"] is None  # nothing worth saying != data not ready


# ---------------------------------------------------------------------------
# The journal counts what the day carried, including what was withdrawn
#
# `list_runs` deliberately carries no items -- fourteen days of frozen cards is
# not a journal -- and `run_journal` derived `itemCount` from that absent list, so
# every row of the history reported 0 and the console's own condition
# (`itemCount > 0`) never opened a day. Migration 321 adds the second count: a day
# whose only claim was retracted must not read like a day whose claim stands.
# ---------------------------------------------------------------------------


def test_the_journal_prefers_the_counts_the_list_query_measured():
    from core.daily_insights_recipe import run_journal

    journal = run_journal(
        {
            "status": "published",
            "insight_date": "2026-07-21",
            "item_count": 3,
            "retracted_count": 1,
            "insights": [],
        }
    )

    assert journal["itemCount"] == 3
    assert journal["retractedCount"] == 1


def test_a_run_read_with_its_items_still_counts_them_itself():
    from core.daily_insights_recipe import run_journal

    journal = run_journal(
        {
            "status": "published",
            "insight_date": "2026-07-21",
            "insights": [
                {"slot": 0, "retracted_at": None},
                {"slot": 1, "retracted_at": "2026-07-23T11:00:00+00:00"},
            ],
        }
    )

    assert journal["itemCount"] == 2
    assert journal["retractedCount"] == 1
    assert journal["slots"] == [0, 1]
