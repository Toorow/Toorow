"""Ce qu'une Skill designe du modele de donnees -- resolu, et ce qui ne resout pas.

POURQUOI. Jean, 2026-08-03 : « dans les skills et les business tu dois pouvoir
utiliser des colonnes de ton MDM avec {{produit}} {{cout}}, pour que s'il en a
besoin indirectement il puisse savoir de quoi tu parles ».

CE QUE CES PORTES TIENNENT, et chacune vient d'une decision prise a la mesure :

  * on RESOUT, on ne REFUSE PAS. La procedure que le produit seme
    (migration 196) porte cinq `mdm_tags` dont aucun n'est un champ du
    catalogue -- rejeter la rendrait non modifiable ;
  * les DEUX cotes sont rendus. Ne publier que les references resolues
    laisserait croire qu'une Skill ne cite que des champs existants ;
  * une reference non resolue reste ECRITE telle quelle au rendu, accolades
    comprises : l'effacer supprimerait la seule trace qu'un champ a disparu, et
    cette trace EST le retour sur la Skill.
"""

from __future__ import annotations

import pytest
from core.mdm_references import references, render, resolve, tags_of

from tests.support.statement_router import (
    StatementInventory,
    UnknownStatement,
    describe,
)

# ---------------------------------------------------------------------------
# LES DEUX SEULS STATEMENTS QUE CE PARCOURS EMET, NOMMES (AI-317).
#
# `mdm_references.resolve` n'interroge la base que par
# `core.governed_field_catalogue.resolve`, qui emet exactement deux lectures --
# le Modele Semantique d'abord (governed_field_catalogue.py:70), le
# dictionnaire en repli (:82) -- et rien d'autre. Ce faux dispatchait par un
# `else` : TOUT ce qui n'etait pas `app.semantic_concepts` recevait le
# dictionnaire, ses sept colonnes et ses lignes. Une lecture ajoutee au
# catalogue gouverne aurait donc ete servie par le magasin d'a cote, et les
# portes d'ici seraient restees vertes en decrivant un chemin jamais parcouru.
#
# Ordre de declaration = ordre d'un `if/elif` : le premier qui matche gagne.
# Les deux relations sont disjointes, donc un fragment suffit a chacune -- et
# aucun n'est elargi jusqu'a avaler l'autre.
# ---------------------------------------------------------------------------
_STATEMENTS = StatementInventory(
    "test_mdm_references._Cursor",
    semantic_concepts="from app.semantic_concepts",
    field_dictionary="from app.target_fields",
)


class _Cursor:
    """Un curseur qui rend le catalogue GOUVERNE -- ses deux magasins.

    Ecrit a la main plutot que moque : la porte doit prouver que le module
    filtre bien sur les noms cites, pas qu'un mock a ete appele.

    DEUX MAGASINS depuis la story 49.3 : `mdm_references` passe par
    `governed_field_catalogue`, qui interroge le Modele Semantique puis le
    dictionnaire. Le double repond a chaque requete SEPAREMENT -- servir les
    memes lignes aux deux cacherait precisement le changement.
    """

    #: Publie au workbench de Concepts, ABSENT de `app.target_fields`.
    _CONCEPTS = {
        "audience_reach": ("audience_reach", "proj_EXAMPLE", "metric",
                           "Audience reach", "integer",
                           "People the campaign reached.",
                           {"function": "sum"}, "additive"),
    }
    _DICTIONARY = {
        "cost": ("cost", "Media cost", "numeric", "metric", "sum",
                 "What was paid to the platform.", "approved"),
        "country": ("country", "Country", "string", "dimension", None,
                    "ISO country of the audience.", "approved"),
    }

    def __init__(self) -> None:
        self._rows: list[tuple] = []
        self.description: list[tuple] = []

    def __enter__(self) -> "_Cursor":
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
                catalog = self._CONCEPTS
            case "field_dictionary":
                catalog = self._DICTIONARY
            case _:  # pragma: no cover -- un nom de l'inventaire sans reponse
                raise _STATEMENTS.unknown(sql)
        self.description = describe(sql)
        self._rows = [catalog[name] for name in sorted(names) if name in catalog]

    def fetchall(self) -> list[tuple]:
        return self._rows


class _Conn:
    def cursor(self) -> _Cursor:
        return _Cursor()


# --- l'extraction ----------------------------------------------------------


def test_references_are_read_in_order_without_duplicates() -> None:
    assert references("compute {{cost}} per {{country}}, then {{cost}} again") == [
        "cost",
        "country",
    ]


