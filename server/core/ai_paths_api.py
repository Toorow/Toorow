"""Read routes for observed AI Paths (Story 49.6).

`context-hub.md` closes with *"AI usage paths and their evidence cannot be
inspected or evaluated"*. `ff263bc` gave the owner its first writer, so paths now
exist; this is the other half of that criterion -- they can be read.

The gap this fills was recorded in the codebase itself. `evidence_index.py`
carried the line *"49.6 has registered the `ai-path` route type but mounted no
screen for it"*, and `navigation.ts` declares `{ type: "ai-path" }` under
Knowledge Graph. The address was reachable and answered nothing. That line is
gone: the screen is mounted, the detail read below carries the exact owner link
of every step, and `_OWNER_ROUTES` now routes `ai-path`.

Two properties, both inherited from `core.ai_paths` rather than re-decided here:

* **Project scope is part of the lookup, not a filter after it.** `load_path`
  raises the same `AiPathNotFound` for a foreign id and a nonexistent one, so
  neither confirms the other Project's path exists. This module keeps that
  shape: 404 discloses nothing.
* **A recording path is readable but not evidence.** `ai_path_reference` refuses
  to pin an unfinalized path because it can still grow steps. The read below
  says which lifecycle it is looking at rather than hiding the distinction, so a
  reader cannot mistake an interaction in progress for a finished one.

The one WRITE that lives here (Story 55.2)
------------------------------------------
`POST .../ai-paths/{path_id}/inspections` records that somebody OPENED a step of
this walk -- which Result, which step, which actor, when, and which honest state
they were shown. It is deliberately in this module rather than in a new one: the
grant that lets a caller read a path is the grant that lets them inspect it, and
splitting the two would mean two answers to one authorization question.

It is not a data read and it re-executes nothing (AC8). The Console calls it
AFTER expanding a subtree it already had in hand; a refusal to record does not
un-expand anything, which is why every failure below answers with a stated code
instead of pretending the display failed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import logging
import os

import psycopg.errors
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.db import request_connection
from core.project_access import AccessDecision, resolve_strict_resource_access

logger = logging.getLogger(__name__)

#: Bounded like the owner bounds it. An unbounded collection read on evidence is
#: how a debugging screen becomes a way to export a tenant.
MAX_LIMIT = 200
DEFAULT_LIMIT = 50


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


def _not_found() -> Response:
    """Existence-hiding: a missing path, a foreign path and a Project the caller
    has no grant on are indistinguishable from here, deliberately."""
    return _no_store(
        JSONResponse({"code": "not_found", "message": "AI Path not found"}, status_code=404)
    )


def _denied() -> Response:
    return _no_store(
        JSONResponse(
            {"code": "denied", "message": "Your access to this Project does not include Context."},
            status_code=403,
        )
    )


def _unavailable(code: str) -> Response:
    return _no_store(
        JSONResponse({"code": code, "message": "AI Paths are unavailable"}, status_code=503)
    )


async def _authorize(request: Request):
    """Return `(project_id, actor, conn_factory)` or a Response that refuses."""
    from core.admin_api import _check_auth  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return _no_store(
            JSONResponse(
                {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
            )
        )
    return (request.path_params.get("project_id") or "").strip(), identity or "anonymous"


def _strict_project_access(
    actor: str,
    conn,
    *,
    project_id: str,
    minimum_capability: str = "view",
) -> AccessDecision:
    """Resolve Project access through the strict seam, with the dev bypass.

    Same documented policy as ``admin_api._strict_project_capability_allowed``:
    auth-disabled local mode keeps the historical anonymous developer workflow.
    Without it every ai-paths route answered 404 under ``make dev`` while the
    rest of the Context surfaces answered (live finding A1, 2026-08-05). The
    org is still read from the Project row, so the inspection write path gets
    the ``org_id`` it needs and an unknown Project stays a plain not_found.
    """
    auth_mode = os.environ.get("TOOROW_AUTH_MODE", "disabled").strip().lower()
    if auth_mode == "disabled" and actor == "anonymous":
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT org_id FROM app.projects WHERE id = %s AND status = 'active'",
                    (project_id,),
                )
                row = cur.fetchone()
        except Exception:
            logger.exception("ai_paths_api: project org lookup unavailable")
            return AccessDecision(False, "access_unavailable")
        if not isinstance(row, (tuple, list)) or not row:
            return AccessDecision(False, "not_found")
        org_id = str(row[0])
        return AccessDecision(
            True,
            "disabled_auth_dev",
            "manage",
            org_id,
            (f"organization:{org_id}", f"project:{project_id}"),
        )
    return resolve_strict_resource_access(
        actor, conn, project_id=project_id, minimum_capability=minimum_capability, hold_access=True
    )


async def _path_stats(request: Request) -> Response:
    """GET the Project's AI Path outcome/verdict aggregate over a bounded window.

    The drift question — "are failures and unverifiables RISING?" — cannot be
    answered by opening paths one at a time. The outcome split is the stored
    finalization column; the verdict split is derived over at most
    `verdict_window` recent finalized paths, and the response carries that
    window so a reader never mistakes it for an all-time rate.
    """
    from core.ai_path_recorder import recording_failures  # noqa: PLC0415
    from core.ai_paths import path_stats  # noqa: PLC0415

    authorized = await _authorize(request)
    if isinstance(authorized, Response):
        return authorized
    project_id, actor = authorized

    raw_days = (request.query_params.get("days") or "").strip()
    if raw_days and (not raw_days.isdigit() or not 1 <= int(raw_days) <= 90):
        return _no_store(
            JSONResponse(
                {"code": "invalid_query", "message": "days must be 1..90"},
                status_code=400,
            )
        )
    days = int(raw_days) if raw_days else 30

    try:
        with request_connection(actor) as conn:
            decision = _strict_project_access(
                actor, conn, project_id=project_id, minimum_capability="view"
            )
            if not decision.allowed:
                if decision.reason == "insufficient_capability":
                    return _denied()
                if decision.reason == "access_unavailable":
                    return _unavailable("ai_path_access_unavailable")
                return _not_found()
            stats = path_stats(conn, project_id=project_id, days=days)
    except Exception as exc:  # noqa: BLE001 -- fail closed without disclosing existence
        logger.warning(
            "ai_paths_api: stats unavailable project=%s: %s", project_id, type(exc).__name__
        )
        return _unavailable("ai_paths_unavailable")

    return _no_store(
        JSONResponse(
            {
                "schema_version": "ai-path-stats.v1",
                "project_id": project_id,
                **stats,
                "recording_failures_this_instance": recording_failures(),
            },
            status_code=200,
        )
    )


async def _inspection_summary(request: Request) -> Response:
    """GET how the Project's evidence branches were actually opened.

    The read that `app.evidence_inspections` did not have. Two surfaces wrote it
    -- this module's own POST below, and `evidence_inspection_mcp` -- and no
    production SELECT existed, so the signal was kept and shown to nobody. That is
    the shape `context-hub.md` refuses of the sibling store ("kept and read by
    nothing -- a recurring rejection that no surface shows is a missing link
    nobody can repair"), one notch earlier in its life.

    Same grant as the reads it sits beside, and the same window contract as
    `/stats`: 1..90 days, validated BEFORE a connection is opened, because a
    malformed query is a client error and must not cost a round trip.
    """
    from core.evidence_inspections import summarize_inspections  # noqa: PLC0415

    authorized = await _authorize(request)
    if isinstance(authorized, Response):
        return authorized
    project_id, actor = authorized

    raw_days = (request.query_params.get("days") or "").strip()
    if raw_days and (not raw_days.isdigit() or not 1 <= int(raw_days) <= 90):
        return _no_store(
            JSONResponse(
                {"code": "invalid_query", "message": "days must be 1..90"},
                status_code=400,
            )
        )
    days = int(raw_days) if raw_days else 30

    try:
        with request_connection(actor) as conn:
            decision = _strict_project_access(
                actor, conn, project_id=project_id, minimum_capability="view"
            )
            if not decision.allowed:
                if decision.reason == "insufficient_capability":
                    return _denied()
                if decision.reason == "access_unavailable":
                    return _unavailable("ai_path_access_unavailable")
                return _not_found()
            summary = summarize_inspections(conn, project_id=project_id, days=days)
    except Exception as exc:  # noqa: BLE001 -- fail closed without disclosing existence
        logger.warning(
            "ai_paths_api: inspection summary unavailable project=%s: %s",
            project_id,
            type(exc).__name__,
        )
        return _unavailable("evidence_inspection_unavailable")

    return _no_store(
        JSONResponse(
            {
                "schema_version": "evidence-inspection-summary.v1",
                "project_id": project_id,
                **summary,
            },
            status_code=200,
        )
    )


async def _list_ai_paths(request: Request) -> Response:
    """GET the Project's recent AI Paths, newest first, keyset-paged."""
    from core.ai_path_recorder import recording_failures  # noqa: PLC0415
    from core.ai_paths import list_paths  # noqa: PLC0415

    authorized = await _authorize(request)
    if isinstance(authorized, Response):
        return authorized
    project_id, actor = authorized

    raw_limit = (request.query_params.get("limit") or "").strip()
    if raw_limit and (not raw_limit.isdigit() or not 1 <= int(raw_limit) <= MAX_LIMIT):
        return _no_store(
            JSONResponse(
                {"code": "invalid_query", "message": f"limit must be 1..{MAX_LIMIT}"},
                status_code=400,
            )
        )
    limit = int(raw_limit) if raw_limit else DEFAULT_LIMIT
    cursor = (request.query_params.get("cursor") or "").strip() or None

    try:
        with request_connection(actor) as conn:
            decision = _strict_project_access(
                actor, conn, project_id=project_id, minimum_capability="view"
            )
            if not decision.allowed:
                if decision.reason == "insufficient_capability":
                    return _denied()
                if decision.reason == "access_unavailable":
                    return _unavailable("ai_path_access_unavailable")
                return _not_found()
            paths = list_paths(conn, project_id=project_id, limit=limit, cursor=cursor)
            from core.ai_paths import steps_digest  # noqa: PLC0415

            digest = steps_digest(conn, path_ids=[str(p.get("id")) for p in paths])
    except Exception as exc:  # noqa: BLE001 -- fail closed without disclosing existence
        logger.warning(
            "ai_paths_api: list unavailable project=%s: %s", project_id, type(exc).__name__
        )
        return _unavailable("ai_paths_unavailable")

    return _no_store(
        JSONResponse(
            {
                "schema_version": "ai-path-collection.v1",
                "project_id": project_id,
                # 2026-09-05: a row says how many steps and which tools, so a
                # picker or a list can name an interaction without opening it.
                "paths": [
                    {**_row(path), **digest.get(str(path.get("id")), {"steps": 0, "tools": []})}
                    for path in paths
                ],
                # How many observations THIS instance failed to record since it
                # started. It travels with the list because this is the exact
                # place the two get confused: an empty list means "nothing was
                # recorded", and a reader has no way to tell that from "nothing
                # COULD be recorded" -- the failure `context-hub.md` refuses for
                # the context store ("an unavailable context store is
                # indistinguishable from an empty one").
                #
                # Process-local and unpersisted on purpose: a counter that
                # needed a write would fail exactly when recording fails. It
                # under-reports across instances, and says so by its name.
                "recording_failures_this_instance": recording_failures(),
                # The keyset cursor is the last id, or null when the page is the
                # end. Absent rather than a page number: ids are the ordering.
                "next_cursor": paths[-1]["id"] if len(paths) == limit else None,
            },
            status_code=200,
        )
    )


def _row(path: dict) -> dict:
    return {
        "id": path["id"],
        "lifecycle": path["lifecycle"],
        "outcome": path["outcome"],
        "actor": path["actor"],
        "started_at": _iso(path.get("started_at")),
        "ended_at": _iso(path.get("ended_at")),
        "w3c_trace_id": path.get("w3c_trace_id"),
    }


def _iso(value) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (value or None)


#: What the DETAIL read says about the owner one step reached. The three states
#: are exclusive and none of them is a guess:
#:
#:  * `governed`     -- the owner is named AND the console has a registered route
#:                      to it, pinned to the exact version the step recorded.
#:  * `unavailable`  -- an owner IS named and no route resolves. Either its type
#:                      has no registered route, or -- for an Event -- its Data
#:                      owner refused to prove the binding inside this caller's
#:                      access context. The two are deliberately the same word:
#:                      distinguishing them would turn the walk into a way to ask
#:                      whether an object exists.
#:  * `not_governed` -- the step reached nothing governed at all. It stays listed
#:                      (the owner's docstring: "an unrepresented tool call must
#:                      stay visible in the path rather than have a graph object
#:                      invented for it"), and it is NOT reported as a broken
#:                      link, because there is no link to break.
OWNER_REFERENCE_GOVERNED = "governed"
OWNER_REFERENCE_UNAVAILABLE = "unavailable"
OWNER_REFERENCE_NOT_GOVERNED = "not_governed"


def _compose_event_references(conn, *, project_id: str, references: list[dict]) -> list[dict]:
    """Resolve observed Event ids through their DATA OWNER (Story 49.6 AC9).

    THE ONE COMPOSER, and that is the whole point of it being a function. The
    graph overlay and the path detail both name the same Events of the same
    walk; two compositions would answer differently the first time one of them
    was fixed, and the reader would have no way to know which screen lied.

    The resolution runs inside the access context the path was read under, so an
    Event this actor may not reach comes back `unavailable` -- exactly like one
    whose binding was never proven, and like one that does not exist. They must
    be indistinguishable or this list becomes a way to ask whether an Event
    exists.
    """
    from core.event_configurations import (  # noqa: PLC0415
        EVENT_BINDING_UNAVAILABLE,
        resolve_event_observation_references,
    )

    resolved = resolve_event_observation_references(
        conn,
        project_id=project_id,
        event_ids=[ref["event_id"] for ref in references],
    )
    return [
        {
            **ref,
            **resolved.get(
                ref["event_id"],
                # Absent from the resolution: another Project's, or gone.
                {"binding_state": EVENT_BINDING_UNAVAILABLE},
            ),
        }
        for ref in references
    ]


INTERACTION_MAX_STEPS = 200


def ordered_interaction_steps(all_raw_steps: list) -> list:
    """The interaction's raw steps in the order they happened -- ALL of them, the assessment's own order.

    Round 5 bounded this list at `INTERACTION_MAX_STEPS`; a clamp that drops the
    LATEST rows drops the crossing that closes a Skill's walk and turns a `pass`
    into a `fail` (the closure happens last). The judgement, the coverage and
    the names read every row; only the DRAWING is bounded, by
    `merge_interaction_steps`, which says so with `truncated`. The structural
    bound stays: one own path and at most four siblings.
    """
    from core.ai_paths import ordered_by_moment  # noqa: PLC0415

    return ordered_by_moment(all_raw_steps)


def assessment_for_read(path: Mapping, all_raw_steps: list) -> Any:
    """The assessment the detail read serves -- the rule lives in `core.ai_paths` (round 9, F2)."""
    from core.ai_paths import assessment_for_read as _rule  # noqa: PLC0415

    return _rule(path, all_raw_steps)


def routed_interaction_rows(all_raw_steps: list, labels: Mapping, events_by_id: Mapping) -> list[dict]:
    """The merged timeline, each row routed like an own step (round 5 B3, guarded round 6)."""
    return [{**row, **_step_owner_reference(row, events_by_id)} for row in merge_interaction_steps(all_raw_steps, labels)]


def merge_interaction_steps(all_raw_steps: list, labels: Mapping) -> list[dict]:
    """The steps of every path of one interaction as ONE sequence, in the order they happened.

    Round 3 of the Opus review rendered the previous merge and read `A0 B0 A1 B1 A2`
    for steps that happened `A0 A1 A2 B0 B1`: each path allocates its ordinals from
    0, and the renderer orders by ordinal, so a merge that kept per-path ordinals
    was re-sorted into an order that never happened, with the step number printed
    twice. Here the moment decides (a datetime, never its string -- a DST fall-back
    sorts backwards as text), then the merged rank BECOMES the ordinal the reader
    sees, continuous across paths -- a rank WITHIN THE DRAWING: when the drawing
    keeps the last 200 of more, the numbers run 1..200 and the page says « the
    last of M ». The path's own ordinal travels beside it as `path_step_order`
    (what an inspection is addressed to), and `id` is unique across the merge.
    """
    from core.ai_paths import _moment, wire_steps_projection  # noqa: PLC0415

    pairs = list(zip(all_raw_steps, wire_steps_projection(all_raw_steps, branch_evidence_frozen=False)))
    pairs.sort(key=lambda pair: (_moment(pair[0].get("observed_at")), str(pair[0].get("path_id") or ""), int(pair[0].get("ordinal") or 0)))
    merged: list[dict] = []
    # THE DRAWING KEEPS THE END (round 7, B2): when it must drop rows it drops the
    # earliest, so the crossing that closed the walk -- the row a reader asking
    # « what closed it » needs -- is always drawn. The rank stays 0..n-1.
    for rank, (raw, projected) in enumerate(pairs[-INTERACTION_MAX_STEPS:]):
        path_id = str(raw.get("path_id") or "")
        merged.append(
            {
                **projected,
                "id": f"{path_id}#{int(raw.get('ordinal') or 0)}",
                "step_order": rank,
                "path_step_order": projected.get("step_order"),
                "path_id": raw.get("path_id"),
                "owner_label": labels.get((str(projected.get("owner_object_type")), str(projected.get("owner_object_id")))),
            }
        )
    return merged


def _steps_or_none(conn, project_id: str, path_id: str) -> list | None:
    """A sibling's steps for the interaction timeline; an unreadable sibling contributes nothing."""
    from core.ai_paths import load_steps  # noqa: PLC0415

    try:
        return load_steps(conn, path_id=path_id, project_id=project_id)
    except Exception:  # noqa: BLE001
        return None


def _step_owner_reference(step: dict, events: dict[str, dict]) -> dict:
    """The exact owner reference for ONE projected step, or a typed absence.

    AC7 asks the trace lens for "exact owner/Evidence links". The link is
    COMPOSED HERE, by `evidence_index.owner_route` -- the same composer the
    Evidence graph and `resolve_event_observation_references` go through, and
    the same registry of owner routes. A console that joined a workspace, a
    section and a tab onto an id would be a second registry, and the day a
    screen moves the two would disagree without either side failing.

    An Event is NOT routed by that table from here: it is Data-owned, and its
    reference is whatever its owner already proved for `event_references` on
    this same read. Composing it twice is exactly the second-owner defect AC9
    forbids.
    """
    from core.ai_paths import OVERLAY_EVENT_OBJECT_TYPE  # noqa: PLC0415
    from core.evidence_index import owner_route  # noqa: PLC0415

    object_type = str(step.get("owner_object_type") or "")
    object_id = str(step.get("owner_object_id") or "")
    if not object_type or not object_id:
        return {
            "owner_reference": None,
            "owner_reference_state": OWNER_REFERENCE_NOT_GOVERNED,
        }
    if object_type == OVERLAY_EVENT_OBJECT_TYPE:
        reference = (events.get(object_id) or {}).get("owner_route")
    else:
        reference = owner_route(object_type, object_id, step.get("owner_version_id"))
    return {
        "owner_reference": reference,
        "owner_reference_state": (
            OWNER_REFERENCE_GOVERNED if reference else OWNER_REFERENCE_UNAVAILABLE
        ),
    }


def _my_step_reactions(
    conn,
    *,
    org_id: str | None,
    project_id: str,
    actor: str,
    path_id: str,
) -> dict[int, dict]:
    """The caller's own recorded reactions on this walk, by step ordinal.

    Delegated to the Feedback owner (`core.feedback_review`), which is why no
    table of that workspace is named anywhere in this module -- not in a
    statement and not in a comment. A read that cannot answer
    is an EMPTY answer, never a failed path read: a person who cannot be told
    what they already recorded must still be able to read the walk, and the
    control simply starts in its unanswered state. The write door itself is the
    thing that must never be permissive, and it is not -- a replay there answers
    with the stored receipt and a different polarity is refused.
    """
    from core.feedback_review import read_actor_path_step_reactions  # noqa: PLC0415

    if not org_id:
        return {}
    try:
        return read_actor_path_step_reactions(
            conn,
            org_id=org_id,
            project_id=project_id,
            actor=actor,
            path_id=path_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ai_paths_api: own reactions unavailable path=%s: %s", path_id, type(exc).__name__
        )
        return {}


def _my_feedback(step: dict, reactions: dict[int, dict]) -> dict:
    """`my_feedback` for ONE projected step: the caller's reaction, or null.

    Keyed on the step's OWN ordinal (`step_order`, the projection of
    `app.ai_path_steps.ordinal`) -- the same identity the write door names and
    the same one the family records an inspection under. Keying on the position
    in the array is the off-by-one AI-134 already cost.
    """
    ordinal = step.get("step_order")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool):
        return {"my_feedback": None}
    recorded = reactions.get(ordinal)
    if not recorded:
        return {"my_feedback": None}
    return {
        "my_feedback": {
            "polarity": recorded.get("polarity"),
            "recorded_at": _iso(recorded.get("recorded_at")),
            # The sentence the person wrote travels with the verdict: a reload
            # that kept the thumb and lost the words forgot half the reaction.
            "comment": recorded.get("comment"),
        }
    }


