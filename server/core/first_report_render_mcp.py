"""toorow -- First-report render / reproduce MCP surface (Story 36.15, Epic 36).

Exposes TWO Insights-profile, read-only tools behind the *starter request*
(E36-FR05):

  * ``render_starter_report``    -- render + validate the first correct answer.
  * ``reproduce_starter_report`` -- a SECOND authorized user reproduces the SAME
    publication/provenance result; unauthorized -> existence-hiding not_found.

Both are registered via ``mcp_profiles.register_profiled(profile="insights",
effect="read", data_class="operational", confirmation_mode="none")`` so the
capability middleware treats them as safe reads (Insights is the default, always
discoverable). They WRAP the ``first_report_render`` domain (E36-NFR05) -- no
second report engine, no parallel access path.

INVARIANTS (mirrored in the tests):
  * READ-ONLY: neither tool mutates anything (pure render/compose).
  * NO FULL DATASET INTO MODEL CONTEXT (E36-NFR06): the model-facing text is a
    LEAN summary; the structured envelope carries BOUNDED evidence (counts /
    hashes / a few figures) + an OPTIONAL authenticated deep-link. The full
    dataset is NEVER embedded -- it lives behind the deep-link the human follows.
  * APP-UI vs COMPACT-TEXT decided from the BOUND host connection (36.14
    ``ui_supported``, resolved inside the domain): no app UI -> compact text WITH
    mandatory bounded evidence (never deep-link-only).
  * FAIL-CLOSED AD-5 access (existence-hiding on denial): the render guards at the
    ``view`` floor; the reproduce guards INDEPENDENTLY for the SECOND identity and
    hides existence on ANY denial (no report / no first-user leak).
  * FIGURES CITE the active publication + mapping versions so the returned
    validation checklist can confirm they match.

Design mirrors operations_mcp / recovery_mcp: ``from __future__ import
annotations``, module logger, ``core.*`` imports LAZY inside function bodies (no
import cycle with core.main), French ToolError / summary microcopy, ASCII-only.
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
    """Build the dual-channel ToolResult (lean text summary + AD-1 envelope).

    INVARIANT (E36-NFR06): ``summary`` is a SHORT French line; ``data`` is the
    bounded render (counts / hashes / a few figures + deep-link). The FULL dataset
    is never in either channel -- it lives behind the authenticated deep-link.
    """
    from fastmcp.tools.tool import ToolResult  # noqa: PLC0415
    from mcp.types import TextContent  # noqa: PLC0415

    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=_envelope(data),
    )


def _guard_project_view(project_id: str, identity: str):
    """Return an ``AccessDecision`` or raise not_found (existence-hiding, view floor).

    Wraps ``project_access.resolve_strict_resource_access`` at the ``view`` floor
    (the starter request is a read). Denial for ANY reason -> a single
    ``not_found`` (E36-NFR02). Fail closed: never proceed unguarded on a guard
    failure.
    """
    from core.db import get_connection  # noqa: PLC0415
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    try:
        with get_connection() as conn:
            decision = resolve_strict_resource_access(
                identity, conn, project_id=project_id, minimum_capability="view"
            )
    except Exception as exc:  # noqa: BLE001 -- fail-closed (never unguarded).
        logger.error("first_report_render_mcp: access guard failed: %s", type(exc).__name__)
        raise _tool_error("not_found", "Rapport introuvable.") from exc
    if not decision.allowed or not decision.org_id:
        raise _tool_error("not_found", "Rapport introuvable.")
    return decision


def register(mcp) -> None:
    """Register the two Insights read-only starter-report tools on *mcp*.

    Called once from core.main AFTER the ``recovery_mcp`` registration and BEFORE
    the ``validate_catalog()`` + middleware block. Both tools register via
    ``register_profiled(profile="insights", effect="read", ...)`` so the boot-time
    validator sees them (Insights read-only, no confirmation).
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    # ---- Read: render + validate the first correct answer (starter request) -----
    def render_starter_report(project_id: str, datastream_id: str):
        """Rend et VALIDE le premier rapport correct (requete de demarrage, lecture).

        Ne s'execute QUE si l'etat de preparation (Story 36.10) est ``ready`` ou
        ``degraded`` (degrade dans la politique) ; ``blocked`` -> refuse (rien a
        rendre). La reponse CITE fraicheur / couverture / provenance (version de
        mapping) / exclusions et rend l'UI finale du rapport quand l'hote la
        supporte. SANS UI applicative : texte compact incluant une PREUVE BORNEE
        obligatoire (totaux / empreintes / quelques figures) + un lien profond
        authentifie optionnel, SANS envoyer le jeu de donnees complet dans le
        contexte du modele (E36-NFR06). Les figures referencent la publication
        active + la version de mapping pour que la validation confirme la
        correspondance ; etat degrade/perime honnete, non-couleur, explicite.
        Guard strict AD-5 (vue) ; rapport etranger -> not_found (existence cachee).
        Lecture pure : ne mute rien.
        """
        project_id = (project_id or "").strip() or None
        datastream_id = (datastream_id or "").strip() or None
        if project_id is None or datastream_id is None:
            raise _tool_error("missing_param", "project_id et datastream_id sont requis.")
        identity = _identity()
        _guard_project_view(project_id, identity)

        from core.db import get_connection  # noqa: PLC0415
        from core.first_report_render import (  # noqa: PLC0415
            FirstReportRenderUnavailable,
            render_first_report,
        )

        try:
            with get_connection() as conn:
                rendered = render_first_report(
                    conn,
                    datastream_id=datastream_id,
                    project_id=project_id,
                    actor=identity,
                )
        except FirstReportRenderUnavailable:
            # Readiness is blocked / not renderable: honest, NOT an access leak
            # (the caller already passed the view guard).
            raise _tool_error(
                "not_renderable",
                "Le rapport n'est pas encore rendable : le pull recent n'est pas publie.",
            )
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "ToolError":
                raise
            logger.error("first_report_render_mcp: render failed: %s", type(exc).__name__)
            raise _tool_error("server_error", "Rendu du rapport indisponible.") from exc

        data = rendered.as_dict()
        mode = "UI applicative" if data["ui_supported"] else "texte compact + preuve bornee"
        summary = (
            f"Premier rapport rendu ({data['overall']}, {mode}) pour le flux "
            f"{datastream_id!r} : publication "
            f"{(data.get('publication') or {}).get('execution_id')!r}, mapping v"
            f"{(data.get('mapping') or {}).get('version_number')}."
        )
        return _result(summary, data)

    # ---- Read: reproduce for a SECOND authorized user (independent access) -------
    def reproduce_starter_report(
        project_id: str, datastream_id: str, workspace_ref: str | None = None
    ):
        """Reproduit le premier rapport pour un SECOND utilisateur (lecture, acces independant).

        L'acces du SECOND utilisateur est reevalue INDEPENDAMMENT via le seam
        strict AD-5 (et, si fourni, le SECOND espace de travail ``workspace_ref``).
        Autorise -> le MEME resultat publication/provenance est reproductible
        (memes versions, meme preuve bornee). NON autorise (autre utilisateur ou
        espace) -> refus SANS exposer l'existence du rapport ni le resultat du
        premier utilisateur (not_found, existence cachee). SANS UI applicative :
        texte compact + preuve bornee (jamais le jeu complet). Lecture pure.
        """
        project_id = (project_id or "").strip() or None
        datastream_id = (datastream_id or "").strip() or None
        workspace_ref = (workspace_ref or "").strip() or None
        if project_id is None or datastream_id is None:
            raise _tool_error("missing_param", "project_id et datastream_id sont requis.")
        second_actor = _identity()

        from core.db import get_connection  # noqa: PLC0415
        from core.first_report_render import (  # noqa: PLC0415
            FirstReportRenderUnavailable,
            FirstReportReproductionDenied,
            reproduce_first_report,
        )

        try:
            with get_connection() as conn:
                rendered = reproduce_first_report(
                    conn,
                    datastream_id=datastream_id,
                    project_id=project_id,
                    second_actor=second_actor,
                    workspace_ref=workspace_ref,
                )
        except FirstReportReproductionDenied:
            # Existence-hiding: the second user/workspace is NOT authorized. NEVER
            # disclose report existence or the first-user result.
            raise _tool_error("not_found", "Rapport introuvable.")
        except FirstReportRenderUnavailable:
            # Authorized second user, but readiness is not renderable -> honest.
            raise _tool_error(
                "not_renderable",
                "Le rapport n'est pas encore rendable : le pull recent n'est pas publie.",
            )
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "ToolError":
                raise
            logger.error("first_report_render_mcp: reproduce failed: %s", type(exc).__name__)
            raise _tool_error("server_error", "Reproduction du rapport indisponible.") from exc

        data = rendered.as_dict()
        data["reproduced"] = True
        summary = (
            f"Reproduction confirmee ({data['overall']}) pour le flux "
            f"{datastream_id!r} : meme publication "
            f"{(data.get('publication') or {}).get('execution_id')!r} "
            f"(acces du second utilisateur evalue independamment)."
        )
        return _result(summary, data)

    register_profiled(
        mcp, render_starter_report,
        profile="insights", effect="read",
        data_class="operational", confirmation_mode="none",
    )
    register_profiled(
        mcp, reproduce_starter_report,
        profile="insights", effect="read",
        data_class="operational", confirmation_mode="none",
    )
