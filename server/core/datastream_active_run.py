"""Which run holds a Datastream, said in words -- story 58.4.

WHAT THIS EXISTS FOR. `uq_datastream_executions_active` has held one
non-terminal execution per Datastream since migration 042, and it had never
spoken to anybody: `datastream_collection_api._open_refetch_run` observes it, answers `None` and
says nothing, so a re-collection asked for while a run was in flight was queued
with no run line of its own and no sentence anywhere explaining the silence.

IT NEVER REFUSES, AND THAT IS THE DECISION (Jean, 2026-08-06). The refetch route
answers `202` whether or not a run holds the Datastream -- « la personne veut
reessayer d avoir sa journee » -- so this module produces a FACT the answer
carries, not a `409`. `datastream_collection_api._refetch_datastream`'s own docstring already
said it: "It is never a reason to refuse the refetch: the pull is the point".
That behaviour is preserved; what changes is that the console can now say what
happened instead of showing a re-collection with no progress line and no reason.

IT MEASURES ONE CAUSE AND CLAIMS NO OTHER. `execution_progress.open_collection_run`
returns `None` for two different reasons -- a concurrent active execution, or a
Datastream with no usable plan/mapping version -- and nothing in that `None` says
which. So this module asks the ONE question it can answer on rows
(`is there an active execution for this pair?`) and answers nothing at all when
there is not: an absent row here means "not this cause", never "that other one".

NO STATE LITERAL. `ACTIVE_STATES` is `core.execution_states`, which is also what
the migration's partial index is compared against
(`tests/conformance/test_execution_state_registry.py`). A hand-written list here
would drift from the index the day a state is added, and this module would then
report "nothing is running" about a Datastream the database is refusing to let a
second run into.
"""

from __future__ import annotations

import logging
from typing import Any

from core.execution_states import ACTIVE_STATES

logger = logging.getLogger(__name__)

#: The machine word for the fact, and the sentence a person reads. Both travel
#: on the wire: a console that composed the sentence itself would be a second
#: wording of one fact, and the two would disagree within a story or two.
ACTIVE_RUN_CODE = "collection_already_running"
ACTIVE_RUN_MESSAGE = "A collection is already running on this Datastream."


def read_active_run(
    conn, *, datastream_id: str, project_id: str
) -> dict[str, Any] | None:
    """The execution holding this Datastream, or `None` when none does.

    THE STATEMENT CARRIES BOTH COLUMNS. `datastream_id = %s AND project_id = %s`
    on one statement is the shape AI-219 asks of every read that takes a stream
    id from a caller -- reading the row and comparing in Python re-opens the race
    it closes.

    Never raises: this is instrumentation on the path of a write that must land.
    A read that failed answers `None` and logs, exactly like the run it explains.
    """
    if not datastream_id or not project_id:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, state, state_changed_at
                FROM app.datastream_executions
                WHERE datastream_id = %s AND project_id = %s
                  AND state = ANY(%s)
                ORDER BY state_changed_at DESC, id DESC
                LIMIT 1
                """,
                (datastream_id, project_id, list(ACTIVE_STATES)),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 -- never blocks the pull it annotates
        logger.warning(
            "datastream_active_run: read_failed ds=%s: %s", datastream_id, exc
        )
        return None
    if not row:
        return None
    # THE TIMESTAMP LEAVES HERE AS A STRING, and that is not a style choice: the
    # stock `JSONResponse` of `admin_api` renders AFTER the handler has returned,
    # outside its `try`, so a `timestamptz` in the payload is a bare `500` with an
    # empty body that no error branch of the route can ever see. Measured on the
    # disposable cluster while writing this story: the held-run walk answered
    # exactly that. `datastream_daily_breakdown_api` met the same trap and solved
    # it with a safe encoder; this one has a single value and converts it.
    since = row[2]
    return {
        "execution_id": str(row[0]),
        "state": row[1],
        "since": since.isoformat() if hasattr(since, "isoformat") else since,
        "code": ACTIVE_RUN_CODE,
        "message": ACTIVE_RUN_MESSAGE,
    }
