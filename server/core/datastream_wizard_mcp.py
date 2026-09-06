"""La porte MCP du parcours gouverné de création d'un Datastream — story 67.23.

CE QUI MANQUAIT, MESURÉ. `grep -rn "datastream_setup_drafts" server/core/*_mcp.py`
rendait **vide**. La porte REST compte **dix-neuf** routes ; la porte MCP en avait
zéro. Un modèle pouvait opérer un Datastream, le publier, le planifier — mais pas
en **créer** un par le parcours gouverné. `flows_upsert` en est un
quasi-équivalent *sans la préconfiguration* : sans plan, sans mapping, sans revue.

CINQ VERBES, ET ELLE NE REFLÈTE PAS LES DIX-NEUF ROUTES. La porte REST sert un
écran qui montre, sonde et fait patienter ; un modèle n'a pas besoin de sonder un
travail d'aperçu, il a besoin de savoir **où il en est** et **ce qui le bloque**.
`get_datastream_draft` porte tout l'état à une adresse, et
`compile_datastream_draft` lance l'aperçu avec la proposition — les deux vont
ensemble, parce qu'une proposition sans aperçu ne peut pas être revue.

LE SECRET DE CONFIRMATION NE VOYAGE PAS JUSQU'AU MODÈLE. Il existe pour lier la
confirmation d'un humain à un instantané exact ; un secret qu'un modèle détient
n'est plus la confirmation de personne. Sur cette porte, c'est
`confirmation_mode="human"` qui lie la même chose, à l'hôte, devant la personne —
et la cérémonie (geler la revue, émettre la confirmation, la consommer) est
composée à l'intérieur de `materialize_datastream_draft`.

CE QUE CETTE PORTE NE PEUT PAS ASSOUPLIR, parce que le refus vit dans le moteur
et que le moteur est APPELÉ : un item `blocked` refuse la matérialisation, et
**chaque** avertissement doit être nommé. `freeze_and_persist_final_review` est
la fonction que la porte REST appelle, extraite le même jour pour que les deux
composent la même revue — un second compositeur serait un second produit.

La cible est écrite dans `datastream-workbench-and-wizard.md`, amendement 67.23,
avant ce fichier et dans le même commit.
"""

from __future__ import annotations

import json
import logging

from fastmcp.server.auth import AccessToken
from fastmcp.server.dependencies import get_access_token

from core.audit import declare_action
from core.mcp_scope import refuse_unless_project_scope

logger = logging.getLogger(__name__)

#: AD-42 : déclarées ici, à côté du code qui les écrit.
ACTION_DRAFT_STARTED = declare_action("datastream_draft_started")
ACTION_DRAFT_MATERIALIZED = declare_action("datastream_draft_materialized")


def _identity() -> str:
    token: AccessToken | None = get_access_token()
    return (token.claims.get("sub") or token.client_id) if token else "anonymous"


def _refuse(code: str, message: str, **extra):
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    return ToolError(json.dumps({"code": code, "message": message, **extra}))


def _translate(exc: Exception):
    """Le refus du moteur, dans la langue de cette porte.

    Les codes du moteur voyagent tels quels : deux portes qui renomment le même
    refus donnent deux vocabulaires pour un seul événement, et une personne qui
    lit les deux croit avoir rencontré deux problèmes.
    """
    from core.datastream_activation import ActivationValidationError  # noqa: PLC0415
    from core.datastream_preconfiguration import (  # noqa: PLC0415
        PreconfigurationConflict,
        PreconfigurationNotFound,
    )

    if isinstance(exc, PreconfigurationNotFound):
        return _refuse("not_found", str(exc) or "Draft not found.")
    if isinstance(exc, PreconfigurationConflict):
        return _refuse("conflict", str(exc))
    if isinstance(exc, ActivationValidationError):
        # C'EST ICI QUE TOMBENT LES DEUX REFUS QUI COMPTENT : un item bloquant,
        # et un avertissement non nommé. Ils gardent la phrase du moteur.
        return _refuse("review_refused", str(exc))
    return None


