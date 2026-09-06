"""Les remarques sur une Skill ou une Knowledge -- une file, pas un journal.

POURQUOI. Jean, 2026-08-03 : « une base a cote pour les feedback, qui permet
au-dela des erreurs de remonter directement sur le skill s'il y a des problemes
ou des ameliorations. Exemple : cette limite ou contrainte n'existe plus, faut la
retirer. »

Le crochet EXISTAIT et ne menait nulle part.
`POST /api/context/nodes/{id}/request-review` acceptait `{node_type, note}`,
ecrivait une ligne `context.review_requested` dans le journal d'audit, et rendait
`201`. Aucune table, aucun lecteur : `review_requested` n'apparaissait nulle part
hors du handler. Un journal d'audit est append-only et scelle -- ce n'est pas une
file de travail, et une remarque qui y tombe n'en ressort pas.

TROIS CHOSES QUE CETTE TABLE EXIGE, ET CHACUNE VIENT D'UNE MESURE :

  * LA VERSION du noeud. « Cette contrainte n'existe plus » ne veut rien dire si
    l'on ignore de quelle version on parle -- une Skill versionnee dont la
    remarque flotte est une remarque qu'on ne peut ni verifier ni clore.
  * L'ORIGINE -- `human` ou `agent`. `context_seed` refuse deja d'ecraser ce dont
    la derniere version n'est pas `system:seed`, et `link_origin` distingue
    `direct` de `derived` depuis la migration 202. Une remarque de machine
    confondue avec une remarque humaine vaut moins que rien.
  * L'ETAT. Une remarque sans issue est un vœu ; `open` / `accepted` / `declined`
    en fait un travail qu'on peut finir.

CE QU'ELLE NE FAIT PAS : notifier. Le handler d'origine le disait deja et avait
raison de le dire -- ne rien promettre qu'on ne livre pas. Elle rend la remarque
RELISIBLE, ce qui est l'autre moitie, et la moitie qui manquait.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ulid import ULID

logger = logging.getLogger(__name__)

ORIGINS = ("human", "agent")

# ---------------------------------------------------------------------------
# L ETAT D UNE REMARQUE -- un vocabulaire, un proprietaire
# ---------------------------------------------------------------------------
#
# `STATUSES` existait et personne ne s en servait : `'open'` etait retape a la
# main dans TROIS requetes de ce fichier et le couple resolvable dans une
# quatrieme. Ce n est pas un detail de style. Le mot vit AUSSI dans le CHECK de
# la migration 215 et dans l index partiel `uq_context_review_request_open` ; un
# quatrieme etat ajoute la-bas et le predicat de `list_open` continue de rendre
# une file qui ne dit plus ce qu elle croit dire, sans que rien ne rougisse.
#
# Le modele est `core.candidate_fate` (story 54.2) : le vocabulaire est declare
# une fois, importe par ses lecteurs, et une garde interdit qu il se retape
# ailleurs -- `tests/conformance/test_review_status_has_one_owner.py`.

STATUS_OPEN = "open"
STATUS_ACCEPTED = "accepted"
STATUS_DECLINED = "declined"

#: Tout etat qu une remarque peut porter. Ensemble CLOS : un quatrieme se declare
#: ici, ce qui force la discussion au bon moment -- et la migration qui elargit
#: le CHECK doit passer par cette ligne.
STATUSES: tuple[str, ...] = (STATUS_OPEN, STATUS_ACCEPTED, STATUS_DECLINED)

#: Les issues qu un humain peut PRONONCER. `open` n en est pas une : rouvrir une
#: remarque close n est pas un verdict, et `resolve_request` le refuse.
RESOLUTION_STATUSES: tuple[str, ...] = (STATUS_ACCEPTED, STATUS_DECLINED)


def is_resolution(status: str) -> bool:
    """Est-ce que *status* clot une remarque ? Le predicat, pas une liste retapee."""
    return status in RESOLUTION_STATUSES


def open_status_predicate(column: str = "status") -> str:
    """Le fragment SQL << cette remarque est encore ouverte >>, ecrit une fois.

    Rendu comme un LITTERAL et non comme un parametre : ces trois requetes le
    portent dans des clauses composees a la main, et un `%s` de plus decalerait
    l ordre des parametres de chacune. La valeur ne vient d aucune entree --
    c est une constante de ce module -- donc il n y a pas d injection a craindre,
    et l assertion ci-dessous le tient vrai.
    """
    assert STATUS_OPEN.isalpha(), "the open status must stay a bare identifier"
    return f"{column} = '{STATUS_OPEN}'"

_COLUMNS = (
    "id", "org_id", "project_id", "node_type", "node_id", "node_version",
    "note", "requested_by", "origin", "status", "resolved_by", "resolved_at",
    "created_at", "proposed_change", "applied_ref",
)


class ReviewRequestError(ValueError):
    """Une remarque qu'on ne peut pas enregistrer telle quelle."""


