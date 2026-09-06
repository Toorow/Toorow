#!/usr/bin/env python3
"""Where the console still composes a name and falls back to an identifier.

WHY THIS IS A SCRIPT AND NOT A `grep`. The census this replaces was one grep
line recorded in `docs/product-architecture/known-debt.json`, and grep reads one
line at a time. A fallback written across two lines --

    name: businessDomains.find((d) => d.id === version.business_domain_id)?.name
      ?? version.business_domain_id,

-- is invisible to it, and two of them were. One sat two lines above a clause
the same commit had just repaired: the instrument could not see the defect it
was standing in. It also counted LINES and reported them as SITES, so a line
carrying two fallbacks counted once.

This reads whole files. A `??` or `||` between a name-ish word and an
identifier-ish expression is a site, however it is wrapped, and two on one line
are two.

WHAT IT DOES NOT DECIDE. Every hit is a fallback the console can only take
because the server served no word for that object; whether that read OWES a name
is an arbitration recorded in `known-debt.json`, not a verdict for this script.
It counts, prints, and exits 0. `--fail-over N` makes it a ratchet when the
arbitration lands.

    python scripts/browser_name_fallback_census.py
    python scripts/browser_name_fallback_census.py --fail-over 47
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: The trees the console is served from. `node_modules` is walked out of them
#: because the card workspaces symlink `ui/cards/shell` into ten packages: the
#: same two files would otherwise be counted eleven times.
ROOTS = ("ui/admin/src", "ui/cards", "ui/shell/src", "ui/widgets", "web/src")

#: A word a person reads, a nullish fallback, and something that ends in an id.
#: `\s*` spans newlines here, which is the whole difference from the grep.
FALLBACK = re.compile(
    r"(?:name|label|title|display_name)[A-Za-z_]*\s*(?:\?\?|\|\|)\s*[A-Za-z_.?]*(?:_id|Id)\b"
)


def _sources() -> list[Path]:
    found: list[Path] = []
    for root in ROOTS:
        for path in sorted((REPO / root).rglob("*")):
            if path.suffix not in (".ts", ".tsx") or not path.is_file():
                continue
            relative = path.relative_to(REPO).as_posix()
            if "node_modules" in relative or "test" in relative.lower():
                continue
            found.append(path)
    return found


def census() -> list[tuple[str, int, bool, str]]:
    """(file, line, spans a line break, the expression) for every site."""
    sites: list[tuple[str, int, bool, str]] = []
    for path in _sources():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for found in FALLBACK.finditer(text):
            line = text.count("\n", 0, found.start()) + 1
            sites.append(
                (
                    path.relative_to(REPO).as_posix(),
                    line,
                    "\n" in found.group(),
                    " ".join(found.group().split()),
                )
            )
    return sites


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fail-over",
        type=int,
        default=None,
        help="exit 1 when the census exceeds this many sites",
    )
    parser.add_argument("--quiet", action="store_true", help="print the counts only")
    arguments = parser.parse_args()

    sites = census()
    files = {site[0] for site in sites}
    wrapped = [site for site in sites if site[2]]

    if not arguments.quiet:
        for path, line, multiline, source in sites:
            print(f"{'WRAPPED ' if multiline else '        '}{path}:{line}  {source}")
        print()
    print(f"{len(sites)} sites in {len(files)} files")
    print(f"{len(wrapped)} of them span a line break and are invisible to a line-by-line grep")

    if arguments.fail_over is not None and len(sites) > arguments.fail_over:
        print(
            f"REFUSED: {len(sites)} sites, over the {arguments.fail_over} recorded in "
            "docs/product-architecture/known-debt.json",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
