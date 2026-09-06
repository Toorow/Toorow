"""toorow -- Context retrieval for the agent surface (Story 11.5).

Data layer for the two CORE-owned MCP tools ``search_context`` and
``get_procedure`` (registered, un-namespaced, in ``core.main`` -- AD-2). This
module owns the retrieval + ranking logic; ``core.main`` owns only the thin
dual-channel envelope wrapping (AD-1).

Ranking algorithm (search_context) -- documented and test-proven
----------------------------------------------------------------
A single lexical + graph pass over the context corpus, scored in three tiers so
the agent always sees the most on-topic fragment first (Epic 11 Architecture
bullet: "titre > description > voisins de graphe"). Vector recall is a non-goal
for v1 (Epic 11 Non-goals); this is deterministic lexical rank + a graph hop.

  TIER_TITLE       (3.0) -- the query term appears in a topic TITLE, a procedure
                            NAME, or a schema_context RELATION name. The strongest
                            signal: the author titled the fragment for this term.
  TIER_DESCRIPTION (2.0) -- the query term appears in the BODY of a topic /
                            schema doc, or a procedure DESCRIPTION/body. The
                            fragment is about the term but is not titled for it.
  TIER_NEIGHBOR    (1.0) -- the fragment is a context_graph neighbour (one hop)
                            of a fragment that matched at TITLE or DESCRIPTION
                            tier, but did not itself match the query lexically.
                            Surfaced so a definition linked to a matched concept
                            is not lost, but always ranked below a direct match.

Results are sorted by (score DESC, then a stable secondary key: kind order
topic < procedure < schema_doc, then id) so ordering is deterministic and
assertable by value in tests. Ties never depend on DB row order.

Scoping (AD-5)
--------------
Every query is scoped to the caller's project: platform rows (``project_id IS
NULL`` for topics/procedures) are visible to everyone; a project sees ITS OWN
project rows plus platform rows, and NEVER another project's rows. schema_context
rows are always project-scoped (``project_id`` is NOT NULL) so they filter on
equality only. Current versions only: topics/procedures filter ``status =
'active'``; schema_context holds the current doc (history lives in
schema_context_versions and is never surfaced here).

Reporting the walk, including the branches not taken (Story 54.2)
-----------------------------------------------------------------
``search_context`` returns the survivors and nothing else, which is the right
answer for the MCP tool and the wrong one for a surface that shows the walk: a
reader cannot tell "it found this" from "this is all there was". ``search_context_walk``
runs the SAME pass and reports it in full -- every candidate with its rank, its
score, its tier, whether it was reached directly or by the graph hop, and its
fate (`core.candidate_fate`). ``search_context`` is a thin wrapper over it, so
there is ONE retrieval and ONE result set; a second search path would be a defect,
not a feature.

Three facts stay distinct, because collapsing them is the honesty problem the
story exists to solve:

* **reached and kept** -- ``selected``;
* **reached and dropped** -- ``rejected`` with ``below_cutoff``, the only reason a
  capped ranking can produce;
* **never reached** -- NOT enumerated, and the walk says so
  (``NOT_REACHED_ENUMERATED``). A node whose term appears nowhere in its title or
  body, or that sits two hops away, was never judged. Listing it as rejected
  would invent an examination.

And the walk states what it IS (``retrieval_descriptor``): lexical matching, one
graph hop, capped at ``limit``, ``semantic_recall`` False. Vector recall is a
declared non-goal of epic 11, not an oversight -- so a surface reading this
payload cannot draw a semantic sweep that never happened.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ulid import ULID

from core import candidate_emission, candidate_fate

logger = logging.getLogger(__name__)

# Ranking tiers (documented above). Kept as module constants so tests assert on
# the exact ordering contract rather than magic numbers.
TIER_TITLE = 3.0
TIER_DESCRIPTION = 2.0
TIER_NEIGHBOR = 1.0

# Deterministic secondary sort within a score tier: topics first, then
# procedures, then schema docs, then id. Keeps ordering reproducible (AI-56:
# assertions on value, never on incidental DB row order).
_KIND_ORDER = {"topic": 0, "procedure": 1, "schema_doc": 2}

# Cap on the number of ranked hits carried in the detail channel. The summary is
# separately capped at <=30 lines by core.main (AD-1).
DEFAULT_LIMIT = 20

# --- What this walk IS (Story 54.2, AC5) -----------------------------------
# Named as fields, not as a footnote for a surface to add if it thinks of it.
# A subtree announcing "20 candidates evaluated" over a lexical one-hop retriever
# tells the reader a semantic sweep happened; these constants are what stops that.

#: How candidates are found: case-insensitive substring matching (SQL ILIKE) on
#: titles/names/relations and bodies. Not embeddings, not similarity.
RETRIEVAL_MODE = "lexical"

#: How far the walk goes into ``app.context_graph``: exactly one hop from a
#: direct lexical match. A node two hops away is never reached.
GRAPH_HOP_DEPTH = 1

#: Vector recall is a DECLARED NON-GOAL of epic 11 (module docstring above;
#: `epic-11-context-layer.md` Non-goals), not a gap this module hides.
SEMANTIC_RECALL = False

#: Whether the never-reached nodes are enumerated. They are not, and they cannot
#: be: enumerating them would mean walking the corpus the walk deliberately did
#: not walk. The field exists so a surface states "not judged" instead of
#: implying the walk was exhaustive.
NOT_REACHED_ENUMERATED = False

#: How the knowledge-tree crossing names itself on the text channel (T4).
WALK_SUBJECT = "Knowledge tree walk"


# --- Comment la requete est LUE -- Story 45.9 ------------------------------
#
# CE QUI ETAIT MESURE LE 2026-08-05 : la requete ENTIERE comparee comme UNE
# sous-chaine (`like = f"%{term}%"`). « attribution window » ne trouvait que ce
# qui contenait litteralement cette chaine : deux mots dans l'autre ordre, un
# pluriel, un accent -> rien.
#
# CE QUI A ETE ETUDIE PUIS REFUSE, avec le chiffre qui le refuse :
#
#   * `pg_trgm` + index GIN. Corpus de PRODUCTION mesure le 2026-08-05 :
#     1 procedure (15 Ko), 0 topic, 0 document de schema, 0 arete de graphe. Et
#     AUCUN index textuel n'existe -- un `ILIKE '%x%'` ne peut de toute facon pas
#     en utiliser un, donc chaque recherche balaye deja tout. Un index trigramme
#     optimiserait une table d'UNE ligne, et une extension est un engagement
#     d'exploitation permanent. A revoir quand le corpus existera, avec le
#     chiffre qui le justifiera.
#   * `to_tsvector` / `ts_rank` -- sans extension, et avec radicalisation. Mais
#     il REMPLACE trois tiers explicables par un score opaque, impose de choisir
#     une langue sur un corpus declare anglais dont les requetes ne le sont pas
#     forcement, et ne vaut sa complexite qu'avec un index -- que le point
#     precedent ecarte. C'est le chemin d'evolution, pas celui du jour.
#
# CE QUI EST FAIT : decouper, replier les accents, et DEUX PASSES sur UNE SEULE
# recuperation. Zero migration, zero extension, les trois tiers intacts -- et un
# resultat dont on peut toujours dire POURQUOI il est la.

#: Les accents replies des deux cotes de la comparaison. `unaccent` ferait mieux
#: et couterait une extension ; cette table couvre ce qu'un clavier francais
#: produit, et son cout est nul puisque la requete balaye deja tout.
_FOLD_FROM = "àáâãäåçèéêëìíîïñòóôõöùúûüýÿ"
_FOLD_TO = "aaaaaaceeeeiiiinooooouuuuyy"
_FOLD_TABLE = str.maketrans(_FOLD_FROM, _FOLD_TO)

#: Au-dela, la requete n'est plus une question. Borne le nombre de comparaisons
#: par ligne, et surtout le nombre de parametres SQL.
MAX_TOKENS = 8

#: Un mot d'une lettre correspond a presque tout : il elargit sans rien apporter.
MIN_TOKEN_LENGTH = 2

#: Les mots qui ne designent rien du corpus. Ils sont retires AVANT la borne, et
#: c'est la seule chose qui rend la borne utile : la relecture adversariale du
#: 2026-08-05 l'a mesure --
#:     tokenise("what is the attribution window for the paid search campaigns
#:              in France")
#:       -> ['what','is','the','attribution','window','for','paid','search']
#: MAX_TOKENS etait atteint par « what is the for », et `campaigns` et `france`
#: -- les deux mots qui portent la question -- tombaient. Une borne qui garde le
#: bruit et jette le signal est pire qu'une absence de borne.
STOP_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do",
    "does", "for", "from", "has", "have", "how", "in", "into", "is", "it", "its",
    "of", "on", "or", "our", "so", "than", "that", "the", "their", "them",
    "then", "there", "these", "they", "this", "to", "was", "were", "what",
    "when", "where", "which", "who", "why", "will", "with", "would", "you",
    "your",
    # Le francais tape dans la meme barre de recherche que l'anglais.
    "au", "aux", "avec", "ce", "ces", "dans", "de", "des", "du", "elle", "en",
    "est", "et", "il", "je", "la", "le", "les", "leur", "lui", "ma", "mais",
    "me", "mes", "mon", "ne", "nos", "notre", "nous", "ou", "par", "pas",
    "pour", "qu", "que", "qui", "sa", "se", "ses", "son", "sur", "ta", "te",
    "tes", "toi", "ton", "tu", "un", "une", "vos", "votre", "vous",
})


def fold(value: str | None) -> str:
    """La forme comparable d'un texte : minuscules, accents replies."""
    return (value or "").lower().translate(_FOLD_TABLE)


