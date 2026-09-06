"""La projection du referentiel de plateforme -- derivee, jamais inventee (AI-288).

CE QUE CES TESTS TIENNENT. Pas la liste des vingt metriques : le CATALOGUE peut
grandir, et un test qui l epingle rougirait a chaque ligne ajoutee sans rien
prouver. Ce qui est tenu, ce sont les regles de derivation, et surtout ce que la
projection REFUSE de fabriquer.

LA PLUS IMPORTANTE : `impression_weighted_average`, `impression_weighted` et
`weighted_ratio` ne sont pas dans `AGGREGATION_FUNCTIONS`. Les approximer en
`average` declarerait sommable-par-moyenne une metrique qui ne l est pas -- c est
exactement ce que le garde AD-4 existe pour empecher. Elles sont NON ADDITIVES et
portent `aggregation = NULL`, et la formule ponderee reste dans le mart, ou elle
est deja juste.
"""

from __future__ import annotations

import pytest
from core.platform_semantic_concepts import (
    PlatformConcept,
    classify_divergences,
    declared_ratios,
    project_catalogue,
    project_delivered_catalogue,
    unresolved_operand_names,
    withheld_from_platform_mint,
)

MONEY = frozenset({"cost", "revenue"})


def _row(name, additive, rule, numerator="", denominator=""):
    return {
        "name": name,
        "additive": additive,
        "aggregation_rule": rule,
        "ratio_numerator": numerator,
        "ratio_denominator": denominator,
    }


def _by_name(concepts):
    return {concept.name: concept for concept in concepts}


# ---------------------------------------------------------------------------
# La derivation
# ---------------------------------------------------------------------------


def test_a_ratio_carries_the_typed_tree_migration_142_writes():
    """La FORME compte : une autre ferait diverger les concepts migres des projetes."""
    concepts, refused = project_catalogue(
        [_row("cpa", "false", "ratio", "cost", "conversions")], money=MONEY
    )
    assert refused == []
    cpa = _by_name(concepts)["cpa"]
    assert cpa.expression == {
        "op": "ratio",
        "zero_denominator": "null",
        "numerator": {"op": "concept_name", "name": "cost"},
        "denominator": {"op": "concept_name", "name": "conversions"},
    }
    # AD-4 : un ratio ne se somme pas, et le mart le dit deja en le calculant en vue.
    assert cpa.additivity_class == "non_additive"
    assert cpa.aggregation is None
    assert cpa.value_type == "ratio"


def test_a_weighted_rule_is_never_approximated_into_an_average():
    """LE REFUS QUI COMPTE.

    `average` est une fonction du contrat ; `impression_weighted_average` ne l est
    pas. Traduire la seconde par la premiere declarerait une facon de rouler la
    metrique que personne n a choisie -- et l AD-4 guard existe pour empecher
    exactement ce genre de somme.
    """
    concepts, refused = project_catalogue(
        [
            _row("average_position", "false", "impression_weighted_average"),
            _row("average_frequency", "false", "impression_weighted"),
            _row("viewability_rate", "false", "weighted_ratio"),
        ],
        money=MONEY,
    )
    assert refused == []
    for name in ("average_position", "average_frequency", "viewability_rate"):
        concept = _by_name(concepts)[name]
        assert concept.additivity_class == "non_additive", name
        # `None`, jamais `{"function": "average"}`.
        assert concept.aggregation is None, name


def test_a_non_additive_rule_the_contract_DOES_know_keeps_its_function():
    """`max` est une fonction du contrat : la perdre serait l autre erreur."""
    concepts, _ = project_catalogue([_row("unique_reach", "false", "max")], money=MONEY)
    concept = _by_name(concepts)["unique_reach"]
    assert concept.additivity_class == "non_additive"
    assert concept.aggregation == {"function": "max"}


def test_an_additive_rule_the_contract_cannot_write_is_REFUSED_by_name():
    """Une metrique additive DOIT dire comment elle s agrege.

    Sans agregation elle serait publiee non-additive, ce qui est une affirmation
    que le catalogue ne fait pas. Refuser par le nom laisse la decision a une
    personne.
    """
    concepts, refused = project_catalogue(
        [_row("some_metric", "true", "impression_weighted")], money=MONEY
    )
    assert concepts == []
    assert len(refused) == 1
    assert "some_metric" in refused[0] and "impression_weighted" in refused[0]


def test_a_ratio_without_its_operands_is_refused_rather_than_half_built():
    concepts, refused = project_catalogue([_row("broken", "false", "ratio")], money=MONEY)
    assert concepts == []
    assert "la formule n est ecrite nulle part" in refused[0]


def test_the_type_cascade_is_the_one_the_applied_migration_settled():
    """ratio > argent > dictionnaire > decimal. Reprendre un autre ordre ferait
    diverger les concepts migres de ceux provisionnes."""
    concepts, _ = project_catalogue(
        [
            _row("cost", "true", "sum"),
            _row("clicks", "true", "sum"),
            _row("installs", "true", "sum"),
        ],
        money=MONEY,
        dictionary={"clicks": "integer", "cost": "decimal"},
    )
    by_name = _by_name(concepts)
    # L argent gagne sur le dictionnaire : `cost` est monetaire, quoi qu il dise.
    assert by_name["cost"].value_type == "money"
    assert by_name["clicks"].value_type == "integer"
    # Et le repli est `decimal`, celui que la migration appliquee a deja pose.
    assert by_name["installs"].value_type == "decimal"


