"""The seed corpus must never be machine-day-local (AI-213, class of AI-66).

WHY. The evals harness (Epic 14) pins ``fixture_sha256`` against the mart built
from the seed corpus. Measured 2026-08-17: nine generators emit dated rows and
only ONE was anchored (google-analytics, AI-66: ``TOOROW_SEED_END_DATE`` + a
fixed RNG seed). The other eight defaulted to ``date.today()``, so the mart's
date window was the day it was built -- 40 of 58 eval fixtures diverged and the
corpus could not be re-derived on any other machine or day. The repair threaded
the anchor from ``seed_all_connectors`` through every loader that declares an
``end_date`` seam down to every generator; this test is the guard that keeps
the class closed.

WHAT IT ASSERTS, statically (AST, so comments and docstrings never trip it):
no file under ``server/modules/*/seeds/`` or ``dbt/seeds/`` may

  1. call ``.today()`` on anything (``date.today()``, ``datetime.today()``),
  2. call ``.date()`` on the result of ``.now(...)`` / ``.utcnow()``
     (the ``datetime.now(UTC).date()`` spelling of the same defect).

``datetime.now(UTC)`` alone stays LEGAL: ``loaded_at`` is load metadata (when
the loader ran), not a dated corpus row -- the staging models supersede on
``pull_id``, they never window on it.

A new generator that wants "the last day of the corpus" reads the anchor:
``date.fromisoformat(os.environ.get("TOOROW_SEED_END_DATE", "2026-07-19"))``
(the google-analytics family keeps its own AI-66 anchor, 2026-07-15, pinned to
the evals as_of_anchor and the checked-in GA4 CSVs).
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Every directory whose .py files build the seed corpus.
_SEED_GLOBS = (
    (REPO_ROOT / "server" / "modules", "*/seeds/*.py"),
    (REPO_ROOT / "dbt" / "seeds", "**/*.py"),
)


def _seed_files() -> list[Path]:
    files: list[Path] = []
    for root, pattern in _SEED_GLOBS:
        files.extend(sorted(root.glob(pattern)))
    return files


def _clock_offences(tree: ast.AST) -> list[tuple[int, str]]:
    """Return (lineno, spelling) for every wall-clock date derivation."""
    offences: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        # 1. Anything.today() -- date.today(), datetime.today(), dt.date.today().
        if node.func.attr == "today":
            offences.append((node.lineno, ast.unparse(node.func) + "()"))
            continue
        # 2. <now-ish call>.date() -- datetime.now(...).date(), utcnow().date().
        if node.func.attr == "date":
            inner = node.func.value
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr in {"now", "utcnow"}
            ):
                offences.append((node.lineno, ast.unparse(node.func) + "()"))
    return offences


def test_seed_files_exist() -> None:
    """The scan must never silently cover nothing (a guard measuring an empty

    set reports 'covered' forever -- the exact defect seed_all_connectors's
    docstring records)."""
    files = _seed_files()
    assert len(files) >= 42, (
        f"expected the seed tree to hold at least the 42 known loaders, found "
        f"{len(files)} .py files -- the glob no longer covers the corpus"
    )


def test_no_seed_file_derives_dates_from_the_wall_clock() -> None:
    failures: list[str] = []
    for path in _seed_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, spelling in _clock_offences(tree):
            failures.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {spelling}")
    assert not failures, (
        "Seed corpus files derive dates from the wall clock -- the corpus "
        "becomes machine-day-local and the evals fixtures underivable (AI-213). "
        "Anchor on TOOROW_SEED_END_DATE (default 2026-07-19) instead:\n  "
        + "\n  ".join(failures)
    )
