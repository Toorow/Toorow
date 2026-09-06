"""Une cible canonique se classe, ou elle se refuse -- et le cliquet a des dents.

« Maximiser la classification par enforcement, justement pour une meilleure
comprehension et automatisation » (Jean, 2026-08-15). Ce qui n'est pas classe ne
peut etre ni compris ni automatise : un champ dont personne n'a dit s'il est une
mesure ou une dimension est somme par le premier qui le lit.

Deux directions comptent, et un cliquet qui n'en tient qu'une est une decoration :
une declaration NEUVE non classee doit rougir, et une ligne de la liste qui n'a
plus de raison d'etre doit rougir aussi -- sinon la liste grossit et personne ne
la vide jamais.

Offline : les manifestes du depot, et un repertoire synthetique pour les mutations.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "check_canonical_target_classification.py"


def _guard():
    spec = importlib.util.spec_from_file_location("check_canonical_target_classification", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest(tmp_path: Path, module_name: str, field: dict) -> Path:
    folder = tmp_path / module_name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.json").write_text(
        json.dumps({"name": module_name, "fields": [field]}), encoding="utf-8"
    )
    return tmp_path


def test_the_repository_carries_no_unclassified_target_the_list_does_not_name():
    """La porte elle-meme, sur le depot : aucune NEUVE, aucune ligne perimee."""
    guard = _guard()
    found = set(guard.unclassified())

    assert found - guard.UNCLASSIFIED_AT_2026_08_15 == set(), (
        "une cible canonique neuve n'est pas classee -- la classer, ou l'inscrire "
        "avec sa raison dans le script"
    )
    assert guard.UNCLASSIFIED_AT_2026_08_15 - found == set(), (
        "une cible inscrite est classee depuis : retirer sa ligne, le cliquet "
        "tourne dans un seul sens"
    )


def test_the_overwhelming_majority_is_classified():
    """Le chiffre qui dit si la doctrine tient : 266 sur 286 au 2026-08-15."""
    from core.platform_canonical_vocabulary import connector_declarations

    declared = connector_declarations()
    unclassified = _guard().unclassified()

    assert len(declared) > 200
    assert len(unclassified) < len(declared) // 10


def test_a_new_unclassified_declaration_is_refused(tmp_path):
    """La mutation : un manifeste neuf qui ne dit pas comment son champ s'agrege."""
    from core.platform_canonical_vocabulary import connector_declarations, project_connectors

    modules = _manifest(
        tmp_path,
        "acme-ads",
        {
            "field_id": "widget_score",
            "source_field": "widget_score",
            "kind": "metric",
            "physical_type": "integer",
            "canonical_target": "widget_score",
            "aggregation": None,
            "non_additive": False,
        },
    )
    fields, refused = project_connectors(connector_declarations(modules), governed=[])

    assert fields == []
    assert len(refused) == 1
    assert "widget_score" in refused[0]
    assert "summed by whatever read it first" in refused[0]


def test_two_manifests_that_classify_the_same_target_differently_are_refused(tmp_path):
    """Le cas `campaign_id`, reproduit en petit : meme nom, deux formes."""
    from core.platform_canonical_vocabulary import connector_declarations, project_connectors

    base = {
        "field_id": "campaign_ref",
        "source_field": "campaign_ref",
        "kind": "dimension",
        "canonical_target": "campaign_ref",
        "aggregation": None,
        "non_additive": False,
    }
    _manifest(tmp_path, "acme-one", {**base, "physical_type": "string"})
    modules = _manifest(tmp_path, "acme-two", {**base, "physical_type": "integer"})

    fields, refused = project_connectors(connector_declarations(modules), governed=[])

    assert fields == []
    assert "campaign_ref" in refused[0] and "disagree" in refused[0]


def test_a_classified_declaration_passes_and_carries_its_classification(tmp_path):
    """Le sens positif : une declaration complete traverse, telle qu'elle est ecrite."""
    from core.platform_canonical_vocabulary import connector_declarations, project_connectors

    modules = _manifest(
        tmp_path,
        "acme-ads",
        {
            "field_id": "widget_score",
            "source_field": "widget_score",
            "kind": "metric",
            "physical_type": "decimal",
            "canonical_target": "widget_score",
            "aggregation": "sum",
            "non_additive": False,
        },
    )
    fields, refused = project_connectors(connector_declarations(modules), governed=[])

    assert refused == []
    assert len(fields) == 1
    assert fields[0].value_type == "decimal"
    assert fields[0].aggregation == "sum"
    assert fields[0].non_additive is False
