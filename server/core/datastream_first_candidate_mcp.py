"""The MCP door onto an armed-but-idle Datastream (AI-142).

WHY MCP AND NOT A SCREEN. The publication half of this path already has a REST
seam -- ``GET .../executions/{id}/candidate-review``, ``POST
.../publish-confirmations``, ``POST .../publish-activate``
(``datastream_preconfiguration_api.py:1056-1070``) -- and it is NOT draft-scoped:
it is keyed on (project, datastream, execution). What has no surface at all is the
step BEFORE it, dispatching a first candidate for a Datastream the wizard did not
create, and the console screens that would host it are the wizard's, which resume
from a draft this Datastream never had. So the door is opened where a Datastream
can be operated without one, and it is opened under the same governance: profile,
effect and confirmation are declared through ``register_profiled``, so the
capability middleware filters discovery AND the call; every tool re-authorizes at
call time on the Datastream scope, fail-closed, existence-hiding.

THREE TOOLS, ONE JOURNEY, AND EACH STEP IS SEPARATELY REFUSABLE:
  1. ``get_datastream_arming``  -- what is armed, what blocks, what is next.
  2. ``start_datastream_first_candidate`` -- creates ONE candidate execution and
     enqueues its materialization job. THIS is the step that pulls: the worker
     runs the registered driver inside ``raw_landing.candidate_execution``, so the
     rows land in a relation staging never names.
  3. ``publish_activate_datastream_candidate`` -- carries ONE NAMED candidate to
     ``publish_activate_mutation``, the only writer of ``lifecycle_state``.

WHAT THESE TOOLS DO NOT DO, on purpose: no tool here writes ``lifecycle_state``,
``current_published_execution_id`` or an execution state. Step 2 stops at
``created``; only the adapter's real evidence, through
``complete_candidate_from_adapter``, can make it Ready. A tool that could shortcut
that would let a Datastream go live having pulled nothing.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "1"


def _envelope(data: dict) -> dict:
    """Build the canonical AD-1 structured_content envelope (same shape as core.main)."""
    return {
        "schema_version": _SCHEMA_VERSION,
        "meta": {"freshness": None, "provenance": None, "alerts": []},
        "data": data,
    }


def _tool_error(code: str, message: str):
    """Return a ToolError carrying the canonical ``{code, message}`` JSON."""
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    return ToolError(json.dumps({"code": code, "message": message}))


def _identity() -> str:
    """Resolve the caller identity from the MCP token.

    Absent token => "anonymous", which the strict access seam then rejects
    (existence-hiding), so a missing token and a foreign resource look identical.
    """
    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    token = get_access_token()
    return token.claims.get("sub", token.client_id) if token else "anonymous"


def _result(summary: str, data: dict):
    """Build the dual-channel ToolResult (lean text summary + AD-1 envelope)."""
    from fastmcp.tools.tool import ToolResult  # noqa: PLC0415
    from mcp.types import TextContent  # noqa: PLC0415

    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=_envelope(data),
    )



def _guard(conn, datastream_id: str, identity: str, *, minimum_capability: str):
    """Re-authorize the Datastream scope at CALL time, fail-closed, non-disclosing.

    A copy of the ``operations_mcp`` guard rather than an import of its private:
    reaching into another module's underscore name would couple this file to a
    file it must not edit. Denial for ANY reason -- not a member, no grant, foreign
    resource, guard failure, database down -- surfaces the SAME ``not_found``, so a
    caller can never use this tool to learn that a Datastream exists.
    """
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    try:
        decision = resolve_strict_resource_access(
            identity,
            conn,
            datastream_id=datastream_id,
            minimum_capability=minimum_capability,
            hold_access=True,
        )
    except Exception as exc:  # noqa: BLE001 -- fail closed; never proceed unguarded.
        logger.error("first_candidate_mcp: access guard failed ds=%s: %s", datastream_id, exc)
        raise _tool_error("not_found", "No such Datastream in this project.") from exc
    if not decision.allowed or not decision.org_id:
        raise _tool_error("not_found", "No such Datastream in this project.")
    return decision


def _require(value: str | None, name: str) -> str:
    resolved = (value or "").strip()
    if not resolved:
        raise _tool_error("missing_param", f"{name} is required.")
    return resolved


def _require_confirm(confirm: str | None, datastream_id: str) -> None:
    """Demand that the caller echo the Datastream identifier, exactly.

    Echoing the identifier is the cheapest check that the model named the object a
    person approved, rather than a boolean it can set to true without reading
    anything. It does NOT replace the human approval that
    ``confirmation_mode="human"`` declares the host obtained; it makes a
    mis-targeted call impossible to make by accident.
    """
    if (confirm or "").strip() != datastream_id:
        raise _tool_error(
            "confirmation_mismatch",
            "confirm must echo the Datastream identifier exactly. This step pulls from "
            "the source and re-running it cannot undo it.",
        )


def get_datastream_arming(project_id: str, datastream_id: str):
    """Dit si un Datastream peut TIRER, et sinon ce qui manque, precisement.

    Rend l'etat arme reel -- versions courantes de plan et de mapping, mode de
    source, fenetre effective, execution en cours, candidat Ready s'il existe
    -- plus la liste COMPLETE des blocages et l'unique action suivante. La
    liste est complete par choix : decouvrir le deuxieme blocage apres avoir
    repare le premier, c'est l'echec au clic.
    """
    project_id = _require(project_id, "project_id")
    datastream_id = _require(datastream_id, "datastream_id")
    identity = _identity()
    try:
        from core.datastream_first_candidate import read_arming  # noqa: PLC0415
        from core.db import request_connection  # noqa: PLC0415

        with request_connection(identity) as conn:
            _guard(conn, datastream_id, identity, minimum_capability="view")
            facts = read_arming(conn, project_id=project_id, datastream_id=datastream_id)
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__name__ == "ToolError":
            raise
        logger.error("first_candidate_mcp: arming read ds=%s: %s", datastream_id, exc)
        raise _tool_error("server_error", "Readiness state unavailable.") from exc
    if facts is None:
        raise _tool_error("not_found", "No such Datastream in this project.")
    blockers = facts["blockers"]
    summary = (
        f"Datastream {facts['name']!r}: lifecycle={facts['lifecycle_state']}, "
        f"schedule={facts['schedule_mode']}, mode={facts['mode']}, "
        f"{len(blockers)} blocker(s), all of them listed. Next: {facts['next_step']}."
    )
    return _result(summary, facts)


def start_datastream_first_candidate(project_id: str, datastream_id: str, confirm: str):
    """Declenche la PREMIERE execution candidate d'un Datastream deja arme.

    C'est l'etape qui TIRE : le worker execute le driver enregistre dans un
    espace propre a l'execution, donc rien n'atteint les marts avant
    publication. Elle n'active RIEN -- ni `lifecycle_state`, ni le pointeur
    publie -- et le candidat reste `created` jusqu'a ce que l'adaptateur rende
    une preuve verifiee.

    `confirm` doit reprendre exactement l'identifiant du Datastream.
    """
    project_id = _require(project_id, "project_id")
    datastream_id = _require(datastream_id, "datastream_id")
    _require_confirm(confirm, datastream_id)
    identity = _identity()
    from datetime import datetime, timezone  # noqa: PLC0415

    today = datetime.now(timezone.utc).date()
    try:
        from core.datastream_first_candidate import (  # noqa: PLC0415
            FirstCandidateRefused,
            dispatch_first_candidate,
        )
        from core.db import request_connection  # noqa: PLC0415

        with request_connection(identity) as conn:
            _guard(conn, datastream_id, identity, minimum_capability="edit")
            try:
                facts, interval, operation = dispatch_first_candidate(
                    conn,
                    project_id=project_id,
                    datastream_id=datastream_id,
                    actor=identity,
                    today=today,
                    # Deterministic over (datastream, versions, window): the SAME
                    # request replays the SAME durable operation instead of
                    # minting a second candidate, and a NEW window is a new key.
                    idempotency_key=(
                        f"datastream.first_candidate:{datastream_id}:{today.isoformat()}"
                    ),
                )
            except FirstCandidateRefused as exc:
                raise _tool_error(
                    "not_armed",
                    "Nothing was enqueued and no execution row survives: "
                    f"{exc}.",
                ) from exc
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__name__ == "ToolError":
            raise
        logger.error("first_candidate_mcp: dispatch ds=%s: %s", datastream_id, exc)
        raise _tool_error("server_error", "Dispatch unavailable.") from exc

    # After the commit, never inside it: the push task must address a row that
    # exists. A dispatch that fails costs latency, not the operation -- the row
    # is the ledger and the polling worker drains it either way.
    _push(operation.result.get("candidate_job_id"))
    data = {
        "project_id": project_id,
        "operation_ref": operation.operation_id,
        "outcome": operation.outcome,
        "replayed": operation.replayed,
        "window": interval,
        "mode_source": facts["mode_source"],
        **operation.result,
    }
    summary = (
        f"Candidat {data.get('candidate_execution_id')} cree pour "
        f"{interval['from']}..{interval['to']} (mode={facts['mode']}); "
        f"lifecycle inchange ({facts['lifecycle_state']})."
    )
    return _result(summary, data)


def publish_activate_datastream_candidate(
    project_id: str, datastream_id: str, execution_id: str, confirm: str
):
    """Publie et active UN candidat NOMME. Porte le `lifecycle_state` a `active`.

    `execution_id` est obligatoire et n'est jamais deduit du « dernier » : la
    revue gele un `content_hash` et un `row_count` que la publication
    reverifie sous verrou. Choisir le candidat a la place de l'appelant, ce
    serait faire approuver des chiffres et en publier d'autres.

    Refuse tout candidat qui n'est pas Ready, vide, non verifie par
    l'adaptateur, ou dont la qualite bloque -- ces refus viennent de la revue
    existante, pas d'une regle inventee ici.

    `confirm` doit reprendre exactement l'identifiant du Datastream.
    """
    project_id = _require(project_id, "project_id")
    datastream_id = _require(datastream_id, "datastream_id")
    execution_id = _require(execution_id, "execution_id")
    _require_confirm(confirm, datastream_id)
    identity = _identity()
    try:
        from core.datastream_activation import ActivationValidationError  # noqa: PLC0415
        from core.datastream_first_candidate import (  # noqa: PLC0415
            FirstCandidateRefused,
            publish_activate_candidate,
        )
        from core.db import request_connection  # noqa: PLC0415
        from core.trial_enforcement import TrialDatastreamLimitError  # noqa: PLC0415

        with request_connection(identity) as conn:
            _guard(conn, datastream_id, identity, minimum_capability="edit")
            try:
                review, operation = publish_activate_candidate(
                    conn,
                    project_id=project_id,
                    datastream_id=datastream_id,
                    execution_id=execution_id,
                    actor=identity,
                    idempotency_key=f"datastream.publish_activate:{execution_id}",
                )
            except TrialDatastreamLimitError as exc:
                # NOT a `server_error`, and not `candidate_refused` either:
                # the candidate is fine, the organization's plan is what
                # refuses. The agent gets the same typed code and the same
                # sentence the console shows, so it can tell the person which
                # gesture frees an allowance instead of retrying a call that
                # will keep failing.
                raise _tool_error(exc.code, exc.message) from exc
            except (ActivationValidationError, FirstCandidateRefused) as exc:
                raise _tool_error(
                    "candidate_refused",
                    "The candidate was not published and the live pointer is "
                    f"unchanged: {exc}.",
                ) from exc
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__name__ == "ToolError":
            raise
        logger.error("first_candidate_mcp: publish ds=%s: %s", datastream_id, exc)
        raise _tool_error("server_error", "Publication unavailable.") from exc

    data = {
        "project_id": project_id,
        "datastream_id": datastream_id,
        "execution_id": execution_id,
        "operation_ref": operation.operation_id,
        "outcome": operation.outcome,
        "replayed": operation.replayed,
        "review_hash": review["review_hash"],
        "row_count": review["row_count"],
        **(operation.result or {}),
    }
    summary = (
        f"Candidate {execution_id} published ({review['row_count']} rows); "
        f"lifecycle=active, cadence={review.get('schedule', {}).get('schedule_mode')}."
    )
    return _result(summary, data)


def register(mcp) -> None:
    """Register the three first-candidate tools under the Operations profile."""
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        get_datastream_arming,
        profile="operations",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    # `confirmed_write` + `human`: dispatching a candidate spends provider quota and
    # lands real rows. `_assert_consistent` refuses any weaker declaration, which is
    # the point -- the ceremony cannot be declared away.
    register_profiled(
        mcp,
        start_datastream_first_candidate,
        profile="operations",
        effect="confirmed_write",
        data_class="operational",
        confirmation_mode="human",
    )
    register_profiled(
        mcp,
        publish_activate_datastream_candidate,
        profile="operations",
        effect="confirmed_write",
        data_class="operational",
        confirmation_mode="human",
    )


def _push(job_id: str | None) -> None:
    """Ask the push backend to run an activation job whose row is ALREADY committed.

    Never raises: the job row is the ledger, so a failed dispatch costs latency and
    the polling worker (or the reconciliation sweep) still drains it. Raising here
    would report a failure for work that WILL happen.
    """
    if not job_id:
        return
    try:
        from core.queue import dispatch_activation_task  # noqa: PLC0415

        dispatch_activation_task(job_id)
    except Exception as exc:  # noqa: BLE001 -- see docstring: latency, not loss.
        logger.warning("first_candidate_mcp: push skipped job=%s: %s", job_id, exc)
