"""La porte MCP des Reports gouvernés — story 67.23, 2026-08-22.

CE QUI MANQUAIT, ET COMMENT ON LE MESURE. `grep -rl "analysis_report"
server/core/*mcp*.py` rendait **0**. Les Reports gouvernés — ceux que la console
d'Analyze compose, versionne et exécute — n'étaient atteignables par aucun outil,
alors que « MCP is the second door of the same product » est ratifié pour cette
surface (`analyze-and-test.md:867`). Un modèle pouvait exécuter un Notebook et
pas un Report, sans qu'une ligne dise pourquoi.

POURQUOI CES TROIS VERBES ET PAS LES CINQ DE LA PORTE REST. La porte REST sert un
écran, qui compose : elle crée, édite, archive, liste les graines de connecteur.
Un modèle ne compose pas un Report — il en **trouve** un, il **lit** ce qu'il
épingle, et il l'**exécute**. Ajouter les verbes d'édition doublerait le coût du
catalogue pour des gestes que personne n'a demandés par ce canal ; le catalogue
est borné (`scripts/mcp_tool_surface_report.py --gate`, 130 assemblés / 60 wire)
et une porte qui grossit sans demande est ce que cette borne existe pour refuser.

LES NOMS, ET LE PIÈGE QU'ILS ÉVITENT. `get_report` est déjà pris — par le
**rapport nommé d'un connecteur**, un autre objet. `glossary.md:492` tranche :
« Report » vaut pour les deux, jamais sans qualifier, et *c'est le côté
connecteur qui dit « report profile »*. Renommer `get_report` est un acte de
surface de fil qui appartient à son propre commit ; en attendant, ces trois-ci se
nomment par ce qu'ils rendent — `get_report_versions` rend les versions,
`run_report_version` exécute **une version**, ce qui est exactement le contrat
gouverné : on n'exécute pas un Report, on exécute la version qu'il épingle.

LA PORTÉE EST PROUVÉE PAR CETTE PORTE, ET L'ÉCRITURE EST ARMÉE. `run_report_version`
crée un Result et dépense le budget d'entrepôt du projet : c'est une écriture, donc
`edit`, et sa connexion porte le contexte d'accès — celle qui écrit est celle qui
doit l'avoir (`db.py:313-316`).
"""

from __future__ import annotations

import json
import logging

from fastmcp.server.auth import AccessToken
from fastmcp.server.dependencies import get_access_token
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from core.audit import declare_action
from core.mcp_scope import refuse_unless_project_scope

logger = logging.getLogger(__name__)

#: AD-42 : déclarée ici, à côté du code qui l'écrit.
ACTION_REPORT_RUN = declare_action("analysis_report_run")


def _identity() -> str:
    """Le sujet du jeton, exactement comme les autres surfaces le résolvent."""
    token: AccessToken | None = get_access_token()
    return (token.claims.get("sub") or token.client_id) if token else "anonymous"


