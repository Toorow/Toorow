"""Record an AI Path from the tool calls that actually happen (Story 49.6).

`context-hub.md` says what an AI Path is: *"trace evidence showing which governed
nodes, knowledge and Skills were used for an answer"*, and its `Incomplete if`
ends with *"AI usage paths and their evidence cannot be inspected or evaluated"*.

`core.ai_paths` shipped the whole owner -- `begin_path`, `append_step`,
`finalize_path`, `load_path`, `list_paths`, `assess` -- with migration 150 behind
it, and **nothing ever called it**. A writer with no caller records nothing, so
the criterion stayed open with a complete implementation sitting behind it. This
module is the caller.

Why a middleware of its own
---------------------------
`core.tracing` already wraps every tool call, and putting the recording there was
the obvious move. It is the wrong one: that middleware returns a passthrough when
no OpenTelemetry tracer is configured, so AI Paths would silently stop being
recorded whenever tracing happened to be off. Evidence that disappears with an
unrelated setting is not evidence.

One path per interaction, not per call
--------------------------------------
An answer is usually several tool calls. They are grouped by the W3C trace id the
client sends in `_meta` -- the same identifier `ai_paths._validate_trace_id`
already expects, which is what that function was written for. Calls without a
trace id each get their own path, because guessing that two unrelated calls
belong together would fabricate a connection the evidence does not support.

Three refusals worth stating
----------------------------
* **A call with no resolvable Project is not recorded.** A path is Project-scoped
  evidence; inventing a scope to have something to write would be worse than the
  gap.
* **Recording never breaks a tool call.** Every failure here is caught and
  logged. Evidence collection that can take down the thing it observes is a
  liability, not a control.
* **The path is left `recording`, and that is honest.** Nothing in the protocol
  tells us an interaction has ended, so finalizing on the last call we happened
  to see would stamp a conclusion nobody reached. `recording` means exactly what
  it says: in progress, or abandoned. `finalize_path` stays available for a
  caller that genuinely knows the answer is complete.

Live emission (Story 54.1)
--------------------------
The same middleware now also *emits* what it observes, while it observes it, as
MCP ``notifications/progress``. Four properties, each of them a refusal:

1. **One middleware, not two.** A second recorder would produce a second AI Path
   per interaction -- two stores, two readers, two truths. The emission is
   grafted onto :class:`AiPathMiddleware`; nothing new is mounted.
2. **Emitted while walking, never recapitulated.** The ``JOB`` line goes out
   before ``call_next`` is awaited, every crossed step goes out at the moment it
   is crossed, and the terminal step goes out before the result is returned. A
   single notification posted as the call returns is blind waiting with a summary
   at the end, which is the thing this story exists to remove.
3. **The five levels are not persisted, and still are not.** ``JOB / SKILL /
   PROCEDURE / CONTEXT / TOOL`` are a *reading grid* computed by :func:`level_of`
   from values already recorded -- the arbitration of 2026-08-01 settled that,
   and no ``level`` column exists. ``app.ai_path_steps`` carries six
   ``step_kind`` values (migration 150) and an ordered sequence of N steps, not
   five rungs.

   What DID change, and it is a different thing: **the crossings themselves are
   now kept.** Until migration 176 this middleware persisted exactly one
   ``tool_call`` step per call while the walk's crossings -- and the candidates
   Story 54.2 judged at each one -- travelled on the progress stream and nowhere
   else. A walk was therefore observable only while somebody watched it, and a
   later reader had to say ``branch count unknown``. Crossings emitted through
   :func:`emit_step` / :func:`emit_step_sync` are buffered on the emitter and
   drained into ``app.ai_path_steps`` by :func:`record_tool_call`, with their
   ``detail``, whether or not a ``progressToken`` was sent. The five rungs stay
   derived; the steps stop being ephemeral.
4. **Nothing emitted is model reasoning.** The payload has a closed key set built
   from recorded values. In particular the ``JOB`` line names the tool and the
   *keys* of its arguments, never their values: ``append_step`` records
   ``tool_name`` and not ``arguments``, so emitting argument values would make
   the stream a second store of things the path never held. This is a deliberate
   divergence from the story's derivation hint ("``tool_name`` + ``arguments``
   (and ``query`` when present)") -- AC5 says the message is *composed from
   recorded values*, and a free-text ``query`` is not one. Stated rather than
   applied in silence.

Without a ``progressToken`` in the client's ``_meta`` there is no
``notifications/progress`` to send, so nothing is composed and nothing is sent:
behaviour identical to before this story. "Universal host compatibility" is not
achievable over this protocol and is not claimed.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

#: Every recorded step is a tool call. `core.ai_paths.STEP_KINDS` carries the
#: other kinds (knowledge reads, Skill steps) for callers that know more than a
#: middleware can: from here, a tool call is all that is observable.
STEP_KIND_TOOL_CALL = "tool_call"
#: Story 45.7 -- le pas d'une Skill, franchi. Le vocabulaire de
#: `app.ai_path_steps` le declarait depuis la migration 150 ; rien ne l'ecrivait.
STEP_KIND_SKILL_STEP = "skill_step"

#: This Result-producing tool owns its AI Path transaction. The generic
#: middleware must therefore not append a second path after the tool returns.
RESULT_EXECUTION_TOOL_NAME = "execute_analyze_query_spec"
SEARCH_CONTEXT_TOOL_NAME = "search_context"

_RESULT_PATH_OUTCOME = {
    "success": "succeeded",
    "empty": "succeeded",
    "degraded": "succeeded",
    "refused": "refused",
    "unavailable": "unavailable",
}

#: L'identifiant de l'appel EN COURS, pose par le middleware qui possede
#: `_meta` et lu par les outils qui doivent s'y rattacher.
#:
#: ⚠️ PAS `tracing.current_trace_id_hex()`. Relecture adversariale du
#: 2026-08-05 : celui-la rend `None` tant que `TRACING_ENABLED` est faux --
#: son defaut, et rien dans `deploy.sh` ne le pose. Le producteur du pas de
#: Skill lisait donc toujours `None`, et AUCUN pas n'etait emis en
#: configuration deployee. Pire : le consommateur, lui, lit le `traceparent`
#: du client -- les deux bouts etaient clefes sur deux identifiants
#: differents. C'est le meme defaut que ce module refuse en tete de fichier :
#: << une preuve qui disparait avec un reglage sans rapport n'est pas une
#: preuve >>.
_ACTIVE_TRACE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "toorow_active_trace", default=None
)


def current_call_trace_id() -> str | None:
    """L'identifiant que le middleware a lu du `_meta` de CET appel."""
    return _ACTIVE_TRACE.get()


def _trace_id_of(meta: Any) -> str | None:
    """Pull the W3C trace id out of a `traceparent`, if the client sent one."""
    try:
        if isinstance(meta, Mapping):
            values = meta
        else:
            model_dump = getattr(meta, "model_dump", None)
            if not callable(model_dump):
                return None
            values = model_dump()
            if not isinstance(values, Mapping):
                return None
        header = values.get("traceparent") or values.get("traceParent")
    except Exception:  # noqa: BLE001 -- malformed metadata is non-authoritative
        return None
    if not isinstance(header, str):
        return None
    parts = header.split("-")
    # W3C version-traceid-spanid-flags. A merely 32-character value is not a
    # trace id: passing one to `begin_path` would turn malformed client metadata
    # into a rollback of the analytical Result transaction.
    if (
        len(parts) == 4
        and len(parts[0]) == 2
        and parts[0] != "ff"
        and len(parts[1]) == 32
        and parts[1] != "0" * 32
        and len(parts[2]) == 16
        and parts[2] != "0" * 16
        and len(parts[3]) == 2
    ):
        try:
            int("".join(parts), 16)
        except ValueError:
            return None
        if header == header.lower():
            return parts[1]
    return None


def _skill_step_of(
    conn: Any, *, trace_id: str | None, tool_name: str | None, project_id: str | None = None
) -> tuple[str, str, str] | None:
    """`(skill_version_id, skill_step_id, procedure_id)` si cet appel franchit un pas.

    Deux conditions, et les deux sont necessaires : la trace a bien PRIS cette
    Skill (memoire de session), et le pas epingle EXISTE dans la version servie
    (`describes_a_step`, en base). La premiere sans la seconde laisserait passer
    un pas qu'une version ne declare plus.
    """
    from core import skill_steps  # noqa: PLC0415

    try:
        observed = skill_steps.observed_step(trace_id, tool_name, project_id=project_id)
        if observed is None:
            return None
        skill_version_id, skill_step_id = observed
        parsed = skill_steps.parse_version_reference(skill_version_id)
        if parsed is None:
            return None
        procedure_id, version_number = parsed
        # SOUS POINT DE REPRISE : une instruction refusee avorte la transaction
        # entiere en psycopg 3, et le `tool_call` qui suit mourrait avec elle --
        # le piege que ce module documente 100 lignes plus bas, oublie sur cette
        # lecture-ci.
        with conn.transaction():
            found = skill_steps.describes_a_step(
                conn,
                procedure_id=procedure_id,
                version_number=version_number,
                skill_step_id=skill_step_id,
            )
        if not found:
            return None
        return skill_version_id, skill_step_id, procedure_id
    except Exception as exc:  # noqa: BLE001 -- observation never breaks the observed
        _swallowed("skill step not resolved", type(exc).__name__)
        return None


