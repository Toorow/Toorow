"""Criterion 13 ratchet: the QUIET set of the guard census may only shrink.

`scripts/quiet_guard_census.py` measures the guards that read a repository
source BY PATH and would stay green if that source moved -- the read returns
nothing, the offender list is empty because nothing was scanned, and
`assert not offenders` is true of a scan that never happened. That is criterion
13 of `docs/product-architecture/module-boundaries.md`, and its named case is
`navigation_backward_audit`, which went from 31 declared object types to 0 while
still reporting "0 rendered by nothing".

The census stood at 1103 source-reading guards on 2026-08-31: 907 carrying a
floor, 196 quiet. A count somebody has to remember to re-run is not a guard, so
the check lives here, where `make test` runs it.

Two tripwires, deliberately redundant:

1. Every QUIET guard must be one of the identities frozen in
   `QUIET_GUARDS_AT_2026_08_31`. A guard written today that passes on an empty
   read fails the suite the day it lands, which is the only day adding the floor
   is free.
2. A baselined identity that no longer exists is refused too. The ratchet turns
   ONE WAY: a guard that gains a floor must leave the list in the same commit,
   so the list can never quietly become a record of guards nobody has.

Identity is `path::test`, not a line number: a test that moves because the file
grew is the same test. A RENAMED test reads as one departure and one arrival,
and both halves redden -- deliberately, because a rename is exactly when someone
should be asked whether the guard still proves anything.

The repair for a refusal is never "add it to the list". It is the floor the
census prints: `assert len(scanned) >= N` with N measured today, and a sentence
saying what a shrunken N means. Ten guards were repaired that way in the lot
that wrote this file; `test_every_staging_supersedes_on_pull_id.py` is the model
that predates it.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"

if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import quiet_guard_census  # noqa: E402

#: Measured 2026-08-31 by `python scripts/quiet_guard_census.py`. The floor and
#: the ceiling of the same population: the census must keep finding at least
#: this many source-reading guards (a census that reads nothing would report
#: zero quiet guards and pass every tripwire below), and no more than this many
#: may be quiet.
SOURCE_READING_GUARDS = 1103
#: 196 on 2026-08-31 at 02:24; **195** the same day, once the classifier learned
#: that `assert match is not None` is a floor and one guard that had carried one
#: since it was written stopped being counted as mute. The ceiling reads the
#: frozen set rather than restating its length: two copies of a number drift.
QUIET_CEILING = len(quiet_guard_census.QUIET_GUARDS_AT_2026_08_31)


def _new_quiet(guards: list) -> list:
    """The quiet guards nobody froze -- the drift the ratchet exists to refuse."""

    return [
        g
        for g in guards
        if g.verdict == quiet_guard_census.QUIET
        and g.identity not in quiet_guard_census.QUIET_GUARDS_AT_2026_08_31
    ]


def _gate_message(new: list, count: int) -> str:
    lines = [
        f"quiet-guard census: {count} QUIET (ceiling {QUIET_CEILING}), "
        f"{len(new)} not in the frozen baseline:",
    ]
    lines += [
        f"  {g.path}:{g.line}  {g.test}  reads {g.reads}: {', '.join(g.targets)}"
        for g in new
    ]
    lines.append(
        "repair: give the guard a floor that names its own coverage -- "
        'assert len(scanned) >= N, "the sources moved; this guard scanned none" -- '
        "or freeze the identity in QUIET_GUARDS_AT_2026_08_31 "
        "(scripts/quiet_guard_census.py) with the reason for accepting a guard "
        "that passes on nothing."
    )
    return "\n".join(lines)


def test_the_census_still_reaches_the_test_tree():
    """The instrument must not measure its own copy: a census of nothing is quiet too."""

    guards = quiet_guard_census.census()
    assert len(guards) >= SOURCE_READING_GUARDS, (
        f"the census found {len(guards)} source-reading guards, "
        f"{SOURCE_READING_GUARDS} were there on 2026-08-31. server/tests moved "
        "or the walk broke, so the verdicts below are about nothing."
    )


def test_quiet_census_does_not_regress():
    guards = quiet_guard_census.census()
    count = sum(1 for g in guards if g.verdict == quiet_guard_census.QUIET)
    new = _new_quiet(guards)
    assert count <= QUIET_CEILING and not new, _gate_message(new, count)


def test_a_repaired_guard_must_leave_the_baseline():
    """The ratchet turns one way: a listed identity that vanished is refused."""

    guards = quiet_guard_census.census()
    live = {g.identity for g in guards if g.verdict == quiet_guard_census.QUIET}
    stale = sorted(quiet_guard_census.QUIET_GUARDS_AT_2026_08_31 - live)
    assert not stale, (
        f"{len(stale)} baselined quiet guard(s) no longer exist. Delete them "
        "from QUIET_GUARDS_AT_2026_08_31 in scripts/quiet_guard_census.py in "
        "the commit that repaired or renamed them -- a baseline that outlives "
        "its subjects stops being a record of anything:\n  " + "\n  ".join(stale)
    )


def test_gate_names_a_new_quiet_guard():
    """The ratchet must be able to go red: a synthetic quiet guard is flagged by name."""

    guards = quiet_guard_census.census()
    intruder = quiet_guard_census.Guard(
        path="server/tests/core/test_synthetic.py",
        test="test_nothing_in_core_says_the_forbidden_word",
        line=1,
        verdict=quiet_guard_census.QUIET,
        reads="directory",
        targets=["server/core"],
        floors=[],
    )
    new = _new_quiet([*guards, intruder])
    assert [g.test for g in new] == ["test_nothing_in_core_says_the_forbidden_word"]
    message = _gate_message(new, QUIET_CEILING + 1)
    assert "test_synthetic.py" in message
    assert "test_nothing_in_core_says_the_forbidden_word" in message


def test_the_classifier_reads_a_floor_named_by_a_constant():
    """`assert len(found) >= _KNOWN` is the repaired shape here, and the bound is a NAME.

    A classifier that only understood numeric literals files 37 well-floored
    guards as quiet -- measured while writing the census. This pins the hop.
    """

    import ast

    tree = ast.parse("assert len(found) >= _KNOWN_STAGING_MODELS\n")
    assertion = tree.body[0]
    assert isinstance(assertion, ast.Assert)
    assert not quiet_guard_census._is_floor(assertion.test, {})
    assert quiet_guard_census._is_floor(assertion.test, {"_KNOWN_STAGING_MODELS": 54.0})


def test_the_classifier_reads_a_single_object_floor():
    """`assert match is not None` fails on an empty read, so it is a floor.

    The shape a guard takes when what it read is ONE object rather than a list:
    `re.search` over a moved file returns None. `is None` is the mirror and is
    NOT a floor -- it is true of a read that never happened.
    """

    import ast

    floor = ast.parse("assert match is not None\n").body[0]
    vacuous = ast.parse("assert match is None\n").body[0]
    assert quiet_guard_census._is_floor(floor.test, {})
    assert not quiet_guard_census._is_floor(vacuous.test, {})
