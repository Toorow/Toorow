"""toorow -- Recovery-proposal MCP surface (Story 36.16, Epic 36).

Exposes ONE Operations-profile tool -- ``propose_datastream_recovery`` -- that is the
BRIDGE from the canonical safe diagnosis (Story 36.12) to the existing recovery
execution seams (Story 36.13 Operations / Story 36.18 Governance). It reads the
canonical diagnosis, maps the error class to EXACTLY ONE allowed procedure and, for a
write-effect procedure, PREPARES (never executes) the recovery through the existing
prepare-confirm-commit seam, returning a reference to the immutable prepared
operation. It NEVER confirms, executes or commits a write itself.

INVARIANTS (mirrors datastream_recovery's contract):
  * recovery maps ONLY to the fixed allowed-procedure set;
  * write-effect recovery goes through 36.13/36.18 authority + confirmation (no
    parallel write path) -- this tool only PREPARES;
  * unknown / contradictory diagnosis -> STOP automation + bounded Support handoff;
  * original op IDs + last-known-good publication ALWAYS visible in the response;
  * E36-NFR01 no raw provider evidence; E36-NFR05 compose existing seams;
  * E36-NFR02 fail-closed AD-5 scope (existence-hiding on denial).

The proposal can PREPARE an immutable write-effect operation, so it guards at the
``edit`` capability floor (same floor as ``prepare_datastream_recovery``). It is
registered under ``profile="operations"``/``effect="read"``/``confirmation_mode="none"``
(it reads + prepares; the consequential confirm lives in Story 36.13/36.18).

Design mirrors operations_mcp: ``from __future__ import annotations``, module logger,
``core.*`` imports LAZY inside function bodies (no import cycle with core.main), French
ToolError / summary microcopy, ASCII-only stdout.
"""

from __future__ import annotations

import json
import logging

from core.project_resolver import PROJECT_NOT_FOUND_CODE

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
    """Return a ToolError carrying the canonical ``{code, message}`` JSON (FR message)."""
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    return ToolError(json.dumps({"code": code, "message": message}))


def _identity() -> str:
    """Resolve the caller identity from the MCP token (existence-hiding on absence)."""
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


def _guard_datastream_edit(datastream_id: str, identity: str):
    """Return an ``AccessDecision`` or raise not_found (existence-hiding, edit floor).

    Wraps ``project_access.resolve_strict_resource_access`` at the ``edit`` floor (the
    proposal can PREPARE an immutable write-effect operation, mirroring
    ``prepare_datastream_recovery``). Denial for ANY reason -> a single ``not_found``
    (E36-NFR02). Fail closed: never proceed unguarded on a guard failure.
    """
    from core.db import request_connection  # noqa: PLC0415
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    try:
        with request_connection(identity) as conn:
            decision = resolve_strict_resource_access(
                identity, conn, datastream_id=datastream_id, minimum_capability="edit"
            )
    except Exception as exc:  # noqa: BLE001 -- fail-closed (never unguarded).
        logger.error("recovery_mcp: access guard failed ds=%s: %s", datastream_id, exc)
        raise _tool_error(PROJECT_NOT_FOUND_CODE, "Project not found.") from exc
    if not decision.allowed or not decision.org_id:
        # One envelope for denied, absent and unavailable (53.1): a distinct
        # "Datastream not found." here was an enumeration oracle -- comparing the
        # two refusals told a stranger which datastream ids exist.
        raise _tool_error(PROJECT_NOT_FOUND_CODE, "Project not found.")
    return decision