def tokenise(query: str | None) -> list[str]:
    """Les mots de la requete : replies, dedupes, dans l'ordre, bornes.

    L'ordre est conserve pour que ce que la marche RAPPORTE se lise comme ce qui
    a ete tape -- un descripteur qui reordonne les mots fait douter de ce qu'il
    decrit.

    LES MOTS VIDES SORTENT AVANT LA BORNE (`STOP_WORDS`), sauf s'il ne reste
    rien : une requete qui n'est faite QUE de mots vides (« what is it ») est
    toujours une requete, et la reduire a zero jeton rendrait « aucun contexte »
    la ou le corpus n'a simplement pas ete interroge sur un mot porteur.
    """
    words: list[str] = []
    for raw in re.split(r"[^0-9a-z]+", fold(query)):
        if len(raw) < MIN_TOKEN_LENGTH or raw in words:
            continue
        words.append(raw)
    carrying = [word for word in words if word not in STOP_WORDS]
    return (carrying or words)[:MAX_TOKENS]


#: Combien de lignes la clause SUR-ENSEMBLE peut rendre, PAR TABLE. Elle est
#: deliberement large -- au moins un mot dans au moins une colonne -- donc sur un
#: corpus qui grossit elle ramene tout ce qui contient « the » ou un mot commun.
#: Mesure du 2026-08-05 sur un corpus de 101 lignes : 101 atteintes, 20 gardees,
#: et 81 lignes ECRITES dans `app.context_candidate_fates` par requete de bruit.
#: `DEFAULT_LIMIT` ne bornait que `selected` (`ContextWalk.selected`), jamais la
#: recuperation.
SUPERSET_ROW_LIMIT = 200


def _any_token_sql(columns: tuple[str, ...], tokens: list[str]) -> str:
    """La clause : au moins un jeton dans au moins une colonne, accents replies.

    C'est deliberement le SUR-ENSEMBLE. La precision -- tous les mots ou non --
    se decide ensuite en Python, sur les MEMES lignes : deux requetes SQL
    feraient deux recuperations, et ce module tient qu'il n'y en a qu'une.
    """
    parts = [
        f"translate(lower({column}), %s, %s) LIKE %s"
        for column in columns
        for _ in tokens
    ]
    return " OR ".join(parts) if parts else "FALSE"


def _token_params(columns: tuple[str, ...], tokens: list[str]) -> list[str]:
    """Les parametres de `_any_token_sql`, dans le meme ordre."""
    return [
        value
        for _column in columns
        for token in tokens
        for value in (_FOLD_FROM, _FOLD_TO, f"%{token}%")
    ]


def _matched(tokens: list[str], *values: str | None) -> set[str]:
    """Les jetons presents dans au moins une des valeurs donnees."""
    haystack = " ".join(fold(value) for value in values)
    return {token for token in tokens if token in haystack}


class ContextHit:
    """One ranked context fragment. Plain data holder (JSON-serialised by main)."""

    __slots__ = (
        "id", "kind", "title", "snippet", "score", "tier", "project_id", "matched",
        "widened", "matched_tokens",
    )

    def __init__(
        self,
        *,
        id: str,
        kind: str,
        title: str,
        snippet: str,
        score: float,
        tier: str,
        project_id: str | None,
        matched: bool,
    ) -> None:
        self.id = id
        self.kind = kind
        self.title = title
        self.snippet = snippet
        self.score = score
        self.tier = tier
        self.project_id = project_id
        self.matched = matched  # True = direct lexical hit; False = graph neighbour
        #: Story 45.9 -- produit par la passe ELARGIE (les mots ne sont pas
        #: tous la). Porte par la marche et par le resume, JAMAIS par
        #: `as_dict` : ce dictionnaire est le contrat de l'outil MCP et son
        #: jeu de cles est epingle.
        self.widened = False
        self.matched_tokens: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "snippet": self.snippet,
            "score": self.score,
            "tier": self.tier,
            "project_id": self.project_id,
        }


class WalkCandidate:
    """One candidate of the walk, with its rank and what became of it (Story 54.2).

    Wraps a ``ContextHit`` rather than copying its fields: the hit stays the one
    description of the node, and the candidate adds only what the walk decided.

    ``as_dict`` deliberately omits ``snippet``. A path is *trace evidence of which
    governed nodes were used* (`context-hub.md:40-50`), not a second content
    store -- the candidate points at a node, it does not re-serve its body.
    """

    __slots__ = ("hit", "rank", "fate", "reason")

    def __init__(self, *, hit: "ContextHit", rank: int, fate: str, reason: str | None) -> None:
        candidate_fate.validate_fate(fate, reason)
        self.hit = hit
        self.rank = rank  # 1-based position in the ranking, dropped ones included
        self.fate = fate
        self.reason = reason

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.hit.id,
            "kind": self.hit.kind,
            "title": self.hit.title,
            "score": self.hit.score,
            "tier": self.hit.tier,
            "matched": self.hit.matched,
            "project_id": self.hit.project_id,
            "rank": self.rank,
            "fate": self.fate,
            "reason": self.reason,
        }


