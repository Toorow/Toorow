#!/usr/bin/env python3
"""Where sessions collide, and where unrelated subjects share a file -- AD-42.

WHY THIS EXISTS, AND WHAT IT IS NOT. `CLAUDE.md` carried *no file beyond 1 000
lines* and 161 files broke it. Length was never the defect. Jean named the two
that are, 2026-08-12:

    "que tu edites toujours au meme endroit et que ca genere des conflits"
    "pas melanger des choses qui ont rien a voir"

Both are properties of HISTORY, not of a line count -- and they are two DIFFERENT
illnesses that look identical if you only count edits:

  * A CROSSROADS. Every subject appends one line to a central list. Measured:
    many distinct subjects, a SMALL median churn. `core/audit.py` -- 97 `ACTION_`
    constants, 43 edits from 29 subjects, median 4 lines. Dividing it repairs
    nothing: whatever file the list lives in, everybody still has to open it.
    THE CURE IS TO INVERT THE REGISTRATION -- each subject declares itself in its
    own file and the centre only collects. The repo already does this three
    times: `register(mcp)`, the route collections `admin_api` splices, and
    `run_origins`.

  * A MIXTURE. Different subjects rewrite logic in the same file. Measured: many
    distinct subjects, a LARGE median churn. `core/cards.py` -- 32 subjects,
    median 102 lines rewritten. THE CURE IS TO DIVIDE BY RESPONSIBILITY. This is
    the one that produces bugs, because two subjects that never met share a
    scope.

A file can be both, which is what `admin_api.py` (207 edits, 144 subjects) and
`core/main.py` (127 edits, 103 subjects) were -- and why both were split.

A LOW MEDIAN IS A SHAPE, NOT A DIAGNOSIS. `ContentRouter.tsx` measured as a
crossroads (35 subjects, median 14) and was a MIXTURE: four jobs inside one
613-line function, each subject adding a small branch to whichever job was its
own. Before prescribing an inversion, read where the diff hunks land --
concentrated in one table means invert, spread across several jobs means divide.
`--hunks <file>` prints that distribution.

THE THIRD SIGNAL, weaker and local: a long function divides on branch DENSITY,
never on length. `(if+for+while+try) / lines`; at or above 5 % it is several
programs in one scope, below 3 % it is a list or an assembly and cutting it moves
the same lines behind more names. Seven functions in `server/` are long and flat,
and AD-42 declares them justified.

    python scripts/division_candidates.py              # both illnesses
    python scripts/division_candidates.py --functions  # + density and criterion 10
    python scripts/division_candidates.py --registrations  # criterion 10 alone
    python scripts/division_candidates.py --json       # for a gate
    python scripts/division_candidates.py --since 2026-07-01

WHAT IS OUT OF SCOPE, and argued in AD-42 rather than skipped as too hard:
`server/modules/*/connector.py` (one connector, one file, the same shape 39
times -- the size is the provider's API surface), `server/tests/**`,
`dbt/target/**`, and applied migrations.
"""

from __future__ import annotations

import argparse
import ast
import collections
import json
import pathlib
import re
import statistics
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]

#: A conventional-commit scope is the closest thing this repo has to "a subject".
_SCOPE = re.compile(r"^(\w+)\(([^)]+)\)")

#: Extensions a division could act on. Everything else is data or generated.
_SOURCE = (".py", ".ts", ".tsx")
_IGNORED = ("/node_modules/", "/target/", "/__pycache__/", "/.venv/")

#: Out of scope, and ARGUED in AD-42 rather than skipped as too hard. A test file
#: is one subject's cases; a connector is one connector, one file, the same shape
#: 39 times -- the size is the provider's API surface, not a mixture. Both are
#: edited by many subjects and neither has a division that would help.
_OUT_OF_SCOPE = re.compile(r"(^|/)tests?/|/modules/[^/]+/connector\.py$")

#: A file nobody else edits is nobody's coordination problem, whatever its size.
MIN_SUBJECTS = 8

