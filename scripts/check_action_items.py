r"""Refuse an ambiguous action-item ledger, and hand out the next free id.

WHY THIS EXISTS. `scripts/check_migration_catalog.py` refuses a migration
catalogue whose numbers are ambiguous, and that guard is the reason a collision
on migration 171 was visible in one command instead of three days later. The
action-item ledger in `sprint-status.yaml` had no equivalent, so the same
collision happened there and nobody noticed: measured 2026-08-01, FOUR ids are
declared twice (AI-104 to AI-107).

A note on that number, because I got it wrong first. An unanchored
`grep 'id: "AI-(\d+)"'` reported five, counting `AI-98` -- which is not declared
twice, only QUOTED inside another item's prose. The parser below anchors on the
start of a list entry for exactly that reason, and a fixture test pins it: an id
mentioned in a description is a reference, not a second definition.

That is not a discipline problem. Two sessions can respect their file perimeters
perfectly and still take the same number, because the number belongs to nobody.
A perimeter cannot allocate; only a counter can. So this script does the two
things a counter needs:

    python scripts/check_action_items.py          # refuse duplicates
    python scripts/check_action_items.py --next   # claim the next free id

WHY IT DOES NOT RENUMBER. `AI-98` is mentioned twelve times across the tracker's
prose. Renumbering someone else's item would silently break every reference to
it, in a file three other sessions are writing to. The duplicates are REPORTED,
with their owners, and repaired by whoever holds them.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

_DEFAULT_LEDGER = Path("_bmad-output/implementation-artifacts/sprint-status.yaml")

#: The settled half of the ledger, split out on 2026-08-04 so the tracker shows
#: only what is left to do. IT IS READ HERE, AND THAT IS NOT AN OPTION: a counter
#: that sees one half hands out numbers the other half already took. Splitting the
#: file without this line would have made every archived id -- 137 of them, AI-01
#: to AI-170 -- reusable, which is exactly the collision this guard exists to
#: refuse, reintroduced by the act of tidying.
_ARCHIVED_NAME = "sprint-status-settled.yaml"


def companion(ledger: Path) -> Path | None:
    """The settled half beside *ledger*, derived from *ledger*'s own path.

    Derived, never taken from the current directory: the guard must find the same
    two files whether it is run from the repository root, from `server/` under
    pytest, or from a hook. A file that is not the tracker -- a fixture, a copy
    under test -- gets no companion, so the logic stays inspectable in isolation.
    """
    if ledger.name != _DEFAULT_LEDGER.name:
        return None
    archived = ledger.parent.parent / "archive" / "implementation-artifacts" / _ARCHIVED_NAME
    return archived if archived.is_file() else None


def read_both(ledger: Path) -> tuple[str, dict[int, str]]:
    """The whole ledger, and the map that says WHICH FILE each line number is in.

    The map is not a nicety. Concatenating the two halves makes every line number
    past the tracker point at a line the reader will not find there, and this
    guard's entire value is that it names the two writers by line. So the offset
    travels with the text.
    """
    text = ledger.read_text(encoding="utf-8")
    where = {0: str(ledger)}
    settled = companion(ledger)
    if settled is not None:
        where[text.count("\n") + 2] = str(settled)
        text += "\n" + settled.read_text(encoding="utf-8")
    return text, where


def locate(line: int, where: dict[int, str]) -> str:
    """``file:line`` for a line number in the concatenated ledger."""
    start = max(k for k in where if k <= line)
    return f"{where[start]}:{line - start + 1 if start else line}"

_ID = re.compile(r'^\s*-\s*id:\s*"(AI-(\d+))"\s*$')
_FIELD = re.compile(r'^\s{4}(\w+):')

#: The ids already duplicated when this guard was written, with the date. Same
#: shape as `known-debt.json` and as the margin guard's recorded band: a guard
#: that goes red on a state nobody has decided to change is a guard people learn
#: to ignore. A SIXTH duplicate fails; these five are reported and not counted.
#: Empty on purpose, and it must stay that way. The four entries this set was
#: born with (AI-104..AI-107) were repaired on 2026-08-01 by the session that had
#: DECLARED them -- renumbering one's own item costs nothing, because its author
#: owns every reference to it. AI-115 followed the rule written into the guard's
#: own failure message: the LATER declaration moved, and it had no reference
#: outside this file. A number that reappears here is not a tolerated state; it
#: is a collision waiting to be renumbered.
KNOWN_DUPLICATES: set[str] = set()


def parse_items(text: str) -> list[tuple[str, int]]:
    """Return ``[(id, line_number)]`` for every action item declared in *text*."""
    items: list[tuple[str, int]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        match = _ID.match(line)
        if match:
            items.append((match.group(1), lineno))
    return items


def duplicates(items: list[tuple[str, int]]) -> dict[str, list[int]]:
    counts = Counter(item_id for item_id, _ in items)
    return {
        item_id: [line for other, line in items if other == item_id]
        for item_id, count in counts.items()
        if count > 1
    }


def padding_ambiguities(items: list[tuple[str, int]]) -> dict[int, list[str]]:
    """Numbers written two ways -- ``AI-07`` and ``AI-7`` are one item, two strings.

    Comparing strings alone would call them distinct while every human reading the
    tracker calls them the same. The ledger pads `AI-01`..`AI-09` and nothing else,
    so any number with two spellings is an ambiguity, not a convention.
    """
    spellings: dict[int, set[str]] = {}
    for item_id, _line in items:
        number = int(item_id.split("-", 1)[1])
        spellings.setdefault(number, set()).add(item_id)
    return {n: sorted(forms) for n, forms in spellings.items() if len(forms) > 1}


def next_free_id(items: list[tuple[str, int]]) -> str:
    """The smallest id strictly above every declared one.

    Deliberately NOT the smallest unused id: a gap usually means an item was
    withdrawn, and reusing its number makes an old reference point at new work.
    """
    numbers = [int(item_id.split("-", 1)[1]) for item_id, _ in items]
    return f"AI-{max(numbers) + 1 if numbers else 1}"


def check(path: Path) -> int:
    text, where = read_both(path)
    items = parse_items(text)
    if not items:
        print(f"no action item found in {path}", file=sys.stderr)
        return 1

    ambiguous = padding_ambiguities(items)
    for number, forms in sorted(ambiguous.items()):
        print(
            f"AMBIGUOUS: number {number} is written as {forms}. One item, two "
            "spellings -- pick one; the ledger pads AI-01..AI-09 and nothing else.",
            file=sys.stderr,
        )

    found = duplicates(items)
    unrecorded = {k: v for k, v in found.items() if k not in KNOWN_DUPLICATES}
    recorded = {k: v for k, v in found.items() if k in KNOWN_DUPLICATES}

    for item_id, lines in sorted(recorded.items()):
        print(
            f"known duplicate {item_id}: {[locate(n, where) for n in lines]} "
            "(recorded 2026-08-01; owned by the sessions that wrote them)"
        )

    # A recorded duplicate that has been repaired must LEAVE the set, or the set
    # silently grants an exemption to a future collision on the same number.
    for item_id in sorted(KNOWN_DUPLICATES - set(found)):
        print(
            f"{item_id} is no longer duplicated -- remove it from KNOWN_DUPLICATES, "
            "or it will excuse the next collision on that number"
        )

    if unrecorded or ambiguous:
        for item_id, lines in sorted(unrecorded.items()):
            print(
                f"DUPLICATE {item_id}: declared at {[locate(n, where) for n in lines]}. "
                "Two sessions took "
                "the same number. Renumber the one that NOTHING REFERENCES yet -- "
                f"`grep -c \"{item_id}\" <ledger>` tells you, and a freshly written "
                "entry is referenced nowhere. Position in the file is only a proxy "
                "for that, and it was WRONG the first time this guard fired: the "
                "earlier entry was the ten-second-old one. Take "
                f"{next_free_id(items)} instead.",
                file=sys.stderr,
            )
        return 1

    halves = "" if companion(path) is None else " (tracker + archive settled)"
    print(
        f"action-item ledger OK{halves}: {len(items)} items, "
        f"next free id {next_free_id(items)}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledger", nargs="?", type=Path, default=_DEFAULT_LEDGER)
    parser.add_argument(
        "--next",
        action="store_true",
        help="print the next free id and exit -- claim it before writing",
    )
    args = parser.parse_args()

    if not args.ledger.is_file():
        print(f"ledger not found: {args.ledger}", file=sys.stderr)
        return 1
    if args.next:
        print(next_free_id(parse_items(read_both(args.ledger)[0])))
        return 0
    return check(args.ledger)


if __name__ == "__main__":
    raise SystemExit(main())
