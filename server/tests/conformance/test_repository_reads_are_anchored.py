"""No test reads a repository file by a path relative to the working directory.

THE DEFECT THIS CLOSES, and it is a repeat. `server/tests/conftest.py` records
the measurement of 2026-08-01: 25 tests passed or failed depending on the
directory pytest was invoked from, because they read a repository file through a
CWD-relative path. `REPO_ROOT` was introduced that day as the single anchor.

**Seven of them came back.** Measured 2026-08-14, running `cd server && pytest`:
`test_inbound_receipts`, `test_epic38_connector_activation` (twice),
`test_epic38_import_templates`, `test_epic38_inbound_credentials` (twice) and
`test_flows_cadence_ownership` all read `infra/nango/...` or `server/core/...`
raw. Every one was red from `server/` and green from the repository root.

That is the worst shape a red can take: it is not a defect in the product, it
carries no information, and it costs a diagnosis every time somebody counts the
suite. Worse, it is asymmetric -- the two sets of failures are not nested, so no
count of reds is comparable to another until the class is closed.

An anchor introduced and not guarded is an anchor that erodes. This is the guard.
"""

from __future__ import annotations

import ast
import re

from tests.conftest import REPO_ROOT

_TESTS = REPO_ROOT / "server" / "tests"

#: A first path segment that means "a file of this repository". `core/` and
#: `tests/` are deliberately absent: written from `server/` they are legitimate
#: relative paths for other purposes, and this guard is about repository-rooted
#: reads, not about every string with a slash in it.
_REPOSITORY_ROOTS = ("infra/", "server/", "ui/", "docs/", "scripts/", "cards/", "screens/")


def _string_constants(node: ast.AST) -> list[str]:
    """Every literal string inside one expression, joined parts included."""
    found: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            found.append(child.value)
    return found


def _anchored(node: ast.Call) -> bool:
    """True when the expression names an anchor rather than trusting the CWD."""
    return bool(
        re.search(r"\b(REPO_ROOT|SERVER_ROOT|_ROOT|ROOT|__file__)\b", ast.unparse(node))
    )


#: Reading a path is what makes it depend on the working directory. A `Path(...)`
#: handed to a predicate as a VALUE -- `detector._is_test(Path("server/core/x.py"))`
#: -- touches no disk and is correct as written. Flagging it would teach the next
#: reader to anchor strings that are not paths at all, which is how a guard
#: becomes noise and then becomes ignored.
_READS = frozenset({
    "read_text", "read_bytes", "open", "is_file", "exists", "is_dir",
    "glob", "rglob", "iterdir", "stat",
})


def _reading_calls(node: ast.AST) -> list[ast.Call]:
    """The `Path(...)`/`open(...)` expression this node actually reads from."""
    if not isinstance(node, ast.Call):
        return []
    name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
    # `open("...")` reads by definition.
    if name == "open" and not isinstance(node.func, ast.Attribute):
        return [node]
    # `Path(...).read_text()`, `Path(...).joinpath(...).read_text()`, ...
    if name in _READS and isinstance(node.func, ast.Attribute):
        receiver: ast.AST = node.func.value
        while isinstance(receiver, (ast.Call, ast.Attribute)):
            if isinstance(receiver, ast.Call):
                inner = getattr(receiver.func, "id", "") or getattr(receiver.func, "attr", "")
                if inner == "Path":
                    return [receiver]
                receiver = receiver.func
            else:
                receiver = receiver.value
    return []


def _offenders() -> list[str]:
    found: list[str] = []
    for path in sorted(_TESTS.rglob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            for call in _reading_calls(node):
                if _anchored(call):
                    continue
                for text in _string_constants(call):
                    if text.startswith(_REPOSITORY_ROOTS):
                        found.append(
                            f"{path.relative_to(REPO_ROOT).as_posix()}:{call.lineno} "
                            f"reads `{text}`"
                        )
    return found


def test_the_anchor_this_guard_defends_still_exists():
    """A guard whose anchor vanished passes by vacuity."""
    assert REPO_ROOT.is_dir()
    assert (REPO_ROOT / "infra" / "nango" / "migrations").is_dir()
    assert _TESTS.is_dir()


def test_no_test_reads_a_repository_file_relative_to_the_working_directory():
    offenders = _offenders()
    assert not offenders, (
        "these reads resolve against the directory pytest was invoked from, so "
        "each one is green from the repository root and red from `server/` -- a "
        "verdict about nothing.\n"
        "`from tests.conftest import REPO_ROOT` and anchor them:\n  "
        + "\n  ".join(offenders)
    )
