"""Where a test's fake cursor recognizes SQL by its TEXT, and what it does when it fails to.

A fake cursor that dispatches on a fragment of the product's SQL is a copy of
that query living in the test. When the product rewrites the query -- an alias,
a join, a reordered projection -- the fragment stops matching. If the fake then
falls through to a silent default (no rows, `description = None`), the test
keeps passing while asserting about a code path it never exercised, or it fails
somewhere far away with a `NoneType` error that names the product instead of the
fixture. That is the class opened as AI-317, found by the competitor fan-out
repair (c9d740cb).

This census walks `server/tests` with the AST and reports, per fake cursor:

  * the statements it recognizes (the literal fragments it matches on), and
  * whether an unrecognized statement is LOUD (a raise the tester can read) or
    MUTE (falls off the end of the chain and answers "no rows").

Only the second column is a defect. A textual fake that raises on the unknown
is honest: it tells you which query moved.

Usage:
    python scripts/fake_cursor_census.py            # summary + the mute list
    python scripts/fake_cursor_census.py --json     # machine-readable
    python scripts/fake_cursor_census.py --loud     # list the loud ones too
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "server" / "tests"

# A name that, when a string is compared against or searched inside it, means
# the fake is dispatching on the product's SQL rather than on its own state.
SQL_NAMES = {
    "sql",
    "query",
    "statement",
    "stmt",
    "flat",
    "text",
    "normalized",
    "lowered",
    "q",
    "s",
}


@dataclass
class FakeCursor:
    path: str
    class_name: str
    line: int
    fragments: list[str] = field(default_factory=list)
    verdict: str = "mute"
    reason: str = ""
    tests_in_file: int = 0


def _is_sql_expression(node: ast.AST, names: frozenset[str] = frozenset()) -> bool:
    """Does this expression evaluate to the statement text the fake was handed?"""

    known = SQL_NAMES | set(names)
    while isinstance(node, ast.Call):
        # sql.lower(), " ".join(sql.split()), sql.strip(), str(sql) ...
        if isinstance(node.func, ast.Attribute):
            if node.func.attr == "join" and node.args:
                node = node.args[0]
            else:
                node = node.func.value
            continue
        if node.args:
            node = node.args[0]
            continue
        return False
    if isinstance(node, ast.Name):
        return node.id in known
    if isinstance(node, ast.Attribute):
        return node.attr in known
    if isinstance(node, ast.Subscript):
        return _is_sql_expression(node.value, names)
    return False


def _sql_aliases(func: ast.FunctionDef) -> frozenset[str]:
    """Local names the fake normalizes the statement into.

    `flat = " ".join(sql.split())`, `sql_upper = sql.strip().upper()`. Without
    this, a fake that dispatches on its normalized copy reads as if it did not
    dispatch on SQL at all -- and the census would call it clean.
    """

    names: set[str] = set()
    for _ in range(4):  # aliases of aliases
        grew = False
        for node in ast.walk(func):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                value = node.value
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if value is None or not _is_sql_expression(value, frozenset(names)):
                    continue
                for target in targets:
                    if isinstance(target, ast.Name) and target.id not in names:
                        names.add(target.id)
                        grew = True
        if not grew:
            break
    return frozenset(names)


def _fragments(test: ast.AST, names: frozenset[str] = frozenset()) -> list[str]:
    """The string literals this condition matches against the statement text."""

    found: list[str] = []

    for node in ast.walk(test):
        if isinstance(node, ast.Compare):
            # "insert into x" in sql
            for op, comparator in zip(node.ops, node.comparators):
                if isinstance(op, (ast.In, ast.NotIn)) and _is_sql_expression(comparator, names):
                    if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
                        found.append(node.left.value)
                elif isinstance(op, (ast.Eq, ast.NotEq)) and _is_sql_expression(node.left, names):
                    if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                        found.append(comparator.value)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            # sql.startswith("select"), sql.lower().find("from app.x")
            probes = {"startswith", "endswith", "find", "index", "count", "search", "match"}
            if node.func.attr in probes:
                if _is_sql_expression(node.func.value, names) or (
                    node.func.attr in {"search", "match"}
                    and node.args
                    and _is_sql_expression(node.args[-1], names)
                ):
                    for arg in node.args:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            found.append(arg.value)
                        elif isinstance(arg, ast.Tuple):
                            found.extend(
                                elt.value
                                for elt in arg.elts
                                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                            )
    return found


def _delegates(body: list[ast.stmt]) -> bool:
    """Does this block hand the statement to ANOTHER cursor?

    `return super().execute(sql, params)` / `return outer.execute(sql, params)`.
    Such a fall-through is not mute by itself -- it is only as honest as the
    cursor it defers to. Counting it as mute would send a repair at a fixture
    that is already loud one level up; counting it as loud would hide a real
    silence. It gets its own column.
    """

    for node in body:
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                if inner.func.attr in {"execute", "executemany", "_execute"}:
                    target = inner.func.value
                    if isinstance(target, ast.Call) and isinstance(target.func, ast.Name):
                        if target.func.id == "super":
                            return True
                    elif isinstance(target, (ast.Name, ast.Attribute)):
                        return True
    return False


def _raises(body: list[ast.stmt]) -> bool:
    """Does this block stop the test loudly rather than answer nothing?"""

    for node in body:
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, ast.Assert):
            # `assert False, "..."` / `assert sql in KNOWN, ...`
            return True
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in {"fail", "xfail", "exit"}:
                return True
    return False


MUTE, LOUD, DELEGATES = "mute", "loud", "delegates"


def _walk_chain(node: ast.If, names: frozenset[str] = frozenset()) -> tuple[list[str], str, str]:
    """Follow an if/elif chain to its end. Report its fragments and its tail."""

    fragments: list[str] = []
    current: ast.If | None = node
    verdict = MUTE
    reason = "no else branch: an unrecognized statement answers no rows"

    while current is not None:
        fragments.extend(_fragments(current.test, names))
        if _raises(current.body):
            # a guard clause at the top (`if sql not in KNOWN: raise`) is loud
            explicit = _fragments(current.test, names)
            if not explicit:
                verdict = LOUD
                reason = f"guard raises at line {current.lineno}"
        orelse = current.orelse
        if len(orelse) == 1 and isinstance(orelse[0], ast.If):
            current = orelse[0]
            continue
        if orelse:
            if _raises(orelse):
                verdict = LOUD
                reason = f"else raises at line {orelse[0].lineno}"
            elif _delegates(orelse):
                verdict = DELEGATES if verdict == MUTE else verdict
                reason = f"else at line {orelse[0].lineno} defers to another cursor"
            else:
                reason = f"else at line {orelse[0].lineno} answers instead of raising"
        current = None

    return fragments, verdict, reason


def _promote(current: str, candidate: str) -> str:
    """LOUD beats DELEGATES beats MUTE: the tail is as honest as its best exit."""

    order = {MUTE: 0, DELEGATES: 1, LOUD: 2}
    return candidate if order[candidate] > order[current] else current


def _inventories(tree: ast.Module) -> dict[str, list[str]]:
    """Module-level ``StatementInventory(...)`` declarations, by variable name.

    A fake converted under AI-317 no longer carries its fragments inside
    ``execute`` -- they live in one declaration and ``match()`` raises on
    anything else. Without this, the conversion would make the cursor VANISH
    from the census rather than move to the loud column, and the count would
    fall for the wrong reason. An instrument that stops seeing what it repaired
    cannot measure the repair.
    """

    found: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "StatementInventory":
            continue
        fragments: list[str] = []
        for keyword in node.value.keywords:
            for inner in ast.walk(keyword.value):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    fragments.append(inner.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                found[target.id] = fragments
    return found


def _routed_through(func: ast.FunctionDef, inventories: dict[str, list[str]]):
    """The inventory this ``execute`` dispatches through, and whether it raises."""

    for node in ast.walk(func):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"match", "find"}:
            continue
        owner = node.func.value
        owner_name = owner.id if isinstance(owner, ast.Name) else None
        if owner_name in inventories:
            return owner_name, node.func.attr == "match"
    return None, False


def scan_function(
    func: ast.FunctionDef, inventories: dict[str, list[str]] | None = None
) -> tuple[list[str], str, str]:
    fragments: list[str] = []
    verdict = MUTE
    reason = "no if/elif chain found"
    names = _sql_aliases(func)

    inventory_name, raises = _routed_through(func, inventories or {})
    if inventory_name:
        fragments.extend((inventories or {})[inventory_name])
        if raises:
            return (
                fragments,
                LOUD,
                f"dispatches through {inventory_name}.match(), which raises on the unknown",
            )
        reason = f"reads {inventory_name}.find() without ever raising"

    # A top-level `raise` reachable after the chain, a delegation to another
    # cursor, or a match/case with a raising wildcard: each is a tail exit.
    for node in func.body:
        if isinstance(node, ast.If):
            got, tail, why = _walk_chain(node, names)
            fragments.extend(got)
            if got or tail != MUTE:
                verdict = _promote(verdict, tail)
                if got:
                    reason = why
        elif isinstance(node, ast.Raise):
            verdict = LOUD
            reason = f"unconditional raise at line {node.lineno}"
        elif isinstance(node, (ast.Return, ast.Expr)) and _delegates([node]):
            verdict = _promote(verdict, DELEGATES)
            if verdict == DELEGATES:
                reason = f"tail defers to another cursor at line {node.lineno}"
        elif isinstance(node, ast.Match):
            for case in node.cases:
                pattern = case.pattern
                fragments.extend(
                    value.value.value
                    for value in ast.walk(pattern)
                    if isinstance(value, ast.MatchValue)
                    and isinstance(value.value, ast.Constant)
                    and isinstance(value.value.value, str)
                )
                if isinstance(pattern, ast.MatchAs) and pattern.pattern is None:
                    if _raises(case.body):
                        verdict = LOUD
                        reason = f"wildcard case raises at line {case.body[0].lineno}"
        elif isinstance(node, (ast.For, ast.While, ast.With, ast.Try)):
            for inner in ast.walk(node):
                if isinstance(inner, ast.If):
                    got, tail, why = _walk_chain(inner, names)
                    fragments.extend(got)
                    if got:
                        verdict = _promote(verdict, tail)
                        reason = why

    if not fragments:
        # Fakes that recognize without branching: `assert "from app.x" in sql`
        # (loud by construction) or `self._is_role = "select m.role" in sql`
        # followed by a ternary (mute -- a rewritten query silently takes the
        # other arm). Both are textual recognition and belong in the census.
        for node in ast.walk(func):
            if isinstance(node, ast.Assert):
                got = _fragments(node.test, names)
                if got:
                    fragments.extend(got)
                    verdict = LOUD
                    reason = f"assert on the statement at line {node.lineno}"
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.IfExp, ast.Return)):
                probe = node.value if not isinstance(node, ast.IfExp) else node.test
                if probe is None:
                    continue
                got = _fragments(probe, names)
                if got:
                    fragments.extend(got)
                    if verdict != LOUD:
                        reason = (
                            f"recognition without a branch at line {node.lineno}: "
                            "an unrecognized statement takes the other arm"
                        )

    return fragments, verdict, reason


def _looks_like_sql(fragments: list[str]) -> bool:
    """Fragments that are SQL, not flags like 'RETURNING' on a mode string."""

    keywords = (
        "select",
        "insert",
        "update",
        "delete",
        "from ",
        "into ",
        "app.",
        "toorow_meta.",
        "information_schema",
        "pg_",
        "create ",
        "alter ",
        "set ",
        "with ",
        "returning",
        "count(",
        "coalesce",
        "join ",
        "where ",
    )
    return any(any(k in f.lower() for k in keywords) for f in fragments)


def scan_file(path: Path) -> list[FakeCursor]:
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    tests_in_file = sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    )

    inventories = _inventories(tree)

    found: list[FakeCursor] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if item.name not in {"execute", "executemany", "_execute"}:
                continue
            fragments, verdict, reason = scan_function(item, inventories)
            if not fragments or not _looks_like_sql(fragments):
                continue
            found.append(
                FakeCursor(
                    path=str(path.relative_to(REPO)).replace("\\", "/"),
                    class_name=node.name,
                    line=item.lineno,
                    fragments=sorted(set(fragments)),
                    verdict=verdict,
                    reason=reason,
                    tests_in_file=tests_in_file,
                )
            )
    return found


def census() -> list[FakeCursor]:
    out: list[FakeCursor] = []
    for path in sorted(TESTS.rglob("*.py")):
        out.extend(scan_file(path))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--loud", action="store_true", help="also list the loud fakes")
    parser.add_argument("--by-tests", action="store_true", help="rank the mute files by suite size")
    args = parser.parse_args()

    found = census()
    mute = [f for f in found if f.verdict == MUTE]
    loud = [f for f in found if f.verdict == LOUD]
    defers = [f for f in found if f.verdict == DELEGATES]

    if args.json:
        print(
            json.dumps(
                {
                    "total": len(found),
                    "mute": len(mute),
                    "loud": len(loud),
                    "delegates": len(defers),
                    "cursors": [f.__dict__ for f in found],
                },
                indent=2,
            )
        )
        return 0

    print(f"textual fake cursors in server/tests : {len(found)}")
    print(f"  MUTE on an unknown statement       : {len(mute)}")
    print(f"  LOUD on an unknown statement       : {len(loud)}")
    print(f"  DEFERS to another cursor           : {len(defers)}")
    print()

    if args.by_tests:
        by_file: dict[str, list[FakeCursor]] = {}
        for cursor in mute:
            by_file.setdefault(cursor.path, []).append(cursor)
        ranked = sorted(
            by_file.items(), key=lambda kv: (-kv[1][0].tests_in_file, kv[0])
        )
        print("mute files, ranked by how much their suite asserts:")
        for path, cursors in ranked:
            names = ", ".join(c.class_name for c in cursors)
            print(f"  {cursors[0].tests_in_file:4d} tests  {path}  ({names})")
        return 0

    print("MUTE:")
    for cursor in mute:
        print(f"  {cursor.path}:{cursor.line}  {cursor.class_name}  "
              f"[{len(cursor.fragments)} fragments, {cursor.tests_in_file} tests]  {cursor.reason}")

    if args.loud:
        print()
        print("LOUD:")
        for cursor in loud:
            print(f"  {cursor.path}:{cursor.line}  {cursor.class_name}  {cursor.reason}")
        print()
        print("DEFERS (only as honest as the cursor it hands the statement to):")
        for cursor in defers:
            print(f"  {cursor.path}:{cursor.line}  {cursor.class_name}  {cursor.reason}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