class ContextWalk:
    """The full report of ONE retrieval pass: survivors, dropped, and what it was.

    ``ranked`` holds every candidate the pass actually reached, already sorted by
    the Story 11.5 ranking -- this class re-ranks nothing. ``limit`` is where the
    cap fell. Everything else is derived from those two, so the survivors a caller
    sees and the candidates a surface draws can never disagree.
    """

    __slots__ = (
        "query", "project_id", "limit", "ranked", "refused", "tokens", "widened_mode",
        "scan_truncated",
    )

    def __init__(
        self,
        *,
        query: str,
        project_id: str | None,
        limit: int,
        ranked: list["ContextHit"],
        refused: list["ContextHit"] | None = None,
        tokens: list[str] | None = None,
        widened_mode: bool = False,
        scan_truncated: bool = False,
    ) -> None:
        self.query = query
        self.project_id = project_id
        self.limit = limit
        self.ranked = ranked
        #: Story 45.5 -- reached, then dropped because the candidate itself
        #: DECLARED this is not its question. They are kept OUT of `ranked` so
        #: they never take a slot under the cap, and reported here so the drop is
        #: explainable: a Skill that vanishes without a word cannot be debugged
        #: by the person who wrote it.
        self.refused = refused or []
        #: Les mots compares, dans l'ordre tape, et sous quelle regle.
        self.tokens = tokens or []
        self.widened_mode = widened_mode
        #: La clause SUR-ENSEMBLE a-t-elle touche sa borne ? Un balayage borne
        #: qui ne le dit pas se lit comme un balayage complet -- et c'est le
        #: meme mensonge que « aucun contexte » sur un magasin indisponible.
        self.scan_truncated = scan_truncated

    @property
    def selected(self) -> list["ContextHit"]:
        """Candidates retained by this retriever, not a claim of model use."""
        return self.ranked[: self.limit]

    @property
    def dropped(self) -> list["ContextHit"]:
        """Reached, scored, and lost to the cap."""
        return self.ranked[self.limit :]

    @property
    def candidates(self) -> list[WalkCandidate]:
        """Every reached candidate, in rank order, carrying its fate."""
        out: list[WalkCandidate] = []
        for index, hit in enumerate(self.ranked):
            kept = index < self.limit
            out.append(
                WalkCandidate(
                    hit=hit,
                    rank=index + 1,
                    fate=(
                        candidate_fate.FATE_SELECTED
                        if kept
                        else candidate_fate.FATE_REJECTED
                    ),
                    # A capped ranking drops candidates for one cause: the cap.
                    # Naming any other reason here would be an invention.
                    reason=None if kept else candidate_fate.REASON_BELOW_CUTOFF,
                )
            )
        # Les refus declares viennent APRES les classes : ils n'ont pas de rang
        # dans le classement puisqu'ils en sont sortis, et les numeroter a la
        # suite dit exactement ca -- atteints, puis ecartes hors competition.
        for offset, hit in enumerate(self.refused):
            out.append(
                WalkCandidate(
                    hit=hit,
                    rank=len(self.ranked) + offset + 1,
                    fate=candidate_fate.FATE_REJECTED,
                    reason=candidate_fate.REASON_ANTI_TRIGGER,
                )
            )
        return out

    def retrieval_descriptor(self) -> dict[str, Any]:
        """What produced these candidates (AC5) -- a field, never a footnote."""
        return {
            "mode": RETRIEVAL_MODE,
            "graph_hop_depth": GRAPH_HOP_DEPTH,
            "semantic_recall": SEMANTIC_RECALL,
            "limit": self.limit,
            "tiers": {
                "title": TIER_TITLE,
                "description": TIER_DESCRIPTION,
                "neighbor": TIER_NEIGHBOR,
            },
            # The third state, stated rather than implied: nodes the walk never
            # touched exist and are NOT listed here, so nothing below may be read
            # as an exhaustive examination of the corpus.
            "not_reached_enumerated": NOT_REACHED_ENUMERATED,
            # Story 45.9 : CE QUI A ETE COMPARE, et sous quelle regle. Un
            # descripteur qui tait le decoupage laisse croire que la phrase
            # entiere a ete cherchee -- ce qui etait vrai jusqu'a cette story.
            "tokens": list(self.tokens),
            "match_mode": "any_token" if self.widened_mode else "all_tokens",
            "widened_count": sum(1 for hit in self.ranked if hit.widened),
            # La recuperation est BORNEE par table, et le dit. `scan_truncated`
            # vrai veut dire « il y en avait davantage » -- jamais « c'est
            # tout ».
            "scan_row_limit": SUPERSET_ROW_LIMIT,
            "scan_truncated": self.scan_truncated,
            "reached_count": len(self.ranked) + len(self.refused),
            "selected_count": len(self.selected),
            "rejected_count": len(self.dropped) + len(self.refused),
            # Compte a part : un refus DECLARE n'est pas un candidat perdu au
            # plafond, et les additionner ferait lire le classement comme trop
            # serre alors que c'est l'auteur qui a ecarte sa propre Skill.
            "refused_count": len(self.refused),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "project_id": self.project_id,
            "retrieval": self.retrieval_descriptor(),
            "candidates": [c.as_dict() for c in self.candidates],
        }


# --- The crossings this walk is seen as (Story 54.2, T3) -------------------
# `ai_path_recorder.level_of` separates PROCEDURE from CONTEXT by the OBJECT TYPE
# reached, not by the step kind -- they share one. So the walk reports itself as
# at most two crossings, in the order the reading grid puts them.

_PROCEDURE_KIND = "procedure"


def walk_crossings(walk: "ContextWalk") -> list[dict[str, Any]]:
    """Split ONE walk into the crossings a reader sees, counts scoped to each.

    Two at most: the procedures it reached, then the rest of the context corpus.
    The counts are recomputed per crossing rather than repeated from the whole
    walk -- a reader who adds two crossings up must not count the same candidate
    twice, and the mode / depth / cap stay global because they are properties of
    the single pass that produced both.
    """
    procedures = [c for c in walk.candidates if c.hit.kind == _PROCEDURE_KIND]
    others = [c for c in walk.candidates if c.hit.kind != _PROCEDURE_KIND]
    base = walk.retrieval_descriptor()

    crossings: list[dict[str, Any]] = []
    for default_type, group in (
        (candidate_emission.OBJECT_TYPE_PROCEDURE, procedures),
        (None, others),
    ):
        if not group:
            continue
        kept = [c for c in group if c.fate == candidate_fate.FATE_SELECTED]
        descriptor = dict(base)
        descriptor["reached_count"] = len(group)
        descriptor["selected_count"] = len(kept)
        descriptor["rejected_count"] = len(group) - len(kept)
        head = kept[0] if kept else group[0]
        crossings.append(
            {
                "owner_object_type": default_type or head.hit.kind,
                # The node the walk actually USED. None when the whole crossing
                # was dropped: an owner invented for a crossing that kept nothing
                # would be a graph object nobody reached.
                "owner_object_id": kept[0].hit.id if kept else None,
                "descriptor": descriptor,
                "candidates": [c.as_dict() for c in group],
            }
        )
    if not crossings:
        # A walk that reached nothing is still a walk that ran, and it must stay
        # VISIBLE: an absent rung is indistinguishable from "it never looked".
        # The owner fields stay null rather than naming a node nobody reached --
        # the same refusal `ai_path_recorder` already applies to a step that
        # touched no governed object.
        crossings.append(
            {
                "owner_object_type": None,
                "owner_object_id": None,
                "descriptor": base,
                "candidates": [],
            }
        )
    return crossings


