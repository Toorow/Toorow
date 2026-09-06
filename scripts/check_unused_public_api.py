r"""Name the public functions of `server/core` that no production code calls.

WHY THIS EXISTS. Measured on 2026-08-08, four repairs in one day had the same
shape, and none of them was a bug: the code existed and NOTHING CALLED IT.

  * the five `dbt/seeds/feetax/` seeders -- written 2026-07-27, never invoked, and
    40 dbt tests were red because of it (AI-165);
  * `seed_fx_source_currency_bindings` (then named `seed_fx_conflict_resolutions`,
    after a table it does not write) -- same, and the 41st test with it (AI-163);
  * `apply_migrations.py --verify-complete` -- the ledger checker, run twice by
    hand and never automatically (AI-241);
  * `core.ai_paths` -- whose own caller had to be written a whole story later, and
    says so in its docstring: "shipped the whole owner and nothing ever called it".

A missing caller rougit nowhere. A test suite is green, a build is green, and the
feature does not exist -- which is exactly the class this repository keeps
rediscovering by hand, one action item at a time. This script is that hand,
mechanised.

WHAT IT PROVES IT CAN FIND. Run against the tree on 2026-08-08 it named, without
being told about them, three defects three DIFFERENT sessions had each found by
reading: `record_gate_confirmation` (AI-92), `compare_boundaries` (AI-167) and
`propose_missing_link` (story 45.8). That is the calibration; a fourth,
`resolve_row_geography`, was not tracked by anyone.

WHAT IT IS NOT. Not a dead-code detector, and it must not be read as one. A
public function with no caller is often work landed ahead of its surface -- the
normal state of a repository under construction. So the total is a RATCHET, not a
red line: it may not grow, and it must come down when a caller lands.

    python scripts/check_unused_public_api.py           # the list, exit 0
    python scripts/check_unused_public_api.py --gate    # refuse growth

HOW IT READS, AND THE TWO THINGS THAT WOULD MAKE IT LIE:

  * a name reached by `getattr(module, "name")` or through a registry string is
    counted as referenced -- string constants are collected on purpose, so a
    dispatch table does not turn its own targets into false orphans;
  * a DECORATED def is skipped entirely. `@mcp.tool`, `@app.route` and their kin
    register the function without ever naming it again, so every one of them
    would be a false positive.

Both limits are the same one, stated twice: this reads the shapes it was taught.
It is a net under the common case, never a proof of deadness -- the same honesty
`AI-232` records for the firing-message guard.
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import sys
from collections import defaultdict

REPO = pathlib.Path(__file__).resolve().parents[1]
SERVER = REPO / "server"
CORE = SERVER / "core"

#: The trees that count as PRODUCTION. A caller living in `server/tests` does not
#: make a function served -- that is the whole point of the measure.
PRODUCTION_TREES = ("core", "inbound", "modules")

#: Recorded total. SHRINK-ONLY, and the second guard below refuses a number left
#: too high after a caller lands -- the half the dbt ratchet did not have, which is
#: why it sat at 41 for twelve days.
#:
#:     182  2026-08-08  mesure d'origine
#:     181  2026-08-09  `record_gate_confirmation` retiree (AI-92) : ce n'etait pas
#:                      un appelant manquant, c'etait un SECOND ecrivain pour une
#:                      seule confirmation, et le premier a retirer que ce script
#:                      ait lui-meme nomme.
RECORDED_TOTAL = 181


def _is_test_helper(name: str) -> bool:
    """A helper that exists FOR the tests is not an orphan, it is a fixture."""
    return name.endswith("_for_tests") or name.endswith("_for_test")


def public_defs(path: pathlib.Path) -> list[tuple[str, int]]:
    """Module-level public defs of *path*, decorated ones excluded."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        (node.name, node.lineno)
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
        and not node.decorator_list
        and not _is_test_helper(node.name)
    ]


def referenced(paths: list[pathlib.Path]) -> dict[str, int]:
    """Every name, attribute tail and string constant mentioned in *paths*."""
    seen: dict[str, int] = defaultdict(int)
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # a file that does not parse is not evidence
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                seen[node.id] += 1
            elif isinstance(node, ast.Attribute):
                seen[node.attr] += 1
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                seen[node.value] += 1
    return seen


def unused() -> list[tuple[str, int, str, int]]:
    """``(file, line, name, test_mentions)`` for every uncalled public def."""
    production = referenced([
        path
        for tree in PRODUCTION_TREES
        if (SERVER / tree).is_dir()
        for path in (SERVER / tree).rglob("*.py")
    ])
    tests = referenced(list((SERVER / "tests").rglob("*.py")))
    found = []
    for path in sorted(CORE.glob("*.py")):
        for name, lineno in public_defs(path):
            # `> 0` and not `> 1`: a def is not a reference to itself under this
            # walk, so any count at all means somebody names it.
            if production.get(name, 0):
                continue
            found.append((path.name, lineno, name, tests.get(name, 0)))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", action="store_true", help="refuse growth")
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args(argv)

    found = unused()
    tested = [row for row in found if row[3]]
    print(
        f"public defs de server/core que rien n'appelle en production : {len(found)}"
        f"  (dont {len(tested)} nommees par les tests -- ecrites, eprouvees, "
        f"jamais servies ; {len(found) - len(tested)} nommees nulle part)"
    )
    for file, lineno, name, mentions in sorted(found, key=lambda r: -r[3])[: args.limit]:
        print(f"  {file}:{lineno} {name}  (tests: {mentions})")
    if len(found) > args.limit:
        print(f"  ... et {len(found) - args.limit} autres (--limit 0 pour tout voir)")

    if not args.gate:
        return 0
    if len(found) > RECORDED_TOTAL:
        print(
            f"\nREFUSE: {len(found)} depasse le total enregistre ({RECORDED_TOTAL}). "
            "Une fonction publique de plus que personne n'appelle.",
            file=sys.stderr,
        )
        return 1
    if len(found) < RECORDED_TOTAL:
        print(
            f"\nREFUSE: {len(found)} est SOUS le total enregistre "
            f"({RECORDED_TOTAL}) -- un appelant a atterri, baissez le chiffre dans "
            "ce fichier. Un cliquet qui ne descend jamais est un plafond.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