#: Median added+deleted lines per edit. Below: everyone appends one line to a
#: list. Above: everyone rewrites logic. The two thresholds are read off the
#: measured tree, not chosen: `App.tsx` and `navigation.ts` sit at 11-13 and are
#: registries; `queue.py` and `scheduler.py` sit at 46-50 and are engines.
APPEND_MEDIAN = 15
REWRITE_MEDIAN = 35

#: Function-level density bands (the local signal).
MIN_FN_LINES = 250
DIVIDE = 5.0
JUDGE = 3.0

FN_ROOTS = ("server/core", "server/inbound", "scripts")

#: Where MCP tools are bound. Criterion 10 of `module-boundaries.md` is about
#: these bodies, and it is measured here rather than by eye.
REG_ROOTS = ("server/core", "server/inbound", "server/modules")

#: The binder's name is not always `register`: `inbound_mcp` calls its own
#: `register_inbound_tools`, and criterion 10 means it too.
_BINDER = re.compile(r"^register(_|$)")


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout


def _tracked(path: str) -> bool:
    if not path.endswith(_SOURCE) or any(bad in path for bad in _IGNORED):
        return False
    return not _OUT_OF_SCOPE.search(path)


def contention(since: str) -> list[dict]:
    """Per file: how many subjects opened it, and how much each one changed."""
    log = _git("log", f"--since={since}", "--numstat", "--pretty=format:@@%s")
    subjects: dict[str, set[str]] = collections.defaultdict(set)
    churn: dict[str, list[int]] = collections.defaultdict(list)
    current = "(no scope)"
    for line in log.split("\n"):
        if line.startswith("@@"):
            subject = line[2:]
            match = _SCOPE.match(subject)
            current = match.group(2) if match else subject.split(":")[0][:24]
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        if not _tracked(path) or not added.isdigit() or not deleted.isdigit():
            continue
        subjects[path].add(current)
        churn[path].append(int(added) + int(deleted))

    rows = []
    for path, seen in subjects.items():
        if len(seen) < MIN_SUBJECTS:
            continue
        values = sorted(churn[path])
        median = statistics.median(values)
        if median <= APPEND_MEDIAN:
            illness, cure = "crossroads", "invert the registration"
        elif median >= REWRITE_MEDIAN:
            illness, cure = "mixture", "divide by responsibility"
        else:
            illness, cure = "both", "invert what is a list, divide what is logic"
        rows.append(
            {
                "file": path,
                "edits": len(values),
                "subjects": len(seen),
                "median_churn": int(median),
                "illness": illness,
                "cure": cure,
            }
        )
    rows.sort(key=lambda r: (-r["subjects"], -r["edits"]))
    return rows


def _density(node: ast.AST, lines: int) -> tuple[int, float]:
    branches = sum(
        1
        for child in ast.walk(node)
        if isinstance(child, (ast.If, ast.For, ast.While, ast.Try))
    )
    return branches, (branches / lines * 100 if lines else 0.0)


def long_functions() -> list[dict]:
    found = []
    for root in FN_ROOTS:
        base = REPO / root
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in str(path):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
            except SyntaxError:
                # A neighbouring session mid-edit. Not this script's business.
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                lines = node.end_lineno - node.lineno + 1
                if lines < MIN_FN_LINES:
                    continue
                branches, density = _density(node, lines)
                found.append(
                    {
                        "file": str(path.relative_to(REPO)).replace("\\", "/"),
                        "name": node.name,
                        "line": node.lineno,
                        "lines": lines,
                        "branches": branches,
                        "density": round(density, 1),
                        "verdict": (
                            "divide"
                            if density >= DIVIDE
                            else "judge"
                            if density >= JUDGE
                            else "justified"
                        ),
                    }
                )
    found.sort(key=lambda f: (-f["density"], -f["lines"]))
    return found