def emit_walk(walk: "ContextWalk") -> int:
    """Post this walk's crossings on the Story 54.1 seam. Returns how many went.

    Zero is the normal answer: without a ``progressToken`` there is no channel,
    nothing is composed and nothing is sent -- `search_context` behaves exactly as
    it did before Story 54.2. Never raises; a walk that could not be reported is
    still a walk that happened.
    """
    if not candidate_emission.observation_is_recorded():
        return 0
    sent = 0
    for crossing in walk_crossings(walk):
        if candidate_emission.emit_candidates(
            candidates=crossing["candidates"],
            descriptor=crossing["descriptor"],
            producer=candidate_emission.PRODUCER_CONTEXT_SEARCH,
            tool_name=candidate_emission.TOOL_SEARCH_CONTEXT,
            owner_object_type=crossing["owner_object_type"],
            owner_object_id=crossing["owner_object_id"],
        ):
            sent += 1
    return sent


def walk_text_line(walk: "ContextWalk") -> str:
    """The same facts as :func:`emit_walk`, on the text channel, as cited data.

    Built by `candidate_emission.cited_fate_line`, which composes only constants,
    counts and enumerated reasons -- so the mode and the depth are stated before
    any count, and a free-text reason cannot appear in it.
    """
    return candidate_emission.cited_fate_line(
        subject=WALK_SUBJECT,
        descriptor=walk.retrieval_descriptor(),
        candidates=[c.as_dict() for c in walk.candidates],
    )