def start_datastream_draft(project_id: str, idempotency_key: str):
    """Ouvre le brouillon de création de Datastream de ce Projet.

    UN SEUL BROUILLON RÉSUMABLE PAR PROJET : rappeler cet outil rend celui qui
    existe déjà, avec ce qui y a été posé, plutôt que d'en ouvrir un second.
    C'est le contrat du moteur, pas une commodité — deux brouillons ouverts
    seraient deux réponses à « où en est la création ».

    - `idempotency_key` : votre clef. Le même appel deux fois rend le même
      brouillon ; deux clefs différentes rendent quand même le brouillon du
      Projet s'il y en a un.
    """
    from core.datastream_preconfiguration import create_or_resume_draft  # noqa: PLC0415
    from core.db import request_connection  # noqa: PLC0415
    from core.main import _resolve_project  # noqa: PLC0415

    identity = _identity()
    project_id = _resolve_project(project_id, identity)
    refuse_unless_project_scope(project_id, identity, minimum_capability="edit")
    try:
        with request_connection(identity) as conn:
            draft = create_or_resume_draft(
                conn,
                project_id=project_id,
                actor=identity,
                idempotency_key=idempotency_key,
            )
            conn.commit()
    except Exception as exc:
        refusal = _translate(exc)
        if refusal is not None:
            raise refusal from None
        logger.warning("start_datastream_draft: failed project=%s: %s", project_id, exc)
        raise _refuse("db_error", f"The draft could not be opened: {exc}") from None
    return draft


def get_datastream_draft(project_id: str, draft_id: str):
    """Tout l'état d'un brouillon, à une seule adresse.

    Ce que la porte REST répartit sur sept lectures — le brouillon, les
    options de source, la dernière observation, la proposition, l'aperçu —
    arrive ici en une fois, et pour une raison : deux adresses qui décrivent
    le même brouillon sont deux états libres de diverger.

    LES BLOCAGES ET LES AVERTISSEMENTS SONT NOMMÉS. Ce sont eux qui décident
    si `materialize_datastream_draft` acceptera : un blocage ne se contourne
    pas, un avertissement doit être nommé explicitement. Les lire ici est la
    seule façon de savoir quoi nommer.
    """
    from core.datastream_preconfiguration import read_draft, read_proposal  # noqa: PLC0415
    from core.db import request_connection  # noqa: PLC0415
    from core.main import _resolve_project  # noqa: PLC0415

    identity = _identity()
    project_id = _resolve_project(project_id, identity)
    refuse_unless_project_scope(project_id, identity)
    try:
        with request_connection(identity) as conn:
            draft = read_draft(conn, project_id=project_id, draft_id=draft_id)
            proposal = None
            proposal_ref = draft.get("current_proposal_id") or draft.get("proposal_ref")
            if proposal_ref:
                proposal = read_proposal(
                    conn,
                    project_id=project_id,
                    draft_id=draft_id,
                    proposal_id=str(proposal_ref),
                )
    except Exception as exc:
        refusal = _translate(exc)
        if refusal is not None:
            raise refusal from None
        logger.warning("get_datastream_draft: failed draft=%s: %s", draft_id, exc)
        raise _refuse("db_error", f"The draft could not be read: {exc}") from None

    blocked, warnings = _review_items(proposal)
    return {
        "draft": draft,
        "proposal": proposal,
        # LES DEUX LISTES QUI DÉCIDENT DE LA SUITE, dérivées de la même
        # proposition que `freeze_final_review` lira. Les composer ici plutôt
        # que de les laisser deviner est ce qui rend la porte utilisable.
        "blocking_items": blocked,
        "warning_items": warnings,
        "can_materialize": bool(proposal) and not blocked,
        "acknowledgements_required": warnings,
    }


def observe_datastream_draft(
    project_id: str,
    draft_id: str,
    expected_revision: int,
    idempotency_key: str,
    request: dict | None = None,
):
    """Regarde la source du brouillon, et enregistre ce qu'elle contient.

    `expected_revision` est la révision que vous croyez lire. Si le brouillon
    a bougé entre-temps, l'appel est refusé plutôt que d'observer contre des
    choix qui ne sont plus ceux de la personne.
    """
    from core.datastream_setup_observations import create_observation  # noqa: PLC0415
    from core.db import request_connection  # noqa: PLC0415
    from core.main import _resolve_project  # noqa: PLC0415

    identity = _identity()
    project_id = _resolve_project(project_id, identity)
    refuse_unless_project_scope(project_id, identity, minimum_capability="edit")
    try:
        with request_connection(identity) as conn:
            observation = create_observation(
                conn,
                project_id=project_id,
                draft_id=draft_id,
                actor=identity,
                idempotency_key=idempotency_key,
                expected_revision=int(expected_revision),
                request=dict(request or {}),
            )
            conn.commit()
    except Exception as exc:
        refusal = _translate(exc)
        if refusal is not None:
            raise refusal from None
        logger.warning("observe_datastream_draft: failed draft=%s: %s", draft_id, exc)
        raise _refuse("db_error", f"The source could not be observed: {exc}") from None
    return observation


