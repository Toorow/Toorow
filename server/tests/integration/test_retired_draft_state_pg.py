"""The wizard draft state `exited` is retired, and stays retired. Migration 284.

WHY THIS FILE EXISTS. `exited` was one of four states declared by migration 134's
CHECK constraint and was **never written by anything** -- no `UPDATE ... SET
state='exited'`, no INSERT, no Python constant, no API field. Its five readers
all spelled the resumable set `draft OR exited`, so the second disjunct was
permanently false and the state was unreachable.

It is retired rather than given a writer, and the reason is worth pinning in a
test rather than only in a comment: the obvious repair -- reuse it to mean
"abandoned" -- is a SEMANTIC INVERSION. Both the partial unique index and the
resume query counted `exited` as LIVE (it occupied the Project's single
resumable-draft slot, and activation accepted it as a legal predecessor of
`materialized`). A draft marked abandoned that way would still be resumable and
still be materializable.

`archived` is the state that could carry a terminal path -- declared, outside the
resumable predicate, and deliberately still unwritten. Whether a draft ends by an
explicit abandon or by expiry, and after how long, is report 05's open question 3
and belongs to Jean. This file therefore asserts that the ROOM for that answer is
still there, which is the half a retirement could easily destroy by accident.

Skipped without TEST_POSTGRES_DSN:
    python scripts/disposable_postgres.py up
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import psycopg
import pytest

_DSN = os.environ.get("TEST_POSTGRES_DSN", "")

pytestmark = pytest.mark.skipif(not _DSN, reason="Requires TEST_POSTGRES_DSN")

_SERVER_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = _SERVER_ROOT.parent


@pytest.fixture()
def conn():
    connection = psycopg.connect(_DSN)
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def test_the_database_refuses_the_retired_state(conn):
    """The CHECK constraint is the guard, so the state cannot come back by hand.

    A comment saying "nothing writes this" is worth exactly as much as the next
    person's grep. The constraint is worth more.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'datastream_setup_drafts_state_check'"
        )
        definition = cur.fetchone()[0]
    assert "exited" not in definition, (
        f"the retired state is still admitted by the CHECK constraint: {definition}"
    )
    for state in ("draft", "materialized", "archived"):
        assert state in definition, (
            f"{state!r} left the constraint: migration 284 retires ONE state, and "
            f"`archived` in particular is the room the terminal path still needs"
        )


def test_the_resumable_index_no_longer_names_the_retired_state(conn):
    """The partial unique index is what made `exited` count as LIVE.

    This is the assertion that catches the semantic inversion: had `exited` been
    reused for "abandoned" instead of retired, it would still be sitting inside
    this predicate, still holding the Project's one resumable slot.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'uq_datastream_setup_draft_resumable'"
        )
        row = cur.fetchone()
    assert row is not None, "the one-resumable-draft-per-Project index disappeared"
    definition = row[0]
    assert "exited" not in definition, definition
    assert "'draft'" in definition, definition
    assert "archived" not in definition, (
        "`archived` must stay OUTSIDE the resumable predicate -- that is exactly "
        "what makes it usable as the terminal state the product still owes"
    )


def test_no_source_file_reads_the_retired_state():
    """The five readers are gone, and a sixth must not appear.

    Scoped to `server/core` and the console's own source. The migration that
    retires the state keeps the word (it explains itself), and so does this file.
    """
    roots = [
        _SERVER_ROOT / "core",
        _REPO_ROOT / "ui" / "admin" / "src",
    ]
    pattern = re.compile(r"""["']exited["']""")
    offenders: list[str] = []
    for root in roots:
        for path in root.rglob("*"):
            if path.suffix not in {".py", ".ts", ".tsx"} or not path.is_file():
                continue
            if "__pycache__" in path.parts or "node_modules" in path.parts:
                continue
            if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
                offenders.append(str(path.relative_to(_REPO_ROOT)))
    assert not offenders, (
        f"{offenders} name the retired draft state `exited`. It has no writer and "
        "the database refuses it; if a terminal state is wanted, `archived` is the "
        "one shaped for it (report 05, open question 3)."
    )
