"""`mdm_tags` nomme des metriques gouvernees -- la porte qui manquait, AI-156.

POURQUOI ELLE N'EXISTAIT PAS AVANT LE 2026-08-04, et pourquoi elle peut exister
maintenant. Le champ servait DEUX usages faute d'un champ libre :

  * des metriques du catalogue -- la console l'intitule « MDM tags — Canonical
    metrics » et le lie par cases a cocher a `app.target_fields` ;
  * des mots-cles -- j'en ai ecrit, et la procedure que le PRODUIT seme
    (migration 196) en portait cinq dont AUCUN ne resolvait.

Le rendre strict aurait donc rendu la donnee du produit non modifiable. Une
validation qui casse la donnee du produit n'est pas une rigueur, c'est une panne.
La migration 203 a separe les deux (`keywords`), et la stricte devient tenable.

CE QUE LA PORTE TIENT, et le controle negatif compte autant : elle refuse en
NOMMANT le tag inconnu et l'endroit ou vit le catalogue. Un refus qui laisse
chercher est un refus qu'on apprend a contourner.
"""

from __future__ import annotations

import pytest
from core.context_store import assert_mdm_tags_resolve, validate_procedure_frontmatter

from tests.support.statement_router import (
    StatementInventory,
    UnknownStatement,
    describe,
)

# ---------------------------------------------------------------------------
# LES DEUX SEULS STATEMENTS QUE CETTE PORTE EMET, NOMMES (AI-317).
#
# `assert_mdm_tags_resolve` (context_store.py:654) n'atteint la base que par
# `governed_field_catalogue.governed_names` -> `resolve`, qui emet exactement
# deux lectures : le Modele Semantique d'abord
# (governed_field_catalogue.py:70), le dictionnaire en repli (:82). Rien
# d'autre n'est interroge sur un chemin teste ici -- `binding_vocabulary` et sa
# troisieme lecture (`app.mdm_canonical_fields`, :90) ne sont PAS sur ce
# chemin, et c'est precisement pour cela qu'elles doivent lever si elles y
# arrivent un jour.
#
# Ce faux dispatchait par un `else` : TOUT ce qui n'etait pas
# `app.semantic_concepts` recevait le dictionnaire, ses sept colonnes et ses
# lignes. Une lecture ajoutee en amont aurait donc ete servie par le magasin
# d'a cote, `cost` et `revenue` auraient continue de resoudre, et la porte
# serait restee verte en decrivant un catalogue jamais lu.
#
# Ordre de declaration = ordre d'un `if/elif` : le premier qui matche gagne.
# Les deux relations sont disjointes, donc un fragment suffit a chacune -- et
# aucun n'est elargi jusqu'a avaler l'autre.
# ---------------------------------------------------------------------------
_STATEMENTS = StatementInventory(
    "test_mdm_tags_resolve._Conn",
    semantic_concepts="from app.semantic_concepts",
    field_dictionary="from app.target_fields",
)


class _Conn:
    """Le catalogue gouverne et ses DEUX magasins, ecrits a la main.

    Story 49.3 : la porte passe par `governed_field_catalogue`, donc elle
    interroge le Modele Semantique PUIS `app.target_fields`. Le double repond
    separement a chacun -- servir les memes noms aux deux rendrait le repli
    indistinguable du successeur, et c'est justement la distinction qui compte.
    """

    #: Publie au workbench de Concepts. ABSENT du dictionnaire.
    _CONCEPTS = {"audience_reach"}
    #: Les lignes que la migration 023 a semees, et qui ne bougeront plus.
    _DICTIONARY = {"cost", "revenue"}

    def __init__(self) -> None:
        self._rows: list[tuple] = []
        self.description: list[tuple] = []

    def cursor(self) -> "_Conn":
        return self

    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, params: dict | None = None) -> None:
        """Repond aux deux magasins, et LEVE sur tout le reste.

        `description` est DERIVE du SELECT (`describe`) au lieu d'etre recopie :
        les deux projections sont plates, donc la liste de colonnes que
        `governed_field_catalogue._fetch` relit vient de la requete elle-meme et
        ne peut plus vieillir a cote d'elle.
        """
        statement = _STATEMENTS.match(sql)
        names = set((params or {}).get("names") or [])
        match statement:
            case "semantic_concepts":
                known = self._CONCEPTS
                row_for = lambda name: (name, None, "metric", name, "integer",  # noqa: E731
                                        None, {"function": "sum"}, "additive")
            case "field_dictionary":
                known = self._DICTIONARY
                row_for = lambda name: (name, name, "integer", "metric", "sum",  # noqa: E731
                                        None, "approved")
            case _:  # pragma: no cover -- un nom de l'inventaire sans reponse
                raise _STATEMENTS.unknown(sql)
        self.description = describe(sql)
        self._rows = [row_for(name) for name in sorted(names) if name in known]

    def fetchall(self) -> list[tuple]:
        return self._rows


