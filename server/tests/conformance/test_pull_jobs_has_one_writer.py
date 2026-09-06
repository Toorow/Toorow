"""`app.pull_jobs` is written from ONE place, so the day grain has no blind spot.

Story 58.1. The daily-breakdown route answers "what happened on this day, on this
flux" for EVERY connector, and the ratified document says so
(`docs/product-architecture/datastream-workbench-and-wizard.md`, "A Datastream
read by day"). That promise does not rest on measurement -- 39 connectors cannot
be pulled to prove it, and the disposable cluster carries pull jobs for one module
out of six, which proves nothing either way. It rests on CONSTRUCTION: the ledger
reads `app.pull_jobs`, and `app.pull_jobs` is inserted into from exactly one
production module, with no connector-specific branch.

This test is what makes that sentence provable instead of plausible. A second
writer appearing anywhere in `server/core/` or `server/modules/` -- a file-source
importer that lands rows without enqueueing, an inbound path that writes its own
row shape -- turns it red. Without it, the day grain would acquire a connector
that silently never appears, and the screen would show a complete-looking strip of
`never_fetched` for a flux that had in fact collected.

It guards a shape, not a style: `INSERT INTO app.pull_jobs`. A writer that reaches
the table another way (a stored procedure, a dynamically built table name) is out
of its reach, and that is stated rather than implied.
"""

from __future__ import annotations

import re
from pathlib import Path

_SERVER = Path(__file__).resolve().parents[2]

#: The one production module allowed to enqueue a pull window.
_DECLARED_WRITER = "core/queue.py"

_INSERT = re.compile(r"INSERT\s+INTO\s+app\.pull_jobs", re.IGNORECASE)


def _production_sources() -> list[Path]:
    """Every production Python file. Tests write fixtures and are not writers."""
    roots = [_SERVER / "core", _SERVER / "modules"]
    return [
        path
        for root in roots
        if root.is_dir()
        for path in sorted(root.rglob("*.py"))
    ]


def test_only_one_production_module_inserts_a_pull_job() -> None:
    """The day grain covers every connector because one connector-agnostic path fills it."""
    writers = sorted(
        path.relative_to(_SERVER).as_posix()
        for path in _production_sources()
        if _INSERT.search(path.read_text(encoding="utf-8", errors="replace"))
    )

    assert writers == [_DECLARED_WRITER], (
        "`app.pull_jobs` must have exactly one production writer -- the extract "
        "ledger, the /ledger route and the daily-breakdown route all derive a "
        "day's status from this table, and the ratified document promises the "
        "answer covers every connector on that basis.\n"
        f"  declared : {_DECLARED_WRITER}\n"
        f"  found    : {writers}\n"
        "A new writer either belongs in the declared module, or the promise in "
        "`datastream-workbench-and-wizard.md` stops being true and must be "
        "rewritten in the same commit."
    )


def test_the_declared_writer_has_no_connector_specific_branch() -> None:
    """A per-connector branch in the enqueue path would reintroduce the blind spot."""
    source = (_SERVER / _DECLARED_WRITER).read_text(encoding="utf-8", errors="replace")

    modules_dir = _SERVER / "modules"
    connector_names = sorted(
        entry.name
        for entry in modules_dir.iterdir()
        if entry.is_dir() and not entry.name.startswith((".", "__"))
    )
    assert connector_names, "no connector modules found -- the sweep would pass vacuously"

    named = [name for name in connector_names if f'"{name}"' in source or f"'{name}'" in source]

    assert named == [], (
        "the enqueue path names connectors, so a pull window is no longer written "
        "the same way for all of them -- the day-grain answer would then be "
        f"complete for some connectors and silently empty for others: {named}"
    )
