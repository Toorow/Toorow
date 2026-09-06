"""A preference change re-derives; it never rewrites what a source said -- AI-168.

`capabilities/reporting-timezone.md` is incomplete if "a preference change mutates native
timestamps or facts". E39-FR15 states the mechanism: changing a project's reporting
timezone RE-DERIVES aligned views "without mutating the captured source report timezones
(source timezone is immutable provenance; alignment is a derivation)".

That invariant held on 2026-08-04 -- and it held by CONVENTION. Nothing prevented the next
`UPDATE` from landing: the observed-boundary table carries no append-only trigger, and a
handful of characters in one SQL string is all it takes to turn provenance into a mutable
field. A criterion whose only guarantee is that nobody has broken it yet is a criterion
that will be broken.

This is a STRUCTURAL guard, deliberately read off the source rather than exercised through
a database. What must never exist is a WRITE, and a test that runs the code paths that do
exist can only show the ones it thought to call. Grepping the whole repository shows the
ones nobody thought of.

Scope, and why it stops there:
  * the OBSERVED boundary evidence -- what a run measured. It is the record a re-derivation
    reads; rewriting it would erase the very fact the derivation is derived FROM.
  * the CAPTURED per-row report timezone -- the zone the source drew its day on. Immutable
    provenance by E39-AD2.
It does NOT police `app.project_preferences`: that IS the preference, and changing it is
the whole point.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SEARCHED = (ROOT / "server", ROOT / "supabase", ROOT / "dbt")

#: The observed-boundary record. Append-only in intent (AI-161 writes it once per run and
#: `record_boundary_evidence` is idempotent by content hash); this pins the intent.
EVIDENCE_TABLE = "datastream_time_boundary_evidence"

#: Any SQL statement that would rewrite or remove a row of it.
_MUTATES_EVIDENCE = re.compile(
    r"\b(?:UPDATE|DELETE\s+FROM)\s+(?:app\.)?" + EVIDENCE_TABLE + r"\b",
    re.IGNORECASE,
)

#: `SET report_timezone = ...` in a SQL UPDATE -- rewriting captured provenance. Written to
#: catch the SQL form only: a Python assignment `report_timezone = tz` is how a connector
#: reads its own provider metadata, which is the legitimate capture.
_REWRITES_CAPTURED_ZONE = re.compile(
    r"\bSET\b[^;]{0,400}?\breport_timezone\s*=", re.IGNORECASE | re.DOTALL
)

_SKIP_DIRS = {"__pycache__", "node_modules", ".git", "dist", "build"}


def _sources():
    for root in SEARCHED:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.suffix not in {".py", ".sql"} or not path.is_file():
                continue
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            if path.name == Path(__file__).name:
                continue  # this file quotes the patterns it forbids
            try:
                yield path, path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue


def test_nothing_rewrites_or_deletes_observed_boundary_evidence():
    """What a run measured is not editable, least of all by a preference.

    A re-derivation READS this table. Letting a write reach it would mean a preference
    change could quietly restate what a source had said -- which is the criterion, word
    for word.
    """
    offenders = [
        f"{path.relative_to(ROOT)}:{content[:m.start()].count(chr(10)) + 1}"
        for path, content in _sources()
        for m in _MUTATES_EVIDENCE.finditer(content)
    ]
    assert not offenders, (
        "observed day-boundary evidence is rewritten or deleted at: "
        + ", ".join(offenders)
        + ". It records what a run MEASURED. A preference change re-derives aligned views "
        "from it and must never restate it (E39-FR15, E39-AD2). If a row is genuinely "
        "wrong, record a new observation -- the reader takes the latest."
    )


def test_nothing_rewrites_a_captured_report_timezone_in_sql():
    """The zone a source drew its day on is provenance, and provenance is not edited."""
    offenders = [
        f"{path.relative_to(ROOT)}:{content[:m.start()].count(chr(10)) + 1}"
        for path, content in _sources()
        for m in _REWRITES_CAPTURED_ZONE.finditer(content)
    ]
    assert not offenders, (
        "a captured report timezone is rewritten by SQL at: "
        + ", ".join(offenders)
        + ". The source report timezone is immutable provenance (E39-AD2): alignment is a "
        "DERIVATION over it, never an edit of it. A day re-stated after the fact cannot be "
        "reconciled with the figures that were drawn on the original clock."
    )


def test_the_guard_actually_scans_something():
    """Guard the guard: an empty corpus would make both tests pass by vacuity."""
    scanned = sum(1 for _ in _sources())
    assert scanned > 100, f"only {scanned} source files scanned -- the sweep is not running"