def _snippet(text: str, *, limit: int = 160) -> str:
    """Single-line snippet: collapse whitespace, trim to *limit* chars."""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def record_fates(
    conn: Any, walk: "ContextWalk", crossed: list[dict[str, Any]] | None = None
) -> int:
    """Garder le SORT des candidats ecartes -- en agregat, jamais en journal.

    Mesure du 2026-08-03 : chaque marche calcule le sort de tout ce qu'elle
    atteint (`selected` / `rejected`, avec `below_cutoff`, `out_of_scope`...) et
    `emit_walk` ne poste rien sans `progressToken`. Le signal etait donc produit a
    CHAQUE recherche et conserve a AUCUNE.

    Ce qu'il vaut : un candidat rejete a repetition pour les questions d'un metier
    auquel il n'est PAS lie est un reclassement a proposer -- une liste de travail
    derivee de l'usage plutot que d'une relecture.

    ⚠️ AGREGAT. Une ligne par (projet, candidat, raison), avec un compteur : le
    volume est borne par la taille du corpus, jamais par le trafic. Un journal de
    chaque rejet de chaque recherche serait ingerable et n'apporterait rien de
    plus -- ce qui compte est COMBIEN DE FOIS, pas quand.

    Ne leve jamais : une recherche qui echoue parce qu'on n'a pas su compter
    serait une regression payee pour une mesure.

    ⚠️ UNE SEULE INSTRUCTION, ET C'EST MESURE (AI-157, 2026-08-04). La question
    posee avant de brancher cette fonction sur le chemin chaud etait : que
    coutent N insertions par requete ? Reponse, sur le corpus reel de la base
    locale (43 Skills, 141 Knowledge, `proj_qa_local`), une requete rejetant 80
    candidats :

        boucle de N `execute`   8.29 ms   (0.100 ms par ligne)
        une instruction         2.62 ms   (0.031 ms par ligne)

    Le facteur 3 n'est pas le cout de l'ECRITURE, c'est celui des ALLERS-RETOURS
    : la boucle en fait N, l'instruction unique en fait un, quel que soit N. En
    local l'aller-retour vaut ~0.07 ms ; sur une base distante il vaut des
    millisecondes, et N=80 ferait de la mesure la chose la plus chere du chemin.
    C'est exactement la mesure qu'on finit par couper.

    ⚠️ ELLE N'ENGAGE RIEN. `get_connection` ne commite pas, et une connexion
    fermee sur une transaction ouverte ROLLBACK : les sorts ecrits ici
    disparaissent si l'appelant ne commite pas. C'est `search_context` (l'outil
    MCP) qui possede sa transaction et qui commite -- pas cette fonction, qui
    commiterait le travail en cours de quelqu'un d'autre.

    ⚠️ SOUS POINT DE REPRISE. En psycopg 3 une instruction qui echoue AVORTE la
    transaction entiere : sans le `transaction()` ci-dessous, un echec d'ecriture
    ferait echouer tout ce que l'appelant tenterait ensuite. « Ne leve jamais »
    ne suffit pas quand l'echec empoisonne la transaction de l'appelant.
    """
    crossed = crossed if crossed is not None else []
    if walk.project_id is None:
        return 0
    # Story 45.5 : un retrait DECLARE ne descend pas ici. L'agregat sert a faire
    # remonter des LIENS MANQUANTS ; un candidat que son auteur a lui-meme ecarte
    # est l'inverse d'un lien manquant, et le proposer au reclassement ferait
    # travailler quelqu'un contre une decision deja prise.
    rejected = [candidate for candidate in walk.candidates
                if candidate.fate == candidate_fate.FATE_REJECTED and candidate.reason
                and candidate.reason not in candidate_fate.UNRECORDED_REASONS]
    if not rejected:
        return 0
    query = (walk.query or "")[:400]

    # (candidat, raison) est la cle de l'index unique : un doublon DANS le meme
    # lot ferait echouer l'instruction (« ON CONFLICT DO UPDATE cannot affect row
    # a second time »). Une marche n'en produit pas -- les candidats sont dedupes
    # par id -- mais un lot qui casse sur une invariante non ecrite est un lot
    # qui casse un jour.
    rows: list[tuple[Any, ...]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in rejected:
        key = (candidate.hit.id, candidate.reason)
        if key in seen:
            continue
        seen.add(key)
        rows.append((f"ccf_{ULID()}", walk.project_id, candidate.hit.id,
                     candidate.hit.kind, candidate.reason, query))

    values = ", ".join(["(%s, %s, %s, %s, %s, %s)"] * len(rows))
    params = [value for row in rows for value in row]
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO app.context_candidate_fates
                    (id, project_id, candidate_id, candidate_kind, reason,
                     last_query)
                VALUES {values}
                ON CONFLICT (project_id, candidate_id, reason) DO UPDATE
                   SET times = app.context_candidate_fates.times + 1,
                       last_query = EXCLUDED.last_query,
                       last_seen_at = now()
                RETURNING candidate_id, candidate_kind, reason, times, last_query
                """,
                params,
            )
            # Story 45.8 : le moment ou le compte ATTEINT le seuil est le moment
            # ou l'ecart est PROUVE -- ni avant (une anecdote), ni apres (la
            # remarque existe deja). `==` et non `>=` : la remarque part une
            # fois, pas a chaque recherche suivante.
            #
            # LA RELECTURE DU 2026-08-05 OBJECTE, ET LA MESURE TRANCHE. « Les
            # lignes deja au-dessus du seuil ne produiront jamais de remarque » :
            # exact, et l'ensemble est VIDE --
            #     SELECT count(*) FILTER (WHERE times >= 3)
            #       FROM app.context_candidate_fates   -> 0 sur 0 (2026-08-05)
            # Un rattrapage coderait une reprise pour un ensemble vide. Et sur la
            # duree, `>=` serait FAUX : une remarque qu'un humain a tranchee
            # reviendrait a chaque recherche suivante -- une file qui redemande
            # ce qu'on vient de lui repondre est une file qu'on cesse de lire.
            crossed.extend(
                dict(zip(_FATE_COLUMNS, row, strict=False))
                for row in cur.fetchall()
                if row[3] == RECURRENCE_THRESHOLD
            )
        return len(rows)
    except Exception:  # noqa: BLE001
        logger.debug("record_fates: skipped", exc_info=True)
        return 0


#: Combien de fois vaut << souvent >>. UNE valeur, lue par la lecture
#: (`recurrent_fates`) ET par le declenchement (`record_fates`) : deux seuils
#: pour un meme fait feraient une file qui ne correspond pas a ce qu'on lit.
RECURRENCE_THRESHOLD = 3

#: Les colonnes que l'upsert rend, dans l'ordre.
_FATE_COLUMNS = ("candidate_id", "candidate_kind", "reason", "times", "last_query")


def recurrent_fates(
    conn: Any, *, project_id: str, minimum: int = RECURRENCE_THRESHOLD
) -> list[dict[str, Any]]:
    """Ce qui a ete ecarte assez souvent pour meriter un reclassement.

    Le seuil est un PARAMETRE, pas une constante cachee : combien de fois vaut
    << souvent >> est une decision de produit, et l'enterrer dans le code
    reviendrait a la prendre en silence.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT candidate_id, candidate_kind, reason, times, last_query,
                   first_seen_at, last_seen_at
              FROM app.context_candidate_fates
             WHERE project_id = %s AND times >= %s
             ORDER BY times DESC, last_seen_at DESC
            """,
            (project_id, minimum),
        )
        columns = ("candidate_id", "candidate_kind", "reason", "times",
                   "last_query", "first_seen_at", "last_seen_at")
        return [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]


def _anti_triggered(frontmatter_yaml: str | None, term: str) -> bool:
    """La Skill a-t-elle declare que ce n'est PAS sa question ? -- Story 45.5.

    LA COMPARAISON EST CELLE DU CLASSEMENT, et c'est deliberé : depuis 45.9 le
    rang compare des MOTS, replies, SANS ORDRE. L'anti-declencheur doit donc se
    lire de la meme facon -- une regle qui exclurait autrement que le classement
    n'inclut produirait des retraits que personne ne peut predire.

    ⚠️ PAR MOTS ET SANS ORDRE, ET C'EST LE REJECT DU 2026-08-05. La comparaison
    precedente cherchait la CHAINE declaree dans la requete entiere
    (`fold(declared) in fold(term)`) pendant que l'inclusion etait devenue des
    jetons sans ordre : « media plan » declare, « plan media » tape -> la Skill
    remontait, et l'extrait rendu au lecteur etait la ligne d'exclusion
    elle-meme. Chaque mot de l'exclusion doit se retrouver dans ce qui a ete
    TAPE ; l'ordre ne veut rien dire des deux cotes.

    Le sens de la comparaison ne change pas : l'anti-declencheur est le terme du
    corpus, la requete est ce qu'on tape. « facturation » declare exclut
    « facturation fournisseur ». Une requete PLUS PRECISE que l'exclusion reste
    exclue -- c'est ce qu'un auteur veut dire en ecrivant un mot large.
    """
    from core.context_store import read_anti_triggers  # noqa: PLC0415

    # LES DEUX COTES SONT REPLIES, comme le classement depuis 45.9. Sans cela
    # un anti-declencheur ecrit avec accent ne repondait plus a la requete tapee
    # sans -- une exclusion qui se comporte autrement que l'inclusion qui a
    # atteint la ligne est une exclusion que personne ne peut predire.
    typed = fold(term).strip()
    if not typed:
        return False
    for declared in read_anti_triggers(frontmatter_yaml):
        # Les mots de l'exclusion, lus par le MEME decoupeur que la requete.
        # Un terme qui ne porte aucun mot comparable (« a », « ? ») retombe sur
        # la chaine entiere plutot que d'exclure tout.
        words = tokenise(declared) or [fold(declared).strip()]
        if all(word and word in typed for word in words):
            return True
    return False


def _searchable_frontmatter(frontmatter_yaml: str | None) -> str:
    """Le frontmatter tel que le CLASSEMENT a le droit de le lire -- Story 45.5.

    ⚠️ L'EXCLUSION NE DOIT JAMAIS ETRE LA CORRESPONDANCE. Mesure de la relecture
    adversariale du 2026-08-05 : `frontmatter_yaml` est compare par ILIKE depuis
    AI-154, donc une Skill dont le seul « billing » est sous `anti_triggers:`
    remontait sur la requete « bill » -- et l'extrait servi au lecteur etait la
    ligne d'exclusion. Le mot qui exclut faisait entrer.

    Le retrait passe par le MEME lecteur de frontmatter que tout le reste
    (`context_store`), jamais par un second dialecte ligne a ligne. Un
    frontmatter illisible est rendu tel quel : ne rien pouvoir retirer n'est pas
    une raison de perdre la ligne.
    """
    from core.context_store import searchable_frontmatter  # noqa: PLC0415

    return searchable_frontmatter(frontmatter_yaml)


def _frontmatter_excerpt(frontmatter_yaml: str | None, term: str) -> str:
    """La ligne du frontmatter qui a fait la correspondance.

    Sans elle, une Skill remonterait sur un tag ou une commande sans que rien
    n'explique pourquoi -- et un resultat qu'on ne peut pas expliquer se lit
    comme du bruit.

    Elle lit le frontmatter CHERCHABLE : la ligne d'exclusion n'est pas une
    explication de presence, c'est une explication d'absence.

    ⚠️ ET DONC PAS TOUJOURS L'OCTET POUR OCTET DE L'AUTEUR. Quand la Skill
    declare des anti-declencheurs, le frontmatter cherchable est re-serialise
    pour les retirer, donc l'extrait peut porter la mise en forme de YAML plutot
    que celle qui a ete tapee. Le dire ici plutot que jurer le contraire : c'est
    un extrait d'explication, pas une citation.
    """
    needle = fold(term)
    for line in _searchable_frontmatter(frontmatter_yaml).splitlines():
        if needle in fold(line):
            return " ".join(line.split())
    return ""


def _tier_name(score: float) -> str:
    if score >= TIER_TITLE:
        return "title"
    if score >= TIER_DESCRIPTION:
        return "description"
    return "neighbor"


def search_context(
    conn: Any,
    *,
    query: str,
    project_id: str | None,
    limit: int = DEFAULT_LIMIT,
) -> list[ContextHit]:
    """Rank the context corpus for *query*, scoped to *project_id* (AD-5).

    Returns ranked ``ContextHit`` objects (highest score first). Matches topics,
    procedures and schema_context docs at the TITLE / DESCRIPTION tiers, then adds
    one-hop context_graph neighbours of matched fragments at the NEIGHBOR tier.
    Current versions only (topics/procedures ``status='active'``; schema_context
    is current by construction). Empty/blank query -> empty result.

    Thin wrapper over ``search_context_walk`` (Story 54.2): ONE retrieval, one
    result set. The return shape here is unchanged -- callers that only want the
    survivors keep getting exactly them.
    """
    return search_context_walk(
        conn, query=query, project_id=project_id, limit=limit
    ).selected


def search_context_walk(
    conn: Any,
    *,
    query: str,
    project_id: str | None,
    limit: int = DEFAULT_LIMIT,
) -> ContextWalk:
    """The same pass as ``search_context``, reported in full (Story 54.2, AC3/AC4).

    Same SQL, same scoping (AD-5), same ranking (Story 11.5) -- the only
    difference is that the candidates ranked past *limit* are kept and returned
    with the fate ``rejected`` / ``below_cutoff`` instead of being discarded by a
    slice. Nothing here re-ranks, re-queries or widens the scope: a dropped
    candidate came out of the same scoped SELECT as a kept one, so reporting it
    can never surface another Project's row.

    What it does NOT report: the nodes it never reached. See
    ``NOT_REACHED_ENUMERATED``.

    AI-157 : LE SORT DES ECARTES EST DESORMAIS GARDE. `record_fates` existait,
    etait teste, et aucune recherche ne l'appelait -- le signal etait donc
    calcule a chaque marche et conserve a aucune. Il est appele ICI, au seul
    endroit ou la marche entiere est connue. Cout mesure : une instruction,
    2.62 ms pour 80 lignes, sur une marche qui en coute 3 a 11 -- et il ne coute
    rien quand rien n'est rejete, ce qui est le cas d'une requete precise.

    ⚠️ CETTE FONCTION NE COMMITE PAS. Le sort n'atteint la base que si
    l'appelant, qui possede la transaction, commite (`search_context` dans
    `core/main.py` le fait). Committer ici emporterait le travail en cours de
    quelqu'un d'autre.
    """
    term = (query or "").strip()
    if not term:
        empty = ContextWalk(query=query or "", project_id=project_id, limit=limit, ranked=[])
        emit_walk(empty)
        return empty

    tokens = tokenise(term)
    if not tokens:
        # Une requete sans aucun mot comparable (ponctuation, une seule lettre)
        # ne trouve rien -- et le dit, au lieu de comparer une chaine vide qui
        # aurait tout rendu.
        empty = ContextWalk(query=term, project_id=project_id, limit=limit, ranked=[])
        emit_walk(empty)
        return empty
    hits: dict[str, ContextHit] = {}
    # Story 45.5 -- atteints puis ecartes par leur propre declaration.
    refused: list[ContextHit] = []
    refused_ids: set[str] = set()
    scan_truncated = False

    with conn.cursor() as cur:
        # --- Topics (platform + project scope, active only) ------------------
        cur.execute(
            f"""
            SELECT id, project_id, title, body_md
            FROM app.context_topics
            WHERE status = 'active'
              AND (project_id IS NULL OR project_id = %s)
              AND ({_any_token_sql(("title", "body_md"), tokens)})
            ORDER BY id
            LIMIT %s
            """,
            [project_id, *_token_params(("title", "body_md"), tokens),
             SUPERSET_ROW_LIMIT],
        )
        topic_rows = cur.fetchall()
        scan_truncated = scan_truncated or len(topic_rows) >= SUPERSET_ROW_LIMIT
        for row in topic_rows:
            tid, pid, title, body = row
            in_title = _matched(tokens, title)
            hit_tokens = in_title | _matched(tokens, body)
            # TOUS les mots, et pas un seul : avant cette story le tier TITRE
            # exigeait la phrase entiere dans le titre. Un seul mot suffisant
            # ferait remonter « Window sizing » au niveau de « Attribution
            # window » sur « attribution window » -- une requete qui marchait
            # rendrait un autre ordre, ce que la story s'interdit.
            title_hit = len(in_title) == len(tokens)
            score = TIER_TITLE if title_hit else TIER_DESCRIPTION
            hits[tid] = ContextHit(
                id=tid,
                kind="topic",
                title=title or "",
                snippet=_snippet(title if title_hit else body),
                score=score,
                tier=_tier_name(score),
                project_id=pid,
                matched=True,
            )
            hits[tid].matched_tokens = tuple(token for token in tokens if token in hit_tokens)

        # --- Procedures (platform + project scope, active only) --------------
        # ⚠️ `frontmatter_yaml` EST COMPARE, et il ne l'etait pas.
        #
        # Mesure du 2026-08-03 : le frontmatter porte les seuls champs
        # STRUCTURES d'une Skill -- `mdm_tags` (les metriques gouvernees qu'elle
        # concerne), `steps` (sa sequence, avec ses cibles et ses commandes) et
        # `acceptance` (ses criteres). Aucun n'etait compare ici, donc trente-
        # deux Skills taguees rendaient ZERO sur une requete par tag, et une
        # Skill dont un pas nomme `npx vitest` etait introuvable par ce mot.
        #
        # Le rang ne bouge pas pour autant : une correspondance de frontmatter
        # marque au tier DESCRIPTION, jamais TITLE. Seul le NOM vaut un titre --
        # sinon un tag ferait remonter une Skill au-dessus de celle qui porte
        # littéralement le mot demande.
        _proc_columns = ("name", "description", "body_md", "frontmatter_yaml")
        cur.execute(
            f"""
            SELECT id, project_id, name, description, body_md, frontmatter_yaml
            FROM app.procedures
            WHERE status = 'active'
              AND (project_id IS NULL OR project_id = %s)
              AND ({_any_token_sql(_proc_columns, tokens)})
            ORDER BY id
            LIMIT %s
            """,
            [project_id, *_token_params(_proc_columns, tokens), SUPERSET_ROW_LIMIT],
        )
        proc_rows = cur.fetchall()
        scan_truncated = scan_truncated or len(proc_rows) >= SUPERSET_ROW_LIMIT
        for row in proc_rows:
            pid_key, pid, name, desc, body, front = row
            # ⚠️ LE FRONTMATTER EST LU MOINS SES EXCLUSIONS. La clause SQL
            # compare la colonne ENTIERE (elle ne sait pas lire du YAML), donc
            # une Skill peut arriver ici par le seul mot qui l'exclut. La
            # precision se decide en Python, comme pour les jetons partiels.
            searchable_front = _searchable_frontmatter(front)
            in_name = _matched(tokens, name)
            proc_tokens = in_name | _matched(tokens, desc, body, searchable_front)
            name_hit = len(in_name) == len(tokens)
            # Story 45.5 -- ce que la Skill a declare ne pas etre sa question.
            # Le retrait se fait ICI et pas dans la clause SQL : une Skill qui
            # disparait du corpus ne peut pas etre deboguee par qui l'a ecrite.
            # Et il doit se faire APRES la clause, pas avant -- `frontmatter_yaml`
            # etant compare par ILIKE, un anti-declencheur ferait autrement
            # REMONTER la Skill par le mot meme qui l'exclut.
            # ⚠️ SUR LA REQUETE ENTIERE, ET PAS JETON PAR JETON. Une relecture
            # adversariale du 2026-08-05 l'a mesure : compare par jeton, un
            # anti-declencheur en DEUX MOTS (« media plan ») ou ACCENTUE
            # (« reconciliation ») ne correspondait plus a rien -- et la Skill
            # remontait alors PAR LE MOT MEME qui l'exclut, puisque le
            # frontmatter est compare. La regle reste celle de 45.5 : le terme
            # declare est cherche dans ce qui a ete TAPE.
            if _anti_triggered(front, term):
                refused.append(ContextHit(
                    id=pid_key,
                    kind="procedure",
                    title=name or "",
                    snippet=_snippet(desc or body),
                    score=0.0,
                    tier="refused",
                    project_id=pid,
                    matched=False,
                ))
                refused_ids.add(pid_key)
                continue
            if not proc_tokens:
                # Atteinte par la clause SQL et par RIEN d'autre : le seul mot
                # present vivait sous `anti_triggers:`. Elle n'a pas ete
                # ecartee, elle n'a jamais correspondu -- l'annoncer comme un
                # refus inventerait un examen, exactement comme enumerer les
                # noeuds jamais atteints (`NOT_REACHED_ENUMERATED`).
                continue
            score = TIER_TITLE if name_hit else TIER_DESCRIPTION
            if not name_hit and not _matched(tokens, desc, body):
                # La correspondance ne vient QUE du frontmatter : l'extrait doit
                # le montrer, sinon le lecteur ne comprend pas pourquoi cette
                # Skill est la.
                # Le PREMIER jeton tape qui a correspondu, pas un tire d'un
                # `set` : le hachage des chaines varie d'un processus a l'autre,
                # donc l'extrait rendu changeait d'une execution a l'autre.
                _first = next((k for k in tokens if k in proc_tokens), term)
                desc = _frontmatter_excerpt(front, _first) or desc
            # NOTE: `searchable_front` a decide la correspondance ; `front` est
            # passe a l'extrait, qui le replie lui-meme par le meme lecteur.
            hits[pid_key] = ContextHit(
                id=pid_key,
                kind="procedure",
                title=name or "",
                snippet=_snippet(name if name_hit else (desc or body)),
                score=score,
                tier=_tier_name(score),
                project_id=pid,
                matched=True,
            )
            hits[pid_key].matched_tokens = tuple(k for k in tokens if k in proc_tokens)

        # --- Schema context docs (project-scoped; a schema TERM surfaces its
        #     doc -- Story 11.5 "also indexes schema_context docs"). -----------
        if project_id is not None:
            cur.execute(
                f"""
                SELECT id, project_id, relation, doc_kind, body_md
                FROM app.schema_context
                WHERE project_id = %s
                  AND ({_any_token_sql(("relation", "body_md"), tokens)})
                ORDER BY id
                LIMIT %s
                """,
                [project_id, *_token_params(("relation", "body_md"), tokens),
                 SUPERSET_ROW_LIMIT],
            )
            schema_rows = cur.fetchall()
            scan_truncated = scan_truncated or len(schema_rows) >= SUPERSET_ROW_LIMIT
            for row in schema_rows:
                sid, pid, relation, doc_kind, body = row
                in_relation = _matched(tokens, relation)
                schema_tokens = in_relation | _matched(tokens, body)
                relation_hit = len(in_relation) == len(tokens)
                score = TIER_TITLE if relation_hit else TIER_DESCRIPTION
                hits[sid] = ContextHit(
                    id=sid,
                    kind="schema_doc",
                    title=f"{relation} ({doc_kind})",
                    snippet=_snippet(relation if relation_hit else body),
                    score=score,
                    tier=_tier_name(score),
                    project_id=pid,
                    matched=True,
                )
                hits[sid].matched_tokens = tuple(k for k in tokens if k in schema_tokens)

        # ── Story 45.9 : LES DEUX PASSES SE DECIDENT AVANT LES VOISINS.
        #
        # Relecture adversariale du 2026-08-05 : les voisins etaient hydrates
        # a partir de `hits` AVANT l'elagage des partiels, donc la marche
        # gardait des voisins dont l'ancre avait ete ecartee -- un candidat
        # au tier VOISIN dont le lecteur ne peut pas trouver le voisinage,
        # ce qui contredit la definition du tier elle-meme.
        #
        # UN SEUL MOT : « tous » et « au moins un » sont la meme chose, donc
        # rien ne bouge pour les requetes d'aujourd'hui. C'est le controle
        # negatif de la story, et il est structurel plutot que promis.
        matched_hits = [hit for hit in hits.values() if hit.matched]
        complete = [h for h in matched_hits if len(h.matched_tokens) == len(tokens)]
        widened_mode = not complete
        for hit in matched_hits:
            hit.widened = widened_mode or len(hit.matched_tokens) < len(tokens)
        if not widened_mode:
            # La premiere passe a repondu : les partiels ne sont pas la reponse.
            for partial in [hit for hit in matched_hits if hit.widened]:
                hits.pop(partial.id, None)

        # --- Graph neighbours (one hop from any direct match) ----------------
        # Surfaced at TIER_NEIGHBOR so a linked definition is not lost, but never
        # outranks a direct lexical hit. Only edges visible in scope (platform
        # edges or the caller's project edges) are followed (AD-5).
        matched_ids = list(hits.keys())
        if matched_ids:
            cur.execute(
                """
                SELECT from_id, from_type, to_id, to_type
                FROM app.context_graph
                WHERE (project_id IS NULL OR project_id = %s)
                  AND (from_id = ANY(%s) OR to_id = ANY(%s))
                """,
                (project_id, matched_ids, matched_ids),
            )
            neighbour_refs: dict[str, str] = {}
            for from_id, from_type, to_id, to_type in cur.fetchall():
                if from_id in hits and to_id not in hits:
                    neighbour_refs.setdefault(to_id, to_type)
                if to_id in hits and from_id not in hits:
                    neighbour_refs.setdefault(from_id, from_type)

            _hydrate_neighbours(
                cur, neighbour_refs, hits, project_id,
                term=term, refused=refused, refused_ids=refused_ids,
            )

            # Un voisin nait de la passe qui l'a atteint. En mode elargi il
            # n'y a AUCUNE passe directe, donc le presenter comme direct --
            # et le laisser passer DEVANT les elargis -- ferait lire comme
            # ferme un resultat qui ne porte aucun des mots demandes.
            if widened_mode:
                for hit in hits.values():
                    if not hit.matched:
                        hit.widened = True

    # ── Story 45.9 : DEUX PASSES, sur UNE SEULE recuperation.
    #
    # La clause SQL est le SUR-ENSEMBLE (au moins un mot). La precision se decide
    # ici, sur les memes lignes : les noeuds qui portent TOUS les mots sont la
    # reponse directe ; les autres ne sont retenus que si la premiere passe n'a
    # rien atteint, et ils sont alors marques ELARGIS et classes sous tout le
    # reste. Deux requetes SQL auraient fait deux recuperations -- ce module
    # tient qu'il n'y en a qu'une, et une seconde serait un defaut, pas un
    # progres.
    #
    # UN SEUL MOT : « tous » et « au moins un » sont la meme chose, donc rien ne
    # bouge pour les requetes d'aujourd'hui. C'est le controle negatif de la
    # story, et il est structurel plutot que promis.

    ranked = sorted(
        hits.values(),
        # Un candidat ELARGI passe apres tout ce que la passe directe a produit,
        # voisin de graphe compris : le voisin est un artefact de la passe
        # directe, l'elargi est ce qu'on a accepte faute de mieux.
        #
        # ⚠️ COMBIEN DE MOTS ONT CORRESPONDU, AVANT L'IDENTIFIANT. Mesure de la
        # relecture adversariale du 2026-08-05, corpus de 101 lignes, requete
        # « what is the attribution window » : en mode elargi AUCUN candidat ne
        # peut atteindre le tier TITRE (il exige tous les mots), donc la cle
        # s'effondrait sur genre -> id, et un id est un ULID. Le seul noeud
        # portant les deux mots porteurs sortait 101e, coupe par le plafond,
        # pendant que `main.py` annonce « most relevant first ». `matched_tokens`
        # etait CALCULE a trois endroits et lu nulle part.
        #
        # En mode direct la valeur est CONSTANTE (tous les candidats portent
        # tous les mots, les voisins sont deja separes par le score) : ce terme
        # ne peut donc pas deplacer une requete qui marchait. C'est le controle
        # negatif, et il est structurel.
        key=lambda h: (
            h.widened, -h.score, -len(h.matched_tokens),
            _KIND_ORDER.get(h.kind, 9), h.id,
        ),
    )
    # The cap is applied by ContextWalk.selected, not here: the ordering above is
    # the whole ranking, and slicing it away was the only reason the dropped
    # candidates had no trace.
    walk = ContextWalk(
        query=term, project_id=project_id, limit=limit, ranked=ranked, refused=refused,
        tokens=tokens, widened_mode=widened_mode, scan_truncated=scan_truncated,
    )
    # Story 54.2 T3: the crossing is reported AT the crossing, on Story 54.1's
    # seam and only it. Silent and free when the client sent no progressToken.
    emit_walk(walk)
    # AI-157 : et le sort des ecartes est GARDE, en agregat. `emit_walk` ne poste
    # rien sans `progressToken` -- sans cette ligne, le signal etait produit a
    # chaque recherche et conserve a aucune.
    crossed: list[dict[str, Any]] = []
    record_fates(conn, walk, crossed)
    # Story 45.8 : le seuil se franchit ICI, pendant une recherche -- c'est donc
    # ici que la remarque part. `origin='agent'` existait dans le schema et
    # n'avait AUCUN producteur : la file se remplissait par relecture et jamais
    # par l'usage. La remarque ne propose PAS de cible : le signal ne nomme qu'un
    # bout, et choisir l'autre serait deviner.
    if crossed:
        from core.context_review import flag_recurring_rejections  # noqa: PLC0415

        flag_recurring_rejections(conn, project_id=walk.project_id, crossed=crossed)
    return walk


def _hydrate_neighbours(
    cur: Any,
    neighbour_refs: dict[str, str],
    hits: dict[str, ContextHit],
    project_id: str | None,
    *,
    term: str,
    refused: list[ContextHit],
    refused_ids: set[str],
) -> None:
    """Resolve neighbour ids to display rows and add them at TIER_NEIGHBOR.

    ⚠️ LE REFUS DECLARE TIENT AUSSI ICI, ET C'EST LE REJECT DE 45.5. Mesure de
    la relecture adversariale du 2026-08-05 : cette fonction reselectionnait
    `app.procedures` SANS `frontmatter_yaml` et n'appelait jamais
    `_anti_triggered`. Une Skill ecartee au premier tour n'etant pas dans
    `hits`, la garde `if pid_key not in hits` la faisait RENTRER au tier VOISIN
    -- le meme noeud simultanement refuse et servi, et `reached_count` comptant
    deux fois un seul noeud, sur le chemin de `emit_walk` et de
    `walk_text_line`, c'est-a-dire de tout agent.

    Un refus qui ne tient qu'au premier tour n'est pas un refus : c'est un
    detour.
    """
    if not neighbour_refs:
        return

    topic_ids = [nid for nid, t in neighbour_refs.items() if t == "topic"]
    proc_ids = [nid for nid, t in neighbour_refs.items() if t == "procedure"]
    schema_ids = [nid for nid, t in neighbour_refs.items() if t == "schema_doc"]

    if topic_ids:
        cur.execute(
            """
            SELECT id, project_id, title, body_md
            FROM app.context_topics
            WHERE status = 'active'
              AND (project_id IS NULL OR project_id = %s)
              AND id = ANY(%s)
            """,
            (project_id, topic_ids),
        )
        for tid, pid, title, body in cur.fetchall():
            if tid not in hits:
                hits[tid] = ContextHit(
                    id=tid, kind="topic", title=title or "",
                    snippet=_snippet(body or title), score=TIER_NEIGHBOR,
                    tier="neighbor", project_id=pid, matched=False,
                )

    if proc_ids:
        cur.execute(
            """
            SELECT id, project_id, name, description, frontmatter_yaml
            FROM app.procedures
            WHERE status = 'active'
              AND (project_id IS NULL OR project_id = %s)
              AND id = ANY(%s)
            """,
            (project_id, proc_ids),
        )
        for pid_key, pid, name, desc, front in cur.fetchall():
            if pid_key in hits:
                continue
            if _anti_triggered(front, term):
                # Deja refuse au premier tour : une seule ligne de refus, pas
                # deux. Un candidat porteur de DEUX sorts contradictoires est
                # exactement ce que la relecture a trouve.
                if pid_key not in refused_ids:
                    refused.append(ContextHit(
                        id=pid_key, kind="procedure", title=name or "",
                        snippet=_snippet(desc or name), score=0.0,
                        tier="refused", project_id=pid, matched=False,
                    ))
                    refused_ids.add(pid_key)
                continue
            hits[pid_key] = ContextHit(
                id=pid_key, kind="procedure", title=name or "",
                snippet=_snippet(desc or name), score=TIER_NEIGHBOR,
                tier="neighbor", project_id=pid, matched=False,
            )

    if schema_ids and project_id is not None:
        cur.execute(
            """
            SELECT id, project_id, relation, doc_kind, body_md
            FROM app.schema_context
            WHERE project_id = %s AND id = ANY(%s)
            """,
            (project_id, schema_ids),
        )
        for sid, pid, relation, doc_kind, body in cur.fetchall():
            if sid not in hits:
                hits[sid] = ContextHit(
                    id=sid, kind="schema_doc", title=f"{relation} ({doc_kind})",
                    snippet=_snippet(body or relation), score=TIER_NEIGHBOR,
                    tier="neighbor", project_id=pid, matched=False,
                )


def get_procedure_by_name(
    conn: Any,
    *,
    name: str,
    project_id: str | None,
) -> dict[str, Any] | None:
    """Fetch the current (active) procedure by NAME, scoped to *project_id* (AD-5).

    A procedure is citable by name (Story 11.5). Project scope wins over platform
    when both define the same name: we prefer the project-scoped row so a project
    override of a platform procedure resolves to the project's version. Returns
    None when no active procedure matches in scope (the caller renders a stable,
    non-disclosing not-found result -- it never reveals whether the name exists in
    another project's scope).
    """
    clean = (name or "").strip()
    if not clean:
        return None

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                p.id, p.project_id, p.name, p.description,
                p.frontmatter_yaml, p.body_md, p.status,
                COALESCE(MAX(v.version_number), 1) AS version_number
            FROM app.procedures p
            LEFT JOIN app.procedures_versions v ON p.id = v.procedure_id
            WHERE p.status = 'active'
              AND p.name = %s
              AND (p.project_id IS NULL OR p.project_id = %s)
            GROUP BY
                p.id, p.project_id, p.name, p.description,
                p.frontmatter_yaml, p.body_md, p.status
            -- Prefer the project-scoped row over the platform row (NULLs last).
            ORDER BY (p.project_id IS NULL) ASC
            LIMIT 1
            """,
            (clean, project_id),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
