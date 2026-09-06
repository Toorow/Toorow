#!/usr/bin/env python3
r"""L'index de ce que le depot sait FAIRE -- et de ce que chaque outil exige.

POURQUOI CE SCRIPT EXISTE. Jean, 2026-08-03, apres six occurrences dans la meme
session : « tu as un probleme d'indexation que tu dois trouver et corriger ».
Il avait raison, et j'ai mis la journee a voir de quel index il parlait.

J'ai indexe les DOCUMENTS -- `_bmad-output/INDEX.md`, `SURFACE-STATE.md`,
`screens/expectations.md`. Je n'ai jamais indexe L'OUTILLAGE. Consequence,
mesuree le meme jour :

  * j'ai reconstruit la methode de test locale commande par commande, alors que
    `make dev`, `make test-full` et `CONTRIBUTING.md` la portent -- et que
    `make test-full` REFUSE sans DSN en imprimant les trois commandes a lancer ;
  * j'ai bute sur `PG_BIN`, que l'aide de `disposable_postgres.py` nomme ;
  * j'ai bute sur le role cluster `toorow_share_reader`, que la migration 162
    crie en toutes lettres : « It is a CLUSTER prerequisite, not a schema object ».

Trois fois, l'outil disait ce qui lui manquait. Trois fois j'ai lu un echec. Le
defaut n'est pas la lecture : c'est qu'aucune page ne dit CE QUI EXISTE avant
qu'on aille le chercher.

    python scripts/toolbox_index.py            # regenere TOOLBOX.md
    python scripts/toolbox_index.py --gate      # non-zero si la page est perimee

CE QU'IL INDEXE, et pourquoi ces trois-la : les cibles `make` (ce qu'on lance),
les scripts de `scripts/` (ce qui existe et ce que ca exige), et les portes de
`e2e/gates/` (ce qui se verifie de bout en bout).

CE QU'IL NE FAIT PAS : ecrire une description a la main. Chaque ligne est LUE --
le commentaire `##` d'une cible make, la premiere ligne de docstring d'un script,
les `os.environ` qu'il consulte. Une description ecrite a la main derive ; une
description derivee ne peut pas mentir plus longtemps que le code.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "TOOLBOX.md"

#: Variables lues partout, sans interet pour choisir un outil.
_NOISE = {"PATH", "HOME", "PWD", "TMPDIR", "TEMP", "USERPROFILE", "CI"}


def make_targets() -> list[tuple[str, str]]:
    """`cible: ## aide` -- la convention deja en place dans le Makefile."""
    makefile = ROOT / "Makefile"
    if not makefile.exists():
        return []
    pattern = re.compile(r"^([a-zA-Z0-9_-]+):.*?##\s*(.+)$")
    return [
        (m.group(1), m.group(2).strip())
        for line in makefile.read_text(encoding="utf-8").splitlines()
        if (m := pattern.match(line))
    ]


def script_facts(path: Path) -> tuple[str, list[str]]:
    """La premiere ligne de docstring, et les variables d'environnement exigees."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(text)
    except (OSError, SyntaxError):
        return "", []
    doc = (ast.get_docstring(tree) or "").strip().splitlines()
    summary = doc[0] if doc else ""
    env = sorted({
        name for name in re.findall(r"environ(?:\.get)?\(\s*[\"']([A-Z][A-Z0-9_]+)[\"']", text)
        if name not in _NOISE
    })
    return summary, env