def compile_datastream_draft(
    project_id: str,
    draft_id: str,
    expected_revision: int,
    idempotency_key: str,
):
    """Compile la proposition du brouillon, et rend ses blocages et avertissements.

    La proposition est IMMUABLE et déterministe : la même observation et les
    mêmes choix la recomposent à l'identique. Ce qui revient ici est ce que la
    revue finale lira — donc la liste exacte des avertissements à nommer.
    """
    from core.datastream_preconfiguration import compile_draft  # noqa: PLC0415
    from core.db import request_connection  # noqa: PLC0415
    from core.main import _resolve_project  # noqa: PLC0415

    identity = _identity()
    project_id = _resolve_project(project_id, identity)
    refuse_unless_project_scope(project_id, identity, minimum_capability="edit")
    try:
        with request_connection(identity) as conn:
            proposal = compile_draft(
                conn,
                project_id=project_id,
                draft_id=draft_id,
                actor=identity,
                idempotency_key=idempotency_key,
                expected_revision=int(expected_revision),
            )
            conn.commit()
    except Exception as exc:
        refusal = _translate(exc)
        if refusal is not None:
            raise refusal from None
        logger.warning("compile_datastream_draft: failed draft=%s: %s", draft_id, exc)
        raise _refuse("db_error", f"The draft could not be compiled: {exc}") from None

    blocked, warnings = _review_items(proposal)
    return {
        "proposal": proposal,
        "blocking_items": blocked,
        "warning_items": warnings,
        "acknowledgements_required": warnings,
    }


def materialize_datastream_draft(
    project_id: str,
    draft_id: str,
    preview_ref: str,
    acknowledged_warning_ids: list,
    idempotency_key: str,
):
    """Crée le Datastream : gèle la revue, la confirme, matérialise.

    LA CÉRÉMONIE EST ENTIÈRE ET ELLE EST COMPOSÉE ICI, jamais recopiée. Geler
    la revue passe par `freeze_and_persist_final_review`, la fonction que la
    porte REST appelle ; la confirmation est émise et consommée dans la même
    transaction, donc le secret ne quitte pas le serveur.

    DEUX REFUS QUE CETTE PORTE NE PEUT PAS LEVER, parce qu'ils vivent dans le
    moteur : un item **bloquant** refuse (« Blocking review items cannot be
    acknowledged away »), et **chaque** avertissement doit figurer dans
    `acknowledged_warning_ids` (« Every warning requires an explicit
    acknowledgement »). `get_datastream_draft` les nomme tous les deux.

    - `preview_ref` : l'aperçu revu. Il doit être courant et passant.
    - `acknowledged_warning_ids` : la clé de chaque avertissement, une par une.
    """
    from core.datastream_activation import (  # noqa: PLC0415
        execute_confirmed_operation,
        materialize_draft_mutation,
        prepare_confirmation,
        read_final_review,
    )
    from core.datastream_preconfiguration_api import (  # noqa: PLC0415
        DATASTREAM_SETUP_CREATE_DRAFT_COMMAND,
        freeze_and_persist_final_review,
    )
    from core.db import request_connection  # noqa: PLC0415
    from core.main import _resolve_project  # noqa: PLC0415

    identity = _identity()
    project_id = _resolve_project(project_id, identity)
    refuse_unless_project_scope(project_id, identity, minimum_capability="edit")
    if not isinstance(acknowledged_warning_ids, list):
        raise _refuse(
            "invalid_input",
            "acknowledged_warning_ids must be a list of warning keys.",
        )
    try:
        with request_connection(identity) as conn:
            frozen = freeze_and_persist_final_review(
                conn,
                project_id=project_id,
                draft_id=draft_id,
                preview_id=preview_ref,
                acknowledged_warning_ids=[str(v) for v in acknowledged_warning_ids],
                actor=identity,
            )
            final_review_ref = frozen["final_review_id"]
            review = read_final_review(
                conn,
                project_id=project_id,
                draft_id=draft_id,
                final_review_id=final_review_ref,
            )
            # LE SECRET EST ÉMIS ET CONSOMMÉ DANS LA MÊME TRANSACTION. Le
            # rendre à l'appelant en ferait la confirmation de personne : sa
            # raison d'être est de lier un humain à un instantané exact, et
            # sur cette porte c'est l'hôte qui tient ce rôle
            # (`confirmation_mode="human"`).
            issued = prepare_confirmation(
                conn,
                command_type=DATASTREAM_SETUP_CREATE_DRAFT_COMMAND,
                actor_person_id=identity,
                project_id=project_id,
                resource_id=final_review_ref,
                content_hash=review["content_hash"],
                idempotency_key=idempotency_key,
            )
            exact = {**review, "final_review_id": final_review_ref}
            operation = execute_confirmed_operation(
                conn,
                command_type=DATASTREAM_SETUP_CREATE_DRAFT_COMMAND,
                actor_person_id=identity,
                actor=identity,
                org_id=review["org_id"],
                project_id=project_id,
                resource_id=final_review_ref,
                content_hash=review["content_hash"],
                idempotency_key=idempotency_key,
                confirmation_id=issued["confirmation_id"],
                confirmation_secret=issued["confirmation_secret"],
                mutation=lambda active_conn, operation_id: materialize_draft_mutation(
                    active_conn, operation_id=operation_id, review=exact, actor=identity
                ),
            )
            conn.commit()
    except Exception as exc:
        refusal = _translate(exc)
        if refusal is not None:
            raise refusal from None
        logger.warning(
            "materialize_datastream_draft: failed draft=%s: %s", draft_id, exc
        )
        raise _refuse(
            "db_error", f"The Datastream could not be created: {exc}"
        ) from None

    result = operation.result or {}
    _dispatch(result.get("candidate_job_id"))
    return {
        "datastream_id": result.get("datastream_id"),
        "final_review_ref": final_review_ref,
        "candidate_job_id": result.get("candidate_job_id"),
        "idempotent_replay": bool(getattr(operation, "replayed", False)),
    }


