"""Conformance — un concept remplace ne se cite pas comme s'il etait la cible.

POURQUOI CE FICHIER EXISTE. Constat de Jean, 2026-08-03, sur une phrase que je
venais d'ecrire moi-meme : « ca prouve tres bien que tu utilise un concept faux
qui a ete rectifie pub/sub + cloud schedule configurable ». J'avais ecrit, dans
le document ratifie du Datastream, que la recurrence dependait des « quatre
conditions de la boucle nocturne » -- le modele du thread en processus, remplace
le 2026-07-31 par AD-36 : Cloud Scheduler pour l'horloge, Cloud Tasks pour les
unites de travail, Pub/Sub pour les faits.

Je ne l'ai pas invente : je l'ai repris d'un action item ecrit avant la
ratification. C'est exactement la mecanique du defaut -- un concept remplace
survit dans les traces, et la trace se lit comme une cible.

LA REGLE QUE CE TEST POSE. Un concept remplace peut etre NOMME -- expliquer une
transition l'exige -- mais jamais sans que son remplacant soit nomme dans le
meme fichier. Un lecteur qui tombe sur « boucle nocturne » doit voir, sans
chercher, ce qui l'a remplacee.

COMMENT EN AJOUTER UN. Une entree dans `SUPERSEDED` : le motif du concept
retire, la chaine qui prouve que le remplacant est nomme, la date et le lieu de
la ratification. Ne jamais y mettre un concept « qu'on n'aime plus » : seulement
un qui a ete RATIFIE ailleurs, avec le document qui le dit.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parents[3]
_DOCS = _REPO_ROOT / "docs"


class Superseded:
    def __init__(self, name: str, pattern: str, replacement: str, ratified: str, allow: set[str]):
        self.name = name
        self.pattern = re.compile(pattern, re.IGNORECASE)
        self.replacement = replacement
        self.ratified = ratified
        self.allow = allow


SUPERSEDED = [
    Superseded(
        name="la boucle nocturne en processus comme MODELE d'execution",
        pattern=r"nightly loop|boucle nocturne|in-process nightly scheduler",
        replacement="Cloud Scheduler",
        ratified="AD-36, docs/product-architecture/execution-substrate.md, ratifie 2026-07-31",
        # Le document qui PORTE la decision doit pouvoir decrire ce qu'il remplace.
        allow={"execution-substrate.md", "changelog.mdx"},
    ),
]


@pytest.mark.parametrize("concept", SUPERSEDED, ids=lambda c: c.name)
def test_a_superseded_concept_never_appears_without_its_replacement(concept: Superseded):
    offenders: list[str] = []
    for path in sorted(_DOCS.rglob("*.md")) + sorted(_DOCS.rglob("*.mdx")):
        if path.name in concept.allow:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if concept.pattern.search(text) and concept.replacement not in text:
            hit = next(
                line.strip()
                for line in text.splitlines()
                if concept.pattern.search(line)
            )
            offenders.append(f"{path.relative_to(_REPO_ROOT)} : {hit[:120]}")
    assert not offenders, (
        f"Concept remplace cite sans son remplacant ({concept.replacement}).\n"
        f"  Ratifie : {concept.ratified}\n  "
        + "\n  ".join(offenders)
    )


def test_the_registry_points_at_documents_that_exist():
    """Une entree qui cite un document introuvable ne prouve plus rien."""
    for concept in SUPERSEDED:
        doc = re.search(r"docs/[\w/.-]+\.mdx?", concept.ratified)
        assert doc, f"{concept.name} : aucune ratification citee"
        assert (_REPO_ROOT / doc.group(0)).is_file(), (
            f"{concept.name} cite {doc.group(0)}, qui n'existe pas"
        )


@pytest.mark.parametrize("concept", SUPERSEDED, ids=lambda c: c.name)
def test_the_guard_can_actually_go_red(concept: Superseded):
    """Controle negatif : un garde qui ne peut pas rougir est du decor.

    Le texte fautif est synthetique -- on ne salit pas le depot pour prouver
    qu'une porte se ferme.
    """
    guilty = f"La recurrence depend de {concept.pattern.pattern.split('|')[0]}."
    innocent = guilty + f" Elle passe desormais par {concept.replacement}."
    assert concept.pattern.search(guilty) and concept.replacement not in guilty
    assert concept.pattern.search(innocent) and concept.replacement in innocent
