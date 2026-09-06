"""AI-317 ratchet: the MUTE count of the fake-cursor census may only go DOWN.

`scripts/fake_cursor_census.py` measures fake cursors that dispatch on the
product's SQL by its text and answer "no rows" when the text stops matching.
The count stood at 37 when this gate was written and had already drifted +3
in three days, each one introduced by a feature commit that had no idea it was
adding a silent fixture. A count you have to remember to re-check is not a
guard, so the check lives here, where `make test` runs it.

Two tripwires, deliberately redundant:

1. The MUTE count must not exceed the frozen ceiling (37). A new silent fake
   fails the suite the day it lands.
2. Every MUTE cursor must be one of the identities frozen below
   (path, class name). Line numbers are NOT part of the identity: a fixture
   that moves because the file grew is still the same fixture. This second
   tripwire catches the swap -- one known mute repaired, one new mute added,
   count unchanged at 37 -- which the ceiling alone cannot see.

The repair path is the one the census docstring describes: make the fake raise
on the unknown (or route it through a StatementInventory), and the cursor
leaves the MUTE column. Widening the ceiling instead means writing down,
deliberately, that the suite gained a fixture that cannot see the product move.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"

if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import fake_cursor_census  # noqa: E402

MUTE_CEILING = 0

#: ZERO since lot 9 (AI-317, 2026-08-30): 37 at the ratchet, all 37 converted. A new
#: mute fake is a regression by name -- convert it (tests/support/statement_router.py).
#: (test file relative to the repo root, fake class name).
KNOWN_MUTE: frozenset[tuple[str, str]] = frozenset()


def _new_mutes(cursors: list) -> list:
    """The MUTE cursors nobody froze -- the drift the ratchet exists to refuse."""

    return [
        c
        for c in cursors
        if c.verdict == fake_cursor_census.MUTE and (c.path, c.class_name) not in KNOWN_MUTE
    ]


def _gate_message(new: list, count: int) -> str:
    lines = [
        f"fake-cursor census: {count} MUTE (ceiling {MUTE_CEILING}), "
        f"{len(new)} not in the frozen KNOWN_MUTE set:",
    ]
    lines += [f"  {c.path}:{c.line}  {c.class_name}  {c.reason}" for c in new]
    lines.append(
        "repair: make the fake raise on an unrecognized statement "
        "(see scripts/fake_cursor_census.py), or freeze the new identity in "
        "KNOWN_MUTE with the reason for accepting a silent fixture."
    )
    return "\n".join(lines)


def test_mute_census_does_not_regress():
    found = fake_cursor_census.census()
    count = sum(1 for c in found if c.verdict == fake_cursor_census.MUTE)
    new = _new_mutes(found)
    assert count <= MUTE_CEILING and not new, _gate_message(new, count)


def test_gate_names_a_new_mute():
    """The ratchet must be able to go red: a synthetic new MUTE is flagged by name."""

    found = fake_cursor_census.census()
    intruder = fake_cursor_census.FakeCursor(
        path="server/tests/core/test_synthetic.py",
        class_name="_SilentCursor",
        line=1,
        verdict=fake_cursor_census.MUTE,
        reason="no else branch: an unrecognized statement answers no rows",
    )
    new = _new_mutes([*found, intruder])
    assert [c.class_name for c in new] == ["_SilentCursor"]
    message = _gate_message(new, sum(1 for c in found if c.verdict == fake_cursor_census.MUTE) + 1)
    assert "test_synthetic.py" in message
    assert "_SilentCursor" in message