def _resolve_project_id(arguments: dict, conn: Any) -> str | None:
    """Resolve the requested project id ON THE CALLER'S OWN CONNECTION.

    IT NO LONGER GOES THROUGH `core.main._resolve_project`, and the reason is the
    arbitration of 2026-08-25. That resolver now acquires its OWN armed
    connection and resolves the caller's ACCESS -- both right for a tool
    answering a caller, and both wrong here twice over. This recorder already
    holds the armed connection of the very call it observes: opening a second
    one would double the acquisition of every observed call, and it would arm it
    for whoever the ambient token names rather than for the `actor` this path is
    being written for. An instrument that measures its own copy of the
    connection measures nothing.

    Reading `app.projects` through the caller's connection answers the same
    question the seam would have: under the Epic-36 floor a project the actor
    may not see returns no row, so absent and invisible are already one answer
    here -- which is exactly the envelope the arbitration ratified.
    """
    from core.project_resolver import resolve_project_id  # noqa: PLC0415

    raw = arguments.get("project_id")
    try:
        project_id = resolve_project_id(raw if isinstance(raw, str) else None, conn)
    except Exception:  # noqa: BLE001 -- unresolvable OR invisible: a no-record, not an error
        return None
    return project_id or None


def _resolve_scope_on_connection(conn: Any, arguments: dict) -> tuple[str, str] | None:
    """Resolve scope on the caller-owned connection so its access context applies."""
    project_id = _resolve_project_id(arguments, conn)
    if project_id is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.debug("ai_path_recorder: scope lookup failed (%s)", type(exc).__name__)
        return None
    if row is None or not row[0]:
        return None
    return project_id, str(row[0])


def _resolve_scope(arguments: dict) -> tuple[str, str] | None:
    """Compatibility scope lookup for generic, non-request-scoped recorders."""
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            return _resolve_scope_on_connection(conn, arguments)
    except Exception as exc:  # noqa: BLE001
        logger.debug("ai_path_recorder: scope lookup failed (%s)", type(exc).__name__)
        return None


def policy_for(trace_id: str | None, project_id: str | None) -> dict[str, Any]:
    """The policy a path pins at opening: the served Skill's sequence, when there is one.

    Until 2026-09-05 every path pinned `{"recorded_by": "mcp_tool_middleware"}`
    and every assessment read `unverifiable` -- "the pinned policy declares no
    required or forbidden node". A trace that took a Skill HAS an expectation:
    the steps the served version declared, the required ones first. Pinned here,
    at opening, against the version served -- a Skill that advances later does
    not relabel a walk already made.
    """
    snapshot: dict[str, Any] = {"recorded_by": "mcp_tool_middleware"}
    try:
        from core import skill_steps  # noqa: PLC0415

        sequences = skill_steps.served_sequences(trace_id, project_id=project_id)
    except Exception as exc:  # noqa: BLE001 -- a policy is never guessed
        _swallowed("served sequences unreadable", type(exc).__name__)
        return snapshot
    expected = [
        {"skill_version": version, "step": step["step"], "tool": step["tool"], "required": bool(step["required"])}
        for version, steps in sequences.items()
        for step in steps
        if step.get("tool")
    ]
    if expected:
        snapshot["expected_skill_steps"] = expected
        snapshot["ordered_skill_steps"] = True
    return snapshot


def _walk_completed_by(
    conn, *, path_id: str, project_id: str, trace_id: str | None, crossed_now: tuple[str, str]
) -> bool:
    """True when THIS crossing is the one that completes the Skill's contract.

    Round 2 of the Opus review proved the previous rule -- « the interaction is
    fulfilled » -- fired again on every later crossing call of the same trace,
    opening and closing a one-step path each time. The closure belongs to one
    call: the one whose step was the last required step still missing. A retry
    of an already-crossed step completes nothing and closes nothing.
    """
    from core import skill_steps  # noqa: PLC0415

    sequences = skill_steps.served_sequences(trace_id, project_id=project_id)
    required = {
        (version, step["step"]) for version, steps in sequences.items() for step in steps if step.get("required")
    }
    # Without a trace nothing was served to it, so nothing is required (round 3:
    # the no-trace query below was dead -- `served_sequences(None)` is empty).
    if not trace_id or not required or crossed_now not in required:
        return False
    with conn.cursor() as cur:
        # THE ROW JUST APPENDED FOR THIS CROSSING IS EXCLUDED: it is already in the
        # table when this runs, and counting it would make `required - before` never
        # equal `{crossed_now}` -- no walk would ever close. Proven on rows by
        # `test_ai_path_closure_pg.py` (round 3: the mock harness never ran the SQL).
        # Exactly ONE row is excluded -- the latest one of this crossing on this path.
        # Excluding every row of the (path, version, step) also removed an earlier
        # crossing of the same step, and a retry then read as the completing call.
        cur.execute(
            "SELECT s.skill_version_id, s.skill_step_id FROM app.ai_path_steps s "
            "JOIN app.ai_paths p ON p.id = s.path_id "
            "WHERE p.project_id = %s AND p.w3c_trace_id = %s AND s.skill_version_id IS NOT NULL "
            "AND NOT (s.path_id = %s AND s.ordinal = ("
            "    SELECT max(ordinal) FROM app.ai_path_steps"
            "     WHERE path_id = %s AND skill_version_id = %s AND skill_step_id = %s))",
            (project_id, trace_id, path_id, path_id, crossed_now[0], crossed_now[1]),
        )
        crossed_before = {(str(a), str(b)) for a, b in cur.fetchall()}
    return required - crossed_before == {crossed_now}


def _walk_fulfilled(conn, *, path_id: str, project_id: str, trace_id: str | None) -> bool:
    """True when every required step of every Skill served to this trace has been crossed IN THIS INTERACTION.

    Over the interaction, not one path: the Skill's first required step is
    usually the Result execution, which records on its own finalized path, while
    the steps around it record on the interaction's path (Opus review of
    efe127b7, finding 2 -- read on one path, the walk never closed).
    """
    from core import skill_steps  # noqa: PLC0415

    sequences = skill_steps.served_sequences(trace_id, project_id=project_id)
    required = {
        (version, step["step"]) for version, steps in sequences.items() for step in steps if step.get("required")
    }
    if not required:
        return False
    with conn.cursor() as cur:
        if trace_id:
            cur.execute(
                "SELECT s.skill_version_id, s.skill_step_id FROM app.ai_path_steps s "
                "JOIN app.ai_paths p ON p.id = s.path_id "
                "WHERE p.project_id = %s AND p.w3c_trace_id = %s AND s.skill_version_id IS NOT NULL",
                (project_id, trace_id),
            )
        else:
            cur.execute(
                "SELECT skill_version_id, skill_step_id FROM app.ai_path_steps "
                "WHERE path_id = %s AND skill_version_id IS NOT NULL",
                (path_id,),
            )
        crossed = {(str(a), str(b)) for a, b in cur.fetchall()}
    return required <= crossed