def propose_datastream_recovery(datastream_id: str):
    """Propose UNE recuperation bornee d'UN flux depuis son diagnostic canonique.

    BRIDGE (Story 36.16) du diagnostic sur (Story 36.12) vers l'execution de
    recuperation (Story 36.13 Operations / Story 36.18 Gouvernance). Lit le
    diagnostic canonique sanitise, mappe la classe d'erreur vers EXACTEMENT UNE
    procedure autorisee de l'ensemble fixe {reconnect, retry, refetch, reload,
    reprocess, replace, reconcile, rollback}, avec une action sure et un
    proprietaire DISTINCTS par classe. Une procedure a effet d'ecriture cree une
    preparation IMMUABLE et renvoie une REFERENCE a l'operation preparee (via
    operations_mcp.prepare / chemin gouverne 36.18) -- elle N'EXECUTE JAMAIS
    l'ecriture ici (la confirmation reste une action de confiance separee).
    Diagnostic inconnu ou contradictoire -> automatisation STOPPEE + transfert
    Support borne propose. Que la recuperation aboutisse, echoue ou soit
    outcome_unknown, les IDs d'operation d'origine et la derniere publication
    connue-bonne restent visibles. Aucune preuve brute (E36-NFR01). Guard strict
    AD-5 (edition) ; flux etranger -> not_found (existence cachee).
    """
    datastream_id = (datastream_id or "").strip() or None
    if datastream_id is None:
        raise _tool_error("missing_param", "datastream_id is required.")
    identity = _identity()
    # Fail-closed AD-5 guard (edit floor; existence-hiding on denial). The bridge
    # re-derives org scope from the datastream row inside propose_recovery.
    _guard_datastream_edit(datastream_id, identity)

    from core.datastream_recovery import propose_recovery  # noqa: PLC0415
    from core.db import request_connection  # noqa: PLC0415

    try:
        with request_connection(identity) as conn:
            proposal = propose_recovery(
                conn,
                datastream_id=datastream_id,
                project_id=None,
                actor=identity,
            )
            # The write-effect prepare seam inserted an immutable proposal; commit
            # the caller-owned transaction once (the durable operation is created
            # only later, at confirm time -- Story 36.13/36.18).
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__name__ == "ToolError":
            raise
        logger.error("recovery_mcp: propose failed ds=%s: %s", datastream_id, exc)
        raise _tool_error("server_error", "Recovery proposal unavailable.") from exc

    status = proposal.get("status")
    procedure = proposal.get("procedure")
    if status == "no_recovery_needed":
        summary = f"Datastream {datastream_id!r}: no recovery required."
    elif status == "stopped_unknown_diagnosis":
        summary = (
            f"Datastream {datastream_id!r}: unknown or contradictory diagnosis -- "
            "automation stopped, a bounded Support handover is proposed."
        )
    else:
        summary = (
            f"Datastream {datastream_id!r}: recommended procedure {procedure!r} "
            f"(owner {proposal.get('owner')})."
        )
    return _result(summary, proposal)


def prepare_datastream_reprocess(datastream_id: str, project_id: str,
                                 reason: str | None = None,
                                 chosen_mapping_version_id: str | None = None):
    """Prepare UN retraitement borne d'UN flux depuis ce qui est deja arrive.

    `Reprocess` reapplique un mapping choisi a la matiere que ce deploiement
    DETIENT deja : aucun appel a la source, aucune depense. Assemble une
    proposition IMMUABLE (AD-27) -- elle n'execute rien et ne cree aucune
    operation durable ; c'est `confirm_datastream_reprocess` qui, sur
    confirmation humaine, route EXACTEMENT UNE operation durable.

    La proposition ENONCE la porte de mapping : `passed` quand le mapping
    choisi differe de celui qui a publie (le retraitement change alors ce que
    les chiffres veulent dire, et une personne confirme en ayant lu les deux
    versions), `skipped` quand c'est le meme (rien ne bouge, et le saut est
    date et attribue plutot que tu). Refus actionnables, chacun nommant le
    geste qui repare : artefact non conserve -> re-importer le fichier ;
    artefact illisible -> defaut de stockage ; mode dont la matiere vit chez
    la source -> Day-by-day coverage. Guard strict AD-5 (edition) ; flux
    etranger -> not_found.
    """
    datastream_id = (datastream_id or "").strip() or None
    project_id = (project_id or "").strip() or None
    if datastream_id is None:
        raise _tool_error("missing_param", "datastream_id is required.")
    if project_id is None:
        raise _tool_error("missing_param", "project_id is required.")
    identity = _identity()
    decision = _guard_datastream_edit(datastream_id, identity)

    from core.bounded_recovery import (  # noqa: PLC0415
        KIND_REPROCESS,
        BoundedRecoveryError,
        prepare_bounded_recovery,
    )
    from core.db import request_connection  # noqa: PLC0415

    try:
        with request_connection(identity) as conn:
            proposal = prepare_bounded_recovery(
                conn,
                org_id=str(decision.org_id),
                datastream_id=datastream_id,
                kind=KIND_REPROCESS,
                actor=identity,
                reason=reason,
                chosen_mapping_version_id=chosen_mapping_version_id,
            )
            conn.commit()
    except BoundedRecoveryError as exc:
        # The refusal reaches the model with the SAME code and the SAME
        # sentence the console shows. A tool that paraphrased it would be a
        # second vocabulary for one decision.
        raise _tool_error(exc.code, exc.message) from exc
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__name__ == "ToolError":
            raise
        logger.error("recovery_mcp: prepare reprocess failed ds=%s: %s", datastream_id, exc)
        raise _tool_error("server_error", "Reprocess preparation unavailable.") from exc

    gate = (proposal.get("impact") or {}).get("mapping_gate") or {}
    summary = (
        f"Datastream {datastream_id!r}: reprocess prepared "
        f"(mapping gate {gate.get('state') or 'unknown'!r}, no source call, "
        "nothing published yet)."
    )
    return _result(summary, proposal)


