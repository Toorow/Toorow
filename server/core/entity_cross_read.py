"""toorow -- croiser un fait avec une classification de l'utilisateur (story 69.3).

CE QUE CE MODULE EST, ET CE QU'IL N'EST PAS. Le croisement lui-meme est du SQL
gouverne : la vue `semantic_fact_by_entity_attribute` resout le rattachement
(le verdict de 68.3), lit l'attribut PORTE dans la fenetre ou il faisait foi et
la classification DERIVEE avec la version de regle qui l'a produite. Ce module
ne recalcule rien de tout cela ; il POSE la question a la vue et rend une
reponse qui DIT d'ou elle vient.

TROIS CHOSES QU'UNE REPONSE DOIT PORTER, ET QUE PERSONNE NE PEUT DEDUIRE APRES
COUP :

  * le CHEMIN ANALYTIQUE -- quelle relation a ete lue (`declare_analytical_path`,
    la meme grammaire que toute autre surface qui emet un nombre). Deux chiffres
    comparables sont deux DECLARATIONS de meme forme, pas une declaration et un
    silence ;
  * la VERSION DE REGLE, quand la classification est derivee (68.6). Republier
    une regle change les reponses sans toucher un seul fait : une reponse qui ne
    nomme pas sa version n'est pas re-derivable, donc pas verifiable ;
  * le RESTE NON RATTACHE, nomme et compte (AD-9). Une part qu'on ne sait pas
    classer est une information ; la fondre dans un vrai libelle fausse le
    total, la jeter fait un total incomplet qui se lit comme complet.

LE REFUS EST NOMME (AC4). Un croisement demande sur un attribut que rien ne
publie -- pas de version de regle, pas d'attribut porte -- est REFUSE en disant
laquelle des deux autorites manque. Rendre un cadre vide dirait << il n'y a
rien >> la ou la verite est << personne n'a encore declare cela >>, et ces deux
phrases n'appellent pas le meme geste.

AD-1 : ce module rend des AGREGATS. Les lignes vont au widget par le canal
canonique ; le canal du modele est borne et un croisement peut avoir beaucoup
de libelles.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from core.envelope import declare_analytical_path

logger = logging.getLogger(__name__)

#: La relation que ce chemin lit. IMPORTEE partout, jamais retapee : deux copies
#: manuscrites d'un nom de relation, c'est ainsi qu'une divulgation se met a
#: decrire un chemin qui a bouge.
CROSS_RELATION = "semantic_fact_by_entity_attribute"

ANALYTICAL_PATH_ENTITY_CROSS = declare_analytical_path(
    path="entity_cross",
    relation=CROSS_RELATION,
    note=(
        "Croisement fait x attribut gouverne, resolu a la date de la ligne ; "
        "lu hors Query Spec gouverne, donc le chemin Explore de la Console peut "
        "differer et aucun des deux ne reconcilie l'autre."
    ),
)

#: Le libelle du groupe que rien ne rattache. UN mot pour toutes les raisons de
#: ne pas etre rattache -- l'utilisateur n'a pas a distinguer `unmatched` de
#: `ambiguous` pour lire un total ; le detail vit a cote, dans `by_state`.
UNATTACHED_LABEL = "non rattache"

#: Les origines qu'un attribut peut avoir, et l'origine du groupe qui n'en a pas.
ORIGIN_CARRIED = "carried"
ORIGIN_DERIVED = "derived"
ORIGIN_UNATTACHED = "unattached"


class CrossReadRefused(RuntimeError):
    """Le croisement est refuse, et le message nomme l'autorite qui manque."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def compose_cross(
    rows: Sequence[Mapping[str, Any]],
    *,
    attribute: str,
) -> dict[str, Any]:
    """Agreger les lignes du croisement en une reponse. Pure.

    ``rows`` est ce que la vue rend : une ligne par (fait, attribut), plus les
    lignes du groupe non rattache. La fonction est pure pour que la regle de
    composition -- notamment la couverture et le reste -- soit prouvable sans
    entrepot.

    Le total est celui du MART : les groupes rattaches plus le reste. C'est la
    seule facon qu'une somme de la reponse egale la somme du fait, et donc que
    la reponse soit verifiable par quelqu'un qui ne fait pas confiance au
    croisement.
    """
    attribute = str(attribute or "").strip()
    groups: dict[str, float] = {}
    unattached_value = 0.0
    unattached_rows = 0
    by_state: dict[str, int] = {}
    rule_versions: set[str] = set()
    origins: set[str] = set()

    for row in rows:
        origin = str(row.get("attribute_origin") or "")
        value = float(row.get("value") or 0.0)
        if origin == ORIGIN_UNATTACHED:
            unattached_value += value
            unattached_rows += 1
            state = str(row.get("resolution_state") or "unknown")
            by_state[state] = by_state.get(state, 0) + 1
            continue
        if str(row.get("attribute") or "") != attribute:
            continue
        origins.add(origin)
        label = row.get("attribute_value")
        label = str(label) if label is not None else UNATTACHED_LABEL
        groups[label] = groups.get(label, 0.0) + value
        version = row.get("rule_set_version_id")
        if version:
            rule_versions.add(str(version))

    attached_value = sum(groups.values())
    total = attached_value + unattached_value
    return {
        "attribute": attribute,
        "groups": [
            {"label": label, "value": groups[label]} for label in sorted(groups)
        ],
        # Le reste est UNE ligne du meme tableau, nommee -- pas une note de bas
        # de page qu'un lecteur peut sauter.
        "unattached": {
            "label": UNATTACHED_LABEL,
            "value": unattached_value,
            "rows": unattached_rows,
            "by_state": {state: by_state[state] for state in sorted(by_state)},
        },
        "coverage": {
            "attached_value": attached_value,
            "total_value": total,
            # `None` et non 0 quand il n'y a rien a couvrir : une fraction sans
            # denominateur n'est pas une couverture de zero.
            "attached_share": (attached_value / total) if total else None,
        },
        "attribute_origins": sorted(origins),
        # La ou les versions de regle qui ont produit ces valeurs. Vide quand
        # l'attribut est porte : il n'y a pas de regle a nommer, et inventer une
        # cle vide se lirait comme une regle absente.
        "rule_set_version_ids": sorted(rule_versions),
    }


