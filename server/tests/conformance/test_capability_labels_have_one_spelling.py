"""Une capacite, une orthographe -- AI-261.

Le 2026-08-11 la meme capacite portait QUATRE orthographes, reparties sur TROIS
tables independantes : `Tax & fee` dans `ProjectSettings.tsx`, `Tax & fees` dans
`datastream_preconfiguration.py`, `Tax & Fees` dans `CapabilityCoverage.tsx` --
et `Reporting timezone` contre `Reporting Timezone`. Elles se contredisaient sur
le meme ecran : la carte disait un nom, la ligne de dependance un autre.

La troisieme table est retiree (8a48a341). Il en reste DEUX, et elles ont une
raison d'exister toutes les deux -- le serveur compose le nom pour pouvoir y
attacher un `evidence_ref`, l'ecran en a besoin la ou aucune charge serveur ne
le porte. Ce que ce fichier interdit, c'est qu'elles DIVERGENT, et qu'une
troisieme apparaisse.

Ce n'est pas une relecture a la main a chaque story : c'est la seule forme qui
tienne, parce que le defaut n'est pas dans une table mais dans la RELATION entre
elles.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SERVER_TABLE = ROOT / "server" / "core" / "datastream_preconfiguration.py"
FRONT_TABLE = ROOT / "ui" / "admin" / "src" / "ui" / "CapabilityCoverage.tsx"

# Les deux seuls fichiers autorises a declarer la table. Toute autre declaration
# est la troisieme table, et elle se remet a diverger le jour ou personne ne
# regarde.
DECLARED_IN = {SERVER_TABLE, FRONT_TABLE}

# La cle est citee en Python (`"country":`) et nue en TypeScript (`country:`).
_ENTRY = re.compile(r'"?(?P<key>[a-z_]+)"?\s*:\s*"(?P<label>[^"]+)"')


def _table(path: Path) -> dict[str, str]:
    """Les entrees de `CAPABILITY_LABELS` du fichier, cle -> libelle."""
    text = path.read_text(encoding="utf-8")
    start = text.index("CAPABILITY_LABELS")
    start = text.index("{", start)
    body = text[start : text.index("}", start)]
    return {m.group("key"): m.group("label") for m in _ENTRY.finditer(body)}


def test_the_two_tables_say_exactly_the_same_thing() -> None:
    """Meme jeu de cles, meme libelle pour chacune -- au caractere pres.

    L'egalite porte sur le dictionnaire entier et non sur les deux entrees qui
    avaient divergé : une comparaison qui ne regarde que le defaut connu laisse
    passer le suivant.
    """
    assert _table(SERVER_TABLE) == _table(FRONT_TABLE)


def test_the_spelling_is_the_one_the_ratified_documents_use() -> None:
    """`Tax & Fees` et `Reporting Timezone`, parce que les documents l'ecrivent.

    Mesure du 2026-08-11 sur `docs/product-architecture/*.md` : 14 `Tax & Fees`
    contre 3 `Tax & fees`, 17 `Reporting Timezone` contre 3. La casse n'est donc
    pas un gout -- c'est le vocabulaire ratifie, et un ecran qui en invente un
    autre fait lire deux noms pour une chose.
    """
    labels = _table(SERVER_TABLE)
    assert labels["tax_fees"] == "Tax & Fees"
    assert labels["reporting_timezone"] == "Reporting Timezone"


def test_no_third_table_declares_the_capability_labels() -> None:
    """Aucun autre fichier ne declare `CAPABILITY_LABELS`.

    Les re-exports (`ui/index.ts`) et les lectures (`capabilityLabel(...)`) ne
    sont pas des declarations et ne comptent pas : ce qui est refuse ici, c'est
    un troisieme ENDROIT ou une orthographe s'ecrit.
    """
    declaration = re.compile(r"CAPABILITY_LABELS\s*(?::[^=]*)?=\s*\{")
    found: set[Path] = set()
    for tree in (ROOT / "server", ROOT / "ui" / "admin" / "src"):
        for path in tree.rglob("*"):
            if path.suffix not in {".py", ".ts", ".tsx"} or not path.is_file():
                continue
            if "node_modules" in path.parts or "__pycache__" in path.parts:
                continue
            if declaration.search(path.read_text(encoding="utf-8", errors="replace")):
                found.add(path)
    assert found == DECLARED_IN, (
        "une table de libelles de capacite est declaree hors des deux fichiers "
        f"autorises : {sorted(str(p.relative_to(ROOT)) for p in found - DECLARED_IN)}"
    )
