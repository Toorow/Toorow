"""toorow -- Governance-profile MCP surface (Story 36.18, Epic 36).

The Governance capability profile: the ONE place a 'ready' agentic mapping proposal
(Story 36.17) becomes LIVE, via the AD-27 prepare-confirm-commit publication that
advances ``app.datastreams.current_mapping_version_id`` atomically with audit +
outbox and leaves the prior version rollbackable. Three tools:

  * ``review_agent_change``   -- Governance, READ. Assemble the governed review
                                 object (profile, scope, versions, diff, interval/
                                 destination, impact, checks, rollback, expiry). The
                                 opaque confirmation secret is minted server-side and
                                 NEVER placed in model/MCP output; only a bounded,
                                 non-secret ``confirmation_handle`` is surfaced.
  * ``confirm_agent_change``  -- Governance, WRITE, confirmation_mode="human". Verify
                                 the (server-verified / trusted-console) opaque secret,
                                 recheck expired/used/replayed/actor-or-workspace-
                                 mismatch/stale/changed-pointer/failed-check, then route
                                 EXACTLY ONE durable publication operation.
  * ``rollback_agent_change`` -- Governance, WRITE, confirmation_mode="human". A
                                 DISTINCT confirmed idempotent operation with its own
                                 audit + outbox that re-points to the prior version.

INVARIANTS (E36-FR07, E36-NFR07, AD-27/AD-28)
---------------------------------------------
* The confirmation secret is OPAQUE and MODEL-HIDDEN. ``review_agent_change`` never
  returns it; ``confirm_agent_change`` receives it out-of-band from the trusted
  console / server-verified in-host presence, verifies its one-way hash, and never
  echoes it back. This module NEVER writes the raw secret into any ToolResult.
* Every tool re-authorizes at call time through ``project_access`` on the governance
  floor (``manage``); a foreign/absent resource is ``not_found`` (existence-hiding).
* The write path routes through ``governed_publication`` -> ``execute_operation``
  (atomic pointer + audit + outbox, idempotent replay). E36-NFR05: no parallel store.

Mirrors metric_semantics_mcp / operations_mcp: ``_identity`` / ``_tool_error`` /
``_result`` / ``_envelope``, lazy ``core.*`` imports inside function bodies (no
cycle), French ToolError microcopy, ASCII-only. Registered via
``mcp_profiles.register_profiled`` so the tools carry their capability declaration and
pass ``validate_catalog``; the capability middleware hides them from discovery and
denies direct calls unless a governance opt-in is present.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "1"


# ---------------------------------------------------------------------------
# Envelope + error + identity + result helpers (local -- no coupling to main).
# ---------------------------------------------------------------------------


def _envelope(data: dict) -> dict:
    return {
        "schema_version": _SCHEMA_VERSION,
        "meta": {"freshness": None, "provenance": None, "alerts": []},
        "data": data,
    }


def _tool_error(code: str, message: str):
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    return ToolError(json.dumps({"code": code, "message": message}))


def _identity() -> str:
    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    token = get_access_token()
    return token.claims.get("sub", token.client_id) if token else "anonymous"


def _host_context() -> dict:
    """Return the server-verified host/workspace context from the capability token.

    Used to bind the confirmation to a server-verified in-host human presence
    (workspace mismatch is a confirm refusal). Sourced ONLY from the verified
    capability context claim, never from a model-controlled tool argument.
    """
    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    try:
        token = get_access_token()
        if token is None:
            return {}
        context = (token.claims or {}).get("capability_context")
        if isinstance(context, dict):
            return {
                k: context.get(k)
                for k in ("host", "workspace_id", "workspace_type", "client_id")
                if context.get(k) is not None
            }
    except Exception as exc:  # noqa: BLE001
        logger.debug("governance_mcp: host context resolution failed (%s)", exc)
    return {}


def _result(summary: str, data: dict):
    from fastmcp.tools.tool import ToolResult  # noqa: PLC0415
    from mcp.types import TextContent  # noqa: PLC0415

    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=_envelope(data),
    )


# ---------------------------------------------------------------------------
# Access guard -- MIRRORS the strict AD-5 seam on the governance floor (manage).
# ---------------------------------------------------------------------------


def _guard_datastream(datastream_id: str, identity: str, *, minimum_capability: str):
    """Return an ``AccessDecision`` or raise not_found (existence-hiding).

    Wraps ``project_access.resolve_strict_resource_access`` on the Datastream scope.
    Governance publication requires ``manage``. Denial for ANY reason surfaces a
    single ``not_found`` -- we NEVER disclose whether the Datastream exists
    (E36-NFR02). Fail-closed on a guard failure (never proceed unguarded).
    """
    from core.db import get_connection  # noqa: PLC0415
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    try:
        with get_connection() as conn:
            decision = resolve_strict_resource_access(
                identity,
                conn,
                datastream_id=datastream_id,
                minimum_capability=minimum_capability,
            )
    except Exception as exc:  # noqa: BLE001 -- fail-closed (never unguarded).
        logger.error("governance_mcp: access guard failed ds=%s: %s", datastream_id, exc)
        raise _tool_error("not_found", "Flux introuvable.") from exc
    if not decision.allowed or not decision.org_id:
        raise _tool_error("not_found", "Flux introuvable.")
    return decision


def _proposal_datastream(conn, proposal_id: str) -> tuple[str | None, str | None]:
    """Resolve (datastream_id, org_id) for a proposal so we can guard its resource."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT datastream_id, org_id FROM app.mapping_proposals WHERE id = %s",
            (proposal_id,),
        )
        row = cur.fetchone()
    if not row:
        return None, None
    return row[0], row[1]


