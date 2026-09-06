"""Name a canonical dimension in the client's own words — from the model.

Story 27.9 ("lignée inversée + nom client") contracts a label the client owns,
carried to the renders. Traced 2026-08-03 across the four planes with the
`capability-trace` method, for the owner's sentence *"give understandable
names"*:

    API        4 routes, `dimension_lineage_api.py:393-396`
    UI         ABSENT then -- `grep -rn "dimension-lineage" ui/admin/src` returned
               nothing. Built since; see the answered question below.
    MCP        ABSENT -- no tool named the verb
    COMPONENT  not applicable until a surface is chosen

So the capability was served and reachable by NOBODY: a person could not rename
a dimension, and neither could a model. This module opens the model's door.

WHY ONLY THIS DOOR, AND NOT THE SCREEN TOO -- ANSWERED 2026-08-24. When this
module was written no ratified page said WHERE a client label is edited (`grep
-rniE "client label|libelle client|display label" docs/product-architecture/*.md`
-> empty), so choosing a surface would have been inventing a design decision.
`governance.md` has since named both doors -- *"the console (`POST
/api/dimension-lineage/labels`) and the model"* -- and the console door is now
built: `ui/admin/src/governance/DimensionLabelPanel.tsx`, on the `lineage` tab of
a Canonical Field, which already read the label and offered nothing. The trace
above is therefore historical: the UI plane is no longer absent.

ONE STATE, TWO DOORS (AD-1). These tools call the SAME functions the REST
handlers call -- `resolve_dimension_labels` and `set_dimension_label` in
`core.dimension_conformance`. Re-implementing the cascade here would produce a
second answer to "what is this dimension called", which is exactly the defect a
client label exists to remove.

THE IDENTIFIER IS NEVER TOUCHED. `set_dimension_label` writes the string a
person reads and leaves `canonical_dimension` alone. A rename that moved the
identifier would break every pinned Result that references it.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: The three scopes a label may live at, mirroring `validate_scope`. Stated here
#: so a value this tool accepts and the validator refuses cannot exist.
#:
#: The same discipline caught me one level up: I first declared these tools with
#: `data_class="governed"`, a value I invented. `mcp_profiles.DATA_CLASSES` holds
#: exactly ("public", "operational", "sensitive") and the catalogue refused the
#: registration at import time -- seven test files failed to collect. A label is
#: `operational`: it names a dimension, it carries no personal or secret datum.
SCOPES = ("platform", "org", "project")


def read_dimension_labels(org_id: str | None = None, project_id: str | None = None):
    """Lit les noms clients des dimensions canoniques, cascade PROJET > ORG > PLATEFORME.

    Rend uniquement les dimensions qui PORTENT un libelle. Une dimension
    absente de la reponse n'a pas de nom client : elle s'affiche sous son
    identifiant stable, et c'est une information, pas un trou.
    """
    from core.dimension_conformance import resolve_dimension_labels  # noqa: PLC0415
    from core.mcp_scope import (  # noqa: PLC0415
        refuse_unless_org_scope,
        refuse_unless_project_scope,
    )

    if not org_id and not project_id:
        return {
            "error": "missing_scope",
            "message": "org_id or project_id is required to resolve the cascade.",
        }
    # Story 53.1. Ce module a ete trouve par la garde d'AC7 une fois qu'elle a
    # cesse de ne lire que `main.py` : il prenait un `project_id` de
    # l'appelant et ne demandait jamais s'il y avait droit.
    #
    # LA BRANCHE ORG EST GARDEE DEPUIS LE 2026-08-21, ET LA REGLE N'EST PAS
    # INVENTEE. Ce qui manquait n'etait pas une decision de conception : la
    # porte REST jumelle du MEME etat la porte deja, ecrite dans son
    # docstring (`dimension_lineage_api.py:15-17`) -- une lecture exige
    # l'appartenance a l'organisation, une ecriture exige owner/admin. Le
    # noyau le dit de son cote (`dimension_conformance.py:1307-1308` :
    # << org_id/project_id are assumed ALREADY AUTHORIZED -- the calling
    # surface owns the org guard >>). Cette surface est une surface
    # appelante, et elle ne le detenait pas.
    #
    # LES DEUX GARDES SONT INDEPENDANTES, PAS ALTERNATIVES. Un appelant qui
    # detient le projet Y de l'organisation B mais passe `org_id=A` fait
    # remonter la cascade dans les libelles de A ; c'est la garde ORG, et
    # elle seule, qui ferme ce chemin.
    identity = _identity()
    if org_id:
        refuse_unless_org_scope(org_id, identity)
    if project_id:
        refuse_unless_project_scope(project_id, identity)
    try:
        labels = resolve_dimension_labels(org_id=org_id, project_id=project_id)
    except Exception as exc:  # the owner could not answer
        return {"error": "labels_unavailable", "message": str(exc)}
    return {
        "scope": {"org_id": org_id, "project_id": project_id},
        "labelled_count": len(labels),
        "labels": labels,
    }


def set_dimension_label(
    canonical_dimension: str,
    display_label: str,
    scope_level: str,
    org_id: str | None = None,
    project_id: str | None = None,
    description: str | None = None,
):
    """Nomme une dimension canonique dans les mots du client. Ecrit dans PostgreSQL.

    - `canonical_dimension` : l'identifiant STABLE. Il n'est jamais modifie ;
      seule la chaine lue par une personne change.
    - `scope_level` : `org` ou `project`. Le plus specifique gagne, et un
      libelle de projet ne modifie pas celui de l'organisation. `platform`
      est refuse ici : ces entrees sont livrees avec le produit.
    - Idempotent : reecrire le meme contenu n'ecrit aucune ligne d'audit.
    """
    from core.dimension_conformance import (  # noqa: PLC0415
        InvalidScope,
    )
    from core.dimension_conformance import (
        set_dimension_label as _set,
    )
    from core.mcp_scope import (  # noqa: PLC0415
        refuse_platform_scope_write,
        refuse_unless_org_scope,
        refuse_unless_project_scope,
    )

    # LA CASSE EST NORMALISEE ICI, COMME SUR LA PORTE JUMELLE -- et ce n'est
    # pas un confort, c'est la reparation d'un outil qui n'avait JAMAIS
    # ECRIT UNE LIGNE.
    #
    # Mesure du 2026-08-22, jouee contre une vraie base : `validate_scope`
    # LEVE `InvalidScope: unknown scope_level: 'org'` -- le magasin ne connait
    # que MAJUSCULES (`app.dimension_labels` CHECK, migration 106:69, et les
    # trois soeurs des migrations 049 et 052), l'outil documentait
    # `org` / `project` en minuscules, et rien entre les deux ne convertissait.
    # Aucun libelle ecrit par cette porte n'a jamais atteint la table.
    #
    # ET L'ECHEC NE SE DISAIT MEME PAS : `InvalidScope` derive de
    # `MetricSemanticsError`, pas de `ValueError`, donc le `except ValueError`
    # plus bas ne l'attrapait pas et l'exception remontait nue.
    #
    # `dimension_lineage_api:250` fait `.strip().upper()` depuis toujours :
    # la decision du produit est deja prise, et c'est celle de CLAUDE.md --
    # << vocabulaire de l'utilisateur, pas celui de la base >>. Le mot que le
    # modele ecrit reste `org` ; c'est la frontiere qui traduit.
    scope_level = (scope_level or "").strip()
    if scope_level.lower() not in SCOPES:
        return {
            "error": "invalid_scope_level",
            "message": f"scope_level must be one of {list(SCOPES)}.",
        }
    scope_level = scope_level.lower()
    #: Le mot du MAGASIN, tenu a part du mot de l'appelant : melanger les deux
    #: dans une variable est exactement ce qui a produit le defaut.
    stored_scope = scope_level.upper()
    # LA PLATEFORME N'EST PAS ECRITE D'ICI. La porte REST du meme etat repond
    # deja `403` a ce mot (`dimension_lineage_api.py:250-256`) parce que les
    # seeds sont l'autorite de la plateforme. Sans cette ligne, ce module
    # etait la seule surface du produit ou un porteur de jeton reecrivait ce
    # que TOUTES les organisations lisent.
    if scope_level == "platform":
        refuse_platform_scope_write()
    # LE SCOPE DECLARE EXIGE SON IDENTIFIANT, ET AVANT LA GARDE. Sans ces
    # deux lignes, `scope_level="org"` sans `org_id` ne gardait rien : il n'y
    # avait aucun identifiant a resoudre, et l'appel descendait tel quel vers
    # le magasin.
    if scope_level == "org" and not org_id:
        return {
            "error": "missing_scope",
            "message": "org_id is required to name a dimension for an organization.",
        }
    if scope_level == "project" and not project_id:
        return {
            "error": "missing_scope",
            "message": "project_id is required to name a dimension for a project.",
        }
    # Story 53.1, AC5 : c'est une ECRITURE, donc `edit` et pas `view`. Un
    # libelle ecrit chez le voisin ne fuit aucune donnee -- il change ce
    # qu'un lecteur croit qu'un nombre compte, dans TOUS ses rendus.
    #
    # LA BRANCHE ORG EXIGE `manage`, ET C'EST LE RANG DE LA PORTE JUMELLE.
    # `dimension_lineage_api._set_label` garde le meme geste par
    # `_guard_org_and_project(..., manage=True)`, c'est-a-dire
    # `identity_can_manage_org` -- owner ou admin. Un rang plus bas ici
    # donnerait deux reponses a << qui peut renommer dans cette
    # organisation >>, ce qu'un etat a deux portes existe pour empecher.
    identity = _identity()
    if org_id:
        refuse_unless_org_scope(org_id, identity, minimum_capability="manage")
    if project_id:
        refuse_unless_project_scope(project_id, identity, minimum_capability="edit")
    try:
        return _set(
            canonical_dimension=canonical_dimension,
            display_label=display_label,
            scope_level=stored_scope,
            org_id=org_id,
            project_id=project_id,
            identity=identity,
            description=description,
        )
    except InvalidScope as exc:
        # LE TRIPLET EST INCOHERENT, ET IL SE DIT. `InvalidScope` derive de
        # `MetricSemanticsError` et non de `ValueError`, donc le `except`
        # d'en dessous ne l'attrapait pas : l'exception remontait nue et le
        # modele recevait une panne la ou il y avait un refus nommable.
        return {"error": "invalid_scope", "message": str(exc)}
    except ValueError as exc:
        # Blank identifier or label. The DB CHECK mirrors these, so the
        # message is the same either way.
        return {"error": "invalid_label", "message": str(exc)}


def register(mcp) -> None:
    """Register the dimension-label tools.

    `read_dimension_labels` is a read. `set_dimension_label` is a write and is
    declared `confirmation_mode="human"`: the label travels to every render, so
    renaming a dimension changes what a reader believes a number counts.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415


    register_profiled(
        mcp, read_dimension_labels,
        profile="governance", effect="read",
        data_class="operational", confirmation_mode="none",
    )
    register_profiled(
        mcp, set_dimension_label,
        profile="governance", effect="confirmed_write",
        data_class="operational", confirmation_mode="human",
    )


def _identity() -> str:
    """Resolve the caller identity from the MCP token, as every other tool does.

    Copied verbatim from `recovery_mcp._identity` rather than invented: the audit
    row this write produces must carry the same subject shape as the others, or
    two tools would name the same person differently.
    """
    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    token = get_access_token()
    return token.claims.get("sub", token.client_id) if token else "anonymous"
