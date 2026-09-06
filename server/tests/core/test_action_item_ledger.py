"""The action-item ledger has a counter, and the counter refuses ambiguity.

WHY THIS TEST EXISTS AND NOT ONLY THE SCRIPT. `scripts/check_action_items.py` is
a guard, and a guard nobody runs is decor -- this repository has paid for that
distinction more than once. So the script is exercised here, on fixtures for its
logic and on the REAL ledger for the property that matters.

WHAT IT DOES NOT DO: fail on the four duplicates (AI-104..AI-107) that already
existed when the guard was written. They belong to neighbouring sessions, and
renumbering someone else's item silently breaks every reference to it -- `AI-98`
is quoted twelve times in the tracker's prose without being declared twice, which
is precisely the distinction an unanchored grep gets wrong. A guard that goes red
on a state nobody has decided to change is a guard people learn to ignore. A
FIFTH duplicate fails.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

check_action_items = pytest.importorskip("check_action_items")

_LEDGER = _REPO_ROOT / "_bmad-output" / "implementation-artifacts" / "sprint-status.yaml"


def _ledger_text() -> str:
    if not _LEDGER.is_file():
        pytest.skip("the tracker is not present in this checkout")
    return _LEDGER.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The logic, on fixtures.
# ---------------------------------------------------------------------------


def test_two_sessions_taking_the_same_number_is_detected():
    text = '\n'.join(
        [
            'action_items:',
            '  - id: "AI-1"',
            '    status: open',
            '  - id: "AI-2"',
            '    status: open',
            '  - id: "AI-2"',
            '    status: open',
        ]
    )
    found = check_action_items.duplicates(check_action_items.parse_items(text))

    assert set(found) == {"AI-2"}
    assert found["AI-2"] == [4, 6], "the guard names the LINES, so both writers are findable"


def test_the_next_free_id_is_above_every_declared_one_not_in_a_gap():
    """Reusing a withdrawn number makes an old reference point at new work."""
    text = '\n'.join(
        ['  - id: "AI-1"', '  - id: "AI-2"', '  - id: "AI-5"']
    )
    assert check_action_items.next_free_id(check_action_items.parse_items(text)) == "AI-6"


def test_an_id_mentioned_in_prose_is_not_read_as_a_declaration():
    """`AI-7` inside a description is a reference, not a second definition --
    otherwise the guard would report a collision every time an item cites another."""
    text = '\n'.join(
        [
            '  - id: "AI-7"',
            '    description: "voir AI-7 et id: \\"AI-7\\" cite dans le texte"',
        ]
    )
    items = check_action_items.parse_items(text)

    assert [i for i, _ in items] == ["AI-7"]


# ---------------------------------------------------------------------------
# The real ledger.
# ---------------------------------------------------------------------------


def test_the_real_ledger_gains_no_new_duplicate():
    items = check_action_items.parse_items(_ledger_text())
    found = set(check_action_items.duplicates(items))
    unrecorded = found - check_action_items.KNOWN_DUPLICATES

    assert not unrecorded, (
        f"two sessions took the same action-item number: {sorted(unrecorded)}. "
        f"Renumber the later one to {check_action_items.next_free_id(items)} "
        "(`python scripts/check_action_items.py --next`), never the earlier -- it is "
        "the one other entries are more likely to reference."
    )


def test_a_repaired_duplicate_must_leave_the_recorded_set():
    """Otherwise the set silently excuses the NEXT collision on that number."""
    found = set(check_action_items.duplicates(check_action_items.parse_items(_ledger_text())))
    stale = check_action_items.KNOWN_DUPLICATES - found

    assert not stale, (
        f"{sorted(stale)} are no longer duplicated: remove them from "
        "KNOWN_DUPLICATES in scripts/check_action_items.py"
    )


def test_every_declared_id_is_well_formed():
    """The ledger's real convention includes `AI-01`..`AI-09`, zero-padded.

    An earlier version of this test asserted no leading zero and failed on nine
    legitimate items -- it invented a rule instead of reading the file. What the
    counter actually needs is that every id be a number it can order.
    """
    items = check_action_items.parse_items(_ledger_text())

    assert items, "the ledger declares no action item at all -- the guard would be blind"
    for item_id, line in items:
        number = item_id.split("-", 1)[1]
        assert number.isdigit(), (
            f"line {line}: {item_id!r} is not an AI-<n> id, so the counter cannot order it"
        )


def test_the_same_number_written_two_ways_is_an_ambiguity_too():
    """`AI-07` and `AI-7` are the same number and two different strings.

    A guard that compared strings only would call them distinct while every human
    reading the tracker calls them one item. Measured 2026-08-01: the ledger has
    no such pair, and this is what keeps it that way.
    """
    ambiguous = check_action_items.padding_ambiguities(
        check_action_items.parse_items(_ledger_text())
    )
    assert not ambiguous, (
        f"the same action-item number is written two ways: {ambiguous}. "
        "Pick one spelling; the ledger pads only AI-01..AI-09."
    )
