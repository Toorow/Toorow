"""The model's door onto stopping a collection -- story 63.6, epic 63.

WHY A THIRD DOOR AND NOT JUST A BUTTON. A run that can only be stopped from one
surface is a run the model cannot stop. The pattern is `schedule_mcp`'s, which
AI-119 settled for the same reason: one write, reached by the console, by REST
and by the model, all three going through the SAME function -- here
`execution_progress.stop_collection_run` -- so there is one schedule of
consequences and never a second truth about what stopping means.

AND IT IS `confirmation_mode="human"`. Stopping a collection ends work that is
under way and gives back days that will not be collected again on their own (the
nightly window is recomputed at each dispatch, so the stopped days are never
re-collected automatically). That is not a preference toggle: a person confirms
it. The `prepare/confirm` machine of `bounded_recovery` is a different object --
it exists for durable operations with a reviewable impact, and this one write is
decided from a payload the caller already holds.

WHAT IT PROMISES, AND WHAT IT REFUSES TO PROMISE. The provider call in flight is
NOT interrupted -- `queue._execute_job` is synchronous and the stale sweep is at
5400 s -- so the answer NAMES the window that will finish and reports what the
run kept. Telling a model that a stop interrupts everything would have it report
a false state to a person watching a counter that has not stopped.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def stop_datastream_run(project_id: str, datastream_id: str, execution_id: str):
    """Stop a collection that is under way. Writes to PostgreSQL.

    Refuses every window this run had NOT started, and closes the run. The
    window already being collected is NOT interrupted: a provider call in
    flight cannot be cancelled, so it finishes and its rows land. Nothing
    already collected is undone -- the raw zone is append-only.

    Returns `windows_refused`, `window_in_flight` (the one that will finish,
    or `null`), `days_kept` and `rows_kept` (`null` when the run measured
    nothing -- never `0`), and `stopped_at`.

    Refusals: `not_found` (no such run in this project), `run_not_running`
    (it already ended), `not_a_collection_run` (its plan is not a retrieval).
    Asked twice, it answers the same thing and writes nothing the second
    time.

    The days that were refused are NOT re-collected automatically: the
    nightly window is recomputed at every dispatch. Ask for a refetch to
    collect them.
    """
    from core.db import request_connection  # noqa: PLC0415
    from core.execution_progress import StopRefused, stop_collection_run  # noqa: PLC0415
    from core.mcp_scope import caller_identity, refuse_unless_project_scope  # noqa: PLC0415

    # Story 53.1. Trouve par la garde d'AC7 elargie. Le controle ci-dessous
    # prouvait que le run appartient AU PROJET NOMME -- jamais que l'appelant
    # a droit a ce projet-la. Un inconnu pouvait donc arreter la collecte
    # d'une autre organisation, et l'acteur audite etait `mcp:<projet>`,
    # c'est-a-dire personne. C'est une ECRITURE : `edit`.
    identity = caller_identity()
    refuse_unless_project_scope(project_id, identity, minimum_capability="edit")

    try:
        with request_connection(identity) as conn:
            # The scope is proven, not taken on trust: the run must belong to
            # this stream AND this project. A run of another project is the
            # envelope of an absent run, never a payload confirming it exists.
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM app.datastream_executions
                    WHERE id = %s AND project_id = %s AND datastream_id = %s
                    """,
                    (execution_id, project_id, datastream_id),
                )
                if cur.fetchone() is None:
                    return {"error": "not_found", "execution_id": execution_id}
            answer = stop_collection_run(
                conn,
                execution_id=execution_id,
                project_id=project_id,
                # L'acteur est QUI a arrete, pas OU. `mcp:<projet>` nommait
                # le projet arrete comme s'il s'etait arrete lui-meme, et la
                # ligne d'audit ne designait donc personne.
                actor=f"mcp:{identity}",
            )
            conn.commit()
    except StopRefused as exc:
        return {"error": exc.code, "execution_id": execution_id}
    return answer


def register(mcp):
    """Register the stop tool.

    One tool, one write, and the same refusals the route answers -- a door with
    a different set of refusals is a door that lets the model do something a
    person cannot, or the other way round.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp, stop_datastream_run,
        profile="operations", effect="confirmed_write",
        data_class="operational", confirmation_mode="human",
    )