def _open_path_for(conn, *, project_id: str, w3c_trace_id: str | None) -> str | None:
    """The recording path already open for this trace, if there is one."""
    if not w3c_trace_id:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM app.ai_paths
             WHERE project_id = %s AND w3c_trace_id = %s AND lifecycle = 'recording'
             ORDER BY started_at DESC
             LIMIT 1
            """,
            (project_id, w3c_trace_id),
        )
        row = cur.fetchone()
    return str(row[0]) if row else None


# ---------------------------------------------------------------------------
# The counterpart of "recording never breaks the observed".
# ---------------------------------------------------------------------------
#
# That refusal is right, and this module's docstring argues it well. What it did
# not have was a counterpart: every failure was caught and logged, and NOTHING
# counted them. So a broken recorder and an idle one produced the same thing at
# every layer above -- an absent path, a `200 {"paths": []}`, and a screen
# saying "No AI Path recorded yet".
#
# That is not a new idea. `context-hub.md` already refuses it, one criterion
# above the AI Path one:
#
#     "an unavailable context store is indistinguishable from an empty one, so
#      a model reads 'nothing is defined here' and answers from its own priors"
#
# The document says it of the CONTEXT store. Reading it as covering the PATH
# store of the same surface is a reconciliation, not a new decision -- but it is
# a reading, and it is written here so it can be contradicted.
#
# It cost exactly what the criterion predicts. On 2026-08-04 a real
# `search_context` call recorded zero steps for two structural reasons, while
# the whole chain -- owner, migration, routes, 128 tests -- was green. Nobody
# could have known without reading server logs or querying the table by hand.
#
# Deliberately process-local and unpersisted: a counter that needed a write
# would fail exactly when recording fails. It answers "has THIS instance failed
# to record since boot", which is far less than perfect and infinitely more than
# nothing. Several instances each keep their own.
_RECORDING_FAILURES = 0


def recording_failures() -> int:
    """How many observations this process failed to record since it started."""
    return _RECORDING_FAILURES


def _swallowed(reason: str, detail: str) -> None:
    global _RECORDING_FAILURES
    _RECORDING_FAILURES += 1
    logger.warning("ai_path_recorder: %s (%s)", reason, detail)


def record_result_execution(
    conn,
    *,
    org_id: str,
    project_id: str,
    actor: str,
    tool_name: str,
    execute: Callable[[str], dict[str, Any]],
    arguments: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute and finalize one Result-producing call without committing.

    *arguments* are the call's own, so the tool-call step of a Result's path
    carries what was chosen (the plan version executed) like any other step
    (`choices_of`, 2026-09-05).

    The caller owns the one transaction. The callback receives the path id
    before inserting the immutable Result; any failure therefore rolls back the
    path, its steps and the Result together instead of requiring a forbidden
    update after delivery.
    """
    from core.ai_paths import append_step, begin_path, finalize_path  # noqa: PLC0415

    trace_id = current_call_trace_id()
    # A Result execution gets a fresh path. Reusing `(project, trace)` could mix
    # two actors and races because that pair is neither unique nor locked; prior
    # generic tool observations remain their own honest recording paths.
    path_id = begin_path(
        conn,
        org_id=org_id,
        project_id=project_id,
        actor=actor,
        w3c_trace_id=trace_id,
        policy_snapshot=policy_for(trace_id, project_id),
    )["id"]

    emitter = _ACTIVE_EMITTER.get()
    if emitter is not None:
        emitter.bind_path(path_id)

    result = execute(path_id)
    result_outcome = str(result.get("outcome") or "")
    try:
        path_outcome = _RESULT_PATH_OUTCOME[result_outcome]
    except KeyError as exc:
        raise ValueError(f"unknown delivered Result outcome: {result_outcome}") from exc
    if result.get("ai_path") != path_id:
        raise ValueError("the Result did not bind the recorder's AI Path")

    # Persist crossings that happened inside the call before its terminal tool
    # step. Unlike generic observation these writes are required: a malformed
    # claimed crossing rolls the Result transaction back instead of leaving a
    # finalized path that silently omitted part of the observation.
    for crossing in emitter.drain_crossings() if emitter is not None else ():
        append_step(
            conn,
            path_id=path_id,
            project_id=project_id,
            step_kind=crossing["step_kind"],
            outcome=crossing.get("outcome") or path_outcome,
            tool_name=crossing.get("tool_name"),
            owner_workspace=crossing.get("owner_workspace"),
            owner_object_type=crossing.get("owner_object_type"),
            owner_object_id=crossing.get("owner_object_id"),
            owner_version_id=crossing.get("owner_version_id"),
            evidence_record_id=crossing.get("evidence_record_id"),
            observed_at=crossing.get("observed_at"),
            detail=crossing.get("detail"),
        )

    pinned = _skill_step_of(
        conn, trace_id=trace_id, tool_name=tool_name, project_id=project_id
    )
    if pinned is not None:
        skill_version_id, skill_step_id, procedure_id = pinned
        append_step(
            conn,
            path_id=path_id,
            project_id=project_id,
            step_kind=STEP_KIND_SKILL_STEP,
            outcome=path_outcome,
            owner_workspace="context-hub",
            owner_object_type="procedure",
            owner_object_id=procedure_id,
            skill_version_id=skill_version_id,
            skill_step_id=skill_step_id,
            #  Meme raison qu'au recorder generique : le pas nomme l'outil qui
            #  l'a franchi, plutot que de laisser deduire par adjacence.
            tool_name=tool_name,
        )

    append_step(
        conn,
        path_id=path_id,
        project_id=project_id,
        step_kind=STEP_KIND_TOOL_CALL,
        outcome=path_outcome,
        tool_name=tool_name,
        detail=choices_of(arguments),
        **(owner_from_choices(conn, arguments) or {}),
    )
    finalize_path(conn, path_id=path_id, project_id=project_id, outcome=path_outcome)
    return result


class _SearchContextObservation:
    """One fresh search path and its still-open request-scoped transaction."""

    def __init__(
        self,
        *,
        manager: Any,
        conn: Any,
        path_id: str,
        project_id: str,
        trace_id: str | None,
        pinned_skill: tuple[str, str, str] | None,
    ) -> None:
        self.manager = manager
        self.conn = conn
        self.path_id = path_id
        self.project_id = project_id
        self.trace_id = trace_id
        self.pinned_skill = pinned_skill
        self.closed = False


def _close_search_context_observation(
    observation: _SearchContextObservation,
) -> None:
    if observation.closed:
        return
    observation.closed = True
    try:
        observation.manager.__exit__(None, None, None)
    except Exception as exc:  # noqa: BLE001 -- closing evidence never breaks search
        _swallowed("search observation connection not closed", type(exc).__name__)


def _begin_search_context_observation(
    *, arguments: dict, meta: Any, actor: str, emitter: "_PathEmitter"
) -> _SearchContextObservation | None:
    """Open the dedicated v2 path before search executes, never reusing a trace."""
    from core.ai_paths import AI_PATH_CONTENT_V2, begin_path  # noqa: PLC0415
    from core.db import request_connection  # noqa: PLC0415

    manager = None
    conn = None
    try:
        manager = request_connection(actor)
        conn = manager.__enter__()
        scope = _resolve_scope_on_connection(conn, arguments)
        if scope is None:
            try:
                manager.__exit__(None, None, None)
            finally:
                manager = None
                conn = None
            return None
        project_id, org_id = scope
        trace_id = _trace_id_of(meta)
        path_id = begin_path(
            conn,
            org_id=org_id,
            project_id=project_id,
            actor=actor,
            w3c_trace_id=trace_id,
            policy_snapshot={
                **policy_for(trace_id, project_id),
                "content_hash_contract": AI_PATH_CONTENT_V2,
            },
        )["id"]
        emitter.bind_path(path_id)
        # Freeze the optional pin before the call. Session memory can change
        # while search runs; evidence must describe the instruction that opened
        # this execution, not whichever instruction happens to be current later.
        pinned = _skill_step_of(
            conn,
            trace_id=trace_id,
            tool_name=SEARCH_CONTEXT_TOOL_NAME,
            project_id=project_id,
        )
        return _SearchContextObservation(
            manager=manager,
            conn=conn,
            path_id=path_id,
            project_id=project_id,
            trace_id=trace_id,
            pinned_skill=pinned,
        )
    except Exception as exc:  # noqa: BLE001 -- observation never breaks search
        if conn is not None:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001 -- retain the original failure signal
                pass
        if manager is not None:
            try:
                manager.__exit__(None, None, None)
            except Exception:  # noqa: BLE001 -- retain the original failure signal
                pass
        _swallowed("search observation not opened", type(exc).__name__)
        return None


def _abort_search_context_observation(
    observation: _SearchContextObservation | None,
) -> None:
    """Rollback an unfinished observer transaction exactly once."""
    if observation is None or observation.closed:
        return
    try:
        observation.conn.rollback()
    except Exception as exc:  # noqa: BLE001 -- cancellation must still propagate
        _swallowed("search observation rollback failed", type(exc).__name__)
    finally:
        _close_search_context_observation(observation)


def _finalize_search_context_observation(
    observation: _SearchContextObservation | None,
    *,
    emitter: "_PathEmitter",
    outcome: str,
) -> str | None:
    """Atomically persist crossings, frozen pin, terminal and finalized header."""
    if observation is None:
        return None
    from core.ai_paths import append_step, finalize_path  # noqa: PLC0415

    conn = observation.conn
    try:
        for crossing in emitter.drain_crossings():
            append_step(
                conn,
                path_id=observation.path_id,
                project_id=observation.project_id,
                step_kind=crossing["step_kind"],
                outcome=crossing.get("outcome") or outcome,
                tool_name=crossing.get("tool_name"),
                owner_workspace=crossing.get("owner_workspace"),
                owner_object_type=crossing.get("owner_object_type"),
                owner_object_id=crossing.get("owner_object_id"),
                owner_version_id=crossing.get("owner_version_id"),
                evidence_record_id=crossing.get("evidence_record_id"),
                observed_at=crossing.get("observed_at"),
                detail=crossing.get("detail"),
            )
        if observation.pinned_skill is not None:
            skill_version_id, skill_step_id, procedure_id = observation.pinned_skill
            append_step(
                conn,
                path_id=observation.path_id,
                project_id=observation.project_id,
                step_kind=STEP_KIND_SKILL_STEP,
                outcome=outcome,
                owner_workspace="context-hub",
                owner_object_type="procedure",
                owner_object_id=procedure_id,
                skill_version_id=skill_version_id,
                skill_step_id=skill_step_id,
                observed_at=datetime.now(timezone.utc),
            )
        append_step(
            conn,
            path_id=observation.path_id,
            project_id=observation.project_id,
            step_kind=STEP_KIND_TOOL_CALL,
            outcome=outcome,
            tool_name=SEARCH_CONTEXT_TOOL_NAME,
            observed_at=datetime.now(timezone.utc),
        )
        finalize_path(
            conn,
            path_id=observation.path_id,
            project_id=observation.project_id,
            outcome=outcome,
        )
        conn.commit()
        return observation.path_id
    except Exception as exc:  # noqa: BLE001 -- observation never breaks search
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001 -- retain the original failure signal
            pass
        _swallowed("search observation rolled back", type(exc).__name__)
        return None
    finally:
        _close_search_context_observation(observation)


