"""A test does not remove an environment key it has no way of putting back.

WHY THIS EXISTS ALONGSIDE A RUNTIME GUARD. `tests/conftest.py` snapshots the
watched environment around every module and names the one that leaked
(`tests/env_leak_guard.py`). That instrument is the honest one -- it measures
what actually happened -- and it has one blind spot this repository has been
bitten by before: **it can only see the code that RAN**. A `os.environ.pop` in a
test that skips for want of a DSN, or in a branch nobody reaches on this
machine, leaks nothing today and everything the day it runs. The runtime guard
finds the leak; this walk finds the ones waiting.

WHAT IT ASKS. Every `os.environ.pop("SOME_KEY", ...)` under `server/tests` must
sit in a function that can put the value back -- it writes `os.environ[...]`
itself, or it holds a `patch.dict(os.environ, ...)`, or it takes `monkeypatch`,
which pytest restores by contract. A pop that does none of the three hands the
next module a setting it never chose.

WHAT IT DELIBERATELY DOES NOT READ. A pop whose key is a VARIABLE
(`os.environ.pop(key, None)`) is, everywhere in this suite, the SECOND half of a
restore -- `for key, value in saved.items(): ... if value is None: pop(key)`.
Five sites, all of that shape, read on 2026-09-01. Counting them would fill the
baseline with the very idiom this file asks for, which is how a guard teaches
people to stop reading it.

MEASURED 2026-09-01: 49 pops under `server/tests`, 5 of them the restore idiom
above, ONE in the baseline below. Nine sites were migrated to
`monkeypatch.delenv` in the same commit -- seven in `test_pull_generic.py`, plus
`TRACING_ENABLED` and `TOOROW_ORG_SCHEMAS`. The baseline turns ONE WAY: striking
an entry is free and records a repair; adding one is an admission.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]
TESTS = ROOT / "server" / "tests"


def _restores(function: ast.FunctionDef | ast.AsyncFunctionDef | None, key: str) -> bool:
    """Can this function put `key` back before it returns?

    TWO WAYS, and `monkeypatch` is deliberately NOT one of them. Taking the
    fixture proves nothing about a bare `os.environ.pop` sitting beside it --
    measured by mutation on 2026-09-01, where a pop reintroduced next to an
    untouched `monkeypatch` parameter passed this guard. A function that already
    has `monkeypatch` has no reason to reach for `os.environ.pop` at all:
    `monkeypatch.delenv(key, raising=False)` is the same line, restored.
    """
    if function is None:
        return False
    source = ast.unparse(function).replace('"', "'")
    return (
        f"environ['{key}']" in source  # writes the value back itself
        or "patch.dict" in source  # unittest restores the whole mapping
    )


def _enclosing_function(node: ast.AST, parents: dict[int, ast.AST]):
    current = node
    while id(current) in parents:
        current = parents[id(current)]
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
            return current
    return None


def bare_pops() -> list[tuple[str, str, str]]:
    """`(file, key, function)` for every pop that cannot be undone."""
    found: list[tuple[str, str, str]] = []
    for path in sorted(TESTS.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:  # pragma: no cover -- a file that does not parse runs nothing
            continue
        parents: dict[int, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[id(child)] = node
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "pop":
                continue
            target = node.func.value
            if not (isinstance(target, ast.Attribute) and target.attr == "environ"):
                continue
            if not (node.args and isinstance(node.args[0], ast.Constant)):
                continue  # the restore idiom -- see the module docstring
            key = str(node.args[0].value)
            function = _enclosing_function(node, parents)
            if _restores(function, key):
                continue
            name = function.name if function is not None else "<module level>"
            found.append((path.relative_to(TESTS).as_posix(), key, name))
    return found


#: THE DECREASING RATCHET. One entry, with the reason the three restores do not
#: apply to it.
BARE_POPS_AT_2026_09_01: dict[tuple[str, str, str], str] = {
    (
        "isolation/test_warehouse_org_isolation.py",
        "TOOROW_DB_MODE",
        "_provision_and_seed_two_orgs",
    ): (
        "Not a test: a helper both of whose callers save `TOOROW_DUCKDB_PATH` and "
        "`TOOROW_DB_MODE` before calling it and restore both in a `finally`. The "
        "contract is real but INVISIBLE from the helper, which is why it is written "
        "here rather than left to be re-derived. Closing it means the helper taking "
        "`monkeypatch` from its callers."
    ),
}


def test_the_walk_finds_pops_at_all() -> None:
    """A walk that matches nothing would baseline nothing and prove nothing."""
    total = 0
    for path in sorted(TESTS.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        total += path.read_text(encoding="utf-8", errors="replace").count("environ.pop(")
    assert total > 30, f"the walk found {total} `environ.pop(` call sites: they moved"


def test_no_new_test_pops_an_env_key_it_cannot_restore() -> None:
    found = set(bare_pops())
    baseline = set(BARE_POPS_AT_2026_09_01)

    appeared = sorted(found - baseline)
    assert not appeared, (
        "these remove an environment key with no way to put it back, so every module "
        "collected after them reads the loss as its own failure. Use "
        "`monkeypatch.delenv(..., raising=False)`:\n  "
        + "\n  ".join(f"server/tests/{name}: {key} in {fn}()" for name, key, fn in appeared)
    )

    repaired = sorted(baseline - found)
    assert not repaired, (
        "these can now restore what they pop -- strike them from "
        f"BARE_POPS_AT_2026_09_01 so the ratchet cannot slip back: {repaired}"
    )


def test_every_baselined_pop_carries_a_reason() -> None:
    """An exemption with no reason is how a real gap gets filed as normal."""
    empty = sorted(
        key for key, reason in BARE_POPS_AT_2026_09_01.items() if len(reason.strip()) < 20
    )
    assert not empty, empty