class ReviewRequestAlreadyResolvedError(ReviewRequestError):
    """Une remarque deja close : un second verdict est un conflit (409), pas
    une absence (404) -- l'id existe, c'est son etat qui refuse."""


def request_review(
    conn: Any,
    *,
    org_id: str,
    project_id: str | None,
    node_type: str,
    node_id: str,
    node_version: int,
    note: str,
    requested_by: str,
    origin: str = "human",
    proposed_change: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deposer une remarque sur un noeud du Hub, a une version precise.

    `proposed_change` est FACULTATIF : une remarque qui ne fait que dire reste
    une remarque, et c'est le cas courant. Quand elle porte une charge, son
    `kind` doit etre connu -- un `kind` inconnu est refuse ICI plutot qu'a
    l'acceptation, pour qu'une file ne se remplisse pas de choses que personne
    ne saura appliquer.
    """
    if node_type not in ("topic", "procedure"):
        raise ReviewRequestError("node_type must be 'topic' or 'procedure'")
    if origin not in ORIGINS:
        raise ReviewRequestError("origin must be 'human' or 'agent'")
    if not isinstance(node_version, int) or node_version < 1:
        raise ReviewRequestError("node_version must be a positive integer")
    clean_note = (note or "").strip()
    if not clean_note:
        raise ReviewRequestError("note cannot be empty")
    if len(clean_note) > 4000:
        raise ReviewRequestError("note is too long (maximum 4000 characters)")
    if proposed_change is not None:
        if not isinstance(proposed_change, dict):
            raise ReviewRequestError("proposed_change must be an attribute mapping")
        kind = proposed_change.get("kind")
        if kind not in PROPOSAL_KINDS:
            raise ReviewRequestError(
                "proposed_change.kind must be one of " + ", ".join(PROPOSAL_KINDS)
            )

    with conn.cursor() as cur:
        # Une remarque identique, sur la meme version, par le meme auteur, est un
        # DOUBLON et non un second signal : l'index partiel la refuse, et on rend
        # celle qui existe deja plutot que de lever.
        cur.execute(
            f"""
            INSERT INTO app.context_review_requests
                (id, org_id, project_id, node_type, node_id, node_version,
                 note, requested_by, origin, proposed_change)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING {", ".join(_COLUMNS)}
            """,
            (f"crr_{ULID()}", org_id, project_id, node_type, node_id,
             node_version, clean_note, requested_by, origin,
             json.dumps(proposed_change) if proposed_change else None),
        )
        row = cur.fetchone()
        if row is None:
            # LE REPLI EST SCOPE AU LOCATAIRE, ET C'EST LA MOITIE QUI MANQUAIT.
            # Sans `org_id`, ce SELECT rendait la remarque d'une AUTRE
            # organisation des que la cle se repetait -- ce qui arrive par
            # construction sur un noeud a portee plateforme, ou `requested_by`
            # vaut la constante globale AGENT_AUTHOR. Le predicat doit refleter
            # exactement la cle de `uq_context_review_request_open` (migration
            # 215), COALESCE compris : un repli plus large rendrait une ligne
            # etrangere, un repli plus etroit ne trouverait pas celle que
            # l'INSERT vient de refuser et on leverait sur un doublon legitime.
            cur.execute(
                f"""
                SELECT {", ".join(_COLUMNS)} FROM app.context_review_requests
                WHERE org_id = %s AND COALESCE(project_id, '') = COALESCE(%s, '')
                  AND node_type = %s AND node_id = %s AND node_version = %s
                  AND requested_by = %s AND md5(note) = md5(%s)
                  AND {open_status_predicate()}
                """,
                (org_id, project_id, node_type, node_id, node_version,
                 requested_by, clean_note),
            )
            row = cur.fetchone()
        return dict(zip(_COLUMNS, row, strict=False))


def list_open(
    conn: Any, *, org_id: str, project_id: str | None, node_id: str | None = None
) -> list[dict[str, Any]]:
    """La file : ce qui reste a trancher, le plus recent d'abord."""
    clauses = ["org_id = %s", open_status_predicate()]
    params: list[Any] = [org_id]
    if project_id is not None:
        clauses.append("(project_id IS NULL OR project_id = %s)")
        params.append(project_id)
    if node_id:
        clauses.append("node_id = %s")
        params.append(node_id)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM app.context_review_requests "
            f"WHERE {' AND '.join(clauses)} ORDER BY created_at DESC",
            params,
        )
        return [dict(zip(_COLUMNS, row, strict=False)) for row in cur.fetchall()]