def record_tool_call(
    *,
    tool_name: str,
    arguments: dict,
    meta: Any,
    outcome: str,
    actor: str,
) -> str | None:
    """Record one tool call against its interaction's AI Path.

    Returns the path id, or None when the call carried nothing that could be
    recorded honestly. Never raises: see the module docstring.
    """
    from core.ai_paths import append_step, begin_path  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        scope = _resolve_scope(arguments)
        if scope is None:
            return None
        project_id, org_id = scope
        trace_id = _trace_id_of(meta)

        with get_connection() as conn:
            path_id = _open_path_for(conn, project_id=project_id, w3c_trace_id=trace_id)
            if path_id is None:
                path_id = begin_path(
                    conn,
                    org_id=org_id,
                    project_id=project_id,
                    actor=actor,
                    w3c_trace_id=trace_id,
                    # What the run will be judged against, pinned before it runs:
                    # the served Skill's sequence when the trace took one.
                    policy_snapshot=policy_for(trace_id, project_id),
                )["id"]
            # The crossings this call made, BEFORE the step that describes the
            # call itself: they happened first, and `append_step` allocates the
            # ordinal, so appending them in order is what puts them in order.
            emitter = _ACTIVE_EMITTER.get()
            for crossing in emitter.drain_crossings() if emitter is not None else ():
                # SAVEPOINT, and it is what makes the line below TRUE. Catching
                # the exception was never enough: a refused statement aborts the
                # whole transaction, so every later statement -- including the
                # `tool_call` step that describes the call itself -- died with
                # `InFailedSqlTransaction`. The comment said "one bad crossing is
                # not the walk"; the database made one bad crossing the end of
                # it. Measured 2026-08-04 on a real `search_context` call, which
                # recorded NOTHING, header included.
                #
                # It is the trap `test_ai_paths_postgres.py` already documents
                # for its own fixtures, arrived at from the other side.
                try:
                    with conn.transaction():
                        append_step(
                            conn,
                            path_id=path_id,
                            project_id=project_id,
                            step_kind=crossing["step_kind"],
                            outcome=crossing.get("outcome") or outcome,
                            tool_name=crossing.get("tool_name"),
                            owner_workspace=crossing.get("owner_workspace"),
                            owner_object_type=crossing.get("owner_object_type"),
                            owner_object_id=crossing.get("owner_object_id"),
                            owner_version_id=crossing.get("owner_version_id"),
                            evidence_record_id=crossing.get("evidence_record_id"),
                            observed_at=crossing.get("observed_at"),
                            detail=crossing.get("detail"),
                        )
                except Exception as exc:  # noqa: BLE001 -- one bad crossing is not the walk
                    _swallowed("crossing not recorded", type(exc).__name__)
            # ── Story 45.7 : le pas de Skill, AVANT l'appel qui le franchit.
            #
            # `skill_step` etait un genre de pas de premiere classe qu'AUCUN
            # emetteur ne produisait : `LEVEL_SKILL` etait un barreau mort, et un
            # Expected AI Path exigeant un pas de Skill ne pouvait pas passer
            # puisque le cote observe ne pouvait pas en contenir.
            #
            # Il precede le `tool_call` parce qu'il le CONTIENT : on franchit le
            # pas 2, et le franchir consiste a appeler cet outil.
            #
            # Le referent est interroge en base plutot que cru sur parole : la
            # memoire de session dit ce qui a ete servi, `describes_a_step` dit
            # ce qui existe. Une trace qui epingle un pas inexistant rendrait la
            # comparaison attendu/observe muette au moment ou elle compte.
            pinned = _skill_step_of(
            conn, trace_id=trace_id, tool_name=tool_name, project_id=project_id
        )
            if pinned is not None:
                skill_version_id, skill_step_id, procedure_id = pinned
                try:
                    with conn.transaction():
                        append_step(
                            conn,
                            path_id=path_id,
                            project_id=project_id,
                            step_kind=STEP_KIND_SKILL_STEP,
                            outcome=outcome,
                            owner_workspace="context-hub",
                            owner_object_type="procedure",
                            owner_object_id=procedure_id,
                            skill_version_id=skill_version_id,
                            skill_step_id=skill_step_id,
                            # LE PAS DIT PAR QUOI IL A ETE FRANCHI. Sans ce nom,
                            # la ligne d'adherence porte « le pas 3 de cette
                            # Skill a ete franchi » et rien d'autre : pour savoir
                            # PAR QUEL outil, un lecteur doit supposer que le
                            # `tool_call` d'a cote, un ordinal plus loin, est le
                            # bon -- une jointure par adjacence, dans un magasin
                            # de preuves. Le nom est deja en main ici : c'est le
                            # meme que `_skill_step_of` a compare pour decider
                            # que ce pas etait franchi.
                            tool_name=tool_name,
                        )
                except Exception as exc:  # noqa: BLE001 -- observer never breaks the observed
                    _swallowed("skill step not recorded", type(exc).__name__)
                    # A CROSSING THAT WAS NOT WRITTEN DID NOT HAPPEN for the closure
                    # (round 3, N4): finalizing on it would seal a path whose own
                    # assessment then reports the step skipped.
                    pinned = None
            append_step(
                conn,
                path_id=path_id,
                project_id=project_id,
                step_kind=STEP_KIND_TOOL_CALL,
                outcome=outcome,
                tool_name=tool_name,
                # THE STEP SAYS WHAT WAS CHOSEN (2026-09-05, Jean: « quelle
                # solution tu as choisie »). Ids, short values and lists of them
                # from the call's own arguments -- never prose, never a payload.
                detail=choices_of(arguments),
                # AND WHAT IT REACHED: the governed object the call acted on.
                **(owner_from_choices(conn, arguments) or {}),
            )
            # A WALK THAT KEPT EVERY REQUIRED PROMISE IS COMPLETE (2026-09-05).
            # Nothing in the protocol says an interaction has ended, so a path
            # stayed `recording` for ever and could never be cited. The Skill
            # says when its contract is fulfilled: every required step crossed.
            # Only then is the path finalized -- succeeded when no step failed.
            try:
                # UNDER A SAVEPOINT: a closure that loses a race with a parallel
                # call of the same trace (the immutability trigger refuses the
                # second finalize) must not take this call's steps down with it.
                # ONLY THE CALL THAT CROSSED A REQUIRED STEP MAY CLOSE THE WALK: a
                # later read on the same trace opens a new recording path, and
                # closing it at once (the interaction IS fulfilled) made every late
                # read its own one-step finalized path (measured on 00263).
                with conn.transaction():
                    if pinned is not None and _walk_completed_by(
                        conn,
                        path_id=path_id,
                        project_id=project_id,
                        trace_id=trace_id,
                        crossed_now=(str(pinned[0]), str(pinned[1])),
                    ):
                        from core.ai_paths import finalize_path  # noqa: PLC0415

                        with conn.cursor() as cur:
                            cur.execute(
                                "SELECT count(*) FROM app.ai_path_steps WHERE path_id = %s AND outcome <> 'succeeded'",
                                (path_id,),
                            )
                            failed = int(cur.fetchone()[0])
                        finalize_path(
                            conn, path_id=path_id, project_id=project_id,
                            outcome="succeeded" if failed == 0 else "failed",
                        )
            except Exception as exc:  # noqa: BLE001 -- closure never breaks the observed
                _swallowed("walk closure not applied", type(exc).__name__)
            conn.commit()
        return path_id
    except Exception as exc:  # noqa: BLE001 -- observation never breaks the observed
        _swallowed(f"not recorded for tool={tool_name}", type(exc).__name__)
        return None


# ---------------------------------------------------------------------------
# The reading grid. Computed for display; nothing below is persisted.
# ---------------------------------------------------------------------------

LEVEL_JOB = "JOB"
LEVEL_SKILL = "SKILL"
LEVEL_PROCEDURE = "PROCEDURE"
LEVEL_CONTEXT = "CONTEXT"
LEVEL_TOOL = "TOOL"

#: The five rungs, in the order a walk crosses them. A reading grid, not a
#: vocabulary of persistence: `app.ai_path_steps.step_kind` keeps its six kinds.
LEVELS = (LEVEL_JOB, LEVEL_SKILL, LEVEL_PROCEDURE, LEVEL_CONTEXT, LEVEL_TOOL)

#: `PROCEDURE` and `CONTEXT` share a `step_kind`: a `get_procedure` is a
#: `tool_call`, and a procedure hit inside `search_context` is a
#: `knowledge_read`. The tool name and the reached object type are what separate
#: them, which is why `level_of` needs all three inputs and not just the kind.
_PROCEDURE_OBJECT_TYPES = frozenset({"procedure"})
_PROCEDURE_TOOL_NAMES = frozenset({"get_procedure"})
_CONTEXT_TOOL_NAMES = frozenset({"search_context"})
_CONTEXT_STEP_KINDS = frozenset({"knowledge_read", "semantic_query"})
_TOOL_STEP_KINDS = frozenset({"tool_call", "data_read"})


