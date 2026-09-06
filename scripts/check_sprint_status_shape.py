r"""Refuse a tracker that has turned back into a journal.

WHY THIS EXISTS. `sprint-status.yaml` answers one question -- what is not
finished, and what blocks it. On 2026-08-03 it answered nothing: 1937 lines,
718 428 bytes, of which 65 % was end-of-line commentary (464 375 characters over
484 lines, 959 on average, 8 287 on the worst one) and 60 % of that commentary
hung on lines already `done`. At that size the file was 112 606 tokens, which is
past what a reading tool accepts -- while `bmad-quick-dev/sync-sprint-status.md`
orders the agent to load it IN FULL. The procedure that maintains the tracker had
become literally unrunnable, and nobody could read what was left to do.

Nothing was deleted: the prose moved, verbatim, to `story-log.md` beside it.

That journal was FROZEN on 2026-09-06. In 34 days it grew from 718 Ko to 2.6 Mo
(35 226 lines, +22 841 in its last thirty days), nothing read it -- no script, no
skill, no test, and a reading tool refuses a file that size -- while every section
already lived in the commit message, in the story file's Dev Agent Record and in
`SESSIONS.md`. A fourth copy nobody reads is not a record, it is a cost. The
journal stays where it is, intact, because its sections are cited by title; the
narrative now goes to the story file (Dev Agent Record), to the commit message when
there is no story, or -- for a long investigation -- to its own file under
`reviews/`.

But a rule written in a header is a wish. This guard is the mechanism, and it is
the same shape as `check_action_items.py` and `check_migration_catalog.py`: one
command, a named failure, and a repair the reader can perform.

    python scripts/check_sprint_status_shape.py

WHAT IT REFUSES, and why each one is the exact way the file grew:

  1. a comment on a `done` line -- 60 % of the volume, and the class that grows
     forever, since `done` lines only accumulate;
  2. a blocker longer than one sentence on a live line -- CLAUDE.md section 7
     asks for what is missing and to whom, not for the measurement, the history
     and the proof;
  3. a comment PARAGRAPH inside `development_status` -- a 900-character block
     split over eight short lines slips under any per-line threshold, which is
     exactly how 32 Ko of epic-header prose got in;
  4. an action-item description written as an essay;
  5. the total size, last -- the others are causes, this one is the symptom that
     made the file unreadable, and it is checked so that a new class of growth
     nobody predicted still fires something;
  6. any change to the line count of `story-log.md` beside the tracker -- it is
     frozen: an appended section goes where someone will read it, and a removed
     section breaks a citation by title.

WHAT IT DOES NOT DO: judge the CONTENT of a blocker. A line saying what was DONE
rather than what is MISSING passes this guard and still fails CLAUDE.md section 7.
That is a reading, not a measurement, and pretending otherwise would make the
guard lie about its own reach.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_DEFAULT = Path("_bmad-output/implementation-artifacts/sprint-status.yaml")

#: Statuses that describe settled work. Their lines carry no commentary: what was
#: done is readable in the commit, in the story file and in `story-log.md`.
FROZEN = frozenset({"done", "superseded", "no-go", "optional", "closed"})

#: One sentence. The degreased file's longest blocker is 182 characters, so this
#: is headroom, not a squeeze -- a guard that fires on the state it just created
#: is a guard people learn to ignore.
MAX_BLOCKER = 260

#: Same reasoning, measured against a longest description of 243.
MAX_DESCRIPTION = 340

#: 101 088 bytes after the degrease. The ceiling is deliberately far above it:
#: this guard is not a diet, it is a tripwire for the day the file doubles again.
MAX_BYTES = 250_000

_STATUS_LINE = re.compile(r"^(  [\w\-.]+): *([\w\-]+) *(?:#\s*(.*))?$")
_COMMENT = re.compile(r"^\s*#\s?(.*)$")
_EPIC_HEADER = re.compile(r"^\s*#\s*Epic \d+:")
_DESCRIPTION = re.compile(r'^    description: "(.*)"$')
_AI_STATUS = re.compile(r"^    status: *([\w\-]+) *(?:#\s*(.*))?$")

_JOURNAL = "story-log.md"

#: Where the narrative goes since the journal froze: the story file first, the
#: commit message when the work has no story.
_HOME = "the story file (Dev Agent Record), or the commit message when there is no story"

#: `story-log.md` on 2026-09-06: 12 lines of freeze header + 35 226 lines of journal.
#: Any other count is a finding -- growth and shrinkage alike.
JOURNAL_FROZEN_LINES = 35238


def _development_status_region(lines: list[str]) -> tuple[int, int]:
    """``[start, end)`` line indices of the ``development_status:`` block."""
    start = end = len(lines)
    for i, line in enumerate(lines):
        if line.startswith("development_status:"):
            start = i + 1
        elif start < len(lines) and line and not line.startswith((" ", "#")):
            end = i
            break
    return start, end


def findings(text: str) -> list[str]:
    """Every reason the tracker is drifting back into a journal, in file order."""
    lines = text.split("\n")
    found: list[str] = []
    start, end = _development_status_region(lines)

    in_action_items = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("action_items:"):
            in_action_items = True

        if not in_action_items:
            match = _STATUS_LINE.match(line)
            if match:
                key, status, comment = match.group(1).strip(), match.group(2), match.group(3)
                if comment and status in FROZEN:
                    found.append(
                        f"L{i + 1} `{key}` is {status} and carries {len(comment)} characters "
                        f"of commentary. A settled line carries none -- move it to {_HOME}."
                    )
                elif comment and len(comment) > MAX_BLOCKER:
                    found.append(
                        f"L{i + 1} `{key}` carries a {len(comment)}-character blocker "
                        f"(max {MAX_BLOCKER}). Keep the sentence that says what is missing "
                        f"and to whom; the evidence goes to {_HOME}."
                    )
                i += 1
                continue

            # A paragraph is judged as a block, never line by line.
            if start <= i < end and _COMMENT.match(line):
                j = i
                while j < len(lines) and _COMMENT.match(lines[j]):
                    j += 1
                block = lines[i:j]
                allowed = 1 if _EPIC_HEADER.match(block[0]) else 0
                if len(block) > max(allowed, 1):
                    found.append(
                        f"L{i + 1}-{j} a {len(block)}-line comment paragraph sits in "
                        f"development_status. An epic header is one line; a paragraph is a "
                        f"journal entry -- move it to {_HOME}."
                    )
                i = j
                continue

            i += 1
            continue

        match = _DESCRIPTION.match(line)
        if match and len(match.group(1)) > MAX_DESCRIPTION:
            found.append(
                f"L{i + 1} an action-item description runs {len(match.group(1))} characters "
                f"(max {MAX_DESCRIPTION}). Say what is wrong and to whom; the measurement "
                f"goes to {_HOME}."
            )
        match = _AI_STATUS.match(line)
        if match and match.group(2) and match.group(1) in FROZEN:
            found.append(
                f"L{i + 1} a {match.group(1)} action item carries {len(match.group(2))} "
                f"characters of commentary. Closed is closed -- move it to {_HOME}."
            )
        i += 1

    if len(text.encode("utf-8")) > MAX_BYTES:
        found.append(
            f"the file is {len(text.encode('utf-8')):,} bytes (ceiling {MAX_BYTES:,}). "
            "Past roughly this size a reading tool refuses it whole, and the procedure "
            "that updates the tracker orders it loaded IN FULL -- that is how it broke "
            "on 2026-08-03."
        )
    return found


def journal_findings(text: str) -> list[str]:
    """The one reason the frozen journal can be wrong: its line count moved."""
    count = len(text.splitlines())
    if count == JOURNAL_FROZEN_LINES:
        return []
    verb = "grew to" if count > JOURNAL_FROZEN_LINES else "shrank to"
    return [
        f"{_JOURNAL} {verb} {count:,} lines (frozen at {JOURNAL_FROZEN_LINES:,} on "
        f"2026-09-06, because nobody read it). A new section goes to {_HOME}, or to its "
        f"own file under reviews/ when it is an investigation; a removed section breaks a "
        f"citation by title -- put it back."
    ]


def check(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    found = findings(text)
    journal = path.with_name(_JOURNAL)
    if journal.is_file():
        found.extend(journal_findings(journal.read_text(encoding="utf-8")))
    if found:
        for line in found:
            print(line, file=sys.stderr)
        print(
            f"\n{len(found)} finding(s). The tracker is a STATE, not a journal: a `done` "
            f"line carries no comment, a live line carries one sentence, everything else "
            f"lives in {_HOME}. {_JOURNAL} is frozen.",
            file=sys.stderr,
        )
        return 1
    print(
        f"sprint-status shape OK: {len(text.encode('utf-8')):,} bytes, "
        f"no journal prose on its lines; {_JOURNAL} frozen at {JOURNAL_FROZEN_LINES:,} lines"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tracker", nargs="?", type=Path, default=_DEFAULT)
    args = parser.parse_args()
    if not args.tracker.is_file():
        print(f"tracker not found: {args.tracker}", file=sys.stderr)
        return 1
    return check(args.tracker)


if __name__ == "__main__":
    raise SystemExit(main())
