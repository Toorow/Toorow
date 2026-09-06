"""Conformance — une etape declaree cite un artefact qui existe.

POURQUOI. Jean, 2026-08-03 : « la page datastream, quelles sont les etapes d'un
setup complet ? Est-ce que j'ai tout ce qu'il faut pour le faire aujourd'hui ?
La reponse doit etre trouvee de maniere claire et efficace. » Avant, la reponse
demandait cinq sources -- document ratifie, ledger de completude, audit des
ecrans montes, table de navigation, puis le code -- et deux se contredisaient :
le ledger donnait `datastream` a 0/14 criteres ouverts pendant que l'audit
comptait six composants montes nulle part.

La reponse vit desormais dans une table du document ratifie, et
`scripts/surface_state.py` la resout. Ce test est ce qui empeche la table de
devenir decorative : une ligne qui cite un fichier deplace ou une route retiree
fait rougir, au lieu d'etre lue comme une garantie.

CE QUI N'EST PAS PROUVE ICI, et la page generee le dit aussi : qu'une etape
FONCTIONNE. Batie, routee, couverte -- ce n'est pas la meme chose que parcourue.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

surface_state = pytest.importorskip("surface_state")

_DATASTREAM = _REPO_ROOT / "docs" / "product-architecture" / "datastream-workbench-and-wizard.md"


def test_every_declared_step_cites_something_that_exists():
    _, problems = surface_state.build()
    assert not problems, "\n".join(problems)


def test_the_generated_page_is_not_stale():
    page, _ = surface_state.build()
    assert surface_state.OUT.exists(), "SURFACE-STATE.md n'a jamais ete genere"
    assert surface_state.OUT.read_text(encoding="utf-8") == page, (
        "SURFACE-STATE.md est perime -- `python scripts/surface_state.py` le regenere"
    )


def test_the_datastream_journey_is_fully_declared():
    """Neuf etapes au parcours cible, neuf lignes a la table."""
    steps = surface_state._steps_of(_DATASTREAM)
    assert len(steps) == 9, f"{len(steps)} etapes declarees pour un parcours cible de 9"
    assert [s.number for s in steps] == [str(n) for n in range(1, 10)]


def test_a_missing_artifact_is_detected_and_a_shipped_one_is_not():
    """Un tableau tout vert ne prouve rien s'il ne peut pas rougir."""
    assert surface_state._resolves("ui/admin/src/datastreams/wizard/wizardApi.ts")[0]
    assert not surface_state._resolves("ui/admin/src/datastreams/wizard/doesNotExist.ts")[0]
    assert surface_state._resolves(
        "POST /api/projects/{project_id}/datastream-setup-drafts")[0]
    assert not surface_state._resolves(
        "POST /api/projects/{project_id}/route-inventee")[0]
