"""La formule d'un ratio est ecrite TROIS fois -- elles doivent dire la meme chose.

Une seule implementation (`scripts/check_metric_formula_parity.py`), deux portes :
celle-ci pour le run local, `make check-metric-formula-parity` pour la CI. Une
regle qui vit dans deux codes finit par vivre dans deux verites, ce qui est
exactement le defaut qu'elle surveille.

La troisieme copie vit en base, donc elle ne se teste pas comme un fichier : les
tests ci-dessous exercent les fonctions PURES qui la lisent et la confrontent
(`expression_operands`, `stored_divergences`, `platform_ratio_expressions`) sur des
lignes construites a la main. Ce qui est prouve ici, c'est que la garde SAIT lire
un ratio publie et SAIT le juger -- une garde qui ne lirait que la forme non
resolue serait verte et aveugle a tout ratio qui sert vraiment.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "check_metric_formula_parity.py"


def _module():
    spec = importlib.util.spec_from_file_location("check_metric_formula_parity", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_declared_ratio_is_computed_on_the_components_it_declares():
    """`dim_metric.csv` declare, `semantic_<metric>.sql` calcule -- jamais deux avis."""
    problems = _module().divergences()
    assert problems == [], "\n".join(problems)


def test_the_three_wired_ratios_are_still_the_ones_the_catalogue_names():
    """Une regression qui VIDE le seed rendrait le test ci-dessus vert pour rien."""
    ratios = _module().declared_ratios()
    assert ratios == {
        "cpa": ("cost", "conversions"),
        "ctr": ("clicks", "impressions"),
        "roas": ("revenue", "cost"),
    }


# --- la troisieme copie : la formule gouvernee -------------------------------


def test_the_platform_reference_is_the_tree_migration_142_writes():
    """Le chemin versionne du referentiel plateforme, DERIVE du catalogue livre.

    La forme compte : c'est celle que `142_semantic_model.sql:825-835` ecrit pour
    un ratio migre. Une projection d'une autre forme serait une seconde liste, pas
    une reference -- exactement ce que la garde surveille.
    """
    projected = _module().platform_ratio_expressions()
    assert projected["cpa"] == {
        "op": "ratio",
        "zero_denominator": "null",
        "numerator": {"op": "concept_name", "name": "cost"},
        "denominator": {"op": "concept_name", "name": "conversions"},
    }
    assert set(projected) == {"cpa", "ctr", "roas"}


def test_operands_are_read_through_the_resolved_shape_a_published_ratio_carries():
    """Un ratio PUBLIE porte `concept_ref`, pas `concept_name`.

    C'est le test qui empeche la garde d'etre verte et aveugle : ne lire que la
    forme non resolue la rendrait muette sur tout ratio reellement servi.
    """
    module = _module()
    published = {
        "op": "ratio",
        "zero_denominator": "null",
        "numerator": {"op": "concept_ref", "concept_id": "sc_A", "version_id": "scv_A1"},
        "denominator": {"op": "concept_ref", "concept_id": "sc_B", "version_id": "scv_B1"},
    }
    names = {"scv_A1": "cost", "scv_B1": "conversions"}
    assert module.expression_operands(published, names) == ("cost", "conversions")


def test_operands_are_read_through_the_unresolved_shape_the_migration_wrote():
    module = _module()
    migrated = {
        "op": "ratio",
        "numerator": {"op": "concept_name", "name": "clicks"},
        "denominator": {"op": "concept_name", "name": "impressions"},
    }
    assert module.expression_operands(migrated, {}) == ("clicks", "impressions")


def test_an_expression_that_is_not_a_ratio_is_not_judged_as_one():
    module = _module()
    assert module.expression_operands({"op": "source_measure", "concept": "cost"}) is None
    assert module.expression_operands(None) is None


def test_a_platform_concept_that_contradicts_the_catalogue_is_a_divergence():
    """Le meme objet dit deux choses : c'est la faute que la garde existe pour voir."""
    module = _module()
    stored = [
        {
            "id": "scv_PLATFORM",
            "project_id": None,
            "name": "cpa",
            "numerator": "cost",
            "denominator": "clicks",  # le catalogue dit `conversions`
        }
    ]
    problems, overrides = module.stored_divergences(stored)
    assert overrides == []
    assert len(problems) == 1
    assert "PLATEFORME" in problems[0]
    assert "clicks" in problems[0] and "conversions" in problems[0]


def test_a_project_concept_that_redefines_a_ratio_is_an_override_not_a_fault():
    """Le client a le droit de sa definition. Ce qui doit se voir, c'est que le
    mart n'implemente PAS la sienne -- pas que le client a tort."""
    module = _module()
    stored = [
        {
            "id": "scv_CLIENT",
            "project_id": "proj_EXAMPLE",
            "name": "cpa",
            "numerator": "cost",
            "denominator": "leads",
        }
    ]
    problems, overrides = module.stored_divergences(stored)
    assert problems == []
    assert len(overrides) == 1
    assert "proj_EXAMPLE" in overrides[0]
    assert "n'implemente PAS" in overrides[0]


def test_a_governed_ratio_whose_operands_cannot_be_read_is_reported_not_ignored():
    """Silence sur une formule illisible = une formule jamais confrontee."""
    module = _module()
    stored = [
        {
            "id": "scv_OPAQUE",
            "project_id": None,
            "name": "roas",
            "numerator": None,
            "denominator": None,
        }
    ]
    problems, _ = module.stored_divergences(stored)
    assert len(problems) == 1
    assert "ne se lisent pas par nom" in problems[0]


def test_a_governed_ratio_that_agrees_with_the_catalogue_says_nothing():
    module = _module()
    stored = [
        {
            "id": "scv_OK",
            "project_id": None,
            "name": "roas",
            "numerator": "revenue",
            "denominator": "cost",
        }
    ]
    assert module.stored_divergences(stored) == ([], [])