def level_of(
    step_kind: str | None,
    tool_name: str | None = None,
    owner_object_type: str | None = None,
) -> str | None:
    """Return the reading-grid level of one observed step, or None.

    ``None`` means *nothing maps* -- and a level that maps to nothing is not
    emitted rather than emitted as an empty rung. ``handoff`` is the standing
    example: it is a recorded kind that names no rung of this grid.
    """
    if not step_kind:
        return None
    if step_kind == "skill_step":
        return LEVEL_SKILL
    if owner_object_type in _PROCEDURE_OBJECT_TYPES or tool_name in _PROCEDURE_TOOL_NAMES:
        return LEVEL_PROCEDURE
    if step_kind in _CONTEXT_STEP_KINDS or tool_name in _CONTEXT_TOOL_NAMES:
        return LEVEL_CONTEXT
    if step_kind in _TOOL_STEP_KINDS:
        return LEVEL_TOOL
    return None


# ---------------------------------------------------------------------------
# The emitted payload. A closed key set, composed from recorded values.
# ---------------------------------------------------------------------------

#: Bumped when the payload shape changes in a way a reader must notice.
EMISSION_VERSION = 1

#: What every notification of this channel announces itself as. A reader filters
#: on it rather than guessing from the shape.
EMISSION_KIND = "ai_path_step"

#: The ONLY state a crossed step is emitted in. There is no `pending` and no
#: `active`: a step is emitted because it happened, never to announce that it is
#: about to.
STEP_STATE_OBSERVED = "observed"

#: The only exception, and it is not a prediction: the request itself arrived.
#: Carried by the `JOB` line alone, emitted before the walk starts so the person
#: sees the question being resolved instead of a spinner.
STEP_STATE_STARTED = "started"

STEP_STATES = (STEP_STATE_STARTED, STEP_STATE_OBSERVED)

#: The closed key set of a payload. A reader may rely on every key being present.
PAYLOAD_FIELDS = (
    "v",
    "kind",
    "ordinal",
    "level",
    "state",
    "step_kind",
    "outcome",
    "tool_name",
    "owner_workspace",
    "owner_object_type",
    "owner_object_id",
    "owner_version_id",
    "detail",
)

#: Names a free-prose or model-authored field would plausibly take. `detail` is
#: the one open door in the payload -- Story 54.2 attaches its candidates
#: through it -- so the door has a lock rather than a convention.
BANNED_DETAIL_KEYS = frozenset(
    {
        "analysis",
        "answer",
        "chain_of_thought",
        "commentary",
        "completion",
        "content",
        "explanation",
        "message",
        "narrative",
        "note",
        "notes",
        "prose",
        "rationale",
        "reasoning",
        "summary",
        "text",
        "thought",
        "thoughts",
    }
)

#: A value longer than this is not an identifier, a name or a score; it is
#: prose. It is dropped rather than truncated -- truncated prose is still prose.
DETAIL_VALUE_MAX_CHARS = 200

#: A detail map is a handful of recorded facts about one step, not a document.
#: Raised from 24 by migration 228: the candidate crossing of story 54.2 carries
#: 25 enumerated facts, and the 25th to be built was `candidate_reasons` -- the
#: motive a candidate was rejected, which is the field that story exists for.
#: It was dropped, silently, from the day the retrieval descriptor grew.
DETAIL_MAX_KEYS = 32

#: How many items one recorded LIST may carry. A different budget from the one
#: above, and it used to be the same constant -- so growing a descriptor by one
#: field shortened every candidate list by one item, which is not a relation
#: anybody intended. Kept at 24, the value both budgets shared.
DETAIL_MAX_LIST_ITEMS = 24

#: How many crossings one tool call may put into its path. A walk that crosses
#: more than this is a loop, and a loop must not be able to grow a path without
#: bound; the excess is dropped rather than truncating any single crossing.
CROSSINGS_PER_CALL_MAX = 64

#: Story 65.6 native MCP progress contract. The final evidence contract stays
#: ``observed-ai-path.v1`` in ``core.ai_paths``; this one is explicitly
#: provisional and never carries a persisted ordinal.
PROGRESS_SCHEMA_VERSION = "observed-ai-path-progress.v1"
PROGRESS_EVENT_RECORDING = "recording"
PROGRESS_EVENT_STEP = "step_observed"
LIVE_EVENT_MAX_COUNT = CROSSINGS_PER_CALL_MAX + 1
LIVE_EVENT_MAX_BYTES = 8_192
LIVE_REQUEST_MAX_BYTES = 131_072
#: Leave scheduling margin so the externally measured middleware wall remains
#: below the story's strict 25 ms ceiling even on Windows.
LIVE_SEND_BUDGET_SECONDS = 0.015

# A timed-out framework send is cancelled without extending the request wall.
# Keep explicit ownership until the event loop acknowledges cancellation; the
# done callback below consumes its outcome and removes it deterministically.
_DETACHED_SENDERS: set[asyncio.Task] = set()


def _acceptable_detail_value(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, str):
        return len(value) <= DETAIL_VALUE_MAX_CHARS
    if isinstance(value, (list, tuple)):
        return len(value) <= DETAIL_MAX_LIST_ITEMS and all(
            _acceptable_detail_value(item) for item in value
        )
    return False


def _copy_detail_value(value: Any) -> Any:
    """Detach an accepted detail value from caller-owned mutable containers."""
    if isinstance(value, (list, tuple)):
        return [_copy_detail_value(item) for item in value]
    return value


#: How deep the arguments of a call are read for the choices they carry: a
#: `request.pivot.rows`, an `intent.carriers[].column` -- and no further.
CHOICE_MAX_DEPTH = 4
#: Choice values are ids, names, short enumerations. A longer string is prose or
#: a payload, and neither is a choice.
CHOICE_VALUE_MAX_CHARS = 80


def _choice_scalar(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str) and 0 < len(value) <= CHOICE_VALUE_MAX_CHARS and "\n" not in value:
        return value
    return None


def _collect_choices(prefix: str, value: Any, out: dict[str, Any], depth: int) -> None:
    if isinstance(value, Mapping):
        if depth >= CHOICE_MAX_DEPTH:
            return
        for key, inner in value.items():
            if not isinstance(key, str) or not key:
                continue
            _collect_choices(f"{prefix}.{key}" if prefix else key, inner, out, depth + 1)
        return
    if isinstance(value, (list, tuple)):
        scalars = [_choice_scalar(item) for item in value if not isinstance(item, (Mapping, list, tuple))]
        scalars = [item for item in scalars if item is not None]
        if scalars:
            out[prefix] = scalars[:DETAIL_MAX_LIST_ITEMS]
        mappings = [item for item in value if isinstance(item, Mapping)]
        if mappings and depth < CHOICE_MAX_DEPTH:
            # A list of choices ({datastream_id, column}, {canonical_field_id,
            # aggregation}): one list per inner key, in the order given.
            columns: dict[str, list[Any]] = {}
            for item in mappings[:DETAIL_MAX_LIST_ITEMS]:
                for key, inner in item.items():
                    scalar = _choice_scalar(inner)
                    if isinstance(key, str) and key and scalar is not None:
                        columns.setdefault(key, []).append(scalar)
            for key, values in columns.items():
                out[f"{prefix}[].{key}"] = values
        return
    scalar = _choice_scalar(value)
    if scalar is not None:
        out[prefix] = scalar


#: Which argument names an object a call ACTS ON, most specific first: a Result
#: before the plan it came from, a plan before the View it reads, the View before
#: a Datastream. One owner per step -- the rest of the ids stay in `chose`.
#: (argument key, workspace, object type, parent lookup)
_OWNER_ARGUMENTS: tuple[tuple[str, str, str, str | None], ...] = (
    ("result_id", "analyze", "result", None),
    ("query_spec_version_id", "analyze", "query-spec", "query_spec"),
    ("visualization_spec_version_id", "analyze", "visualization", "visualization"),
    ("semantic_view_version_id", "governance", "semantic-view", "semantic_view"),
    ("view_version_id", "governance", "semantic-view", "semantic_view"),
    ("semantic_view_id", "governance", "semantic-view", None),
    ("canonical_field_id", "governance", "canonical-field", None),
    ("datastream_id", "data", "datastream", None),
    ("path_id", "context-hub", "ai-path", None),
)
_PARENT_LOOKUPS: dict[str, tuple[str, str]] = {
    # version id -> (table, parent column): the console routes the OBJECT and pins the version.
    "query_spec": ("app.query_spec_versions", "query_spec_id"),
    "visualization": ("app.visualization_spec_versions", "visualization_id"),
    "semantic_view": ("app.semantic_view_versions", "view_id"),
}


