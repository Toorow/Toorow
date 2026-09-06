"""Conformance — ce qu'on attend d'un ecran est declare, et son etat est calcule.

POURQUOI. Un referentiel ecran-par-ecran produit par lecture de documentation
avait la bonne forme et un contenu invérifiable : routes inexistantes, index de
26 fichiers dont aucun n'existe, verdicts « conforme » que la mesure contredit.
La forme etait juste ; ce qui manquait etait la tracabilite.

`screens/expectations.json` DECLARE ce qu'on attend de chaque ecran, avec la
preuve qui le rendrait vrai. `scripts/screen_expectations.py` RESOUT ces preuves
contre le depot. Ce test empeche la declaration de devenir decorative.

CE QU'IL NE FAIT PAS : echouer parce qu'une fonction manque. Une attente non
livree est le travail restant -- c'est le but de l'ecrire. Le test echoue quand
la DECLARATION ment : un ecran qui n'existe plus, un type de preuve inconnu, ou
une page generee qui ne correspond plus au depot.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

expectations = pytest.importorskip("screen_expectations")


def test_no_declaration_is_broken():
    _, broken, _tally = expectations.build()
    assert not broken, "\n".join(broken)


def test_the_generated_page_is_not_stale():
    page, _broken, _tally = expectations.build()
    assert expectations.OUT.exists(), "screens/expectations.md n'a jamais ete genere"
    assert expectations.OUT.read_text(encoding="utf-8") == page, (
        "screens/expectations.md est perime -- "
        "`python scripts/screen_expectations.py` le regenere"
    )


def test_every_declared_screen_exists_on_the_board():
    # Le board passe par son proprietaire (`screens.load_board`), qui refuse de
    # servir un cache plus vieux que ses sources depuis le 2026-08-21.
    board = set(expectations.board_by_id())
    declared = {k for k in json.loads(
        expectations.DECLARATIONS.read_text(encoding="utf-8")) if not k.startswith("_")}
    assert declared <= board, f"ecrans declares hors board : {sorted(declared - board)}"


def test_every_expectation_uses_a_known_proof():
    declared = json.loads(expectations.DECLARATIONS.read_text(encoding="utf-8"))
    unknown = {
        e["proof"]
        for key, entry in declared.items() if not key.startswith("_")
        for e in entry["expects"] if e["proof"] not in expectations.PROOFS
    }
    assert not unknown, f"types de preuve inconnus : {sorted(unknown)}"


def test_the_route_detector_sees_a_composed_path():
    """Controle negatif du piege qui a fait mentir la premiere version.

    `analyze/renders` est servie par `Route(f"{_BASE}/renders", ...)`. Un
    detecteur qui ne lit que les chemins litteraux rend « aucune route » -- il
    aurait fait dire a la page qu'une route absente est la cause, alors que
    l'ecran est le seul fautif.
    """
    assert expectations._server_serves(
        "POST /api/projects/{project_id}/analyze/renders"), (
        "la route composee n'est plus vue : le detecteur a regresse"
    )
    assert not expectations._server_serves(
        "POST /api/projects/{project_id}/analyze/route-inventee")


def test_a_missing_function_is_reported_without_failing_the_gate():
    """Le coeur du contrat : montrer le manque n'est pas echouer."""
    page, broken, _tally = expectations.build()
    assert "MANQUE" in page, (
        "aucune attente non livree -- soit tout est fait, soit la resolution est cassee"
    )
    assert not broken


def test_every_journey_step_points_at_a_declared_screen_and_expectation():
    """Un parcours qui cite une attente inexistante ne mesure rien.

    C'est la seule facon dont un chemin transverse peut mentir : declarer une
    etape « verte » parce que son attente n'a jamais ete resolue.
    """
    _, broken, _tally = expectations.build()
    assert not [b for b in broken if b.startswith("parcours")], "\n".join(broken)