# ---------------------------------------------------------------------------
# register(mcp) -- define each handler locally + register_profiled.
# ---------------------------------------------------------------------------


def register(mcp) -> None:
    """Register the Governance-profile MCP tools on *mcp* (Story 36.18).

    Called once from core.main AFTER the mapping_proposal_mcp registration and BEFORE
    ``validate_catalog()`` so the boot validator sees these tools. Every tool is
    registered via ``mcp_profiles.register_profiled`` under ``profile="governance"``
    so the capability middleware hides it unless a governance opt-in is present. The
    read tool is ``effect="read"``/``confirmation_mode="none"``; both write tools are
    ``effect="write"``/``confirmation_mode="human"``.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    # ---- Read: assemble the governed review (secret stays model-hidden) ----
    def review_agent_change(proposal_id: str):
        """Ouvre la revue gouvernee d'une proposition de mapping PRETE (Story 36.18).

        Assemble l'objet de revue en LECTURE : profil (governance), perimetre
        acteur/ressource, versions (candidate + prior), diff de mapping (via
        diff_mappings, sans valeurs brutes du provider), destination (avancee du
        pointeur de mapping), impact attendu, controles enregistres, chemin de
        rollback (version prior) et expiration. Le secret de confirmation est frappe
        cote serveur et reste MODELE-CACHE : il n'apparait JAMAIS dans la sortie ;
        seul un 'confirmation_id' non secret est expose. Guard strict AD-5 (manage) ;
        proposition/flux etranger -> not_found. Ne cree AUCUNE operation durable et
        n'avance AUCUN pointeur.
        """
        proposal_id = (proposal_id or "").strip() or None
        if proposal_id is None:
            raise _tool_error("missing_param", "proposal_id est requis.")
        identity = _identity()

        from core.db import get_connection  # noqa: PLC0415
        from core.governed_publication import (  # noqa: PLC0415
            PublicationReviewUnavailable,
            prepare_publication_review,
        )

        try:
            with get_connection() as conn:
                datastream_id, _org = _proposal_datastream(conn, proposal_id)
                if datastream_id is None:
                    raise _tool_error("not_found", "Proposition introuvable.")
                decision = _guard_datastream(datastream_id, identity, minimum_capability="manage")
                review = prepare_publication_review(
                    conn,
                    proposal_id=proposal_id,
                    actor=identity,
                    org_id=str(decision.org_id),
                    host_context=_host_context(),
                )
                conn.commit()
        except PublicationReviewUnavailable as exc:
            # Existence-hiding: a not-ready/out-of-scope proposal is not disclosed.
            raise _tool_error("not_found", "Proposition introuvable ou non publiable.") from exc
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "ToolError":
                raise
            logger.error("governance_mcp: review failed prop=%s: %s", proposal_id, exc)
            raise _tool_error("server_error", "Revue indisponible.") from exc

        # Return ONLY the model-safe review object. The confirmation secret
        # (review.confirmation_secret) is DELIBERATELY dropped here so it never enters
        # model context; the trusted console retrieves it out-of-band.
        data = dict(review.review)
        data["confirmation_secret_present"] = False  # explicit: secret is model-hidden
        summary = (
            f"Revue gouvernee de la proposition {proposal_id!r} : profil "
            f"'governance', avancee du pointeur de mapping (a confirmer, secret "
            f"non expose au modele)."
        )
        return _result(summary, data)

    # ---- Write: confirm + publish -> ONE durable operation (AD-27 commit) ----
    def confirm_agent_change(confirmation_id: str, confirmation_secret: str):
        """Confirme et PUBLIE une proposition : UNE operation durable (Story 36.18).

        Verifie le secret de confirmation OPAQUE (console de confiance ou presence
        humaine in-host verifiee cote serveur ; compare un digest a sens unique, ne
        renvoie JAMAIS le secret), puis RE-VERIFIE : expiree / deja utilisee / rejouee
        / acteur non concordant / espace de travail non concordant / versions perimees
        / pointeur change / politique changee / proposition non prete -> AUCUNE
        operation creee ni dispatchee. Sinon, route EXACTEMENT UNE operation durable
        (Story 36.2) dont la mutation avance current_mapping_version_id (pointeur +
        journal + audit + outbox atomiques) et laisse la version prior ROLLBACKABLE.
        Un confirm en double / timeout renvoie l'operation ORIGINALE (jamais de
        doublon). Sur echec / outcome_unknown, la reconciliation utilise les
        references d'origine (aucune mutation fraiche aveugle). Guard strict AD-5
        (manage) ; confirmation etrangere -> not_found.
        """
        confirmation_id = (confirmation_id or "").strip() or None
        confirmation_secret = confirmation_secret or ""
        if confirmation_id is None:
            raise _tool_error("missing_param", "confirmation_id est requis.")
        if not confirmation_secret.strip():
            raise _tool_error("missing_param", "confirmation_secret est requis.")
        identity = _identity()

        from core.db import get_connection  # noqa: PLC0415
        from core.governed_publication import (  # noqa: PLC0415
            PublicationConfirmationRefused,
            confirm_and_publish,
        )

        try:
            with get_connection() as conn:
                confirmation = _load_confirmation_scope(conn, confirmation_id)
                if confirmation is None:
                    raise _tool_error("not_found", "Confirmation introuvable.")
                decision = _guard_datastream(
                    confirmation["datastream_id"], identity, minimum_capability="manage"
                )
                if str(decision.org_id) != str(confirmation["org_id"]):
                    raise _tool_error("not_found", "Confirmation introuvable.")
                result = confirm_and_publish(
                    conn,
                    confirmation_id=confirmation_id,
                    confirmation_secret=confirmation_secret,
                    actor=identity,
                    org_id=str(decision.org_id),
                    host_context=_host_context(),
                )
                conn.commit()
        except PublicationConfirmationRefused as exc:
            logger.info(
                "governance_mcp: confirm refused conf=%s code=%s", confirmation_id, exc.code
            )
            raise _tool_error(exc.code, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "ToolError":
                raise
            logger.error("governance_mcp: confirm failed conf=%s: %s", confirmation_id, exc)
            raise _tool_error("server_error", "Confirmation indisponible.") from exc

        # NEVER echo the secret. Return only the operation outcome + versions.
        data = {
            "confirmation_id": result.confirmation_id,
            "operation_id": result.operation_id,
            "outcome": result.outcome,
            "replayed": result.replayed,
            "current_mapping_version_id": result.current_mapping_version_id,
            "prior_mapping_version_id": result.prior_mapping_version_id,
            "prior_version_rollbackable": result.prior_mapping_version_id is not None,
        }
        replayed = " (rejeu de l'operation originale)" if result.replayed else ""
        summary = (
            f"Publication confirmee : operation {result.operation_id} "
            f"({result.outcome}){replayed}. Pointeur de mapping avance ; la version "
            f"prior reste rollbackable."
        )
        return _result(summary, data)

    # ---- Write: rollback -> a DISTINCT confirmed idempotent operation ----
    def rollback_agent_change(confirmation_id: str, target_mapping_version_id: str):
        """Annule une publication : operation DISTINCTE idempotente (Story 36.18).

        Re-pointe current_mapping_version_id vers la version prior (capturee lors de
        la publication). C'est une operation durable SEPAREE (jamais un rejeu de
        l'operation de publication) avec son PROPRE audit + outbox. La cible doit etre
        la version que cette confirmation a remplacee ; toute autre cible est refusee.
        Idempotente au rejeu. Guard strict AD-5 (manage) ; confirmation etrangere ->
        not_found.
        """
        confirmation_id = (confirmation_id or "").strip() or None
        target_mapping_version_id = (target_mapping_version_id or "").strip() or None
        if confirmation_id is None:
            raise _tool_error("missing_param", "confirmation_id est requis.")
        if target_mapping_version_id is None:
            raise _tool_error("missing_param", "target_mapping_version_id est requis.")
        identity = _identity()

        from core.db import get_connection  # noqa: PLC0415
        from core.governed_publication import (  # noqa: PLC0415
            PublicationConfirmationRefused,
            rollback_publication,
        )

        try:
            with get_connection() as conn:
                confirmation = _load_confirmation_scope(conn, confirmation_id)
                if confirmation is None:
                    raise _tool_error("not_found", "Confirmation introuvable.")
                decision = _guard_datastream(
                    confirmation["datastream_id"], identity, minimum_capability="manage"
                )
                if str(decision.org_id) != str(confirmation["org_id"]):
                    raise _tool_error("not_found", "Confirmation introuvable.")
                result = rollback_publication(
                    conn,
                    confirmation_id=confirmation_id,
                    target_mapping_version_id=target_mapping_version_id,
                    actor=identity,
                    org_id=str(decision.org_id),
                    host_context=_host_context(),
                )
                conn.commit()
        except PublicationConfirmationRefused as exc:
            logger.info(
                "governance_mcp: rollback refused conf=%s code=%s", confirmation_id, exc.code
            )
            raise _tool_error(exc.code, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "ToolError":
                raise
            logger.error("governance_mcp: rollback failed conf=%s: %s", confirmation_id, exc)
            raise _tool_error("server_error", "Rollback indisponible.") from exc

        data = {
            "confirmation_id": result.confirmation_id,
            "operation_id": result.operation_id,
            "outcome": result.outcome,
            "replayed": result.replayed,
            "current_mapping_version_id": result.current_mapping_version_id,
            "prior_mapping_version_id": result.prior_mapping_version_id,
            "distinct_operation": True,
        }
        replayed = " (rejeu de l'operation originale)" if result.replayed else ""
        summary = (
            f"Rollback confirme : operation distincte {result.operation_id} "
            f"({result.outcome}){replayed}. Pointeur de mapping ramene a "
            f"{target_mapping_version_id!r}."
        )
        return _result(summary, data)

    register_profiled(
        mcp, review_agent_change,
        profile="governance", effect="read",
        data_class="sensitive", confirmation_mode="none",
    )
    register_profiled(
        mcp, confirm_agent_change,
        profile="governance", effect="write",
        data_class="sensitive", confirmation_mode="human",
    )
    register_profiled(
        mcp, rollback_agent_change,
        profile="governance", effect="write",
        data_class="sensitive", confirmation_mode="human",
    )


def _load_confirmation_scope(conn, confirmation_id: str) -> dict | None:
    """Resolve (datastream_id, org_id) for a confirmation so we can guard its resource.

    A lightweight scope read used only to run the strict AD-5 guard BEFORE the domain
    module re-loads the full confirmation FOR UPDATE. Existence-hiding is preserved:
    an absent row yields ``not_found`` at the caller.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT datastream_id, org_id FROM app.publication_confirmations WHERE id = %s",
            (confirmation_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {"datastream_id": row[0], "org_id": row[1]}