def test_the_delivered_catalogue_projects_without_a_single_refusal():
    """Si une ligne du seed livre devient inexprimable, c est ICI qu on le voit."""
    concepts, refused = project_delivered_catalogue()
    assert refused == [], "\n".join(refused)
    assert len(concepts) >= 20
    # Les trois ratios que la garde de parite surveille sont bien la.
    assert {"roas", "ctr", "cpa"} <= {concept.name for concept in concepts}


# ---------------------------------------------------------------------------
# Le semis de la migration, distingue d une declaration humaine
# ---------------------------------------------------------------------------


_PROJECTED = PlatformConcept(
    name="average_position",
    value_type="decimal",
    expression={"op": "source_measure", "concept": "average_position"},
    additivity_class="non_additive",
    aggregation=None,
)


def _stored(**overrides):
    base = {
        "version_id": "scv_X",
        "value_type": "decimal",
        "additivity_class": "additive",
        "expression": {"op": "source_measure", "concept": "average_position"},
        "aggregation": {"function": "sum"},
        "untouched_seed": True,
    }
    base.update(overrides)
    return {"average_position": base}


def test_an_untouched_migration_seed_is_told_apart_from_a_human_declaration():
    """LA MESURE DU 2026-08-17, et pourquoi elle decide du traitement.

    Le semis de la migration 142 s est fait contre une table VIDE :
    `app.metric_definitions` est peuplee par l APPLICATION, pas par une migration,
    donc la derivation retombait sur son `ELSE 'additive'`. Resultat mesure sur un
    cluster fraichement migre : `average_position` -- non additive dans
    `dim_metric.csv` ET dans `metric_definitions` -- est gouvernee `additive` avec
    `aggregation = sum`, et le Modele Semantique GAGNE la precedence.

    Ce n est pas une decision qu on ecraserait : c est un defaut d ordre.
    """
    seeded, declared = classify_divergences(_stored(), [_PROJECTED])
    assert declared == []
    assert len(seeded) == 2
    assert any("additivity_class" in line for line in seeded)
    assert any("aggregation" in line for line in seeded)


def test_a_version_somebody_published_is_reported_and_never_repaired():
    """Une declaration humaine reste une divergence : rapportee, jamais resolue."""
    seeded, declared = classify_divergences(_stored(untouched_seed=False), [_PROJECTED])
    assert seeded == []
    assert len(declared) == 2


def test_a_concept_that_agrees_produces_no_line_at_all():
    agreeing = _stored(additivity_class="non_additive", aggregation=None)
    assert classify_divergences(agreeing, [_PROJECTED]) == ([], [])


def test_a_concept_with_no_version_is_not_judged():
    """Une tete sans version pointee n est pas une contradiction, c est un trou."""
    assert classify_divergences(_stored(version_id=None), [_PROJECTED]) == ([], [])


@pytest.mark.parametrize("name", ["roas", "ctr", "cpa"])
def test_every_wired_ratio_is_projected_as_a_governable_concept(name):
    """Le point de tout ceci : la troisieme copie de la formule EXISTE desormais.

    Sans elle, `check_metric_formula_parity` ne pouvait comparer que deux copies
    et le disait a chaque run.
    """
    concepts, _ = project_delivered_catalogue()
    concept = _by_name(concepts)[name]
    assert concept.expression["op"] == "ratio"
    assert concept.expression["numerator"]["op"] == "concept_name"


# ---------------------------------------------------------------------------
# Publiable a la portee PLATEFORME -- le predicat, et la phrase qu il produit.
# ---------------------------------------------------------------------------


def test_a_source_measure_is_publishable_and_a_named_operand_is_not():
    """Ce qui interdit la publication n est pas d etre un RATIO.

    C est de porter un operande que rien n epingle a une version exacte -- lu par
    le nom du noeud, sur tout l arbre, et non par le mot `ratio` : une future
    forme composee qui nommerait un operande serait aussi impubliable.
    """
    concepts = _by_name(project_delivered_catalogue()[0])
    assert concepts["clicks"].publishable_at_platform_scope is True
    assert concepts["clicks"].unresolved_operands == []
    for name in ("roas", "ctr", "cpa"):
        assert concepts[name].publishable_at_platform_scope is False

    nested = {
        "op": "add",
        "left": {"op": "source_measure", "concept": "a"},
        "right": {"op": "ratio", "numerator": {"op": "concept_name", "name": "buried"},
                  "denominator": {"op": "source_measure", "concept": "b"}},
    }
    assert unresolved_operand_names(nested) == ["buried"]


def test_the_withholding_sentence_names_the_division_in_the_ORDER_it_is_written():
    """Une phrase qui nomme la mauvaise division est pire qu une absence de phrase.

    Ecrite depuis `unresolved_operands` -- un ensemble TRIE -- elle disait
    << roas divise cost par revenue >>, soit l inverse du catalogue. Elle lit
    desormais l arbre dans son ordre.
    """
    concepts = _by_name(project_delivered_catalogue()[0])
    for name, (numerator, denominator) in declared_ratios().items():
        sentence = withheld_from_platform_mint(concepts[name])
        assert sentence.startswith(f"{name}: adopt it in a Project.")
        assert f"divides {numerator} by {denominator}" in sentence
        #  Le geste d abord, la cause ensuite -- jamais l inverse.
        assert sentence.index("adopt it in a Project") < sentence.index("unpublishable")
