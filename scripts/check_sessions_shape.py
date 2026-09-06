"""Refuse a SESSIONS.md that has turned back into a journal.

`SESSIONS.md` says WHO WRITES WHAT, NOW, so that parallel sessions do not edit the
same file. Its own header (2026-07-31) says a line older than a day is dead and a
"TERMINE, je ne tiens plus rien" line coordinates nothing. It was pruned at 527
lines that day. On 2026-09-06 it held 4 151 lines and 303 Ko: forty-five dated
entries of finished work and a "Sessions actives" table whose every row said
TERMINE since 2026-08-04. A rule written in a header is a wish; this guard is the
mechanism, in the shape of `check_sprint_status_shape.py`: one command, a named
failure, and a repair the reader can perform.

    python scripts/check_sessions_shape.py

WHAT IT REFUSES:

  1. a dated entry -- `## 2026-..`, `> **2026-..`, `| 2026-..` -- older than
     MAX_AGE_DAYS: the session is gone, its line coordinates nothing;
  2. a dated entry whose first line says TERMINE / TERMINEE / FERME: finished work
     lives in its commit, its story file or its action item, not here;
  3. the total size, last -- the symptom, checked so that a class of growth nobody
     predicted still fires something.

WHAT IT DOES NOT DO: judge whether an undated section (rules, "Tranches", "Acquis")
still earns its place. That is a reading, not a measurement.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

_DEFAULT = Path("SESSIONS.md")

#: The header says one day. Three leaves a weekend, and a guard that fires on the
#: state it just created is a guard people learn to ignore.
MAX_AGE_DAYS = 3

#: After the 2026-09-06 prune the file weighed about 50 Ko. Twice that is headroom.
MAX_BYTES = 100_000

_ARCHIVE = "_bmad-output/archive/sessions-journal-2026-07-31-to-2026-09-05.md"
_DATED = re.compile(r"^(?:## |> \*\*|\| )(?:⚠️ |INCIDENT )?(\d{4}-\d{2}-\d{2})")
_FINISHED = re.compile(r"TERMIN[ÉE]E?|\bFERM[ÉE]E?\b", re.IGNORECASE)


def findings(text: str, today: dt.date) -> list[str]:
    """Every reason the file no longer says who writes what now, in file order."""
    found: list[str] = []
    for i, line in enumerate(text.split("\n")):
        match = _DATED.match(line)
        if not match:
            continue
        try:
            day = dt.date.fromisoformat(match.group(1))
        except ValueError:
            continue
        age = (today - day).days
        if age > MAX_AGE_DAYS:
            found.append(
                f"L{i + 1} is dated {day} ({age} days old, max {MAX_AGE_DAYS}). The session is "
                f"gone: remove the entry; what it found lives in its commit, story file or action "
                f"item. Verbatim history goes to {_ARCHIVE}."
            )
        elif _FINISHED.search(line):
            found.append(
                f"L{i + 1} says the work is finished. A finished line coordinates nothing -- "
                f"remove it in the commit that carries the last change."
            )
    size = len(text.encode("utf-8"))
    if size > MAX_BYTES:
        found.append(
            f"the file is {size:,} bytes (ceiling {MAX_BYTES:,}). Past this it is a journal "
            f"again, and three sessions once searched it for their own line among closed dialogues."
        )
    return found


def check(path: Path, today: dt.date) -> int:
    text = path.read_text(encoding="utf-8")
    found = findings(text, today)
    if found:
        for line in found:
            print(line, file=sys.stderr)
        print(
            f"\n{len(found)} finding(s). SESSIONS.md says who writes what NOW: a dated entry "
            f"older than {MAX_AGE_DAYS} days or marked finished is removed, not kept.",
            file=sys.stderr,
        )
        return 1
    print(f"SESSIONS.md shape OK: {len(text.encode('utf-8')):,} bytes, no dead entry")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=_DEFAULT)
    parser.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    args = parser.parse_args()
    if not args.path.is_file():
        print(f"file not found: {args.path}", file=sys.stderr)
        return 1
    return check(args.path, args.today)


if __name__ == "__main__":
    raise SystemExit(main())
