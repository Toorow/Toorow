"""A confirmation handed to a caller must survive the request that minted it.

`core.db.get_connection` does NOT commit -- it closes, and psycopg rolls back an
open transaction on close; its own docstring says so. `issue_entry_confirmation`
INSERTs and deliberately leaves the transaction to its caller, like every other
store in this codebase.

So an endpoint that opens `get_connection()`, issues a confirmation and returns
201 with the raw secret gives the caller a receipt for a row that never landed.
The next call presents that id, `consume_entry_confirmation` finds no row, and
answers `confirmation_invalid` -- a refusal that reads like a bad secret and is
actually a lost write.

MEASURED, 2026-08-07, on the live deployment: three `POST .../settings/change-sets/
{id}/confirmations` returned 201 with distinct ids; `app.entry_confirmations`
still held exactly ONE row, dated 2026-07-27. Every confirm answered 409. The
consequence reached far past Settings: no Project could confirm a change set, so
`app.project_configuration_versions` was EMPTY on 8 projects out of 8, and the
Datastream assistant refused every preview with "An active Project configuration
version is required" -- for all three modes, not just managed_feed.

This is a source guard rather than a request test on purpose: the defect is a
missing statement in a `with` block, it appeared twice independently, and the
seam suites that would catch it functionally cost 30-45s per test. A guard that
reads the blocks costs milliseconds and cannot pass by accident.
"""

from __future__ import annotations

import re
from pathlib import Path

_API_DIR = Path(__file__).resolve().parents[2] / "core"

#: THE INVARIANT IS THE SECRET, NOT THE FUNCTION NAME. A first version of this
#: guard matched `issue_*confirmation(` and missed `_prepare_draft_confirmation`,
#: which calls `prepare_confirmation` -- so the wizard's own confirmation went on
#: being returned and rolled back. What matters is whether the endpoint hands a
#: raw secret back: if it does, the row it came from must survive the request.
#: Callers get renamed; that does not.
_ISSUERS = re.compile(r"confirmation_secret")

#: What makes the INSERT durable. `conn.transaction()` commits its block on a
#: clean exit, so a block that uses it is as safe as an explicit commit.
_DURABLE = (
    "conn.commit()",
    "conn.transaction()",
    "with transaction(",
)

#: `execute_confirmed_operation` is NOT durable on its own, and believing it was
#: cost a materialization that returned 201 for rows that never landed.
#: It wraps its mutation in `conn.transaction()`, and psycopg3 makes that a
#: SAVEPOINT when a transaction is already open -- which it always is here,
#: because the endpoint READ the review first, and a SELECT opens one. Releasing
#: a savepoint commits nothing; `get_connection` then closes and rolls the whole
#: thing back. The comment in story 56.3 that says the row "is committed once it
#: returns" is true only of a connection that was idle, and none of these are.
#: Measured 2026-08-07: `POST .../materialize` -> 201 with a datastream_id, and
#: app.datastreams, app.datastream_setup_materializations and app.operations all
#: held ZERO rows for it, with the draft still `draft`.


def _blocks_opening_a_connection(source: str) -> list[tuple[int, str]]:
    """Return (line number, body) for each `with get_connection() as conn:` block."""
    lines = source.split("\n")
    blocks: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        if "with get_connection() as conn:" not in line:
            continue
        indent = len(line) - len(line.lstrip())
        body: list[str] = []
        for following in lines[index + 1 :]:
            if following.strip() and (len(following) - len(following.lstrip())) <= indent:
                break
            body.append(following)
        blocks.append((index + 1, "\n".join(body)))
    return blocks


def _enclosing_function(source: str, line_number: int) -> str:
    """The endpoint the block sits in -- the secret is returned from its body."""
    lines = source.split("\n")
    start = next(
        (i for i in range(line_number - 1, -1, -1) if lines[i].startswith("async def ")),
        0,
    )
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith(("async def ", "def "))),
        len(lines),
    )
    return "\n".join(lines[start:end])


def test_every_issued_confirmation_is_committed_before_its_secret_is_returned() -> None:
    offenders: list[str] = []
    for path in sorted(_API_DIR.glob("*_api.py")):
        source = path.read_text(encoding="utf-8")
        for line_number, body in _blocks_opening_a_connection(source):
            if not _ISSUERS.search(_enclosing_function(source, line_number)):
                continue
            if any(marker in body for marker in _DURABLE):
                continue
            offenders.append(f"{path.name}:{line_number}")
    assert offenders == [], (
        "these endpoints return a confirmation secret for a row that is rolled "
        f"back on connection close: {', '.join(offenders)}"
    )
