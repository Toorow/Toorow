"""A criterion may not close on a symbol nothing calls.

Four times on 2026-07-30/31 the same defect was found BY HAND: a writer that
exists, is tested, is cited as proof — and that no production path ever calls.
`core/entity_bindings.record_observation` closed a competitors criterion while
`app.entity_observation_versions` stayed empty; `begin_path` is the AI Path
owner and has one occurrence, its own definition; the scheduler was never armed;
`TOOROW_DB_MODE=bigquery` was a mode the code accepted and did not honour.

The audit already detects the two visible halves of this shape — a component
mounted nowhere, a route the UI never calls. This is the third half, on the
server, and it is the one that lets a completeness criterion be closed against
nothing.

WHY A NAME IS NOT ENOUGH, and this is the whole difficulty:
`external_bq_registration.py` defines its OWN `record_observation` and calls it
twice. Counting `record_observation(` across the tree returns 2 and reads as
alive. The homonym is exactly what hid the defect from a human reading grep
output, and a first version of this script fell into the same hole. Resolution
is therefore per MODULE, through the import graph, not by bare name.

Tests do not count as callers. A function whose only callers are its own tests
is tested, not wired — which is precisely the state that produced a green
criterion over an empty table.

FOUR BUGS THIS SCRIPT HAS ALREADY HAD. They are listed because a detector can
carry the very disease it detects, and three of the four made it report a
FALSE POSITIVE — the failure mode that gets an instrument switched off.

1. Bare-name counting (`grep -c`): the homonym above. Fixed by AST + per-module
   resolution.
2. The home file was excluded wholesale, so `money_evidence.classify_gaps` was
   reported dead while line 161 of its own module calls it. A helper used by its
   own module IS wired.
3. Multi-line `from x import (a, b)` was missed by the regex. Fixed by AST.
4. **Module aliasing, found 2026-08-01, and it produced a live false positive.**
   `server/modules/google-analytics/connector.py:336` does
   ``from core import report_timezone as _rtz`` and line 350 calls
   ``_rtz.resolve_capture(...)``. The import handler read that as "the name
   `_rtz` came from package `core`" and never learned that `_rtz` IS the module
   `report_timezone`, so a genuinely wired function was reported as called by
   nothing — and `reporting-timezone[0]` was listed as a criterion to reopen
   when it was not. Module aliases are now resolved (`from pkg import mod as a`,
   `import pkg.mod as a`, and the fully dotted `pkg.mod.fn(...)` form).

WHAT THIS CHECK CANNOT SEE. Named because a silent blind spot is worse than a
declared one, and because two of these are the shape of defects already in the
tracker:

* **Dynamic dispatch by string** — `getattr(mod, name)`, a registry built from
  `importlib`, a handler picked out of a dict keyed by a string read from the
  database. No AST edge exists, so a genuinely reachable symbol reads as dead
  (false positive) — and, worse, a symbol reachable ONLY that way is not proof
  of anything anyway.
* **Reachability is not execution.** A caller that itself has no caller still
  counts here. `check_cross_source_day_offset` is called by `datamodel.py` and
  passes this check, while the guard on its call site makes it unreachable in
  the case the criterion is about. This script answers "is there an edge", never
  "does the path run". AI-75 (scheduler never armed) and AI-88 (the whole
  file-source chain) are of that second kind and would pass here.
* **Value references count as wiring.** Passing a function into a dispatch table
  or a callback argument is treated as a call, because it usually is one. A
  function merely *mentioned* — assigned to a name nobody reads — therefore
  passes.
* **Routes mounted by spreading a list**, or by a decorator this file does not
  recognise (see `REGISTRARS`). A decorated handler has no AST call site and is
  still reachable; the recognised decorators are handled, an unrecognised one
  would be a false positive.
* **Non-Python callers**: SQL, dbt, a Cloud Scheduler job, a shell entry point.
* **Only `core/…` citations are checked.** Evidence citing a connector module,
  a route or a screen is not resolved here at all.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "server" / "core"
LEDGER = ROOT / "docs" / "product-architecture" / "completeness-ledger.json"

#: Everything that ships. `server/modules` is IN — a connector is production.
SCANNED_ROOTS = ("server", "scripts")

# `core/module.function` or `core.module.function` inside evidence prose.
CITATION = re.compile(r"(?:core/|core\.)([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]{3,})")

#: Decorator attributes that MOUNT a function. `@router.get(...)`, `@mcp.tool()`.
#: Such a handler is reachable and has no AST call site anywhere; without this
#: set every route handler cited as evidence would read as dead.
REGISTRARS = frozenset(
    {
        "get", "post", "put", "patch", "delete", "head", "options",
        "websocket", "route", "api_route", "add_api_route",
        "tool", "resource", "prompt", "middleware", "on_event",
        "exception_handler", "task", "command", "register", "callback",
    }
)


def _is_test(path: Path) -> bool:
    """Test files, precisely.

    The first version said ``"test" in path.name``, which also swallows any
    production module whose name merely *contains* the substring — `latest_*.py`
    being the obvious one. None exists today (`find server scripts -name '*.py'
    | grep -v /tests/ | grep test` is empty), so nothing changes now; the rule is
    narrowed so that adding such a file later cannot silently shrink the caller
    search and turn a wired symbol into a finding.
    """
    if "tests" in path.parts:
        return True
    name = path.name
    return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


def _sources() -> Iterator[Path]:
    for root in SCANNED_ROOTS:
        for path in sorted((ROOT / root).rglob("*.py")):
            if _is_test(path) or "__pycache__" in path.parts:
                continue
            yield path


_TREES: dict[Path, ast.Module | None] = {}


def _tree(path: Path) -> ast.Module | None:
    if path not in _TREES:
        try:
            _TREES[path] = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            _TREES[path] = None
    return _TREES[path]


_STEMS: set[str] | None = None


def core_module_stems() -> set[str]:
    """Every `core/<stem>.py` — needed to tell a module alias from a plain name."""
    global _STEMS
    if _STEMS is None:
        _STEMS = {p.stem for p in CORE.rglob("*.py") if not _is_test(p)}
    return _STEMS


def public_definitions() -> dict[tuple[str, str], Path]:
    """(module_stem, function_name) -> file, for every public core function."""
    found: dict[tuple[str, str], Path] = {}
    for path in sorted(CORE.rglob("*.py")):
        if _is_test(path):
            continue
        tree = _tree(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not node.name.startswith("_"):
                    found[(path.stem, node.name)] = path
    return found


def _dotted(node: ast.Attribute) -> str | None:
    """`a.b.c` -> "a.b.c"; None when the base is not a plain name (`f().x`)."""
    parts: list[str] = []
    cur: ast.expr = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


def _walk(root: ast.AST, skip_def: str | None) -> Iterator[ast.AST]:
    """`ast.walk`, except it does not descend into a def named `skip_def`.

    Used on the defining file only: a function that references itself is
    recursive, not wired.
    """
    stack: list[ast.AST] = [root]
    while stack:
        cur = stack.pop()
        yield cur
        for child in ast.iter_child_nodes(cur):
            if (
                skip_def is not None
                and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name == skip_def
            ):
                continue
            stack.append(child)


class Scan:
    """What one file references, and where its imported names come from."""

    __slots__ = ("names", "dotted", "origin", "alias")

    def __init__(self) -> None:
        self.names: set[str] = set()
        self.dotted: set[str] = set()
        #: imported name -> module stem it was imported FROM
        self.origin: dict[str, str] = {}
        #: local binding -> core module stem it IS (`import x as a`)
        self.alias: dict[str, str] = {}


def scan(tree: ast.AST, core_stems: set[str], *, skip_def: str | None = None) -> Scan:
    out = Scan()
    for node in _walk(tree, skip_def):
        if isinstance(node, ast.Name):
            # A bare name only ever matches through `origin`, so a local
            # variable that happens to share a function's name cannot count.
            out.names.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.names.add(node.attr)
            path = _dotted(node)
            if path:
                out.dotted.add(path)
        elif isinstance(node, ast.ImportFrom) and node.module:
            package = node.module.split(".")[-1]
            for item in node.names:
                bound = item.asname or item.name
                if item.name in core_stems:
                    # `from core import report_timezone as _rtz`: the bound name
                    # IS a module. This is bug 4 in the docstring.
                    out.alias[bound] = item.name
                else:
                    out.origin[bound] = package
        elif isinstance(node, ast.Import):
            for item in node.names:
                stem = item.name.split(".")[-1]
                if item.asname:
                    out.alias[item.asname] = stem
                # Plain `import core.report_timezone` binds `core`; usage is then
                # `core.report_timezone.fn(...)`, which the dotted suffix sees.
    return out


_SCANS: dict[Path, Scan] = {}


def _file_scan(path: Path, tree: ast.AST) -> Scan:
    """`scan` for a whole file, memoised — the same files are swept per symbol."""
    if path not in _SCANS:
        _SCANS[path] = scan(tree, core_module_stems())
    return _SCANS[path]


def _references(found: Scan, module: str, name: str) -> bool:
    if name in found.names and found.origin.get(name) == module:
        return True
    for path in found.dotted:
        segments = path.split(".")
        if len(segments) >= 2 and segments[-1] == name:
            owner = segments[-2]
            if owner == module or found.alias.get(owner) == module:
                return True
    return False


def mounted_by_decorator(home: Path, name: str) -> str | None:
    """`@router.post("/x")` mounts a handler that nothing ever calls by name."""
    tree = _tree(home)
    if tree is None:
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                if isinstance(target, ast.Attribute) and target.attr in REGISTRARS:
                    return ast.unparse(target)
    return None


def production_callers(module: str, name: str, home: Path) -> list[str]:
    """Files that genuinely reference `module.name` — imports resolved, tests out.

    A reference inside `home` itself COUNTS: a helper used by its own module is
    wired (bug 2). Its own definition does not (`skip_def`).
    """
    callers: list[str] = []
    for path in _sources():
        tree = _tree(path)
        if tree is None:
            continue
        if path == home:
            found = scan(tree, core_module_stems(), skip_def=name)
            if name in found.names or _references(found, module, name):
                callers.append(path.relative_to(ROOT).as_posix())
            continue
        if _references(_file_scan(path, tree), module, name):
            callers.append(path.relative_to(ROOT).as_posix())
    return callers


def unreachable_evidence() -> list[dict]:
    """Ledger criteria closed by citing a core symbol nothing calls."""
    if not LEDGER.exists():
        return []
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    known = public_definitions()
    findings: list[dict] = []
    for surface, block in ledger.items():
        if surface.startswith("_") or not isinstance(block, dict):
            continue
        for criterion, entry in block.items():
            evidence = entry.get("evidence", "") if isinstance(entry, dict) else ""
            for module, name in sorted(set(CITATION.findall(evidence))):
                home = known.get((module, name))
                if home is None:
                    continue  # cites something that is not a core function
                if mounted_by_decorator(home, name):
                    continue  # registered, not called — reachable all the same
                if not production_callers(module, name, home):
                    findings.append(
                        {
                            "criterion": f"{surface}[{criterion}]",
                            "symbol": f"core/{module}.{name}",
                            "file": home.relative_to(ROOT).as_posix(),
                        }
                    )
    return sorted(findings, key=lambda f: (f["criterion"], f["symbol"]))


def evidence_coverage() -> tuple[int, int]:
    """(criteria carrying a resolvable citation, criteria total) -- AI-171.

    The blind spot this instrument records about ITSELF, turned into a number it
    prints on every run. It resolves SYMBOLS written as ``core.module.name``; an
    entry whose evidence is prose, a test-file name or a bare backticked symbol
    matches nothing, is examined for nothing, and passes.

    NO TOTAL IS WRITTEN HERE, and that is the repair AI-173 asked for. This
    docstring said "94 of 103", frozen on 2026-08-04. The numerator had not
    moved since; the DENOMINATOR had -- 103 criteria became 123 -- so the
    sentence meant to disclose a blind spot was itself out of date, and a reader
    comparing it to the run got two different answers from one instrument.

    A number that the measurement can contradict does not belong in prose beside
    the measurement. The command is the number::

        uv run python scripts/ledger_evidence_reachability.py

    It is REPORTED, not enforced. Failing on prose evidence would reopen the vast
    majority of the ledger in one commit, and a criterion is not wrong merely
    because its proof is a conformance sweep or the ABSENCE of a write --
    reporting-timezone[3] is exactly that, and says so in its own evidence. What
    must stop is a green run READING as coverage it does not have.
    """
    if not LEDGER.exists():
        return (0, 0)
    cited = total = 0
    for _surface, surface_cited, surface_total in coverage_by_surface():
        cited += surface_cited
        total += surface_total
    return (cited, total)


def coverage_by_surface() -> list[tuple[str, int, int]]:
    """`(surface, cited, total)` per surface, worst coverage first -- AI-173.

    The aggregate said 9 of 123 and left nowhere to start. "Make the evidence
    citable" is not a task anyone can pick up; "reporting-timezone: 0 of 7 is"
    -- it names one document, its criteria, and a person who owns it.

    Sorted by what is MISSING, descending, so the top line is the largest single
    piece of the blind spot rather than the alphabetically first.
    """
    if not LEDGER.exists():
        return []
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    rows: list[tuple[str, int, int]] = []
    for surface, block in ledger.items():
        if surface.startswith("_") or not isinstance(block, dict):
            continue
        cited = total = 0
        for entry in block.values():
            evidence = entry.get("evidence", "") if isinstance(entry, dict) else ""
            total += 1
            if CITATION.search(evidence):
                cited += 1
        if total:
            rows.append((surface, cited, total))
    return sorted(rows, key=lambda row: (row[1] - row[2], row[0]))


def main() -> int:
    findings = unreachable_evidence()
    cited, total = evidence_coverage()
    if total:
        print(
            f"Evidence carrying a resolvable core citation: {cited}/{total} criteria "
            f"({cited * 100 // total}%). The other {total - cited} are examined for "
            "NOTHING here -- prose, test-file names and bare symbols match no citation."
        )
        # AI-173: the aggregate names no owner. This does -- worst first, so the
        # top line is the biggest single piece of the blind spot.
        print("")
        print("  Per surface, largest gap first (uncited/total):")
        for surface, surface_cited, surface_total in coverage_by_surface():
            missing = surface_total - surface_cited
            if not missing:
                continue
            print(f"    {surface:44s} {missing:3d}/{surface_total}")
    if not findings:
        print("Every ledger-cited core symbol has a production caller.")
        return 0
    print(f"{len(findings)} criterion/criteria closed on a symbol nothing calls:\n")
    for finding in findings:
        print(f"  {finding['criterion']:24s} {finding['symbol']}")
        print(f"  {'':24s}   defined in {finding['file']}, called by no production path")
    print(
        "\nA criterion closed on an uncalled writer is a green light over an empty "
        "table. Wire it, or reopen the criterion (remove its entry: the ledger's "
        "own rule is that an unrecorded criterion counts as still incomplete)."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
