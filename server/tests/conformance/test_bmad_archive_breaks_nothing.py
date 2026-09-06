"""Conformance — rien dans l'archive n'est cite par un document vivant.

POURQUOI CE FICHIER EXISTE, et il est ne d'une faute que j'ai commise.

La regle de l'archive etait bonne : « on n'ecarte que ce que personne ne cite ».
L'INSTRUMENT etait faux. J'avais compte les citations avec `git grep -o`, qui ne
rend qu'UNE occurrence par ligne : un fichier cite sur une ligne ou un autre nom
apparaissait d'abord comptait pour zero. Vingt-sept stories sont donc parties a
l'archive alors qu'un document vivant les nommait -- dont plusieurs citees depuis
`server/`, c'est-a-dire depuis du CODE :

    server/modules/shopify/connector.py            -> 15-4-shopify.md
    server/tests/core/test_google_token_store.py   -> 18-1-encrypted-token-store.md
    server/modules/youtube-analytics/...            -> 30-2-connecteur-youtube-analytics.md

La deuxieme erreur a ete de mesurer la reparation avec un `git grep` dont le
pathspec `:!_bmad-output/archive` n'est pas supporte par ce git : la commande
echouait, stderr n'etait pas lu, et le script annonçait serieusement « 0 fichier
cite ». Un instrument qui echoue en silence rend le resultat qu'on espere
(CLAUDE.md, ne pas mesurer avec un instrument que je pollue).

Ce test refait la mesure a l'endroit : on lit les fichiers, on n'exclut rien par
pathspec, et le code de retour de `git grep` est verifie.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parents[3]
_ARCHIVE = _REPO_ROOT / "_bmad-output" / "archive"


def _archived_names() -> list[str]:
    """Les noms ECARTES, et seulement eux.

    Un nom porte ailleurs dans le depot -- `README.md` en tete -- ne peut pas
    servir de sonde : il matcherait partout et rendrait un faux positif. La
    premiere version de ce test a echoue exactement la-dessus, sur le README que
    j'avais ecrit dans l'archive elle-meme.
    """
    elsewhere = {
        p.name
        for p in _REPO_ROOT.rglob("*.md")
        if "/archive/" not in p.as_posix() and "node_modules" not in p.as_posix()
    }
    return sorted(p.name for p in _ARCHIVE.rglob("*.md") if p.name not in elsewhere)


def test_the_archive_exists_and_is_not_empty():
    """Un test vert sur une archive vide ne prouverait rien."""
    assert _ARCHIVE.is_dir(), "l'archive a disparu"
    assert _archived_names(), "l'archive est vide -- ce test ne prouverait plus rien"


def test_no_archived_file_is_cited_by_a_living_document():
    names = _archived_names()
    args = ["git", "grep", "-l", "-F"]
    for name in names:
        args += ["-e", name]

    result = subprocess.run(args, cwd=_REPO_ROOT, capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    # 0 = des correspondances, 1 = aucune. Tout le reste est un instrument casse,
    # et un instrument casse rend « aucune correspondance ».
    if result.returncode not in (0, 1):
        pytest.fail(f"git grep a echoue, la mesure ne vaut rien : {result.stderr[:300]}")

    citing = [
        line.strip().replace("\\", "/")
        for line in result.stdout.splitlines()
        if line.strip() and "/archive/" not in line.replace("\\", "/")
    ]

    broken: dict[str, str] = {}
    for src in citing:
        text = (_REPO_ROOT / src).read_text(encoding="utf-8", errors="replace")
        for name in names:
            if name in text:
                broken.setdefault(name, src)

    assert not broken, (
        "des fichiers ecartes sont encore nommes par un document vivant : "
        + ", ".join(f"{n} (<- {s})" for n, s in sorted(broken.items()))
        + ". Les ramener, ou reecrire ce qui les cite -- jamais laisser un lien pendre."
    )