def _org_of(conn, project_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
        row = cur.fetchone()
    return str(row[0]) if row and row[0] else None


def _not_found(what: str) -> "Exception":
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    return ToolError(json.dumps({"code": "not_found", "message": what}))


def list_reports(project_id: str, include_archived: bool = False):
    """Liste les Reports d'Analyze configurés pour ce Projet.

    Ce sont les Reports que la console compose et versionne, pas les profils
    de rapport déclarés par un connecteur — ceux-là se lisent avec
    `get_report`, qui prend un connecteur et un identifiant de profil.

    - `include_archived` : par défaut NON. Un archivage qui ne vide pas la
      liste est un drapeau, pas une retraite ; les archivés restent
      atteignables en le demandant, parce que leurs versions et leurs runs
      sont des preuves.
    """
    from core.analyze_artifacts import list_reports as _list  # noqa: PLC0415
    from core.db import request_connection  # noqa: PLC0415
    from core.main import _resolve_project  # noqa: PLC0415

    identity = _identity()
    project_id = _resolve_project(project_id, identity)
    refuse_unless_project_scope(project_id, identity)

    with request_connection(identity) as conn:
        org_id = _org_of(conn, project_id)
        if org_id is None:
            raise _not_found("Project not found or archived.")
        reports = _list(
            conn,
            org_id=org_id,
            project_id=project_id,
            include_archived=bool(include_archived),
        )
    return {
        "project_id": project_id,
        # LE COMPTE EST DIT, et il l'est parce qu'une liste vide et une
        # lecture qui n'a pas eu lieu ne se rendent jamais l'une pour
        # l'autre : ici la lecture a eu lieu, et le compte le prouve.
        "count": len(reports),
        "include_archived": bool(include_archived),
        "reports": reports,
    }


def get_report_versions(project_id: str, report_id: str):
    """Un Report d'Analyze et ses versions — ce que chacune épingle.

    Le nom dit ce qu'il rend. Une version épingle une version de Query Spec :
    c'est elle qu'on exécute, jamais le Report, parce qu'une tête bougerait
    sous le lecteur entre le moment où il choisit et celui où il exécute.
    """
    from core.analyze_artifacts import ArtifactNotFound, get_report  # noqa: PLC0415
    from core.db import request_connection  # noqa: PLC0415
    from core.main import _resolve_project  # noqa: PLC0415

    identity = _identity()
    project_id = _resolve_project(project_id, identity)
    refuse_unless_project_scope(project_id, identity)

    with request_connection(identity) as conn:
        org_id = _org_of(conn, project_id)
        if org_id is None:
            raise _not_found("Project not found or archived.")
        try:
            return get_report(
                conn, org_id=org_id, project_id=project_id, report_id=report_id
            )
        except ArtifactNotFound:
            # LE MÊME REFUS QU'UN PROJET ABSENT : distinguer « il n'est pas à
            # vous » de « il n'existe pas » apprend qu'il existe.
            raise _not_found(f"Report not found: {report_id}") from None


def run_report_version(project_id: str, report_version_id: str, as_of: str = ""):
    """Exécute la version d'un Report d'Analyze, et rend le Result qu'elle produit.

    CHAQUE APPEL PRODUIT UN NOUVEAU RESULT. `run_report_version` n'a aucune
    branche qui réutiliserait un Result antérieur, et c'est délibéré : un
    Report exécuté deux fois répond deux fois sur les données du moment.
    C'est la différence avec un Notebook Run, qui est idempotent sur sa clef.

    - `as_of` : date ISO pour rejouer sur l'état d'un jour. Vide = aujourd'hui.
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    from core.analyze_artifacts import ArtifactNotFound, ArtifactRefused  # noqa: PLC0415
    from core.analyze_artifacts import run_report_version as _run  # noqa: PLC0415
    from core.audit import write_audit_row  # noqa: PLC0415
    from core.db import request_connection  # noqa: PLC0415
    from core.main import _resolve_project  # noqa: PLC0415

    identity = _identity()
    project_id = _resolve_project(project_id, identity)
    # `edit` : ce chemin crée un Result et dépense le budget d'entrepôt du
    # projet. Une exécution croisée ne fuit pas une donnée, elle en fabrique.
    refuse_unless_project_scope(project_id, identity, minimum_capability="edit")

    requested_as_of = (as_of or "").strip() or None
    try:
        with request_connection(identity) as conn:
            org_id = _org_of(conn, project_id)
            if org_id is None:
                raise _not_found("Project not found or archived.")
            answer = _run(
                conn,
                org_id=org_id,
                project_id=project_id,
                report_version_id=report_version_id,
                actor=identity,
                requested_as_of=requested_as_of,
            )
            conn.commit()
    except ArtifactNotFound:
        raise _not_found(f"Report version not found: {report_version_id}") from None
    except ArtifactRefused as refused:
        # Tous les refus, avec leur sujet -- jamais le premier seulement.
        raise ToolError(json.dumps(refused.as_dict())) from None
    except ToolError:
        raise
    except Exception as exc:
        logger.warning(
            "run_report_version: failed version=%s: %s", report_version_id, exc
        )
        raise ToolError(
            json.dumps({"code": "db_error", "message": f"Report run failed: {exc}"})
        ) from None

    write_audit_row(
        identity=identity,
        action=ACTION_REPORT_RUN,
        provider_account="",
        connection_ref="",
        metadata={
            "project_id": project_id,
            "report_version_id": report_version_id,
            "run_id": answer.get("run_id"),
            "result_id": answer.get("result_id"),
            "as_of": requested_as_of,
        },
    )
    return ToolResult(
        content=[
            TextContent(
                type="text",
                text=(
                    f"Report version {report_version_id} run "
                    f"{answer.get('run_id')}: {answer.get('state', 'accepted')}."
                ),
            )
        ],
        structured_content=answer,
    )


def register(mcp) -> None:  # noqa: ANN001 -- a FastMCP instance
    """Bind the three governed-Report tools."""
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp, list_reports,
        # `insights`, comme `get_report` et `get_daily_report` : c'est le profil
        # des lectures d'analyse. `analyze` n'est pas un profil declare, et en
        # inventer un ferait une famille de plus a filtrer.
        profile="insights", effect="read",
        data_class="operational", confirmation_mode="none",
    )
    register_profiled(
        mcp, get_report_versions,
        profile="insights", effect="read",
        data_class="operational", confirmation_mode="none",
    )
    register_profiled(
        mcp, run_report_version,
        # `operations`, comme la paire notebook : c'est une ECRITURE qui depense
        # le budget d'entrepot du projet, et `operations` est le profil que la
        # confirmation hote garde.
        profile="operations", effect="confirmed_write",
        data_class="operational", confirmation_mode="host",
    )