async def _graph_overlay(request: Request) -> Response:
    """GET the Knowledge Graph decoration for one AI Path (Story 49.6 AC7).

    A SEPARATE read on purpose. AC7 says the overlay "decorates the current
    canonical graph and does not mutate it", so `GET /api/context/graph` keeps
    returning the one canonical bundle and this route returns what to draw ON
    it. Folding the decoration into the bundle would make the canonical graph
    vary with which path is selected -- the mutation AC7 forbids, arrived at
    through the payload instead of through a write.

    Server-composed, and that is also AC7: "the React client does not join AI
    Path, graph, Feedback and owner APIs into an authority." The client asks
    for a state per node; it never derives one.
    """
    from core.ai_paths import AiPathNotFound, graph_overlay, load_path  # noqa: PLC0415

    authorized = await _authorize(request)
    if isinstance(authorized, Response):
        return authorized
    project_id, actor = authorized
    path_id = (request.path_params.get("path_id") or "").strip()

    try:
        with request_connection(actor) as conn:
            decision = _strict_project_access(
                actor, conn, project_id=project_id, minimum_capability="view"
            )
            if not decision.allowed:
                if decision.reason == "insufficient_capability":
                    return _denied()
                if decision.reason == "access_unavailable":
                    return _unavailable("ai_path_access_unavailable")
                return _not_found()
            path = load_path(conn, path_id=path_id, project_id=project_id)
            overlay = graph_overlay(path)

            # Story 49.6 AC9, through the ONE composer this module owns. The
            # per-path detail read calls the same function on the same list, so
            # the canvas and the workbench cannot name the same Event
            # differently.
            overlay["event_references"] = _compose_event_references(
                conn, project_id=project_id, references=overlay["event_references"]
            )
    except AiPathNotFound:
        return _not_found()
    except Exception as exc:  # noqa: BLE001 -- fail closed without disclosing existence
        logger.warning(
            "ai_paths_api: overlay unavailable path=%s: %s", path_id, type(exc).__name__
        )
        return _unavailable("ai_paths_unavailable")

    return _no_store(JSONResponse({**overlay, "project_id": project_id}, status_code=200))