def test_a_journey_stops_at_its_first_broken_step(tmp_path, monkeypatch):
    """Controle negatif synthetique du calcul du premier arret.

    Le parcours a un maillon casse en position 2 et un maillon livre apres lui.
    Compter les lignes rendues ou recalculer depuis le resultat de ``build`` ne
    prouverait rien ; cette entree volontairement cassee fixe l'oracle a 2.
    """
    board = {
        "screens": [
            {"id": "start", "title": "Start", "route": "/start", "mounted_at": "App"},
            {"id": "broken", "title": "Broken", "route": "/broken", "mounted_at": None},
            {"id": "after", "title": "After", "route": "/after", "mounted_at": "App"},
        ]
    }
    declared = {
        screen_id: {
            "mission": f"Synthetic {screen_id}",
            "expects": [{"what": "Etre monte", "proof": "mounted", "value": "route"}],
        }
        for screen_id in ("start", "broken", "after")
    }
    journeys = {
        "synthetic": {
            "name": "Synthetic broken journey",
            "why": "Prove first-stop calculation",
            "steps": [
                {"step": "Start", "screen": "start", "expectation": "Etre monte"},
                {"step": "Break", "screen": "broken", "expectation": "Etre monte"},
                {"step": "After", "screen": "after", "expectation": "Etre monte"},
            ],
            "proves_gates": [],
        }
    }
    paths = {
        "DECLARATIONS": ("expectations.json", declared),
        "JOURNEYS": ("journeys.json", journeys),
    }
    monkeypatch.setattr(
        expectations, "board_by_id",
        lambda: {screen["id"]: screen for screen in board["screens"]},
    )
    for attribute, (filename, payload) in paths.items():
        target = tmp_path / filename
        target.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(expectations, attribute, target)
    monkeypatch.setattr(expectations, "qa_gates", lambda: ({}, [], "2026-08-18"))

    page, broken, tally = expectations.build()

    assert broken == []
    assert tally["_journeys"] == {"synthetic": 2}
    assert "Le parcours s'arrete a l'etape 2" in page
    assert "| 3 | After | `after` | OK |" in page


def test_the_executed_journey_tracker_is_controlled_too():
    """Les deux mesures sont gardees, pas seulement la derivee.

    `bati` se derive du depot ; `parcouru` est tenu a la main, donc il ment plus
    facilement. La garde refuse un `pass` ou un `fail` sans date, sans rapport,
    ou dont le rapport n'existe pas -- un vert qui ne prouve rien.
    """
    gates, problems, dated = expectations.qa_gates()
    assert gates, "le tracker de parcours execute a disparu ou n'a plus de portes"
    assert not problems, "\n".join(problems)
    assert dated and dated != "None", "le tracker n'a plus de date de releve"


def test_a_pass_without_its_report_is_caught():
    """Controle negatif : une garde qui ne peut pas rougir est du decor."""
    import tempfile
    from pathlib import Path as _Path

    import yaml

    payload = yaml.safe_load(expectations.QA_JOURNEY.read_text(encoding="utf-8"))
    passing = next(k for k, g in payload["gates"].items()
                   if str(g.get("status")) == "pass")
    payload["gates"][passing]["report"] = "_bmad-output/qa/runs/inexistant.json"
    fake = _Path(tempfile.mkdtemp()) / "qa.yaml"
    fake.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")

    real = expectations.QA_JOURNEY
    expectations.QA_JOURNEY = fake
    try:
        _, problems, _ = expectations.qa_gates()
    finally:
        expectations.QA_JOURNEY = real
    assert any("rapport absent" in p for p in problems)


def test_every_journey_names_the_gate_that_would_prove_it():
    """Un parcours sans porte declaree ne peut jamais passer de bati a vecu."""
    import json as _json

    journeys = _json.loads(expectations.JOURNEYS.read_text(encoding="utf-8"))
    gates, _, _ = expectations.qa_gates()
    for key, journey in journeys.items():
        if key.startswith("_"):
            continue
        claimed = journey.get("proves_gates")
        assert claimed, f"parcours `{key}` ne nomme aucune porte d'execution"
        unknown = [g for g in claimed if g not in gates]
        assert not unknown, f"parcours `{key}` cite des portes inconnues : {unknown}"