# ---------------------------------------------------------------------------
# Criterion 10 -- what a `register(mcp)` body declares, and whether the
# declaration is READABLE. AD-42 states the cure in one line: "One tool, one
# module-level function; `register` keeps the wiring. It carries no behaviour at
# all." Two different things are therefore measured here, and they are not the
# same defect:
#
#   * HOW MANY tools a body binds, and how much of the tool's BODY still lives
#     inside the binder. The criterion's own numbers. Binding several tools of
#     ONE surface is not by itself an illness -- `context_hub` binds seven -- so
#     this half is a census that stays open until the bodies are hoisted.
#   * WHETHER each binding NAMES its tool. This half is a defect wherever it
#     appears, and not a matter of taste: three instruments read the handler out
#     of the AST as `call.args[1].id` --
#     `scripts/check_legacy_analytics_migration.py` (the producer census),
#     `server/tests/conformance/test_mcp_tools_resolve_project_scope.py` (the
#     project-scope ratchet) and this script. A `for handler in (...)` loop hands
#     all three the loop VARIABLE, so every tool it binds becomes the anonymous
#     `#tool:handler` and disappears from each of them -- silently, and reported
#     as compliance rather than as a gap. Both cases were measured: the census on
#     2026-08-30 (`daily_insight_mcp`), the ratchet on 2026-08-28
#     (`project_capabilities_mcp`, which carries the note in its own body).
#
# The rule is therefore: every registration is a LITERAL
# `register_profiled(mcp, <function_name>, ...)` call -- no loop, no
# comprehension, no local alias.
# `server/tests/conformance/test_register_bodies_name_their_tools.py` imports
# `registrations()` rather than deriving a second copy of it.
# ---------------------------------------------------------------------------


def _local_bindings(binder: ast.AST) -> set[str]:
    """Names ASSIGNED inside the binder -- a handler among them has no readable name."""
    bound: set[str] = set()
    for node in ast.walk(binder):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.comprehension) and isinstance(node.target, ast.Name):
            bound.add(node.target.id)
    return bound