def test_governed_tags_pass() -> None:
    assert_mdm_tags_resolve(_Conn(), ["cost", "revenue"])


def test_a_tag_the_semantic_model_alone_governs_passes() -> None:
    """L'ELARGISSEMENT est le point : depuis le 2026-08-25 le dictionnaire ne
    peut plus grandir, donc un tag neuf ne peut venir que du successeur. Cette
    porte le refusait, en renvoyant vers un catalogue en lecture seule."""
    assert_mdm_tags_resolve(_Conn(), ["audience_reach"], "proj_EXAMPLE")


def test_the_fallback_is_not_dropped() -> None:
    """Le successeur AJOUTE ; il ne retire jamais. Un tag que seul le
    dictionnaire porte doit continuer de passer, sinon la migration casserait
    toutes les Skills existantes."""
    assert_mdm_tags_resolve(_Conn(), ["cost"], "proj_EXAMPLE")


def test_no_tag_queries_nothing_and_passes() -> None:
    """Une Skill sans tag n'est pas fautive -- le champ est facultatif."""
    assert_mdm_tags_resolve(_Conn(), None)
    assert_mdm_tags_resolve(_Conn(), [])


def test_an_unknown_tag_is_refused_and_named() -> None:
    with pytest.raises(ValueError) as caught:
        assert_mdm_tags_resolve(_Conn(), ["cost", "role-sequence", "screen-fixer"])
    message = str(caught.value)

    # Ce qui est inconnu, nomme -- pas « un tag est invalide ».
    assert "role-sequence" in message
    assert "screen-fixer" in message
    # Ce qui est connu ne doit PAS etre accuse.
    assert "cost" not in message.split("unknown:")[1]
    # Ou aller ensuite : le champ libre, et le geste qui MARCHE. Le refus
    # nommait `GET /api/datamodel/fields`, dont les portes d'ecriture repondent
    # 409 `legacy_store_is_read_only` depuis le 2026-08-25 : envoyer quelqu'un
    # vers un catalogue ou il ne peut rien ajouter n'est pas un geste.
    assert "'keywords'" in message
    assert "Semantic Model" in message
    assert "/api/datamodel/fields" not in message


def test_the_fake_refuses_a_statement_it_was_never_taught() -> None:
    """AI-317 : un statement jamais enseigne se NOMME au lieu d'etre servi.

    L'`else` que ceci remplace rendait le dictionnaire a n'importe quelle
    requete. La troisieme lecture du meme module -- `binding_vocabulary` sur
    `app.mdm_canonical_fields` (governed_field_catalogue.py:90) -- aurait recu
    les sept colonnes et les lignes de `app.target_fields`, et `cost` serait
    reste gouverne par un magasin que la porte n'a jamais interroge.
    """
    conn = _Conn()
    with pytest.raises(UnknownStatement) as raised:
        conn.execute(
            "SELECT canonical_name FROM app.mdm_canonical_fields"
            " WHERE status = 'active'",
            {},
        )
    message = str(raised.value)
    # La requete qui a bouge, et l'inventaire auquel la comparer.
    assert "app.mdm_canonical_fields" in message
    assert "semantic_concepts" in message


def test_keywords_are_a_separate_field_and_free() -> None:
    """C'est la separation qui debloque la validation : sans champ libre, les
    deux usages se disputaient une seule cle."""
    parsed = validate_procedure_frontmatter(
        'name: demo\ndescription: "d"\n'
        "keywords:\n  - datastream\n  - testing\n"
        "mdm_tags:\n  - cost\n"
    )
    assert parsed["keywords"] == ["datastream", "testing"]
    assert parsed["mdm_tags"] == ["cost"]
    assert_mdm_tags_resolve(_Conn(), parsed["mdm_tags"])


def test_keywords_refuse_a_duplicate_like_every_other_list() -> None:
    with pytest.raises(ValueError, match="duplicate value"):
        validate_procedure_frontmatter(
            'name: demo\ndescription: "d"\nkeywords:\n  - same\n  - same\n'
        )
