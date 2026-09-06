# -*- coding: utf-8 -*-
"""Le cliquet de la table des pas d'ecriture d'une fiche produit.

CE QUE CE FICHIER EMPECHE DE REVENIR. `working_context_corpus._product_sheet_steps`
lisait le motif `| n | mot | ... | ... |` sur TOUT `product-sheet.md`. Ce motif
decrit aussi la ligne 1 et la ligne 2 de chaque fiche d'exemple du document, donc
la Skill « Write a toorow product sheet » projetee dans le Context Hub portait
vingt et un pas au lieu de sept, dont `2. Audience -> navigation/governance.ts:26`
-- un pas qui envoie un modele vers un fichier de navigation au lieu de
`get_procedure`. `check_skill_targets.py` le disait par la seule de ces cibles qui
ressemblait a un chemin, et le disait depuis assez longtemps pour etre lu comme
du bruit.

POURQUOI UN TEST ET PAS SEULEMENT LA BORNE. La borne est trois lignes ; la
remettre est aussi facile que de l'enlever. Ce test appelle le lecteur REEL contre
le document REEL et refuse un pas dont la cible ressemble a un chemin : la classe
entiere, et pas la seule ligne trouvee.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"
DOC = ROOT / "docs" / "product-architecture" / "product-sheet.md"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

#: Une cible qui ressemble a un chemin du depot. C'est exactement le predicat de
#: `check_skill_targets._looks_like_path`, applique ici a la SOURCE plutot qu'a la
#: projection, pour que le refus arrive avant la seance de seed.
_PATH_SHAPED = re.compile(r"[\w./-]+\.(?:ts|tsx|py|md|json|sql)\b")


@pytest.fixture(scope="module")
def steps() -> list[dict]:
    from working_context_corpus import _product_sheet_steps  # noqa: PLC0415

    return _product_sheet_steps()


def test_the_steps_are_exactly_the_writing_steps_table(steps):
    """Sept pas, numerotes 1 a 7, et aucun doublon de numero.

    Un numero en double est la signature du defaut : deux tables differentes ont
    ete lues comme une seule.
    """
    numbers = [s["step"] for s in steps]
    assert numbers == sorted(set(numbers)), (
        "un numero de pas apparait deux fois -- le lecteur a ramasse une seconde "
        f"table : {numbers}"
    )
    assert numbers == list(range(1, len(numbers) + 1)), numbers
    assert len(steps) == 7, f"la table declare 7 pas, le lecteur en rend {len(steps)}"


def test_no_step_points_at_a_file_of_the_repository(steps):
    """Un pas agit sur un OUTIL ou sur la fiche, jamais sur un fichier du depot.

    C'est ce que la table dit d'elle-meme : « Each step names what it acts on --
    an MCP tool ... ». Une cible en `path.ts:NN` est une ligne de fiche
    d'exemple qui s'est fait passer pour un pas.
    """
    offenders = [(s["step"], s["target"]) for s in steps if _PATH_SHAPED.search(s["target"])]
    assert not offenders, (
        "un pas cite un fichier du depot comme cible -- la table des pas n'est "
        f"plus bornee a sa section : {offenders}"
    )


def test_the_bound_is_the_section_and_the_reader_says_so_when_it_is_gone(tmp_path, monkeypatch):
    """Le document sans sa section rend ZERO pas, jamais une invention.

    Un lecteur qui retomberait sur le document entier repasserait les lignes des
    fiches d'exemple : la seule reponse honnete a une section absente est aucune.
    """
    import working_context_corpus as corpus  # noqa: PLC0415

    without = DOC.read_text(encoding="utf-8").replace("## Writing steps", "## Something else")
    monkeypatch.setattr(corpus, "_read", lambda _path: without)
    assert corpus._product_sheet_steps() == []


def test_the_example_sheets_still_carry_the_rows_that_used_to_leak():
    """Le document n'a pas ete vide pour faire passer le test.

    Si les fiches d'exemple disparaissaient, les deux tests ci-dessus
    passeraient sans rien prouver -- le piege que `module-boundaries.md`
    appelle un scan vide.
    """
    page = DOC.read_text(encoding="utf-8")
    assert "| 2 | Audience |" in page
    assert page.count("| 2 | Audience |") >= 5, "les fiches d'exemple ont disparu du document"