def _first_named(arguments: Mapping[str, Any], key: str) -> str | None:
    """`key` at the top level, else the first one found one level down (an `intent`, a `request`)."""
    value = arguments.get(key)
    if isinstance(value, str) and value:
        return value
    for inner in arguments.values():
        if isinstance(inner, Mapping):
            candidate = inner.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
            for deeper in inner.values():
                if isinstance(deeper, list):
                    for item in deeper:
                        if isinstance(item, Mapping) and isinstance(item.get(key), str) and item.get(key):
                            return item[key]
    return None


def owner_from_choices(conn: Any, arguments: Mapping[str, Any] | None) -> dict[str, str | None] | None:
    """The governed object a call acted on, read from its own arguments (2026-09-05).

    « Reached: nothing governed » was every tool call's line, while its arguments
    named the plan version executed, the View composed, the canonical field
    pinned. A step now carries that object as its owner -- the most specific one
    -- with its version when the argument is a version id (the parent object is
    looked up once, on the caller's connection; a lookup that fails leaves the
    step without owner rather than with a guessed one).
    """
    if not isinstance(arguments, Mapping) or not arguments:
        return None
    for key, workspace, object_type, parent in _OWNER_ARGUMENTS:
        value = _first_named(arguments, key)
        if not value:
            continue
        if parent is None:
            return {
                "owner_workspace": workspace,
                "owner_object_type": object_type,
                "owner_object_id": value,
                "owner_version_id": None,
            }
        table, column = _PARENT_LOOKUPS[parent]
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(f"SELECT {column} FROM {table} WHERE id = %s", (value,))  # noqa: S608 - table and column are this module's constants
                    row = cur.fetchone()
        except Exception as exc:  # noqa: BLE001 -- an owner is never guessed
            _swallowed("owner parent not resolved", type(exc).__name__)
            return None
        if not row or not row[0]:
            return None
        return {
            "owner_workspace": workspace,
            "owner_object_type": object_type,
            "owner_object_id": str(row[0]),
            "owner_version_id": value,
        }
    return None