#: Ce qu'une remarque peut PROPOSER de faire, et pas davantage. Un `kind`
#: inconnu est refuse a l'acceptation plutot qu'ignore : appliquer en silence ce
#: qu'on ne comprend pas est la seule facon de rendre une file dangereuse.
PROPOSAL_KINDS = ("business_link",)


def _apply_business_link(
    conn: Any, request: dict[str, Any], change: dict[str, Any], actor: str
) -> str:
    """Poser le lien qu'une remarque proposait, marque `derived`.

    POURQUOI PAS LE RAIL DE PUBLICATION GOUVERNEE. `governed_publication` fait
    porter a une confirmation un secret OPAQUE cache du modele, avec protection
    de rejeu -- parce qu'une publication de mapping AVANCE UN POINTEUR : elle
    change ce que les donnees veulent dire, et revenir en arriere demande une
    operation dediee.

    Un lien de taxonomie n'a rien de tout cela : reversible par un appel
    (`DELETE /api/context/business-links/{id}`), il ne deplace aucun pointeur.
    Lui imposer la meme ceremonie serait recopier le mecanisme le plus lourd du
    depot parce qu'il existe -- et une ceremonie qu'on ne peut pas justifier
    finit contournee.

    CE QUI RESTE, ET QUI EST L'ESSENTIEL : un humain accepte, et le lien nait
    `derived` -- donc distinguable pour toujours d'une decision humaine.
    """
    from core.business_taxonomy import create_link  # noqa: PLC0415

    row = create_link(
        conn,
        org_id=request["org_id"],
        project_id=request["project_id"],
        taxonomy_type=change["taxonomy_type"],
        taxonomy_id=change["taxonomy_id"],
        target_type=request["node_type"],
        target_id=request["node_id"],
        relation_type=change["relation_type"],
        actor=actor,
        reason=f"accepted review request {request['id']}: {request['note'][:160]}",
        link_origin="derived",
    )
    return row["id"]


_APPLIERS = {"business_link": _apply_business_link}