async def _skill_walks(request: Request) -> Response:
    """GET .../ai-paths/skills/{procedure_id}/walks -- what the Skill's last walks say about it (iteration 3)."""
    from core.ai_paths import skill_walk_stats  # noqa: PLC0415

    authorized = await _authorize(request)
    if isinstance(authorized, Response):
        return authorized
    project_id, actor = authorized
    procedure_id = (request.path_params.get("procedure_id") or "").strip()
    if not procedure_id:
        return _not_found()
    try:
        with request_connection(actor) as conn:
            decision = _strict_project_access(actor, conn, project_id=project_id, minimum_capability="view")
            if not decision.allowed:
                if decision.reason == "insufficient_capability":
                    return _denied()
                if decision.reason == "access_unavailable":
                    return _unavailable("ai_path_access_unavailable")
                return _not_found()
            stats = skill_walk_stats(conn, project_id=project_id, procedure_id=procedure_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ai_paths_api: skill walks unavailable procedure=%s: %s", procedure_id, type(exc).__name__)
        return _unavailable("ai_paths_unavailable")
    return _no_store(JSONResponse({"schema_version": "ai-path-skill-walks.v1", "project_id": project_id, **stats}, status_code=200))


async def _get_ai_path(request: Request) -> Response:
    """GET one AI Path: header, ordered steps and the derived assessment."""
    from core.ai_paths import (  # noqa: PLC0415
        OBSERVED_AI_PATH_MAX_BYTES,
        OBSERVED_AI_PATH_MAX_STEPS,
        AiPathNotFound,
        canonical_json_v2,
        graph_overlay,
        load_path,
        wire_steps_projection,
    )

    authorized = await _authorize(request)
    if isinstance(authorized, Response):
        return authorized
    project_id, actor = authorized
    path_id = (request.path_params.get("path_id") or "").strip()

    try:
        with request_connection(actor) as conn:
            decision = _strict_project_access(
                actor, conn, project_id=project_id, minimum_capability="view"
            )
            if not decision.allowed:
                if decision.reason == "insufficient_capability":
                    return _denied()
                if decision.reason == "access_unavailable":
                    return _unavailable("ai_path_access_unavailable")
                return _not_found()
            # No widening in the loader: the siblings are read ONCE below, and the
            # assessment is made over those same steps (round 4, F5 -- the loader's
            # own widening read them a second time when a Skill was pinned).
            path = load_path(conn, path_id=path_id, project_id=project_id, assess_over_interaction=False)
            # 2026-09-05 -- the rest of the interaction (same trace), routed so
            # the page can open them; and what the Skill prescribed vs crossed.
            from core.ai_paths import paths_sharing_trace, skill_coverage  # noqa: PLC0415
            from core.evidence_index import owner_route  # noqa: PLC0415

            same_interaction = [
                {**sibling, "owner_route": owner_route("ai-path", sibling["path_id"])}
                for sibling in paths_sharing_trace(
                    conn, project_id=project_id, trace_id=path.get("w3c_trace_id"), exclude=path_id
                )
            ]
            # THE READER'S WORDS AND THE WHOLE INTERACTION (rendering iteration,
            # 2026-09-05): names for what the steps reached and chose, and the
            # steps of every path of the trace merged in the order they happened,
            # each tagged with its path -- one timeline for one answer.
            from core.ai_paths import names_for, owner_labels  # noqa: PLC0415

            interaction_paths = [path] + [
                {"id": sibling["path_id"], "lifecycle": sibling.get("state"), "steps": _steps_or_none(conn, project_id, sibling["path_id"])}
                for sibling in same_interaction[:4]
            ]
            all_raw_steps = ordered_interaction_steps(
                [
                    {**step, "path_id": entry["id"]}
                    for entry in interaction_paths
                    for step in (entry.get("steps") or [])
                    if isinstance(step, Mapping)
                ]
            )
            labels = owner_labels(conn, all_raw_steps)
            names = names_for(conn, all_raw_steps)
            # Coverage over the WHOLE interaction: the Result's execution and the
            # calls around it are two paths, and the Skill was followed across both.
            # Read from the steps just merged -- the siblings are read ONCE for the
            # timeline, the coverage and the labels (round 3, aggregate residual).
            coverage = skill_coverage(conn, all_raw_steps)
            path["assessment"] = assessment_for_read(path, all_raw_steps)
            # AC9 on the WORKBENCH. A path opened directly used to lose the
            # Data deep-link the graph overlay gives, so the same evidence read
            # two ways answered differently. `graph_overlay` decides WHICH
            # Events the walk went through and in which order -- it is the one
            # projection that knows -- and the Data owner resolves them, in the
            # access context this path was just read under.
            event_references = _compose_event_references(
                conn,
                project_id=project_id,
                references=graph_overlay(path)["event_references"],
            )
            # AC8. The screen does not ask again what this person already answered
            # (`context-hub.md`, amendment of 2026-08-30). Without this, the
            # control could only ever be in its "not asked yet" state, so a
            # reload re-asked a question the person had already answered and a
            # second reaction was filed for the same step.
            #
            # A READ THAT WRITES NOTHING, through the Feedback owner's own
            # function -- the same seam the Events above go through. This module
            # never names the Feedback owner's tables -- a conformance test
            # here fails on the mere mention: Context Hub "owns neither Feedback
            # nor Events and adds no editor for them", and reading the owner's
            # answer is not owning it. The actor is the authenticated
            # identity, so what comes back is the caller's own reaction and
            # nobody else's.
            my_reactions = _my_step_reactions(
                conn,
                org_id=decision.org_id,
                project_id=project_id,
                actor=actor,
                path_id=path_id,
            )
    except AiPathNotFound:
        return _not_found()
    except Exception as exc:  # noqa: BLE001
        logger.warning("ai_paths_api: read unavailable path=%s: %s", path_id, type(exc).__name__)
        return _unavailable("ai_paths_unavailable")

    raw_steps = path.get("steps")
    if not isinstance(raw_steps, list) or len(raw_steps) > OBSERVED_AI_PATH_MAX_STEPS:
        return _unavailable("ai_paths_unavailable")
    snapshot = path.get("policy_snapshot")
    content_hash_contract = (
        snapshot.get("content_hash_contract") if isinstance(snapshot, dict) else None
    )
    events_by_id = {ref["event_id"]: ref for ref in event_references}
    payload = {
                "schema_version": "ai-path.v2",
                **_row(path),
                "model_ref": path.get("model_ref"),
                "tool_catalog_version": path.get("tool_catalog_version"),
                # Pinned before the run: it is what the run is judged against.
                "policy_snapshot": path.get("policy_snapshot"),
                "policy_snapshot_hash": path.get("policy_snapshot_hash"),
                "content_hash": path.get("content_hash"),
                # One path-level allocation: per-step loops cannot enforce the
                # aggregate candidate/byte walls or persisted-ordinal priority.
                # AC7 -- ordered steps, exact versions, EXACT OWNER LINKS. The
                # projection carried `owner_workspace/type/id/version` and no
                # route, so every owner reached a person as plain text and the
                # walk named objects it could not open. The reference is
                # composed here and never in the browser: see
                # `_step_owner_reference`.
                "steps": [
                    {
                        **step,
                        **_step_owner_reference(step, events_by_id),
                        **_my_feedback(step, my_reactions),
                        "owner_label": labels.get((str(step.get("owner_object_type")), str(step.get("owner_object_id")))),
                    }
                    for step in wire_steps_projection(
                        raw_steps,
                        branch_evidence_frozen=path.get("lifecycle") == "finalized",
                        content_hash_contract=content_hash_contract,
                    )
                ],
                "names": names,
                "interaction": (
                    {
                        "paths": [{"path_id": e["id"], "state": e.get("lifecycle")} for e in interaction_paths],
                        # Each merged row is routed like an own step (round 5, B3): the
                        # same composer, so « Whole interaction » opens what the path opens.
                        "steps": routed_interaction_rows(all_raw_steps, labels, events_by_id),
                        # The drawing is bounded; the judgement above is not. Said on the wire.
                        "truncated": len(all_raw_steps) > INTERACTION_MAX_STEPS,
                        "total_steps": len(all_raw_steps),
                    }
                    if same_interaction
                    else None
                ),
                # The Events the walk crossed, by REFERENCE -- ids, walk order
                # and the Data-owned route, never a copy of the Event. Same
                # shape and same composer as the graph overlay's list.
                "event_references": event_references,
                # Derived by the owner from the pinned snapshot, never recomputed
                # here: two places deciding what a path is worth is two answers.
                "assessment": path.get("assessment"),
                # Said plainly so a reader cannot mistake an interaction still in
                # progress for a finished one. `ai_path_reference` refuses to pin
                # a recording path as evidence for exactly this reason.
                "referenceable_as_evidence": path.get("lifecycle") == "finalized",
                "same_interaction": same_interaction,
                "skill_coverage": coverage,
    }
    try:
        if len(canonical_json_v2(payload).encode("utf-8")) > OBSERVED_AI_PATH_MAX_BYTES:
            return _unavailable("ai_paths_unavailable")
    except (TypeError, ValueError):
        return _unavailable("ai_paths_unavailable")
    return _no_store(JSONResponse(payload, status_code=200))


async def _record_inspection(request: Request) -> Response:
    """POST that somebody opened a step of this walk (Story 55.2 AC4).

    The body carries only what an observation can carry: the act, the surface, the
    honest state that was shown, the branch count when there was a listing, and
    the Result / Render the reader was looking at. Nothing about WHAT was read --
    `app.ai_path_steps` already holds the walk, and a second copy here would be a
    second store with a second truth.
    """
    from core import evidence_inspections  # noqa: PLC0415

    authorized = await _authorize(request)
    if isinstance(authorized, Response):
        return authorized
    project_id, actor = authorized
    path_id = (request.path_params.get("path_id") or "").strip()

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- a malformed body is a client error, named
        body = None
    if not isinstance(body, dict):
        return _no_store(
            JSONResponse(
                {"code": "invalid_body", "message": "A JSON object body is required."},
                status_code=400,
            )
        )

    try:
        with request_connection(actor) as conn:
            decision = _strict_project_access(
                actor, conn, project_id=project_id, minimum_capability="view"
            )
            if not decision.allowed:
                if decision.reason == "insufficient_capability":
                    return _denied()
                if decision.reason == "access_unavailable":
                    return _unavailable("ai_path_access_unavailable")
                return _not_found()
            if not decision.org_id:
                return _unavailable("ai_path_access_unavailable")
            # The strict writer, on purpose: a client that described the
            # inspection wrongly must be told, and the display it belongs to has
            # already happened. The never-raising `record_inspection` is for the
            # in-process call sites that render while they record.
            inspection_id = evidence_inspections.insert_inspection(
                conn,
                org_id=decision.org_id,
                project_id=project_id,
                actor=actor,
                kind=body.get("kind"),
                surface=body.get("surface"),
                displayed_state=body.get("displayed_state"),
                branches_listed=body.get("branches_listed"),
                result_ref=body.get("result_ref"),
                render_ref=body.get("render_ref"),
                ai_path_id=path_id or None,
                step_ordinal=body.get("step_ordinal"),
            )
            conn.commit()
    except evidence_inspections.InspectionRefused as exc:
        return _no_store(
            JSONResponse(
                {"code": exc.code, "message": str(exc)},
                status_code=400,
            )
        )
    except (psycopg.errors.CheckViolation, psycopg.errors.ForeignKeyViolation):
        # Un ai_path_id bien forme ou non qui ne pointe vers aucun chemin
        # enregistre : le CHECK de format ou la FK (ai_path_id, org_id,
        # project_id) mordent a l'INSERT. C'est un id inconnu -- le meme 404
        # non-divulgateur que les lectures, pas une indisponibilite du service
        # (constat live 2026-08-05 : le 503 suggerait une panne).
        return _not_found()
    except Exception as exc:  # noqa: BLE001 -- fail closed without disclosing existence
        logger.warning(
            "ai_paths_api: inspection not recorded path=%s: %s", path_id, type(exc).__name__
        )
        return _unavailable("evidence_inspection_unavailable")

    return _no_store(
        JSONResponse(
            {"schema_version": "evidence-inspection.v1", "id": inspection_id},
            status_code=201,
        )
    )


AI_PATH_ROUTES = [
    # Before `/{path_id}`: Starlette matches in declaration order, and a
    # trailing segment would otherwise be swallowed by the id pattern.
    Route(
        "/api/projects/{project_id}/context/ai-paths/stats",
        endpoint=_path_stats,
        methods=["GET"],
        name="context-ai-path-stats",
    ),
    # Fully literal, and declared here for the same reason `stats` is: the two
    # segments after `ai-paths` would be read as `{path_id}` + a suffix by any
    # route declared above it, and the id patterns below would then answer
    # "AI Path not found" for a Project-wide aggregate that names no path.
    Route(
        "/api/projects/{project_id}/context/ai-paths/inspections/summary",
        endpoint=_inspection_summary,
        methods=["GET"],
        name="context-ai-path-inspection-summary",
    ),
    Route(
        "/api/projects/{project_id}/context/ai-paths/skills/{procedure_id}/walks",
        endpoint=_skill_walks,
        methods=["GET"],
        name="context-ai-path-skill-walks",
    ),
    Route(
        "/api/projects/{project_id}/context/ai-paths/{path_id}/graph-overlay",
        endpoint=_graph_overlay,
        methods=["GET"],
        name="context-ai-path-graph-overlay",
    ),
    Route(
        "/api/projects/{project_id}/context/ai-paths/{path_id}/inspections",
        endpoint=_record_inspection,
        methods=["POST"],
        name="context-ai-path-inspections",
    ),
    Route(
        "/api/projects/{project_id}/context/ai-paths/{path_id}",
        endpoint=_get_ai_path,
        methods=["GET"],
        name="context-ai-path",
    ),
    Route(
        "/api/projects/{project_id}/context/ai-paths",
        endpoint=_list_ai_paths,
        methods=["GET"],
        name="context-ai-paths",
    ),
]

__all__ = ["AI_PATH_ROUTES", "DEFAULT_LIMIT", "MAX_LIMIT"]