def test_spacing_inside_the_braces_is_tolerated() -> None:
    assert references("{{ cost }}") == ["cost"]


def test_what_is_not_a_field_name_is_not_a_reference() -> None:
    """Laisser passer n'importe quoi INVENTERAIT des references."""
    assert references("{{ two words }} {{a.b}} {{}} {{9lives}}") == []


def test_tags_survive_a_frontmatter_that_cannot_be_parsed() -> None:
    """Une LECTURE ne doit pas echouer sur une Skill mal formee -- le validateur
    refuse a l'ECRITURE, c'est son role, pas celui du lecteur."""
    assert tags_of("mdm_tags:\n  - cost\n  - country\n") == ["cost", "country"]
    assert tags_of("name: [unclosed") == []
    assert tags_of("") == []


# --- la resolution ---------------------------------------------------------


def test_resolution_returns_both_sides() -> None:
    out = resolve(
        _Conn(),
        body_md="Compute {{cost}} per {{country}}, split by {{product}}.",
        mdm_tags=["cost", "role-sequence"],
    )
    assert [field["name"] for field in out["inline"]["resolved"]] == ["cost", "country"]
    assert out["inline"]["unresolved"] == ["product"]

    # Le tag qui ne resout pas est NOMME : c'est l'ecart entre ce que la console
    # promet (« Canonical metrics ») et ce que le produit ecrit reellement.
    assert [field["name"] for field in out["tags"]["resolved"]] == ["cost"]
    assert out["tags"]["unresolved"] == ["role-sequence"]


def test_a_resolved_field_carries_what_makes_it_understandable() -> None:
    field = resolve(_Conn(), body_md="{{cost}}")["inline"]["resolved"][0]
    assert field["data_type"] == "numeric"
    assert field["field_kind"] == "metric"
    assert field["measure"] == "sum"
    assert field["status"] == "approved"
    assert field["description"] == "What was paid to the platform."


def test_a_field_only_the_semantic_model_carries_resolves() -> None:
    """LE POINT DE LA STORY 49.3. `app.target_fields` ne peut plus grandir : ses
    cinq portes d'ecriture repondent 409 depuis le 2026-08-25. Une metrique
    declaree au workbench de Concepts DOIT donc resoudre ici, sans quoi une Skill
    ne peut plus jamais citer un champ neuf."""
    field = resolve(_Conn(), body_md="{{audience_reach}}")["inline"]["resolved"][0]
    assert field["name"] == "audience_reach"
    assert field["display_name"] == "Audience reach"
    assert field["field_kind"] == "metric"
    assert field["measure"] == "sum"
    # `published` EST l'approbation du successeur, dite dans le mot que les
    # lecteurs de ce catalogue testent.
    assert field["status"] == "approved"


def test_the_fake_refuses_a_statement_it_was_never_taught() -> None:
    """AI-317 : un statement jamais enseigne se NOMME au lieu d'etre servi.

    L'`else` que ceci remplace rendait le dictionnaire a n'importe quelle
    requete. Une troisieme lecture du catalogue gouverne -- celle de
    `binding_vocabulary` sur `app.mdm_canonical_fields`, par exemple -- aurait
    recu sept colonnes et des lignes de `app.target_fields`, et le silence
    aurait tenu.
    """
    cursor = _Cursor()
    with pytest.raises(UnknownStatement) as raised:
        cursor.execute(
            "SELECT canonical_name FROM app.mdm_canonical_fields"
            " WHERE status = 'active'",
            {},
        )
    message = str(raised.value)
    # La requete qui a bouge, et l'inventaire auquel la comparer.
    assert "app.mdm_canonical_fields" in message
    assert "semantic_concepts" in message


def test_nothing_cited_means_nothing_queried() -> None:
    out = resolve(_Conn(), body_md="no reference here")
    assert out == {
        "inline": {"resolved": [], "unresolved": []},
        "tags": {"resolved": [], "unresolved": []},
    }


# --- le rendu --------------------------------------------------------------


def test_render_swaps_a_resolved_field_for_its_governed_label() -> None:
    catalog = {"cost": {"name": "cost", "display_name": "Media cost"}}
    assert render("Total {{cost}} last month", catalog) == "Total Media cost last month"


def test_render_leaves_an_unresolved_reference_visible() -> None:
    """L'effacer supprimerait la seule trace qu'un champ a disparu."""
    assert render("Total {{product}}", {}) == "Total {{product}}"


def test_render_falls_back_to_the_name_when_there_is_no_display_name() -> None:
    assert render("{{cost}}", {"cost": {"name": "cost", "display_name": None}}) == "cost"
