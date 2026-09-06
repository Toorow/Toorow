"""A teardown does not write the org row's DELETE itself (AI-291, class II).

WHY. `test_no_fixture_deletes_a_project_by_hand.py` closed this class one level
down, for `app.projects`. The org level was left open, and it is the one where a
hand-written teardown cannot work AT ALL: since migration 130 the org row is
held by `mdm_business_domains` and by the other seeded satellites in
`ON DELETE RESTRICT`, and the append-only ledgers inside the tree refuse a
DELETE that does not flag `app.rgpd_erasure` (migration 098). A bare
`DELETE FROM app.organizations` is refused inside a `finally`, which poisons the
transaction -- the triage of 2026-09-01 measured seven failures in one module
from one such teardown, every one of them reading as a defect of the code under
test.

WHAT THE 2026-09-01 SWEEP ACTUALLY FOUND, and it is not what the class name
suggests. 24 sites spell the statement, in 21 files. Twenty of them, across 18
files, no longer wrote a BARE delete: they had each been taught the two-statement
pair by hand --

    purge_org_tree(conn, org_id)                 # the FK graph, the product's
    DELETE FROM app.organizations WHERE id = %s  # the tail the purge leaves

-- twenty copies of a contract that `tests.conftest.purge_fixture_org` already
owns, each with its own lazy import of `core.org_purge` and its own paragraph
re-deriving the same measurement. A copied pair is a pair that stops being
copied: the day the contract gains a third statement, eighteen files are wrong
and not one of them says so. All twenty sites are migrated to the helper in the
same commit as this guard; the four that remain are named below.

WHAT THIS GUARD ASKS. No file under `server/tests` may issue
`DELETE FROM app.organizations` unless it is listed below WITH ITS REASON. The
list is inventory, not exemption: it holds the helper itself and the two files
where the statement is the SUBJECT of the test rather than its teardown -- four
sites in three files. A test that asserts `rowcount == 1` on the org row is
measuring the contract, and routing it through the helper would erase the thing
it measures.

WHERE IT DEPARTS FROM ITS PROJECT-LEVEL TWIN, and why. That guard greps the file
text and pays for it with four ALLOWED entries whose reason is « the match is
PROSE ». Five files here explain in a docstring or a comment why the bare delete
cannot work -- they are the record of the repair, they SHOULD say the words, and
filing them as exemptions would make the guard blind to a real statement
appearing in them later. So this one reads the AST and counts only STRING
LITERALS THAT ARE NOT DOCSTRINGS: a comment is not in the tree at all, a
docstring is identified by position, and everything else that spells the
statement is code -- including a statement built by implicit concatenation
across lines, which the line-anchored regex cannot see.

THE GUARD IS STATIC on purpose, exactly as its twin is: the defect hides behind
a skip (no DSN, no fixture, no failure), so a check that also needed a DSN would
hide in the same place.
"""

from __future__ import annotations

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]
TESTS = ROOT / "server" / "tests"

_DELETE = re.compile(r"DELETE\s+FROM\s+app\.organizations", re.IGNORECASE)

#: Files that issue the org DELETE in code, each with the reason
#: `tests.conftest.purge_fixture_org` does not fit.
ALLOWED: dict[str, str] = {
    "conftest.py": (
        "It IS the helper. `purge_fixture_org` calls `core.org_purge.purge_org_tree` "
        "and then issues the tail that the purge leaves to its caller by contract."
    ),
    "integration/test_org_purge_end_to_end_pg.py": (
        "The statement is the SUBJECT, twice: both tests assert `cur.rowcount == 1` "
        "on it, to prove the contract that `purge_org_tree` leaves the org row for "
        "its caller. Calling the helper here would erase what the test measures."
    ),
    "core/test_rule_version_rgpd_hatch.py": (
        "Measures that the append-only ledgers ACCEPT the erasure pair instead of "
        "refusing it. The statement is the hatch under test, and the assertions read "
        "the ledger rows around it."
    ),
}


def _docstring_constants(tree: ast.Module) -> set[int]:
    """`id()` of every string Constant that is a docstring -- prose, by position."""
    holders = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, holders) or not node.body:
            continue
        first = node.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                found.add(id(first.value))
    return found


def _statement_lines(path: pathlib.Path) -> list[int]:
    """Lines where the file SPELLS the statement in code, docstrings excluded."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:  # pragma: no cover -- a file that does not parse runs nothing
        return []
    prose = _docstring_constants(tree)
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in prose
        and _DELETE.search(node.value)
    )


def _offending_files() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in sorted(TESTS.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        # This file spells the statement it forbids, in its own regex source.
        # Excluding it by name is narrower than teaching the walk to skip itself.
        if path.name == pathlib.Path(__file__).name:
            continue
        lines = _statement_lines(path)
        if lines:
            found[path.relative_to(TESTS).as_posix()] = lines
    return found


def test_the_guard_still_sees_something() -> None:
    """Calibration: the allowlist must describe files that exist.

    An entry naming a file that no longer deletes an org is an exemption
    outliving its reason -- the shape this repository keeps finding in guards
    with a fixed numerator.
    """
    present = set(_offending_files())
    stale = sorted(set(ALLOWED) - present)
    assert not stale, f"ALLOWED names files that no longer delete an org: {stale}"


def test_prose_is_not_counted_as_a_statement() -> None:
    """The walk must still tell the record of the repair from the defect.

    These five files were migrated to `purge_fixture_org` and KEPT the sentence
    that says why the bare delete cannot work. If the walk ever counts them, the
    next repair will be asked to delete its own reasoning.
    """
    explained = {
        "core/test_epic36_invitation_lifecycle_live.py",
        "core/test_metric_semantics_monetary.py",
        "core/test_org_branding_profiles.py",
        "core/test_org_creation_cap_pg.py",
    }
    offending = set(_offending_files())
    counted = sorted(explained & offending)
    assert not counted, (
        "these files only EXPLAIN the statement and are being counted as writing it: "
        f"{counted}"
    )


def test_no_new_fixture_deletes_an_org_by_hand() -> None:
    offenders = [
        f"server/tests/{name}:{lines}: writes `DELETE FROM app.organizations` in code. "
        f"Use `tests.conftest.purge_fixture_org(conn, org_id)`, which walks the "
        f"product's own FK graph and then removes the row -- or add an entry to "
        f"ALLOWED saying why it does not fit."
        for name, lines in _offending_files().items()
        if name not in ALLOWED
    ]
    assert not offenders, "\n".join(offenders)


def test_every_allowance_carries_a_reason() -> None:
    """An exemption with no reason is how a real gap gets filed as normal."""
    empty = sorted(name for name, reason in ALLOWED.items() if len(reason.strip()) < 20)
    assert not empty, empty