def _repeated_nodes(binder: ast.AST) -> set[int]:
    """Every node that sits inside a loop or a comprehension of this binder."""
    inside: set[int] = set()
    for node in ast.walk(binder):
        if isinstance(
            node,
            (
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.ListComp,
                ast.SetComp,
                ast.DictComp,
                ast.GeneratorExp,
            ),
        ):
            for child in ast.walk(node):
                if child is not node:
                    inside.add(id(child))
    return inside


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def registrations() -> list[dict]:
    """Per `register(mcp)` body: the tools it binds, and every unreadable binding."""
    rows = []
    for root in REG_ROOTS:
        base = REPO / root
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            posix = path.as_posix()
            if "__pycache__" in posix or _OUT_OF_SCOPE.search(posix):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                # A neighbouring session mid-edit. Not this script's business.
                continue
            for binder in ast.walk(tree):
                if not isinstance(binder, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not _BINDER.match(binder.name):
                    continue
                local = _local_bindings(binder)
                repeated = _repeated_nodes(binder)
                tools: list[str] = []
                defects: list[dict] = []
                for node in ast.walk(binder):
                    if not isinstance(node, ast.Call):
                        continue
                    if _call_name(node) != "register_profiled":
                        continue
                    if len(node.args) < 2:
                        defects.append(
                            {
                                "line": node.lineno,
                                "why": "the handler is not the second positional argument",
                            }
                        )
                        continue
                    handler = node.args[1]
                    if not isinstance(handler, ast.Name):
                        defects.append(
                            {
                                "line": node.lineno,
                                "why": f"the handler is a {type(handler).__name__}, "
                                "not a name the census can read",
                            }
                        )
                        continue
                    tools.append(handler.id)
                    if id(node) in repeated:
                        defects.append(
                            {
                                "line": node.lineno,
                                "why": "registered inside a loop -- every tool it "
                                f"binds reaches the census as {handler.id!r}",
                            }
                        )
                    elif handler.id in local:
                        defects.append(
                            {
                                "line": node.lineno,
                                "why": f"the handler {handler.id!r} is a local alias, "
                                "not the tool's own name",
                            }
                        )
                if not tools and not defects:
                    continue
                named = set(tools)
                inline = [
                    node
                    for node in ast.walk(binder)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node is not binder
                    and node.name in named
                ]
                rows.append(
                    {
                        "file": str(path.relative_to(REPO)).replace("\\", "/"),
                        "binder": binder.name,
                        "line": binder.lineno,
                        "tools": sorted(tools),
                        "count": len(tools),
                        "declared_inline": len(inline),
                        "inline_lines": sum(
                            node.end_lineno - node.lineno + 1 for node in inline
                        ),
                        "defects": defects,
                    }
                )
    rows.sort(key=lambda row: (-row["count"], row["file"]))
    return rows


def _print_registrations(rows: list[dict]) -> None:
    """Criterion 10, and ONLY criterion 10, first.

    The report used to lead with "N bodies declare more than one tool", ranked
    every binder by that number, and printed the criterion's two actual halves
    at the bottom. AI-332 (arbitrated 2026-08-31) says in so many words that
    binding several tools of ONE surface is NOT this defect and is not asked to
    stop -- so the loudest number in the output was a non-defect, and a reader
    coming to check the criterion read a ranking of something nobody has to fix.
    The two halves now lead, each with its own verdict, and the multi-tool
    ranking follows under a heading that says what it is.
    """

    multi = [row for row in rows if row["count"] > 1]
    inline = [row for row in rows if row["declared_inline"]]
    defective = [row for row in rows if row["defects"]]
    total = sum(row["count"] for row in rows)
    inline_tools = sum(row["declared_inline"] for row in inline)
    inline_lines = sum(row["inline_lines"] for row in inline)
    unreadable = sum(len(row["defects"]) for row in defective)

    print("\nCriterion 10 -- one tool, one module-level function; and a")
    print("registration NAMES its tool.")
    print(f"  scope: {len(rows)} binders, {total} tools.\n")

    print(
        f"  BODY  {len(inline)} binder(s) hold a tool BODY inside themselves "
        f"({inline_tools} tools, {inline_lines} lines)"
    )
    print(
        "        "
        + (
            "-> OPEN. AD-42 asks the binder to keep its WIRING only."
            # At zero the old unconditional sentence contradicted the number it
            # followed (found the day the last binder was hoisted, 2026-08-31).
            if inline
            else "-> CLOSED. Every register keeps its wiring only."
        )
    )
    print(f"  NAME  {unreadable} binding(s) in {len(defective)} binder(s) hide their tool")
    print(
        "        "
        + (
            "-> OPEN. A loop or an alias hands the census the loop variable, so"
            "\n           the tools vanish from the producer census AND from the"
            "\n           project-scope ratchet, reported as compliance."
            if defective
            else "-> CLOSED. Every registration is a literal call naming its tool."
        )
    )

    offenders = [row for row in rows if row["declared_inline"] or row["defects"]]
    if offenders:
        print(f"\n  the {len(offenders)} binder(s) the criterion names:")
        print(f"  {'tools':>5} {'inline':>6} {'lines':>6}  binder")
        for row in offenders:
            print(
                f"  {row['count']:>5} {row['declared_inline']:>6} {row['inline_lines']:>6}"
                f"  {row['file']}:{row['line']} :: {row['binder']}"
            )
            print(f"                {', '.join(row['tools'])}")
            for defect in row["defects"]:
                print(f"             !! line {defect['line']}: {defect['why']}")

    print(
        f"\nContext, and NOT a defect -- AD-42 as amended by AI-332, 2026-08-31:"
        f"\nbinding several tools of ONE surface is not this criterion, and this"
        f"\ndocument does not ask for it to stop. {len(multi)} binder(s) declare more"
        f"\nthan one tool, carrying {sum(row['count'] for row in multi)}:"
    )
    print(f"  {'tools':>5}  binder")
    for row in multi:
        print(f"  {row['count']:>5}  {row['file']}:{row['line']} :: {row['binder']}")
        print(f"                {', '.join(row['tools'])}")


def _hunks(path: str, since: str) -> int:
    """Which LINES of one file the edits touched, bucketed by hundreds.

    A file whose hunks pile into one region is a table everybody appends to.
    One whose hunks spread is several jobs sharing a scope -- which is what
    `ContentRouter.tsx` turned out to be, against its own median.
    """
    log = _git("log", f"--since={since}", "-U0", "--pretty=format:@@", "--", path)
    buckets: collections.Counter = collections.Counter()
    total = 0
    for line in log.split(chr(10)):
        match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)", line)
        if match:
            buckets[int(match.group(1)) // 100 * 100] += 1
            total += 1
    if not total:
        print(f"no edit to {path} since {since}")
        return 0
    print(f"{total} hunks in {path} since {since}")

    widest = max(buckets.values())
    for start in sorted(buckets):
        count = buckets[start]
        bar = "#" * max(1, round(count / widest * 40))
        print(f"  lines {start:>5}-{start + 99:<5} {count:>4}  {bar}")
    print()
    print("Piled into one region -> a table: invert the registration.")
    print("Spread across regions  -> several jobs in one scope: divide.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--since", default="2026-06-01", help="history window")
    parser.add_argument("--functions", action="store_true", help="density signal too")
    parser.add_argument(
        "--registrations",
        action="store_true",
        help="criterion 10 -- what each register(mcp) body declares, and every "
        "binding that hides its tool's name from the census",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable")
    parser.add_argument(
        "--hunks",
        metavar="FILE",
        help="where the edits landed INSIDE one file -- the step that tells a "
        "crossroads from a mixture",
    )
    args = parser.parse_args()

    if args.hunks:
        return _hunks(args.hunks, args.since)

    rows = [] if args.registrations and not args.json else contention(args.since)
    result = {"since": args.since, "contention": rows}
    if args.functions or args.json:
        result["functions"] = long_functions()
    if args.functions or args.registrations or args.json:
        result["registrations"] = registrations()

    if args.json:
        print(json.dumps(result, indent=1))
        return 0

    if args.registrations:
        _print_registrations(result["registrations"])
        print(
            "\nThe rule and every exclusion: "
            "docs/product-architecture/module-boundaries.md"
        )
        return 0

    print(f"Where sessions collide, since {args.since}")
    print("  subjects = distinct commit scopes that opened the file")
    print("  churn    = median added+deleted lines per edit\n")
    print(f"{'subj':>5} {'edits':>6} {'churn':>6}  {'illness':<10} {'cure':<44} file")
    for row in rows:
        print(
            f"{row['subjects']:>5} {row['edits']:>6} {row['median_churn']:>6}"
            f"  {row['illness']:<10} {row['cure']:<44} {row['file']}"
        )

    counts = collections.Counter(row["illness"] for row in rows)
    print(
        f"\n{len(rows)} files opened by {MIN_SUBJECTS}+ subjects: "
        f"{counts['crossroads']} crossroads, {counts['mixture']} mixtures, "
        f"{counts['both']} both."
    )

    if args.functions:
        bands = collections.Counter(f["verdict"] for f in result["functions"])
        print(
            f"\nAnd inside a function, on branch density: {bands['divide']} to divide, "
            f"{bands['judge']} to judge, {bands['justified']} long but flat "
            "(justified -- leave them).\n"
        )
        for entry in result["functions"]:
            if entry["verdict"] == "justified":
                continue
            print(
                f"  {entry['density']:>5.1f} %  {entry['lines']:>4} l."
                f"  {entry['branches']:>3} br.   {entry['file']}:{entry['line']}"
                f" :: {entry['name']}"
            )

        _print_registrations(result["registrations"])

    print("\nThe rule and every exclusion: docs/product-architecture/module-boundaries.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