def confirm_datastream_reprocess(preparation_id: str, project_id: str,
                                 datastream_id: str):
    """Confirme UN retraitement prepare et route UNE operation durable (AD-27).

    Re-verifie chaque precondition contre la cible VIVANTE (portee exacte,
    version de plan, politique, verrou d'execution) puis rejoue l'artefact
    conserve par le MEME chemin d'import qui atterrit, promeut et publie --
    et enregistre la NOUVELLE version de sortie qui nomme la relation
    OBSERVEE. L'ancienne version reste en place, immuable, et cesse d'etre la
    tete lue. Un confirm en double / timeout / retry renvoie l'operation
    ORIGINALE (jamais de doublon). Guard strict AD-5 (edition).
    """
    preparation_id = (preparation_id or "").strip() or None
    project_id = (project_id or "").strip() or None
    datastream_id = (datastream_id or "").strip() or None
    for name, value in (("preparation_id", preparation_id),
                        ("project_id", project_id),
                        ("datastream_id", datastream_id)):
        if value is None:
            raise _tool_error("missing_param", f"{name} is required.")
    identity = _identity()
    decision = _guard_datastream_edit(datastream_id, identity)

    from core.bounded_recovery import (  # noqa: PLC0415
        BoundedRecoveryError,
        confirm_bounded_recovery,
    )
    from core.db import request_connection  # noqa: PLC0415

    try:
        with request_connection(identity) as conn:
            outcome = confirm_bounded_recovery(
                conn,
                preparation_id=preparation_id,
                expected_org_id=str(decision.org_id),
                expected_project_id=project_id,
                expected_datastream_id=datastream_id,
                actor=identity,
                trace_id=f"mcp:reprocess:{preparation_id}",
            )
            conn.commit()
    except BoundedRecoveryError as exc:
        raise _tool_error(exc.code, exc.message) from exc
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__name__ == "ToolError":
            raise
        logger.error("recovery_mcp: confirm reprocess failed id=%s: %s", preparation_id, exc)
        raise _tool_error("server_error", "Reprocess confirmation unavailable.") from exc

    result = outcome.get("result") or {}
    summary = (
        f"Datastream {datastream_id!r}: reprocess {outcome.get('outcome')!r} "
        f"(execution {result.get('execution_id')!r}, output version "
        f"{result.get('output_version_id')!r})."
    )
    return _result(summary, outcome)


def register(mcp) -> None:
    """Register the recovery-proposal Operations-profile tool on *mcp*.

    Called once from core.main AFTER the governance_mcp registration and BEFORE the
    ``validate_catalog()`` + middleware block. Registers via ``register_profiled``
    under ``profile="operations"`` so the capability middleware filters it; the
    consequential confirm/execute stays in Story 36.13/36.18.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    # ---- Reprocess: prepare -> human confirm (chantier 67-15b) ---------------
    #
    # WHY HERE AND NOT ON `operations_mcp`'s prepare/confirm pair. That pair's
    # vocabulary is `{retry, refetch, reconcile}` and its confirm re-checks the
    # things a PROVIDER call owes -- account exposure, quota budget -- before it
    # dispatches. A reprocess calls no provider and spends nothing, so it would
    # have to be excused from both, and a confirm with two verbs' worth of
    # exceptions inside it is how a gate comes to be skipped for the wrong one.
    # `bounded_recovery` already owns the prepare/confirm that asks a reprocess's
    # own questions; these two tools are its MCP door, not a second engine.
    #
    # The REST door for the same verb is POST /api/datastreams/{id}/bounded/
    # prepare|confirm (Member). Both reach `core.bounded_recovery` -- one seam,
    # two doors, which is the parity rule this repository keeps failing.

    register_profiled(
        mcp, propose_datastream_recovery,
        profile="operations", effect="read",
        data_class="operational", confirmation_mode="none",
    )
    # prepare writes an immutable proposal row and authorizes nothing; the
    # consequential act stays on the separate human-confirmed tool.
    register_profiled(
        mcp, prepare_datastream_reprocess,
        profile="operations", effect="prepare",
        data_class="operational", confirmation_mode="none",
    )
    register_profiled(
        mcp, confirm_datastream_reprocess,
        profile="operations", effect="confirmed_write",
        data_class="operational", confirmation_mode="human",
    )