def resolve_request(
    conn: Any, *, request_id: str, org_id: str, status: str, resolved_by: str
) -> dict[str, Any] | None:
    """Clore une remarque. `accepted` ou `declined` -- jamais un silence."""
    if not is_resolution(status):
        raise ReviewRequestError(
            "status must be " + " or ".join(f"'{value}'" for value in RESOLUTION_STATUSES)
        )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE app.context_review_requests
               SET status = %s, resolved_by = %s, resolved_at = now()
             WHERE id = %s AND org_id = %s AND {open_status_predicate()}
            RETURNING {", ".join(_COLUMNS)}
            """,
            (status, resolved_by, request_id, org_id),
        )
        row = cur.fetchone()
    if row is None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM app.context_review_requests WHERE id = %s AND org_id = %s",
                (request_id, org_id),
            )
            existing = cur.fetchone()
            if existing is not None:
                raise ReviewRequestAlreadyResolvedError(
                    f"Review request '{request_id}' has already been resolved "
                    f"(current status: '{existing[0]}')"
                )
        return None
    request = dict(zip(_COLUMNS, row, strict=False))

    # ACCEPTER ET APPLIQUER NE SONT PAS LE MEME FAIT. Un refus ne touche rien, et
    # une acceptation sans charge non plus -- c'est le cas courant. Quand il y a
    # une charge, ce qu'elle a produit est ecrit dans `applied_ref` : sans lui on
    # ne distinguerait pas « un humain a dit oui » de « le changement est en base ».
    change = request.get("proposed_change")
    if status != STATUS_ACCEPTED or not change:
        return request

    applier = _APPLIERS.get(change.get("kind"))
    if applier is None:
        raise ReviewRequestError(
            f"proposed_change.kind '{change.get('kind')}' has no applier"
        )
    applied_ref = applier(conn, request, change, resolved_by)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.context_review_requests SET applied_ref = %s WHERE id = %s",
            (applied_ref, request_id),
        )
    request["applied_ref"] = applied_ref
    return request


# ---------------------------------------------------------------------------
# La moitie machine du rail -- Story 45.8
# ---------------------------------------------------------------------------
#
# CE QUI MANQUAIT, MESURE LE 2026-08-05 :
#
#     grep 'origin="agent"' server --include=*.py (hors tests)
#       -> 1 resultat, et c'etait UN COMMENTAIRE
#
# Le schema acceptait `agent`, `request_review` prenait le parametre, la
# resolution humaine ecrivait le lien `derived`, et la console lisait les rejets
# recurrents. Il manquait le PRODUCTEUR : la file se remplissait par relecture et
# jamais par l'usage.
#
# DEUX DECLENCHEURS, ET ILS NE PROPOSENT PAS LA MEME CHOSE -- c'est la decision
# qui porte tout le reste :
#
#   * `missing_path` (le verdict des evals) nomme LES DEUX BOUTS : la route
#     attendue porte son domaine et sa cible. La proposition peut donc porter la
#     charge -- le lien exact a poser.
#   * un REJET RECURRENT ne nomme qu'un bout. Le signal dit « ce candidat a ete
#     atteint puis ecarte N fois pour des requetes comme X » ; X est du texte
#     libre, pas une taxonomie. En deduire une cible serait deviner, et une
#     proposition qui ne peut pas dire ce qui l'a produite est une devinette avec
#     de meilleures manieres. Elle part donc SANS charge : elle met l'objet dans
#     la file humaine, la ou la cible se choisit.
#
# L'auteur machine est NOMME. Une remarque de machine confondue avec celle d'un
# collegue vaut moins que rien -- c'est ce que `origin` existe pour empecher, et
# `requested_by` doit le dire aussi pour qui lit une ligne seule.

#: L'auteur des remarques deposees par le produit lui-meme.
AGENT_AUTHOR = "agent:toorow"


def propose_missing_link(
    conn: Any,
    *,
    org_id: str,
    project_id: str | None,
    node_type: str,
    node_id: str,
    node_version: int,
    taxonomy_type: str,
    taxonomy_id: str,
    relation_type: str,
    evidence: str,
) -> dict[str, Any]:
    """Deposer une proposition de lien, avec CE QUI L'A PRODUITE.

    IDEMPOTENTE PAR CONSTRUCTION, et pas par rattrapage d'erreur : la note est
    DERIVEE du fait observe, donc le meme fait produit la meme note, que l'index
    partiel reconnait comme un doublon. Un producteur qui daterait sa note
    fabriquerait une ligne par execution -- une file qu'on apprend a ignorer.
    """
    note = (
        f"Proposed by the platform: link this {node_type} to "
        f"{taxonomy_type} {taxonomy_id} ({relation_type}). Evidence: {evidence}"
    )
    return request_review(
        conn,
        org_id=org_id,
        project_id=project_id,
        node_type=node_type,
        node_id=node_id,
        node_version=node_version,
        note=note,
        requested_by=AGENT_AUTHOR,
        origin="agent",
        proposed_change={
            "kind": "business_link",
            "taxonomy_type": taxonomy_type,
            "taxonomy_id": taxonomy_id,
            "relation_type": relation_type,
        },
    )


def flag_recurring_rejection(
    conn: Any,
    *,
    org_id: str,
    project_id: str,
    node_type: str,
    node_id: str,
    node_version: int,
    reason: str,
    times: int,
    last_query: str | None,
) -> dict[str, Any]:
    """Signaler un candidat ecarte a repetition -- SANS proposer de cible.

    Le signal ne nomme qu'un bout. Ecrire une charge ici reviendrait a choisir un
    metier a la place de quelqu'un, sur la foi d'une chaine de caracteres tapee
    dans une recherche.

    LA NOTE NE PORTE PAS LE COMPTE. `times` monte a chaque recherche : l'inclure
    ferait une note differente a chaque passage, donc une ligne de plus a chaque
    passage. Le compte se lit ou il vit -- `recurrent_fates` -- et la remarque
    dit ce qui ne bouge pas : ce candidat est ecarte, pour cette raison.
    """
    # ⚠️ LA NOTE NE PORTE PAS NON PLUS LA REQUETE. Relecture adversariale du
    # 2026-08-05 : `md5(note)` fait partie de l'index unique partiel, donc une
    # requete differente -- le meme noeud ecarte dans un autre projet, sur
    # d'autres mots -- produisait une SECONDE ligne ouverte sur le meme noeud.
    # Le compte avait ete retire pour cette raison exacte ; ce champ-la est tout
    # aussi volatil. Ce qui ne bouge pas : ce noeud, cette raison.
    note = (
        f"Flagged by the platform: this {node_type} is reached and dropped "
        f"({reason}) for queries it is not linked to. It may be missing a "
        f"business link. The counts and the queries are in the recurring "
        f"rejections, where they belong."
    )
    return request_review(
        conn,
        org_id=org_id,
        project_id=project_id,
        node_type=node_type,
        node_id=node_id,
        node_version=node_version,
        note=note,
        requested_by=AGENT_AUTHOR,
        origin="agent",
    )


#: Les genres de candidats qui SONT des noeuds du Hub. `schema_doc` et
#: `target_field` sont ecartes a repetition comme les autres et n'ont pas de
#: remarque possible : la table n'accepte que `topic` et `procedure`.
_FLAGGABLE_KINDS = {"topic": "topic", "procedure": "procedure"}


#: Ou vit la version d'un noeud du Hub. ⚠️ ELLE N'EST PAS SUR LA TABLE DU NOEUD :
#: `app.context_topics` et `app.procedures` ne portent AUCUNE colonne
#: `version_number` -- le numero vit dans la table d'historique, et
#: `context_store.get_topic` / `get_procedure` le lisent deja par
#: `COALESCE(MAX(v.version_number), 1)`. Cette table dit la meme chose, en lot.
_VERSION_SOURCES = {
    "topic": ("app.context_topics", "app.context_topics_versions", "topic_id"),
    "procedure": ("app.procedures", "app.procedures_versions", "procedure_id"),
}


def _node_versions(
    conn: Any, *, node_type: str, node_ids: list[str]
) -> dict[str, tuple[str | None, int]]:
    """`{id: (project_id, version_number)}` pour TOUS les noeuds d'un coup.

    ⚠️ UNE INSTRUCTION, PAS UNE PAR NOEUD. Relecture adversariale du 2026-08-05 :
    ce producteur tourne sur le chemin d'une RECHERCHE, et une marche peut
    franchir le seuil sur plusieurs candidats a la fois. Une lecture par candidat
    faisait 2N+1 allers-retours -- exactement le cout que `record_fates` a mesure
    puis refuse a cote (8.29 ms en boucle contre 2.62 ms en une instruction).

    ⚠️ LA VERSION VIENT DE LA TABLE D'HISTORIQUE, ET C'EST LA REPARATION DU
    2026-08-10. Mesure contre le Postgres jetable :

        SELECT id, project_id, version_number FROM app.context_topics ...
        -> UndefinedColumn: la colonne « version_number » n'existe pas

    Les deux tables de noeuds n'ont jamais porte cette colonne. Le producteur
    livre par 45.8 levait donc a CHAQUE franchissement de seuil, pour les deux
    genres de noeud : zero remarque deposee depuis qu'il existe -- et l'echec,
    hors point de reprise, emportait au passage les sorts que `record_fates`
    venait d'ecrire dans la meme transaction.
    """
    if not node_ids:
        return {}
    source = _VERSION_SOURCES.get(node_type)
    if source is None:
        return {}
    node_table, version_table, version_key = source
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT n.id, n.project_id, COALESCE(MAX(v.version_number), 1)
              FROM {node_table} n
              LEFT JOIN {version_table} v ON n.id = v.{version_key}
             WHERE n.id = ANY(%s) AND n.status = 'active'
             GROUP BY n.id, n.project_id
            """,
            (node_ids,),
        )
        return {row[0]: (row[1], row[2]) for row in cur.fetchall()}


