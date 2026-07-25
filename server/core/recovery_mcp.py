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
    from core.db import get_connection  # noqa: PLC0415
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    try:
        with get_connection() as conn:
            decision = resolve_strict_resource_access(
                identity, conn, datastream_id=datastream_id, minimum_capability="edit"
            )
    except Exception as exc:  # noqa: BLE001 -- fail-closed (never unguarded).
        logger.error("recovery_mcp: access guard failed ds=%s: %s", datastream_id, exc)
        raise _tool_error("not_found", "Flux introuvable.") from exc
    if not decision.allowed or not decision.org_id:
        raise _tool_error("not_found", "Flux introuvable.")
    return decision


def register(mcp) -> None:
    """Register the recovery-proposal Operations-profile tool on *mcp*.

    Called once from core.main AFTER the governance_mcp registration and BEFORE the
    ``validate_catalog()`` + middleware block. Registers via ``register_profiled``
    under ``profile="operations"`` so the capability middleware filters it; the
    consequential confirm/execute stays in Story 36.13/36.18.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

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
            raise _tool_error("missing_param", "datastream_id est requis.")
        identity = _identity()
        # Fail-closed AD-5 guard (edit floor; existence-hiding on denial). The bridge
        # re-derives org scope from the datastream row inside propose_recovery.
        _guard_datastream_edit(datastream_id, identity)

        from core.datastream_recovery import propose_recovery  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        try:
            with get_connection() as conn:
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
            raise _tool_error("server_error", "Proposition de recuperation indisponible.") from exc

        status = proposal.get("status")
        procedure = proposal.get("procedure")
        if status == "no_recovery_needed":
            summary = f"Flux {datastream_id!r} : aucune recuperation requise."
        elif status == "stopped_unknown_diagnosis":
            summary = (
                f"Flux {datastream_id!r} : diagnostic inconnu/contradictoire -- "
                "automatisation stoppee, transfert Support borne propose."
            )
        else:
            summary = (
                f"Flux {datastream_id!r} : procedure recommandee {procedure!r} "
                f"(proprietaire {proposal.get('owner')})."
            )
        return _result(summary, proposal)

    register_profiled(
        mcp, propose_datastream_recovery,
        profile="operations", effect="read",
        data_class="operational", confirmation_mode="none",
    )