def cross_meta(cross: Mapping[str, Any]) -> dict[str, Any]:
    """Le `meta` d'une reponse de croisement : le chemin et la version lue."""
    meta: dict[str, Any] = {
        "analytical_path": dict(ANALYTICAL_PATH_ENTITY_CROSS),
        "attribute": cross.get("attribute"),
        "attribute_origins": list(cross.get("attribute_origins") or []),
        "coverage": dict(cross.get("coverage") or {}),
        "unattached": dict(cross.get("unattached") or {}),
    }
    versions = list(cross.get("rule_set_version_ids") or [])
    if versions:
        meta["rule_set_version_ids"] = versions
    return meta


def assert_attribute_is_published(
    conn,
    *,
    project_id: str,
    object_kind: str,
    attribute: str,
) -> dict[str, Any]:
    """Refuser, en nommant l'autorite qui manque, plutot que rendre un cadre vide.

    Deux autorites peuvent porter un attribut, et il suffit d'UNE :

      * un attribut PORTE, declare par les versions de noeud du registre ;
      * une classification DERIVEE, produite par une version de regle PUBLIEE
        (68.6).

    Aucune des deux, et la question n'a pas de reponse a donner -- ce qui n'est
    pas la meme chose qu'une reponse vide. Rend la description de l'autorite
    trouvee, pour que l'appelant puisse la citer.
    """
    project_id = str(project_id or "").strip()
    object_kind = str(object_kind or "").strip()
    attribute = str(attribute or "").strip()
    if not (project_id and object_kind and attribute):
        raise CrossReadRefused(
            "cross_request_incomplete",
            "a cross names a Project, an entity type and an attribute",
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
              FROM app.master_data_derived_attributes d
              JOIN app.master_data_registries r
                ON r.id = d.registry_id
              JOIN app.governance_rule_sets g
                ON g.id = d.rule_set_id
             WHERE r.project_id = %s AND r.object_kind = %s AND d.attribute = %s
               AND g.current_version_id = d.rule_set_version_id
            """,
            (project_id, object_kind, attribute),
        )
        derived = int(cur.fetchone()[0])
        cur.execute(
            """
            -- LA PORTEE VIENT DU REGISTRE. `create_node_version` insere
            -- `project_id` a NULL deliberement (une version de noeud est scopee
            -- par son registre) : filtrer sur la colonne de la version ne
            -- rendrait jamais rien, et le refus serait toujours prononce.
            SELECT COUNT(*)
              FROM app.master_data_object_versions v
              JOIN app.master_data_registries r
                ON r.id = v.registry_id
             WHERE r.project_id = %s AND r.object_kind = %s
               AND v.status <> 'draft'
               AND v.payload -> 'attributes' ? %s
            """,
            (project_id, object_kind, attribute),
        )
        carried = int(cur.fetchone()[0])

    if derived:
        return {"origin": ORIGIN_DERIVED, "rows": derived}
    if carried:
        return {"origin": ORIGIN_CARRIED, "rows": carried}
    raise CrossReadRefused(
        "cross_attribute_not_published",
        f"nothing publishes {attribute!r} for {object_kind!r} in this Project: "
        "no governed attribute carries it and no published rule set derives it. "
        "Declare it on the entity type, or publish the rule that derives it.",
    )


def assert_cross_has_coverage(
    conn,
    *,
    project_id: str,
    object_kind: str,
) -> dict[str, Any]:
    """Refuser un croisement qu'AUCUNE ligne ne peut porter (story 69.4, AC2).

    UN CADRE VIDE N'EST PAS UNE REPONSE. Un croisement demande sur un type dont
    pas une seule occurrence n'est rattachee rendrait un tableau ou toute la
    valeur est dans le groupe << non rattache >> -- techniquement honnete, et
    illisible : le lecteur conclurait que la donnee est absente alors que la
    verite est que rien ne la RELIE encore. Les deux appellent des gestes
    opposes (importer des donnees / declarer la liaison).

    LA COUVERTURE EST CELLE DE 68.3, PAS UN SECOND COMPTE. Le verdict par
    occurrence est deja la mesure de ce qui est rattache ; en recalculer une
    ici donnerait un jour deux fractions differentes pour une seule question.

    `unavailable` N'EST PAS ZERO. Un magasin de verdicts illisible se dit ;
    refuser sur cette base accuserait le Projet d'un defaut qui est le notre.
    """
    from core.entity_key_matching import matching_coverage  # noqa: PLC0415

    object_kind = str(object_kind or "").strip()
    report = matching_coverage(conn, project_id=project_id)
    if report.get("state") == "unavailable":
        raise CrossReadRefused(
            "cross_coverage_unavailable",
            "the matching coverage could not be read, so whether this cross is "
            "supported is unknown. This is not a coverage of zero -- retry.",
        )
    rows = [
        row
        for row in (report.get("rows") or [])
        if not object_kind or str(row.get("object_kind")) == object_kind
    ]
    bound = sum(int(row.get("bound") or 0) for row in rows)
    eligible = sum(int(row.get("eligible") or 0) for row in rows)
    if bound == 0:
        raise CrossReadRefused(
            "cross_no_coverage",
            f"no occurrence of {object_kind!r} is attached to a governed entity in "
            f"this Project ({eligible} occurrence(s) seen, none resolved), so a "
            "cross would put every figure in the unattached group. Declare the "
            "key binding on the mapping, or record the aliases that name these "
            "entities.",
        )
    return {"bound": bound, "eligible": eligible, "rows": rows}


__all__ = [
    "ANALYTICAL_PATH_ENTITY_CROSS",
    "CROSS_RELATION",
    "ORIGIN_CARRIED",
    "ORIGIN_DERIVED",
    "ORIGIN_UNATTACHED",
    "UNATTACHED_LABEL",
    "CrossReadRefused",
    "assert_attribute_is_published",
    "assert_cross_has_coverage",
    "compose_cross",
    "cross_meta",
]
