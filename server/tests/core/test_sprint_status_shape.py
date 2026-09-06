"""The tracker is a state, and a guard refuses to let it become a journal again.

WHY THIS TEST EXISTS AND NOT ONLY THE SCRIPT. `scripts/check_sprint_status_shape.py`
is a guard, and a guard nobody runs is decor -- the same reason
`test_action_item_ledger.py` exists beside `check_action_items.py`. So the script
is exercised here on fixtures for its logic, and on the REAL tracker for the
property that matters: it must be green today, and it must have been RED on the
file as it stood before the 2026-08-03 degrease. A guard that cannot tell those
two apart proves nothing.

WHAT IT DOES NOT DO: judge whether a blocker says the right thing. A line that
recounts what was DONE instead of what is MISSING passes the guard and still
fails CLAUDE.md section 7. That distinction is a reading, not a measurement.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

shape = pytest.importorskip("check_sprint_status_shape")

_TRACKER = _REPO_ROOT / "_bmad-output" / "implementation-artifacts" / "sprint-status.yaml"

_CLEAN = """\
development_status:
  # Epic 1: something
  epic-1: done
  1-1-a-story: done
  1-2-another: in-progress  # il manque la migration 184, chez qui tient queue.py

action_items:
  - id: "AI-1"
    epic: 1
    description: "Court, et il dit a qui."
    owner: "orchestrator"
    target: "1-2"
    status: open
"""


def test_a_clean_tracker_has_no_finding():
    assert shape.findings(_CLEAN) == []


def test_a_done_line_may_not_carry_commentary():
    text = _CLEAN.replace(
        "  1-1-a-story: done",
        "  1-1-a-story: done  # 2026-08-01 livre, 13/13 patches, ruff vert",
    )
    found = shape.findings(text)
    assert len(found) == 1
    assert "carries" in found[0] and "story-log.md" in found[0]


def test_a_live_line_may_carry_one_sentence_but_not_an_essay():
    text = _CLEAN.replace(
        "il manque la migration 184, chez qui tient queue.py",
        "x" * (shape.MAX_BLOCKER + 1),
    )
    found = shape.findings(text)
    assert len(found) == 1
    assert f"max {shape.MAX_BLOCKER}" in found[0]


def test_a_paragraph_is_judged_as_a_block_not_line_by_line():
    """Eight short lines are how 32 Ko slipped under a per-line threshold."""
    paragraph = "\n".join(f"  # ligne {n} d'un paragraphe" for n in range(8))
    text = _CLEAN.replace("  # Epic 1: something", "  # Epic 1: something\n" + paragraph)
    found = shape.findings(text)
    assert len(found) == 1
    assert "comment paragraph" in found[0]


def test_an_epic_header_stays_allowed():
    assert shape.findings(_CLEAN.replace("  # Epic 1: something", "  # Epic 2: navigation")) == []


def test_an_action_item_description_may_not_be_an_essay():
    text = _CLEAN.replace("Court, et il dit a qui.", "x" * (shape.MAX_DESCRIPTION + 1))
    found = shape.findings(text)
    assert len(found) == 1
    assert f"max {shape.MAX_DESCRIPTION}" in found[0]


def test_a_closed_action_item_carries_no_commentary():
    text = _CLEAN.replace("    status: open", "    status: done  # ferme le 2026-08-01 par abc1234")
    found = shape.findings(text)
    assert len(found) == 1
    assert "Closed is closed" in found[0]


def test_the_real_tracker_is_clean():
    assert shape.findings(_TRACKER.read_text(encoding="utf-8")) == []


def test_the_guard_would_have_fired_on_the_file_it_was_written_for():
    """Green on today's file proves nothing unless it is red on yesterday's."""
    import subprocess

    before = subprocess.run(
        ["git", "show", "665c9861~1:_bmad-output/implementation-artifacts/sprint-status.yaml"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if before.returncode != 0:  # shallow clone, or the commit is gone
        pytest.skip("the pre-degrease revision is not reachable from here")
    found = shape.findings(before.stdout)
    assert len(found) > 500, f"expected the 2026-08-03 file to be massively red, got {len(found)}"
