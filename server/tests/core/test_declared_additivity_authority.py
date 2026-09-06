"""Deux magasins declarent une metrique. Lequel gagne, et pourquoi ce test existe.

AUDIT MDM & GOUVERNANCE, 2026-08-14. `app.semantic_concept_versions` et
`app.metric_definitions` declarent tous deux le comportement d'une metrique. La
migration 142 annoncait leur reconciliation ; les deux sont restes vivants. La
PRECEDENCE etait implementee dans `resolve_declared_additivity` -- le Semantic
Model d'abord, `metric_definitions` en dessous -- et ecrite dans aucun document
ratifie, donc defendue par aucun test.

Une precedence sans test n'est pas une regle, c'est un ordre de lecture qu'un
refactor peut inverser sans que rien ne rougisse. Et l'inverser a un cout precis :
une definition curee par MCP ferait taire une classe d'additivite PUBLIEE, et une
metrique declaree non-additive dans le workbench redeviendrait sommable.

Ces tests sont OFFLINE : les deux lectures sont remplacees, ce qui est teste est
l'arbitrage lui-meme.
"""

from __future__ import annotations

import pytest
from core import metric_semantics


@pytest.fixture
def stores(monkeypatch):
    """Remplace les deux lectures ; rend deux setters pour les garnir."""
    state: dict[str, object] = {"semantic": [], "definitions": {}}

    monkeypatch.setattr(
        metric_semantics,
        "_load_declared_additivity_rows",
        lambda project_id: list(state["semantic"]),
    )
    monkeypatch.setattr(
        metric_semantics,
        "resolve_metric_definitions",
        lambda project_id: dict(state["definitions"]),
    )
    return state


def test_the_semantic_model_wins_over_a_curated_definition(stores):
    """Le workbench a publie `non_additive` ; le MCP dit `additive`. Le workbench gagne."""
    stores["semantic"] = [("efficiency_index", "non_additive")]
    stores["definitions"] = {"efficiency_index": {"additive": True}}

    declared = metric_semantics.resolve_declared_additivity("proj_EXAMPLE")

    assert declared["efficiency_index"] == "non_additive"
    #  Et la consequence, qui est le vrai enjeu : la metrique reste non sommable.
    assert "efficiency_index" in metric_semantics.declared_non_additive(declared)


def test_a_definition_answers_for_a_metric_the_semantic_model_does_not_carry(stores):
    """Ce n'est pas un avis concurrent : c'est la couche en dessous."""
    stores["semantic"] = []
    stores["definitions"] = {
        "cost": {"additive": True},
        "stored_balance": {"additive": True, "non_additive_dimensions": ["date"]},
        "bounce_rate": {"additive": False},
    }

    declared = metric_semantics.resolve_declared_additivity("proj_EXAMPLE")

    assert declared["cost"] == "additive"
    assert declared["stored_balance"] == "semi_additive"
    assert declared["bounce_rate"] == "non_additive"


def test_a_metric_nobody_declared_is_absent_rather_than_guessed(stores):
    """L'appelant garde son defaut de plateforme plutot qu'une reponse deguisee."""
    stores["semantic"] = []
    stores["definitions"] = {"cost": {"additive": None}}

    assert metric_semantics.resolve_declared_additivity("proj_EXAMPLE") == {}


def test_an_unreadable_store_never_widens_the_answer(stores, monkeypatch):
    """Fail-soft : on degrade vers les defauts, jamais vers une permission."""

    def boom(project_id):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(metric_semantics, "_load_declared_additivity_rows", boom)
    stores["definitions"] = {"bounce_rate": {"additive": False}}

    declared = metric_semantics.resolve_declared_additivity("proj_EXAMPLE")

    #  Le magasin qui repond encore repond ; celui qui est tombe ne rend rien --
    #  et surtout pas `additive` par defaut.
    assert declared == {"bounce_rate": "non_additive"}


def test_semi_additive_is_on_the_refusal_side_of_the_line(stores):
    """« Sommable sur CERTAINES dimensions » n'est pas « sommable »."""
    stores["semantic"] = [("stored_balance", "semi_additive"), ("cost", "additive")]

    declared = metric_semantics.resolve_declared_additivity("proj_EXAMPLE")
    non_additive = metric_semantics.declared_non_additive(declared)

    assert "stored_balance" in non_additive
    assert "cost" not in non_additive


def test_no_project_reads_nothing(stores):
    """Sans projet, aucune declaration ne s'applique -- et aucune lecture n'est tentee."""
    stores["semantic"] = [("efficiency_index", "non_additive")]

    assert metric_semantics.resolve_declared_additivity(None) == {}
    assert metric_semantics.resolve_declared_additivity("") == {}