def register(mcp) -> None:  # noqa: ANN001 -- a FastMCP instance
    """Bind the five governed-wizard tools."""
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    # LA GARDE EST APPELEE DANS CHAQUE VERBE, PAS DERRIERE UN HELPER.
    #
    # Elle a d'abord vecu dans un `_scoped(...)` partage, et deux instruments
    # l'ont refuse le meme jour : l'inventaire de `test_mcp_tool_scope_refusal`
    # ne voyait plus AUCUN des cinq outils comme garde -- il voyait le helper --
    # et le harnais reclamait de classer un nom qui n'est pas un outil. Une
    # indirection qui CACHE la garde aux instruments est la meme famille que
    # << un import de garde n'est pas une garde >> : le code etait correct et la
    # preuve avait disparu. Trois lignes repetees cinq fois valent mieux qu'une
    # abstraction qui rend la propriete invisible.

    register_profiled(
        mcp, start_datastream_draft,
        profile="operations", effect="confirmed_write",
        data_class="operational", confirmation_mode="host",
    )
    register_profiled(
        mcp, observe_datastream_draft,
        profile="operations", effect="confirmed_write",
        data_class="operational", confirmation_mode="host",
    )
    register_profiled(
        mcp, compile_datastream_draft,
        profile="operations", effect="confirmed_write",
        data_class="operational", confirmation_mode="host",
    )
    register_profiled(
        mcp, get_datastream_draft,
        profile="operations", effect="read",
        data_class="operational", confirmation_mode="none",
    )
    register_profiled(
        mcp, materialize_datastream_draft,
        profile="operations", effect="confirmed_write",
        data_class="operational",
        # `human`, et c'est la moitie de la ceremonie qui reste dehors : le secret
        # ne voyage pas, donc c'est l'hote qui porte la confirmation devant la
        # personne. Un `host` ici materialiserait un Datastream sans que personne
        # ait vu la revue.
        confirmation_mode="human",
    )


def _review_items(proposal: dict | None) -> tuple[list[str], list[str]]:
    """Les clés bloquantes et les clés à acquitter, lues comme le moteur les lit.

    LA MÊME LECTURE QUE `freeze_final_review`, et c'est ce qui rend la porte
    honnête : si elle nommait les avertissements autrement, un modèle acquitterait
    des clés que la revue ne reconnaîtrait pas, et se ferait refuser sans savoir
    quoi corriger.
    """
    blocked: list[str] = []
    warnings: list[str] = []
    for section in (proposal or {}).get("sections") or []:
        for item in section.get("items") or []:
            if item.get("status") in {"blocked", "missing"} or item.get("blockers"):
                blocked.append(str(item.get("key")))
            if item.get("status") in {"warning", "needs_review"} or item.get("warnings"):
                warnings.append(str(item.get("key")))
    return sorted(set(blocked)), sorted(set(warnings))


def _dispatch(candidate_job_id) -> None:
    """Pousser le travail de candidat, sans jamais faire tomber la création.

    Le rejeu d'une opération déjà matérialisée redispatche volontiers : la
    réclamation refuse ce qui n'est pas réclamable, donc un doublon est un no-op,
    tandis qu'un premier envoi manqué reçoit ici sa seconde chance.
    """
    if not candidate_job_id:
        return
    try:
        from core.datastream_preconfiguration_api import (  # noqa: PLC0415
            _dispatch_activation_task,
        )

        _dispatch_activation_task(candidate_job_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("materialize_datastream_draft: dispatch failed: %s", exc)