def build() -> str:
    out: list[str] = []
    w = out.append
    w("<!-- GENERE par scripts/toolbox_index.py -- ne pas editer a la main. -->")
    w("<!-- Regenerer : python scripts/toolbox_index.py  |  verifier : --gate -->")
    w("")
    w("# Ce que ce depot sait faire — et ce que chaque outil exige")
    w("")
    w("**Ouvrir cette page AVANT de reconstruire une methode.** Elle existe parce")
    w("que la methode de test locale a ete rederivee commande par commande alors")
    w("qu'elle etait deja cablee, et que trois prerequis (`PG_BIN`, le role")
    w("`connector`, le role cluster `toorow_share_reader`) etaient nommes par les")
    w("outils eux-memes, dans leur aide ou leur message d'erreur.")
    w("")
    w("Rien ici n'est ecrit a la main : les aides `##` du Makefile, les premieres")
    w("lignes de docstring et les variables d'environnement sont LUES dans le code.")
    w("")

    targets = make_targets()
    w(f"## Les {len(targets)} cibles `make`")
    w("")
    w("| Commande | Ce qu'elle fait |")
    w("| --- | --- |")
    for name, help_text in targets:
        w(f"| `make {name}` | {help_text} |")
    w("")

    scripts = sorted(p for p in (ROOT / "scripts").glob("*.py") if not p.name.startswith("_"))
    w(f"## Les {len(scripts)} scripts de `scripts/`")
    w("")
    w("La colonne **Exige** est ce que le script lit dans l'environnement. Une")
    w("variable absente est la premiere chose a verifier quand il refuse de tourner")
    w("— c'est exactement ce qui a coute une demi-session avec `PG_BIN`.")
    w("")
    w("| Script | Ce qu'il fait | Exige |")
    w("| --- | --- | --- |")
    for path in scripts:
        summary, env = script_facts(path)
        needs = ", ".join(f"`{e}`" for e in env[:5]) or "—"
        w(f"| `{path.name}` | {summary[:110]} | {needs} |")
    w("")

    gates_dir = ROOT / "e2e" / "gates"
    if gates_dir.exists():
        gates = sorted(p for p in gates_dir.glob("*.py") if not p.name.startswith("_"))
        w(f"## Les {len(gates)} portes de parcours (`e2e/gates/`)")
        w("")
        w("`uv run python e2e/runner.py <PORTE>` — et `--list` dit lesquelles")
        w("existent. Les portes NON listees ici ne sont pas `not_tested` : elles ne")
        w("sont pas ECRITES. Confondre les deux fait attendre une execution qui")
        w("n'arrivera jamais.")
        w("")
        w("| Porte | Titre |")
        w("| --- | --- |")
        for path in gates:
            text = path.read_text(encoding="utf-8", errors="replace")
            gate = re.search(r'^GATE\s*=\s*["\'](\w+)["\']', text, re.M)
            #  La citation FERMANTE est la meme que l'ouvrante. Sans le retour
            #  arriere, `"La chaine analytique -- de l'autorisation au rendu"`
            #  s'arretait sur l'apostrophe et l'index publiait un titre coupe au
            #  milieu d'un mot.
            title = re.search(r'^TITLE\s*=\s*(["\'])(.+?)\1', text, re.M)
            w(f"| `{gate.group(1) if gate else path.stem}` | "
              f"{title.group(2) if title else ''} |")
        w("")

    #  Les INSTRUMENTS du harnais, a cote des portes : ce qui arme, lie ou invite
    #  par les routes du produit pour qu'une porte ait quelque chose a mesurer.
    #  Le 2026-09-04, quatre scripts d'armement ont vecu dans le dossier
    #  temporaire d'une session ; une session partie de zero ne pouvait pas armer
    #  un second cas. Un instrument absent de cette page est un instrument a
    #  reecrire.
    e2e_dir = ROOT / "e2e"
    instruments = sorted(
        p for p in e2e_dir.glob("*.py")
        if not p.name.startswith("_") and p.name not in {"runner.py", "auth.py", "targets.py"}
    )
    if instruments:
        w(f"## Les {len(instruments)} instruments du harnais (`e2e/*.py`)")
        w("")
        w("`uv run python e2e/<instrument>.py` — chacun passe par les routes du produit,")
        w("signe en `gcloud` comme le proprietaire de l'organisation du harnais")
        w("(`e2e/auth.py`), et n'ecrit que dans le projet du harnais.")
        w("")
        w("| Instrument | Ce qu'il fait | Exige |")
        w("| --- | --- | --- |")
        for path in instruments:
            summary, env = script_facts(path)
            needs = ", ".join(f"`{e}`" for e in env[:5]) or "—"
            w(f"| `{path.name}` | {summary[:110]} | {needs} |")
        w("")

    w("## Là où la méthode est écrite en prose")
    w("")
    w("| Fichier | Ce qu'il porte |")
    w("| --- | --- |")
    w("| `CONTRIBUTING.md` | démarrer le serveur, vérifier l'outil de santé, bâtir un widget |")
    w("| `e2e/README.md` | le harnais de parcours |")
    w("| `e2e/AUTONOMIE.md` | pointer ce harnais sur une pile LOCALE, et sous quel mode |")
    w("| `CLAUDE.md` | les règles de travail, et l'ordre de priorité quand elles se contredisent |")
    w("")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gate", action="store_true")
    args = ap.parse_args()
    page = build()
    if args.gate:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != page:
            print("TOOLBOX.md est perime -- `python scripts/toolbox_index.py` le regenere.",
                  file=sys.stderr)
            return 1
        print("TOOLBOX.md est a jour")
        return 0
    OUT.write_text(page, encoding="utf-8")
    print(f"{OUT.relative_to(ROOT)} regenere")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