def flag_recurring_rejections(
    conn: Any, *, project_id: str, crossed: list[dict[str, Any]]
) -> list[str]:
    """Deposer une remarque pour chaque candidat qui vient d'atteindre le seuil.

    APPELEE DEPUIS LA MARCHE, et c'est deliberé : le seuil se franchit pendant
    une recherche, pas pendant une relecture. Un bouton aurait fait dependre la
    file de quelqu'un qui pense a cliquer, et un balayage nocturne aurait ajoute
    un planificateur pour un fait deja sous la main.

    NE LEVE JAMAIS. Une remarque qu'on ne peut pas deposer ne doit pas emporter
    la recherche qui l'a revelee -- observer ne casse jamais l'observe.
    """
    filed: list[str] = []
    if not crossed:
        return filed
    # ⚠️ SOUS POINT DE REPRISE, et « ne leve jamais » ne suffisait pas.
    # Relecture adversariale du 2026-08-05 : en psycopg 3 une instruction
    # refusee AVORTE la transaction entiere. Sans ce savepoint, un echec ici
    # empoisonnait la transaction de la RECHERCHE -- et le `commit` de
    # `core/main.py` devenait un ROLLBACK silencieux qui emportait tous les sorts
    # que `record_fates` venait d'ecrire. Le module d'a cote documente ce piege
    # depuis le 2026-08-04 ; cette ecriture-ci l'avait oublie.
    try:
        # ⚠️ LE POINT DE REPRISE COUVRE TOUT LE PRODUCTEUR, ET IL N'EN COUVRAIT
        # QU'UNE INSTRUCTION. Le commentaire du 2026-08-05 jurait deja ce que
        # ces lignes ne faisaient pas : `with conn.transaction()` ne portait que
        # le `SELECT org_id`, et les lectures de version comme les depots de
        # remarque tournaient DEHORS. Mesure du 2026-08-10 contre le Postgres
        # jetable : la troisieme marche levait `UndefinedColumn`, la transaction
        # de la RECHERCHE etait avortee, et le `commit` de l'appelant devenait un
        # rollback silencieux qui emportait les cinq sorts que `record_fates`
        # venait d'ecrire (`test_live_a_second_walk_counts_rather_than_piles_up`
        # rendait 0 ligne la ou il en attend 5). Une docstring qui jure une garde
        # que le code ne pose pas est pire que pas de garde du tout.
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT org_id FROM app.projects WHERE id = %s", (project_id,)
                )
                row = cur.fetchone()
            if row is None:
                return filed
            org_id = row[0]
            # Les versions de TOUS les candidats franchis, par genre, en deux
            # instructions au plus -- pas une par candidat.
            by_kind: dict[str, list[str]] = {}
            for fate in crossed:
                node_type = _FLAGGABLE_KINDS.get(fate.get("candidate_kind") or "")
                if node_type is not None:
                    by_kind.setdefault(node_type, []).append(fate["candidate_id"])
            versions = {
                node_type: _node_versions(conn, node_type=node_type, node_ids=ids)
                for node_type, ids in by_kind.items()
            }
            for fate in crossed:
                node_type = _FLAGGABLE_KINDS.get(fate.get("candidate_kind") or "")
                if node_type is None:
                    continue
                found = versions.get(node_type, {}).get(fate["candidate_id"])
                if found is None:
                    continue
                node_project_id, version_number = found
                request = flag_recurring_rejection(
                    conn,
                    org_id=org_id,
                    # Un noeud de PLATEFORME porte `project_id IS NULL` et
                    # concerne tout le monde : sa remarque aussi. La rattacher au
                    # projet qui l'a revelee la cacherait aux autres.
                    project_id=node_project_id,
                    node_type=node_type,
                    node_id=fate["candidate_id"],
                    node_version=version_number,
                    reason=fate.get("reason") or "",
                    times=fate.get("times") or 0,
                    last_query=fate.get("last_query"),
                )
                filed.append(request["id"])
    except Exception:  # noqa: BLE001 -- observer never breaks the observed
        logger.debug("flag_recurring_rejections: skipped", exc_info=True)
        # Un depot partiellement annule n'a rien depose : le point de reprise a
        # tout defait, donc annoncer des identifiants deposes serait un faux.
        return []
    return filed