def choices_of(arguments: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The choices a call carried, read from its arguments and bounded like any detail.

    What a model CHOSE is in what it asked: which Datastreams, which canonical
    measures in which pivot wells, which family, which columns pinned to which
    field. That is evidence of the path and it was recorded nowhere -- every
    tool_call step carried `detail: null` (measured 2026-09-05 on the reference
    project). Nested mappings are read to `CHOICE_MAX_DEPTH` as dotted keys,
    lists of mappings as one list per inner key; prose-shaped keys, long strings
    and payloads are refused by `sanitize_detail`, which also bounds the keys.
    """
    if not isinstance(arguments, Mapping) or not arguments:
        return None
    out: dict[str, Any] = {}
    # The scope is not a choice: the path is already Project-scoped, and a step
    # whose only argument names the Project recorded nothing before this.
    _collect_choices("", {k: v for k, v in arguments.items() if k not in _SCOPE_KEYS}, out, 0)
    return sanitize_detail(out)


_SCOPE_KEYS = frozenset({"project_id", "org_id", "organization_id"})


def sanitize_detail(detail: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Keep only what a recorded value can look like. Pure, and total.

    Offending entries are dropped, never truncated and never fatal: a step that
    carried one bad key must still reach the person, and an emission that raises
    would break the thing it observes.

    THE OVER-BUDGET CASE NOW SAYS SO. Every other refusal here logs; this one
    -- the only LOSSY one, because it drops keys the caller built correctly --
    logged nothing, and the map it returned was indistinguishable from a
    complete one. That is how `candidate_reasons` disappeared from the story
    54.2 crossing for as long as it took three tests to be read.
    """
    if not isinstance(detail, Mapping) or not detail:
        return None
    kept: dict[str, Any] = {}
    for key, value in detail.items():
        if len(kept) >= DETAIL_MAX_KEYS:
            logger.warning(
                "ai_path_recorder: detail truncated at %d keys -- dropped %s",
                DETAIL_MAX_KEYS,
                sorted(set(detail) - set(kept)),
            )
            break
        if not isinstance(key, str) or not key:
            continue
        if key.lower() in BANNED_DETAIL_KEYS:
            logger.debug("ai_path_recorder: detail key %r refused (prose-shaped)", key)
            continue
        if not _acceptable_detail_value(value):
            logger.debug("ai_path_recorder: detail value for %r refused", key)
            continue
        kept[key] = _copy_detail_value(value)
    return kept or None


def build_step_payload(
    *,
    ordinal: int,
    level: str,
    state: str = STEP_STATE_OBSERVED,
    step_kind: str | None = None,
    outcome: str | None = None,
    tool_name: str | None = None,
    owner_workspace: str | None = None,
    owner_object_type: str | None = None,
    owner_object_id: str | None = None,
    owner_version_id: str | None = None,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The exact dict serialised into a progress notification's ``message``.

    Every key of :data:`PAYLOAD_FIELDS` is present, including the owner quartet
    when the step reached nothing governed: the owner already refuses to invent a
    graph object for an unrepresented call, and the stream refuses with it -- the
    step stays visible with its owner fields null.
    """
    return {
        "v": EMISSION_VERSION,
        "kind": EMISSION_KIND,
        "ordinal": int(ordinal),
        "level": level,
        "state": state,
        "step_kind": step_kind,
        "outcome": outcome,
        "tool_name": tool_name,
        "owner_workspace": owner_workspace,
        "owner_object_type": owner_object_type,
        "owner_object_id": owner_object_id,
        "owner_version_id": owner_version_id,
        "detail": sanitize_detail(detail),
    }


# ---------------------------------------------------------------------------
# The emission seam. One emitter per tool call, reachable from inside the call.
# ---------------------------------------------------------------------------


def progress_token_of(fastmcp_context: Any) -> Any:
    """The client's ``progressToken``, or None when it did not send one.

    Read here rather than left to ``Context.report_progress`` so that *nothing*
    is composed when there is no channel: no payload, no JSON, no call. That is
    what AC7 means by "identical to HEAD".
    """
    try:
        request_context = getattr(fastmcp_context, "request_context", None)
        meta = getattr(request_context, "meta", None)
        return getattr(meta, "progressToken", None)
    except Exception:  # noqa: BLE001 -- an unreadable context is simply not armed
        return None


class _PathEmitter:
    """Keep crossings and dispatch one Result-bound live prefix without blocking."""

    def __init__(self, fastmcp_context: Any, loop: asyncio.AbstractEventLoop | None):
        self._context = fastmcp_context
        self._loop = loop
        self._token = progress_token_of(fastmcp_context)
        self._crossings: list[dict[str, Any]] = []
        self._recording_open = True
        self._path_id: str | None = None
        self._sequence = 0
        self._accepted_bytes = 0
        self._send_budget_remaining = LIVE_SEND_BUDGET_SECONDS
        self._queue: deque[tuple[int, str]] = deque()
        self._lock = threading.Lock()
        self._accepting = True
        self._sender_task: asyncio.Task | None = None
        self._legacy_ordinal = 0
        self._legacy_enabled = True

    @property
    def armed(self) -> bool:
        return self._token is not None and self._context is not None

    @property
    def live_closed(self) -> bool:
        with self._lock:
            return not self._accepting

    def bind_path(self, path_id: str) -> bool:
        """Bind the fresh Result path and enqueue the sole recording event."""
        if not isinstance(path_id, str) or not path_id or len(path_id) > 512:
            self._stop_accepting(clear_pending=False)
            return False
        queued = False
        with self._lock:
            if self._path_id is not None:
                self._accepting = False
                return False
            self._path_id = path_id
            # A crossing before binding cannot be reordered behind a recording
            # event. Fail the live stream closed while keeping persistence.
            if self._crossings:
                self._accepting = False
                return False
            if self.armed:
                queued = self._offer_locked(PROGRESS_EVENT_RECORDING)
        if queued:
            self._kick_sender()
        return queued

    def disable_legacy(self) -> None:
        """Prevent this call from falling back to the unbounded legacy channel."""
        with self._lock:
            self._legacy_enabled = False

    def record_crossing(self, **fields: Any) -> bool:
        """Keep one crossing for persistence, independently of live delivery."""
        fields.setdefault("observed_at", datetime.now(timezone.utc))
        if not fields.get("owner_object_type") or not fields.get("owner_object_id"):
            fields = {**fields, "owner_workspace": None}
        with self._lock:
            if not self._recording_open or len(self._crossings) >= CROSSINGS_PER_CALL_MAX:
                return False
            self._crossings.append(fields)
            return True

    def drain_crossings(self) -> list[dict[str, Any]]:
        with self._lock:
            crossings, self._crossings = self._crossings, []
            self._recording_open = False
            return crossings

    def record_and_route_step(self, fields: Mapping[str, Any]) -> tuple[bool, bool]:
        """Atomically record and select Result FIFO or legacy transport.

        Returns ``(accepted_live, use_legacy_transport)``.
        """
        fields = dict(fields)
        fields.setdefault("observed_at", datetime.now(timezone.utc))
        step = None
        if self.armed:
            try:
                from core.ai_paths import (  # noqa: PLC0415
                    project_observed_ai_path_progress_step,
                )

                step = project_observed_ai_path_progress_step(
                    {key: value for key, value in fields.items() if key != "observed_at"}
                )
            except Exception as exc:  # noqa: BLE001 -- observation never breaks execution
                logger.warning(
                    "ai_path_recorder: unsafe live step refused (%s)", type(exc).__name__
                )

        queued = False
        with self._lock:
            if not self._recording_open or len(self._crossings) >= CROSSINGS_PER_CALL_MAX:
                return False, False
            self._crossings.append(fields)
            if self._path_id is None:
                return False, self._legacy_enabled
            if not self.armed:
                return False, False
            if step is None:
                self._accepting = False
                return False, False
            queued = self._offer_locked(PROGRESS_EVENT_STEP, step=step)
        if queued:
            self._kick_sender()
        return queued, False

    async def emit_legacy(self, **fields: Any) -> bool:
        """Preserve the historical stream for non-Result tools.

        Story 65.6 narrows the new Result-bound contract to
        ``execute_analyze_query_spec``. Existing generic tool progress remains
        byte-compatible until its own qualified migration.
        """
        if not self._legacy_enabled or not self.armed or self._path_id is not None:
            return False
        try:
            ordinal = self._legacy_ordinal
            self._legacy_ordinal += 1
            payload = build_step_payload(ordinal=ordinal, **fields)
            await self._context.report_progress(
                float(ordinal + 1),
                None,
                json.dumps(payload, separators=(",", ":")),
            )
            return True
        except Exception as exc:  # noqa: BLE001 -- observation is fail-safe
            logger.warning(
                "ai_path_recorder: legacy step not emitted (%s)",
                type(exc).__name__,
            )
            return False

    def emit_legacy_from_thread(self, **fields: Any) -> bool:
        if not self._legacy_enabled or not self.armed or self._path_id is not None:
            return False
        loop = self._loop
        if loop is None or loop.is_closed():
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(self.emit_legacy(**fields), loop)
            return bool(future.result(timeout=_EMIT_FROM_THREAD_TIMEOUT_SECONDS))
        except Exception as exc:  # noqa: BLE001 -- observation is fail-safe
            logger.warning(
                "ai_path_recorder: legacy step not emitted from thread (%s)",
                type(exc).__name__,
            )
            return False

    def _offer(self, event: str, *, step: dict[str, Any] | None = None) -> bool:
        with self._lock:
            queued = self._offer_locked(event, step=step)
        if queued:
            self._kick_sender()
        return queued

    def _offer_locked(self, event: str, *, step: dict[str, Any] | None = None) -> bool:
        if not self._accepting or self._path_id is None:
            return False
        sequence = self._sequence
        payload: dict[str, Any] = {
            "schema_version": PROGRESS_SCHEMA_VERSION,
            "event": event,
            "path_id": self._path_id,
            "sequence": sequence,
        }
        if step is not None:
            payload["step"] = step
        message = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        size = len(message.encode("utf-8"))
        if (
            self._sequence >= LIVE_EVENT_MAX_COUNT
            or size > LIVE_EVENT_MAX_BYTES
            or self._accepted_bytes + size > LIVE_REQUEST_MAX_BYTES
        ):
            self._accepting = False
            return False
        self._queue.append((sequence, message))
        self._sequence += 1
        self._accepted_bytes += size
        return True

    def _kick_sender(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            self._stop_accepting(clear_pending=True)
            return
        try:
            loop.call_soon_threadsafe(self._ensure_sender)
        except RuntimeError:
            self._stop_accepting(clear_pending=True)

    def _ensure_sender(self) -> None:
        if self._sender_task is None or self._sender_task.done():
            with self._lock:
                pending = bool(self._queue)
            if pending:
                self._sender_task = asyncio.create_task(self._dispatch())

    async def _dispatch(self) -> None:
        try:
            while True:
                with self._lock:
                    if not self._queue:
                        return
                    sequence, message = self._queue.popleft()
                    budget = self._send_budget_remaining
                if budget <= 0:
                    self._stop_accepting(clear_pending=True)
                    return
                started = time.perf_counter()
                try:
                    await asyncio.wait_for(
                        self._context.report_progress(float(sequence + 1), None, message),
                        timeout=budget,
                    )
                except asyncio.CancelledError:
                    self._stop_accepting(clear_pending=True)
                    raise
                except Exception as exc:  # noqa: BLE001 -- observation is fail-safe
                    logger.warning(
                        "ai_path_recorder: live progress stopped (%s)",
                        type(exc).__name__,
                    )
                    self._stop_accepting(clear_pending=True)
                    return
                finally:
                    elapsed = time.perf_counter() - started
                    with self._lock:
                        self._send_budget_remaining = max(
                            0.0, self._send_budget_remaining - elapsed
                        )
        finally:
            self._sender_task = None

    def _stop_accepting(self, *, clear_pending: bool) -> None:
        with self._lock:
            self._accepting = False
            if clear_pending:
                self._queue.clear()

    @staticmethod
    def _consume_finished_task(task: asyncio.Task) -> None:
        """Retrieve a detached sender outcome after bounded cancellation."""
        _DETACHED_SENDERS.discard(task)
        try:
            task.exception()
        except BaseException:  # noqa: BLE001 -- cleanup never affects the tool
            pass

    def _cancel_sender(self, task: asyncio.Task) -> None:
        _DETACHED_SENDERS.add(task)
        task.cancel()
        task.add_done_callback(self._consume_finished_task)
        # ``wait_for`` itself waits for the sink's cancellation handler. A
        # second cancellation on the next loop turn interrupts a slow handler
        # without extending this request's fixed wall budget.
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon(task.cancel)

    async def finish(self) -> None:
        """Drain the accepted prefix within the remaining 25 ms send budget."""
        if not self.armed or self._path_id is None:
            self._stop_accepting(clear_pending=True)
            return
        self._stop_accepting(clear_pending=False)
        self._ensure_sender()
        with self._lock:
            budget = self._send_budget_remaining
        task = self._sender_task
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=max(0.000_001, budget))
        except TimeoutError:
            self._stop_accepting(clear_pending=True)
            self._cancel_sender(task)
        except asyncio.CancelledError:
            self._stop_accepting(clear_pending=True)
            self._cancel_sender(task)
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise

_ACTIVE_EMITTER: contextvars.ContextVar[_PathEmitter | None] = contextvars.ContextVar(
    "ai_path_active_emitter", default=None
)

# Historical generic-tool compatibility only. Result-bound live crossings use
# the non-blocking FIFO and never wait on this timeout.
_EMIT_FROM_THREAD_TIMEOUT_SECONDS = 2.0


def emission_is_armed() -> bool:
    """True when a live channel exists for the tool call in flight.

    A caller with expensive detail to assemble tests this first; there is no
    reason to build a payload nobody will receive.
    """
    emitter = _ACTIVE_EMITTER.get()
    return emitter is not None and emitter.armed


def observation_is_recorded() -> bool:
    """True when a crossing emitted now will be KEPT, watched or not.

    The gate a caller with expensive detail to assemble should test. It replaces
    `emission_is_armed` at those call sites: since migration 176 a crossing is
    persisted on `app.ai_path_steps` whether or not the client sent a
    ``progressToken``, so building the payload only for watchers meant an
    unwatched walk recorded no branches at all -- and a later reader could not
    distinguish that from a walk that judged nothing.

    `emission_is_armed` still exists and still means what it says: is there a LIVE
    channel. Two different questions, two predicates.
    """
    return _ACTIVE_EMITTER.get() is not None


def _crossing_fields(
    *,
    step_kind: str,
    outcome: str | None,
    tool_name: str | None,
    owner_workspace: str | None,
    owner_object_type: str | None,
    owner_object_id: str | None,
    owner_version_id: str | None,
    evidence_record_id: str | None,
    detail: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not owner_object_type or not owner_object_id:
        owner_workspace = owner_object_type = owner_object_id = owner_version_id = None
    return {
        "step_kind": step_kind,
        "outcome": outcome,
        "tool_name": tool_name,
        "owner_workspace": owner_workspace,
        "owner_object_type": owner_object_type,
        "owner_object_id": owner_object_id,
        "owner_version_id": owner_version_id,
        "evidence_record_id": evidence_record_id,
        "observed_at": datetime.now(timezone.utc),
        # One detached, sanitized snapshot feeds both provisional projection and
        # final persistence. Callers may reuse or mutate their input after this
        # function returns without rewriting evidence already observed.
        "detail": sanitize_detail(detail),
    }


async def emit_step(
    *,
    step_kind: str,
    outcome: str | None = None,
    tool_name: str | None = None,
    owner_workspace: str | None = None,
    owner_object_type: str | None = None,
    owner_object_id: str | None = None,
    owner_version_id: str | None = None,
    evidence_record_id: str | None = None,
    detail: Mapping[str, Any] | None = None,
) -> bool:
    """Record one crossing and return whether live delivery accepted it.

    The canonical seam for anything running inside a tool call that reaches a
    governed node -- a context hit, a procedure, a Skill step. Never raises, and
    Result-bound calls enqueue without awaiting the transport. ``False`` is
    normal when there is no emitter, progress token or open live prefix.

    ``step_kind`` is one of ``core.ai_paths.STEP_KINDS``; the level is derived,
    never passed in, so the grid stays in one place.
    """
    emitter = _ACTIVE_EMITTER.get()
    if emitter is None:
        return False
    level = level_of(step_kind, tool_name, owner_object_type)
    if level is None:
        return False
    fields = _crossing_fields(
        step_kind=step_kind,
        outcome=outcome,
        tool_name=tool_name,
        owner_workspace=owner_workspace,
        owner_object_type=owner_object_type,
        owner_object_id=owner_object_id,
        owner_version_id=owner_version_id,
        evidence_record_id=evidence_record_id,
        detail=detail,
    )
    accepted, use_legacy = emitter.record_and_route_step(fields)
    if not use_legacy:
        return accepted
    if not emitter.armed:
        return False
    legacy_fields = {
        key: value
        for key, value in fields.items()
        if key not in {"evidence_record_id", "observed_at"}
    }
    return await emitter.emit_legacy(
        level=level,
        state=STEP_STATE_OBSERVED,
        **legacy_fields,
    )


def emit_step_sync(
    *,
    step_kind: str,
    outcome: str | None = None,
    tool_name: str | None = None,
    owner_workspace: str | None = None,
    owner_object_type: str | None = None,
    owner_object_id: str | None = None,
    owner_version_id: str | None = None,
    evidence_record_id: str | None = None,
    detail: Mapping[str, Any] | None = None,
) -> bool:
    """:func:`emit_step` for a synchronous call site (``search_context`` is one).

    Same contract, same return value, same refusal to raise. Result-bound calls
    never wait for the progress sink; ``True`` means FIFO acceptance. An
    unwatched call returns ``False`` while the crossing remains recorded.
    """
    emitter = _ACTIVE_EMITTER.get()
    if emitter is None:
        return False
    level = level_of(step_kind, tool_name, owner_object_type)
    if level is None:
        return False
    fields = _crossing_fields(
        step_kind=step_kind,
        outcome=outcome,
        tool_name=tool_name,
        owner_workspace=owner_workspace,
        owner_object_type=owner_object_type,
        owner_object_id=owner_object_id,
        owner_version_id=owner_version_id,
        evidence_record_id=evidence_record_id,
        detail=detail,
    )
    accepted, use_legacy = emitter.record_and_route_step(fields)
    if not use_legacy:
        return accepted
    if not emitter.armed:
        return False
    legacy_fields = {
        key: value
        for key, value in fields.items()
        if key not in {"evidence_record_id", "observed_at"}
    }
    return emitter.emit_legacy_from_thread(
        level=level,
        state=STEP_STATE_OBSERVED,
        **legacy_fields,
    )


def build_middleware():
    """Return a FastMCP middleware that records every tool call, or None.

    Mirrors `core.tracing.build_middleware` and `core.mcp_profiles.build_middleware`:
    returns None when the FastMCP base is unimportable, so app construction never
    fails because evidence collection could not be installed.
    """
    try:
        from fastmcp.server.middleware import Middleware  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        logger.debug("ai_path_recorder: FastMCP middleware base unavailable (%s)", exc)
        return None

    class AiPathMiddleware(Middleware):
        """Turns every tool call into a step of its interaction's AI Path.

        And, since Story 54.1, emits those steps as they are crossed. The two
        jobs share one middleware on purpose: a second one would open a second
        path per interaction.
        """

        async def on_call_tool(self, context, call_next):
            from core.ai_paths import (  # noqa: PLC0415
                OUTCOME_FAILED,
                OUTCOME_SUCCEEDED,
            )

            msg = getattr(context, "message", None)
            tool_name = getattr(msg, "name", None) or "unknown_tool"
            arguments = getattr(msg, "arguments", None) or {}
            fastmcp_context = getattr(context, "fastmcp_context", None)
            request_context = getattr(fastmcp_context, "request_context", None)
            meta = getattr(request_context, "meta", None)
            if meta is None:
                meta = getattr(msg, "meta", None)

            actor = "anonymous"
            try:
                from core.mcp_profiles import _identity  # noqa: PLC0415

                actor = _identity() or "anonymous"
            except Exception:  # noqa: BLE001 -- an unknown caller is still an actor
                pass

            emitter = _build_emitter(context)
            token = _ACTIVE_EMITTER.set(emitter)
            # MEME SOURCE que `record_tool_call` plus bas : les deux bouts d'un
            # pas de Skill doivent se reconnaitre, donc lire le meme champ.
            trace_token = _ACTIVE_TRACE.set(_trace_id_of(meta))
            search_observation = None
            try:
                if tool_name == SEARCH_CONTEXT_TOOL_NAME:
                    emitter.disable_legacy()
                    search_observation = _begin_search_context_observation(
                        arguments=arguments,
                        meta=meta,
                        actor=actor,
                        emitter=emitter,
                    )
                if tool_name not in {
                    RESULT_EXECUTION_TOOL_NAME,
                    SEARCH_CONTEXT_TOOL_NAME,
                }:
                    await emitter.emit_legacy(
                        level=LEVEL_JOB,
                        state=STEP_STATE_STARTED,
                        tool_name=tool_name,
                        detail={
                            "argument_keys": sorted(
                                key for key in arguments if isinstance(key, str)
                            )
                        }
                        if isinstance(arguments, dict) and arguments
                        else None,
                    )
                try:
                    result = await call_next(context)
                except asyncio.CancelledError:
                    _abort_search_context_observation(search_observation)
                    raise
                except Exception:
                    # The failure is recorded BEFORE re-raising: a path that only
                    # keeps its successes is not evidence of what happened.
                    if tool_name == SEARCH_CONTEXT_TOOL_NAME:
                        _finalize_search_context_observation(
                            search_observation,
                            emitter=emitter,
                            outcome=OUTCOME_FAILED,
                        )
                    elif tool_name != RESULT_EXECUTION_TOOL_NAME:
                        record_tool_call(
                            tool_name=tool_name, arguments=arguments, meta=meta,
                            outcome=OUTCOME_FAILED, actor=actor,
                        )
                        await _emit_terminal_step(
                            emitter, tool_name, OUTCOME_FAILED
                        )
                    raise

                if tool_name == SEARCH_CONTEXT_TOOL_NAME:
                    _finalize_search_context_observation(
                        search_observation,
                        emitter=emitter,
                        outcome=OUTCOME_SUCCEEDED,
                    )
                elif tool_name != RESULT_EXECUTION_TOOL_NAME:
                    record_tool_call(
                        tool_name=tool_name, arguments=arguments, meta=meta,
                        outcome=OUTCOME_SUCCEEDED, actor=actor,
                    )
                    await _emit_terminal_step(
                        emitter, tool_name, OUTCOME_SUCCEEDED
                    )
                return result
            finally:
                try:
                    _abort_search_context_observation(search_observation)
                    await emitter.finish()
                finally:
                    _ACTIVE_EMITTER.reset(token)
                    _ACTIVE_TRACE.reset(trace_token)

    return AiPathMiddleware()


def _build_emitter(context: Any) -> _PathEmitter:
    """The emitter for one tool call, armed only if the client asked for one."""
    fastmcp_context = getattr(context, "fastmcp_context", None)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover -- a middleware always runs on a loop
        loop = None
    return _PathEmitter(fastmcp_context, loop)


async def _emit_terminal_step(
    emitter: _PathEmitter, tool_name: str, outcome: str
) -> None:
    level = level_of(STEP_KIND_TOOL_CALL, tool_name, None)
    if level is None:
        return
    await emitter.emit_legacy(
        level=level,
        state=STEP_STATE_OBSERVED,
        step_kind=STEP_KIND_TOOL_CALL,
        outcome=outcome,
        tool_name=tool_name,
    )


__all__ = [
    "BANNED_DETAIL_KEYS",
    "DETAIL_MAX_KEYS",
    "DETAIL_MAX_LIST_ITEMS",
    "DETAIL_VALUE_MAX_CHARS",
    "EMISSION_KIND",
    "EMISSION_VERSION",
    "LEVELS",
    "LEVEL_CONTEXT",
    "LEVEL_JOB",
    "LEVEL_PROCEDURE",
    "LEVEL_SKILL",
    "LEVEL_TOOL",
    "LIVE_EVENT_MAX_BYTES",
    "LIVE_EVENT_MAX_COUNT",
    "LIVE_REQUEST_MAX_BYTES",
    "LIVE_SEND_BUDGET_SECONDS",
    "PAYLOAD_FIELDS",
    "PROGRESS_EVENT_RECORDING",
    "PROGRESS_EVENT_STEP",
    "PROGRESS_SCHEMA_VERSION",
    "RESULT_EXECUTION_TOOL_NAME",
    "STEP_KIND_TOOL_CALL",
    "STEP_STATES",
    "STEP_STATE_OBSERVED",
    "STEP_STATE_STARTED",
    "build_middleware",
    "build_step_payload",
    "emission_is_armed",
    "observation_is_recorded",
    "emit_step",
    "emit_step_sync",
    "level_of",
    "progress_token_of",
    "record_result_execution",
    "record_tool_call",
    "sanitize_detail",
]
