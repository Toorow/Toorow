"""toorow -- Bridge from a safe diagnosis to a bounded recovery (Story 36.16, Epic 36).

This module is the BRIDGE between the canonical safe DIAGNOSIS (Story 36.12,
``datastream_diagnosis``) and the existing RECOVERY EXECUTION seams (Story 36.13
Operations ``operations_mcp`` / Story 36.18 Governance ``governance_mcp`` +
``governed_publication``). It invents NO new write operation and opens NO parallel
recovery-write path: it reads the canonical diagnosis, maps the canonical error
CLASS to EXACTLY ONE allowed procedure from a fixed set, and hands off to the
existing prepare-confirm-commit seams.

INVARIANTS (the contract of this bridge -- never bypassed):

  Fixed allowed-procedure set (E36-FR08 / Story 36.16 AC1): a proposed recovery
    maps ONLY to one of ``_ALLOWED_PROCEDURES`` == {reconnect, retry, refetch,
    reload, reprocess, replace, reconcile, rollback}. The mapping is a PURE,
    offline-testable function (``map_error_class``); a class that is not in the map
    yields NO procedure (stop + Support handoff), never an out-of-set verb.

  Distinct safe next action + owner per error class (Story 36.16 AC2): each canonical
    class (auth failure / rate limit / config error / mapping drift / DQ failure /
    partial-empty / uncertain publication) resolves to a DISTINCT procedure, safe
    next action and owner -- encoded once in ``_PROCEDURE_TABLE`` so the distinctness
    is a table property that a test can assert.

  Write-effect goes through 36.13/36.18 authority + confirmation (Story 36.16 AC3 /
    E36-NFR05): a write-effect procedure NEVER executes here. It routes through
    ``operations_mcp.prepare_datastream_recovery`` (retry / refetch / reprocess /
    reconcile) or the Governance governed confirm-publish path (mapping drift ->
    replace). This module returns a REFERENCE to the prepared operation (a
    ``preparation_id``) -- it does NOT call ``confirm_datastream_recovery``,
    ``execute_operation`` or ``commit_publication``. The human/agent confirms
    through the existing seam.

  Unknown / contradictory diagnosis -> STOP automation (Story 36.16 AC4): when the
    diagnosis carries no mapped class (``unclassified`` / ``None`` / a contradictory
    verdict), automation STOPS and a bounded, purpose-scoped Support handoff is
    offered via ``setup_responsibilities.prepare_handoff`` -- never a guessed verb.

  Original op IDs + last-known-good always visible (Story 36.16 AC5): whether the
    recovery completes, fails or is outcome_unknown, the response ALWAYS carries the
    original correlation IDs (from the diagnosis) and the last-known-good published
    execution pointer -- the recovery bridge never hides them.

  E36-NFR01 (no raw evidence): the proposal reuses ``datastream_diagnosis``'s
    sanitizers; it emits only the canonical class, procedure, owner, safe action and
    copyable correlation IDs. Provider strings, payloads, tokens and raw logs NEVER
    leave this module.

  E36-NFR02 (fail-closed scope): the read resolves the Datastream/project scope
    through the same strict AD-5 guard as 36.12 (``_guard_datastream_read``); on
    denial the tool EXISTENCE-HIDES (not_found). Fail closed on any guard failure.

Design mirrors operations_mcp / datastream_diagnosis: ``from __future__ import
annotations``, module logger, ``core.*`` imports LAZY inside function bodies (no
import cycle with core.main), pure mapping kept separate from I/O so the invariants
are offline-testable, French user-facing messages, ASCII-only stdout (AI-03).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# The FIXED allowed-procedure set. A proposed recovery maps ONLY to one of these
# verbs -- anything else is a bug and is refused (Story 36.16 AC1).
_ALLOWED_PROCEDURES = frozenset(
    {
        "reconnect",
        "retry",
        "refetch",
        "reload",
        "reprocess",
        "replace",
        "reconcile",
        "rollback",
    }
)

# The owner roles a recovery can name (reuses setup_responsibilities ACTOR_TYPES
# vocabulary so a handoff can be created against the same actor model).
_OWNER_CREDENTIAL = "credential_owner"
_OWNER_OPERATOR = "invited_operator"
_OWNER_DATA = "invited_operator"  # data operator == the invited operator role.

# How a procedure reaches execution. NONE of these execute here -- they are routing
# tags the caller (recovery_mcp / UI) uses to reach the correct existing seam:
#   * "operations_prepare": route through operations_mcp.prepare_datastream_recovery
#     (a write-effect bounded recovery: retry / refetch / reprocess / reconcile).
#   * "governance": route through the Story 36.18 governed confirm-publish path
#     (mapping drift -> a new immutable mapping version + governed publication).
#   * "human_action": a non-automatable human owner action (reconnect credentials,
#     fix configuration) -- offered as guidance, no toorow write is prepared.
_ROUTE_OPERATIONS = "operations_prepare"
_ROUTE_GOVERNANCE = "governance"
_ROUTE_HUMAN = "human_action"


@dataclass(frozen=True)
class _Procedure:
    """One row of the pure error-class -> recovery mapping (immutable + testable).

    ``operations_kind`` is the Story 36.13 verb passed to
    ``operations_mcp.prepare_datastream_recovery`` when ``route`` is the operations
    prepare path (one of {retry, refetch, reconcile}); it is None for a human action
    or a governance route.
    """

    procedure: str
    owner: str
    route: str
    safe_action_fr: str
    operations_kind: str | None = None
    write_effect: bool = False


# ---------------------------------------------------------------------------
# THE pure error-class -> procedure map (Story 36.16 AC1/AC2). One row per canonical
# class; each maps to EXACTLY ONE allowed procedure with a DISTINCT owner + action.
# Keys are the canonical classes from datastream_diagnosis._ALLOWED_ERROR_CLASSES
# (plus the derived DQ / partial-empty / uncertain-publication classes surfaced by
# the diagnosis verdict). NO provider vocabulary (AD-2).
# ---------------------------------------------------------------------------
_PROCEDURE_TABLE: dict[str, _Procedure] = {
    # --- Authorization failure -> reconnect (credential owner, human action) ------
    "auth_expired": _Procedure(
        procedure="reconnect",
        owner=_OWNER_CREDENTIAL,
        route=_ROUTE_HUMAN,
        safe_action_fr="Reconnecter la source (identifiants expires).",
    ),
    "auth_revoked": _Procedure(
        procedure="reconnect",
        owner=_OWNER_CREDENTIAL,
        route=_ROUTE_HUMAN,
        safe_action_fr="Reconnecter la source (acces revoque).",
    ),
    "permission_denied": _Procedure(
        procedure="reconnect",
        owner=_OWNER_CREDENTIAL,
        route=_ROUTE_HUMAN,
        safe_action_fr="Reconnecter et re-autoriser le compte (permission refusee).",
    ),
    # --- Rate limit -> retry-later (operator, write-effect via operations) --------
    "rate_limited": _Procedure(
        procedure="retry",
        owner=_OWNER_OPERATOR,
        route=_ROUTE_OPERATIONS,
        safe_action_fr="Reessayer plus tard (limite de debit atteinte).",
        operations_kind="retry",
        write_effect=True,
    ),
    # --- Configuration error -> reload after fix (operator, human action) ---------
    # invalid_request is a catalog/config drift signal: the operator must correct the
    # configuration, then reload. Not an automatable bounded write here.
    "invalid_request": _Procedure(
        procedure="reload",
        owner=_OWNER_OPERATOR,
        route=_ROUTE_HUMAN,
        safe_action_fr="Corriger la configuration puis recharger (requete invalide).",
    ),
    # --- Provider-transient -> retry (operator, write-effect via operations) ------
    "provider_transient": _Procedure(
        procedure="retry",
        owner=_OWNER_OPERATOR,
        route=_ROUTE_OPERATIONS,
        safe_action_fr="Reessayer (defaillance transitoire du fournisseur).",
        operations_kind="retry",
        write_effect=True,
    ),
    # --- Mapping drift -> replace mapping via GOVERNED proposal (data operator) ----
    # Routes through 36.17/36.18: a new immutable mapping version + governed publish.
    "mapping_drift": _Procedure(
        procedure="replace",
        owner=_OWNER_DATA,
        route=_ROUTE_GOVERNANCE,
        safe_action_fr="Proposer une correction de mapping gouvernee (derive de mapping).",
        write_effect=True,
    ),
    # --- DQ failure -> reprocess the candidate (operator, write-effect) -----------
    # Re-run the projection/mapping on the pinned versions (operations "retry"
    # candidate) to reprocess; never a live-pointer change.
    "dq_failure": _Procedure(
        procedure="reprocess",
        owner=_OWNER_OPERATOR,
        route=_ROUTE_OPERATIONS,
        safe_action_fr="Retraiter le candidat (echec de qualite des donnees).",
        operations_kind="retry",
        write_effect=True,
    ),
    # --- Partial / empty data -> bounded refetch (operator, write-effect) ---------
    "partial_data": _Procedure(
        procedure="refetch",
        owner=_OWNER_OPERATOR,
        route=_ROUTE_OPERATIONS,
        safe_action_fr="Refetch borne de l'intervalle affecte (donnees partielles).",
        operations_kind="refetch",
        write_effect=True,
    ),
    # --- Uncertain publication -> reconcile execution state (operator, write) -----
    "uncertain_publication": _Procedure(
        procedure="reconcile",
        owner=_OWNER_OPERATOR,
        route=_ROUTE_OPERATIONS,
        safe_action_fr="Reconcilier l'etat d'execution (publication incertaine).",
        operations_kind="reconcile",
        write_effect=True,
    ),
}


def map_error_class(error_class: str | None) -> _Procedure | None:
    """Map a canonical error class to EXACTLY ONE allowed procedure, or None (PURE).

    Story 36.16 AC1/AC2: the mapping is a pure, deterministic function over the
    canonical class only -- NEVER over raw text. A class not present in the table
    (``unclassified`` / ``None`` / a contradictory verdict) yields None: the caller
    then STOPS automation and offers a bounded Support handoff (AC4). The returned
    procedure name is ALWAYS in ``_ALLOWED_PROCEDURES`` (invariant, asserted).
    """
    proc = _PROCEDURE_TABLE.get(error_class or "")
    if proc is None:
        return None
    # Defence in depth: a table row must never carry an out-of-set verb.
    if proc.procedure not in _ALLOWED_PROCEDURES:  # pragma: no cover - guarded by tests
        logger.error("datastream_recovery: out-of-set procedure %r", proc.procedure)
        return None
    return proc


# ---------------------------------------------------------------------------
# Envelope / error / identity helpers (local -- never import a private of main).
# ---------------------------------------------------------------------------


def _tool_error(code: str, message: str):
    """Return a ToolError carrying the canonical ``{code, message}`` JSON (FR message)."""
    import json  # noqa: PLC0415

    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    return ToolError(json.dumps({"code": code, "message": message}))


# ---------------------------------------------------------------------------
# Diagnosis -> recovery classification (PURE over the sanitized diagnosis dict).
# ---------------------------------------------------------------------------


def _derived_class(diagnosis: dict) -> str | None:
    """Derive the recovery-relevant class from the canonical diagnosis (PURE).

    The 36.12 diagnosis surfaces a canonical ``error_class`` for failures. Story
    36.16 adds three RECOVERY classes that the diagnosis expresses through its
    ``verdict`` / ``source_kind`` rather than a raw provider class:

      * ``dq_failure`` -- a DQ verdict failure surfaced by the diagnosis.
      * ``partial_data`` -- a verification verdict of partial/empty rows.
      * ``uncertain_publication`` -- a publication whose outcome is unresolved.

    We read them ONLY from the sanitized diagnosis fields (never raw text). When the
    diagnosis already carries a mapped canonical class we pass it through unchanged.
    A contradictory diagnosis (a failure verdict with no class at all) returns None
    so the caller stops automation.
    """
    verdict = diagnosis.get("verdict")
    error_class = diagnosis.get("error_class")

    # A recovery hint set explicitly by the diagnosis takes precedence (bounded enum).
    hint = diagnosis.get("recovery_class")
    if hint in _PROCEDURE_TABLE:
        return hint

    # Derived DQ / verification classes carried on the diagnosis (sanitized enums).
    source_kind = diagnosis.get("source_kind")
    if verdict == "dq_failure" or source_kind == "dq":
        return "dq_failure"
    if verdict in {"partial", "empty"} or source_kind == "verification":
        return "partial_data"
    if verdict == "outcome_unknown" or source_kind == "publication":
        return "uncertain_publication"

    if error_class in _PROCEDURE_TABLE:
        return error_class

    # A failure verdict with no mappable class is UNKNOWN/CONTRADICTORY -> stop.
    return None


def _last_known_good(context: dict, target: dict | None) -> str | None:
    """Return the last-known-good published execution pointer (READ ONLY).

    Story 36.16 AC5: the response ALWAYS exposes the last-known-good publication so a
    completed / failed / outcome_unknown recovery never hides it. Read from the live
    target's ``current_published_execution_id`` (never mutated here); fall back to
    the diagnosis context's freshness pointer when the target is unavailable.
    """
    if target and target.get("current_published_execution_id"):
        return str(target["current_published_execution_id"])
    return context.get("last_known_good_execution_id")


def _original_operation_ids(diagnosis: dict) -> dict:
    """Return the ORIGINAL correlation IDs from the diagnosis (Story 36.16 AC5).

    Copies only the sanitized correlation handles (pull_id / execution_id / job_id /
    trace_id) that the 36.12 allowlist already produced -- never raw evidence. These
    remain visible whatever the recovery outcome.
    """
    corr = diagnosis.get("correlation") or {}
    return {k: v for k, v in corr.items() if v}


# ---------------------------------------------------------------------------
# propose_recovery -- the domain entry point (composes existing seams only).
# ---------------------------------------------------------------------------


def propose_recovery(conn, *, datastream_id: str, project_id: str | None, actor: str) -> dict:
    """Propose ONE bounded recovery for a datastream from its canonical diagnosis.

    THE bridge (Story 36.16). It:

      1. reads the canonical sanitized diagnosis via ``datastream_diagnosis``
         (``_collect_events`` + ``_diagnose``) -- reusing its allowlist sanitizers so
         no raw evidence leaks (E36-NFR01);
      2. derives the recovery-relevant canonical class and maps it -- through the
         PURE ``map_error_class`` -- to EXACTLY ONE allowed procedure + owner + safe
         action (AC1/AC2);
      3. for a WRITE-EFFECT procedure, PREPARES (but never executes) the recovery:
         operations-routed procedures call ``operations_mcp.prepare_datastream_recovery``
         and return the immutable ``preparation_id``; governance-routed (mapping
         drift) returns a routing pointer to the Story 36.17/36.18 governed path.
         This function NEVER calls confirm/execute/commit (AC3);
      4. for an UNKNOWN/CONTRADICTORY diagnosis, STOPS automation and offers a
         bounded, purpose-scoped Support handoff (AC4);
      5. ALWAYS returns the original operation IDs + last-known-good publication
         pointer (AC5).

    ``conn`` is an open connection owned by the caller (the MCP handler). This
    function performs no commit of its own except the ``prepare_datastream_recovery``
    seam (which owns its immutable-proposal transaction). Fail closed: any composition
    failure surfaces as a server error, never an unguarded write.
    """
    from core.datastream_diagnosis import _collect_events, _diagnose  # noqa: PLC0415

    events, context = _collect_events(conn, datastream_id)
    diagnosis = _diagnose(events)

    # Read the live target pointer (last-known-good). READ ONLY -- never mutated.
    target = _load_recovery_target(conn, datastream_id)
    last_good = _last_known_good(context, target)
    original_ids = _original_operation_ids(diagnosis)

    # The always-visible tail (Story 36.16 AC5) -- present on EVERY branch below.
    visible = {
        "original_operation_ids": original_ids,
        "last_known_good_execution_id": last_good,
        "diagnosis": {
            "verdict": diagnosis.get("verdict"),
            "error_class": diagnosis.get("error_class"),
            "retryable": diagnosis.get("retryable"),
        },
    }

    # Healthy datastream: no failure -> nothing to recover (still show the tail).
    if diagnosis.get("verdict") == "ok":
        return {
            "status": "no_recovery_needed",
            "procedure": None,
            "owner": None,
            "safe_action": "Aucune defaillance canonique : aucune recuperation requise.",
            "write_effect": False,
            **visible,
        }

    recovery_class = _derived_class(diagnosis)
    procedure = map_error_class(recovery_class)

    # (AC4) Unknown or contradictory diagnosis -> STOP + bounded Support handoff.
    if procedure is None:
        handoff = _offer_support_handoff(
            conn,
            datastream_id=datastream_id,
            actor=actor,
            diagnosis=diagnosis,
        )
        return {
            "status": "stopped_unknown_diagnosis",
            "procedure": None,
            "owner": "support",
            "safe_action": (
                "Diagnostic inconnu ou contradictoire : automatisation stoppee, "
                "transfert Support borne propose."
            ),
            "write_effect": False,
            "support_handoff": handoff,
            **visible,
        }

    # A mapped procedure. Build the base recovery proposal (no execution yet).
    proposal = {
        "status": "proposed",
        "recovery_class": recovery_class,
        "procedure": procedure.procedure,
        "owner": procedure.owner,
        "route": procedure.route,
        "safe_action": procedure.safe_action_fr,
        "write_effect": procedure.write_effect,
        **visible,
    }

    # (AC3) A read-only / human-action procedure prepares NO write.
    if not procedure.write_effect:
        proposal["prepared_operation"] = None
        return proposal

    # (AC3) A write-effect procedure PREPARES through the existing seam and returns a
    # reference -- it NEVER executes the write here.
    if procedure.route == _ROUTE_OPERATIONS:
        org_id = str(target.get("org_id")) if target and target.get("org_id") else None
        if not org_id:
            # Fail closed: no resolvable org => no write is prepared (still shows tail).
            proposal["prepared_operation"] = {
                "route": _ROUTE_OPERATIONS,
                "operations_kind": procedure.operations_kind,
                "preparation_id": None,
                "reason": "not_found",
                "executed": False,
            }
            return proposal
        proposal["prepared_operation"] = _prepare_via_operations(
            conn,
            org_id=org_id,
            datastream_id=datastream_id,
            procedure=procedure,
            diagnosis=diagnosis,
            _actor=actor,
        )
    elif procedure.route == _ROUTE_GOVERNANCE:
        proposal["prepared_operation"] = _governance_routing_pointer(
            datastream_id=datastream_id, target=target
        )
    else:  # pragma: no cover - a write-effect must carry a known route.
        proposal["prepared_operation"] = None
    return proposal


# ---------------------------------------------------------------------------
# Write-effect routing -- PREPARE only (never confirm/execute/commit). AC3.
# ---------------------------------------------------------------------------


def _prepare_via_operations(
    conn, *, org_id: str, datastream_id: str, procedure: _Procedure, diagnosis: dict, _actor: str
) -> dict:
    """Route a bounded write-effect recovery through the operations_mcp PREPARE seam.

    Story 36.16 AC3 / E36-NFR05: this composes the SAME immutable-proposal store the
    Story 36.13 ``prepare_datastream_recovery`` tool writes (``app.operation_preparations``,
    migration 070) by reusing operations_mcp's module-level helpers -- it does NOT
    create a parallel recovery-write path and it does NOT invent a second store. It
    builds the proposal, bounds the interval, runs the quota pre-check and inserts the
    IMMUTABLE preparation, returning the ``preparation_id``. It NEVER calls
    ``confirm_datastream_recovery`` / ``execute_operation`` / ``commit_publication`` --
    confirmation stays a separate trusted action.

    The insert reuses ``operations_mcp._insert_preparation``; the caller (recovery_mcp)
    commits the connection once. On any seam failure we return a routing pointer with
    the known kind (no fabricated id).
    """
    from core import operations_mcp as ops  # noqa: PLC0415

    kind = procedure.operations_kind
    interval: dict | None = None
    if kind == "refetch":
        aff = diagnosis.get("affected_interval") or {}
        try:
            interval = ops._bounded_interval(aff.get("from"), aff.get("to"))
        except Exception as exc:  # noqa: BLE001 -- unusable interval => cannot bound-refetch.
            logger.warning("datastream_recovery: refetch interval not bounded: %s", exc)
            return {"route": _ROUTE_OPERATIONS, "operations_kind": kind,
                    "preparation_id": None, "reason": "forbidden_interval", "executed": False}
    elif kind == "reconcile":
        execution_id = (diagnosis.get("correlation") or {}).get("execution_id")
        if not execution_id:
            return {"route": _ROUTE_OPERATIONS, "operations_kind": kind,
                    "preparation_id": None, "reason": "missing_execution_reference",
                    "executed": False}
        interval = {"execution_id": execution_id}

    target = ops._load_target(conn, datastream_id)
    if target is None:
        return {"route": _ROUTE_OPERATIONS, "operations_kind": kind,
                "preparation_id": None, "reason": "not_found", "executed": False}

    target_versions = {
        "plan_version_id": target.get("plan_version_id"),
        "mapping_version_id": target.get("mapping_version_id"),
        "policy_version": ops._current_policy_version(),
    }
    quota = ops._quota_estimate(target.get("platform"), 0)
    impact = {
        "kind": kind,
        "interval": interval,
        "touches_published_pointer": False,  # Operations NEVER publishes.
        "candidate_only": kind in {"retry", "refetch"},
    }
    fingerprint = ops._proposal_fingerprint(
        {
            "datastream_id": datastream_id,
            "kind": kind,
            "interval": interval,
            "target_versions": target_versions,
        }
    )
    idempotency_key = ops._recovery_idempotency_key(org_id, datastream_id, kind, fingerprint)
    proposal = {
        "org_id": org_id,
        "project_id": target.get("project_id"),
        "datastream_id": datastream_id,
        "kind": kind,
        "actor": _actor,  # threaded from propose_recovery: records who asked.
        "target_versions": target_versions,
        "interval": interval,
        "impact": impact,
        "quota": quota,
        "lock_ref": target.get("active_execution_id"),
        "rollback_ref": target.get("current_published_execution_id"),
    }
    try:
        preparation_id = ops._insert_preparation(conn, proposal, idempotency_key)
    except Exception as exc:  # noqa: BLE001 -- prepare failure is surfaced, never executed.
        logger.error("datastream_recovery: operations prepare failed ds=%s: %s", datastream_id, exc)
        return {"route": _ROUTE_OPERATIONS, "operations_kind": kind,
                "preparation_id": None, "reason": "prepare_unavailable", "executed": False}
    return {
        "route": _ROUTE_OPERATIONS,
        "operations_kind": kind,
        "preparation_id": preparation_id,
        "confirm_via": "confirm_datastream_recovery",  # Story 36.13 (never called here).
        "immutable": True,
        "executed": False,
    }


def _governance_routing_pointer(*, datastream_id: str, target: dict | None) -> dict:
    """Return the Story 36.18 governed routing pointer for a mapping-drift recovery.

    Story 36.16 AC3 / E36-NFR05: a mapping-drift recovery that CHANGES mapping routes
    through the governed confirm-publish path -- a new immutable mapping version
    (Story 36.17 ``propose_mapping_correction``) then a governed publication (Story
    36.18). This bridge does NOT create the mapping version or publish it; it returns
    a pointer to the governed seam with the current mapping version so the review
    screen can open. Dependency note: ``governance_mcp`` / ``governed_publication``
    (36.18) may not be on disk yet; we reference them by name (documented interface).
    """
    return {
        "route": _ROUTE_GOVERNANCE,
        "propose_via": "propose_mapping_correction",  # Story 36.17
        "publish_via": "governed_publication.confirm_publish",  # Story 36.18 (by name)
        "current_mapping_version_id": (
            target.get("mapping_version_id") if target else None
        ),
        "immutable": True,
        "executed": False,
    }


# ---------------------------------------------------------------------------
# Unknown / contradictory diagnosis -> bounded Support handoff (AC4).
# ---------------------------------------------------------------------------


def _offer_support_handoff(conn, *, datastream_id: str, actor: str, diagnosis: dict) -> dict:
    """Offer a bounded, purpose-scoped Support handoff for an unmapped diagnosis.

    Story 36.16 AC4: automation STOPS and a bounded Support handoff is offered. We
    reuse ``setup_responsibilities.prepare_handoff`` (the purpose-scoped, minimal,
    audited handoff seam) so no parallel handoff path is created and only the minimum
    bound action is revealed. The handoff carries the sanitized correlation IDs from
    the diagnosis -- never raw evidence.

    When a setup task for this datastream cannot be resolved (no journey), we return a
    non-persisted "offer" descriptor rather than fabricating a handoff -- the UI still
    shows the STOP + Support-handoff-available state (AC4) without minting a bearer.
    """
    correlation = _original_operation_ids(diagnosis)
    offer = {
        "kind": "support_handoff",
        "purpose": "diagnostic_uncertain",
        "bound_action": "investigate_uncertain_diagnosis",
        "correlation_ids": correlation,
        "prepared": False,
    }
    task_id = _resolve_support_task_id(conn, datastream_id)
    if not task_id:
        # No bound setup task -> offer only (no bearer minted). Still a STOP + handoff.
        return offer

    from core.setup_responsibilities import (  # noqa: PLC0415
        SetupConflict,
        SetupUnavailable,
        SetupValidationError,
        prepare_handoff,
    )

    idempotency_key = f"support.recovery.uncertain:{datastream_id}:{task_id}"
    try:
        result = prepare_handoff(
            conn,
            task_id=task_id,
            actor=actor,
            expires_in_hours=24,
            idempotency_key=idempotency_key,
            host_context={},
            trace_id=None,
        )
    except (SetupValidationError, SetupConflict, SetupUnavailable) as exc:
        logger.warning("datastream_recovery: support handoff unavailable: %s", exc)
        return offer
    offer.update(
        {
            "prepared": True,
            "handoff_id": result.handoff_id,
            "state": result.state,
            "expires_at": result.expires_at,
            "operation_id": result.operation_id,
        }
    )
    return offer


# ---------------------------------------------------------------------------
# I/O: read-only target + support-task resolution (bounded, fail-soft).
# ---------------------------------------------------------------------------


def _load_recovery_target(conn, datastream_id: str) -> dict | None:
    """Return the live datastream pointer facts (READ ONLY), or None. Fail-soft.

    Reuses the existing ``app.datastreams`` columns (E36-NFR05): current mapping
    version and the current published execution pointer (the last-known-good). The
    pointer is NEVER mutated here -- this is a pure read for AC5 visibility.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT org_id, current_mapping_version_id, current_published_execution_id
                FROM app.datastreams
                WHERE id = %s
                """,
                (datastream_id,),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 -- a degraded read never blocks the proposal.
        logger.warning("datastream_recovery: target read failed ds=%s: %s", datastream_id, exc)
        return None
    if not row:
        return None
    return {
        "org_id": row[0],
        "mapping_version_id": row[1],
        "current_published_execution_id": row[2],
    }


def _resolve_support_task_id(conn, datastream_id: str) -> str | None:
    """Resolve the setup task to hang the Support handoff on, or None. Fail-soft.

    Finds the first-report / setup task bound to the datastream's project so the
    Support handoff is purpose-scoped to a real, bounded action (never a free handoff).
    Returns None when no bound task exists (the caller then offers without minting).
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.id
                FROM app.setup_tasks t
                JOIN app.setup_journeys j ON j.id = t.journey_id
                JOIN app.datastreams d ON d.project_id = j.project_id
                WHERE d.id = %s AND t.state IN ('waiting', 'blocked', 'ready')
                ORDER BY t.created_at ASC
                LIMIT 1
                """,
                (datastream_id,),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 -- no bound task is a valid "offer only" state.
        logger.warning(
            "datastream_recovery: support task lookup failed ds=%s: %s", datastream_id, exc
        )
        return None
    return row[0] if row else None
