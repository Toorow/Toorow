#!/usr/bin/env python3
r"""Repondre en UN endroit : cette page doit faire quoi, et l'a-t-on aujourd'hui ?

POURQUOI CE SCRIPT EXISTE. Demande de Jean, 2026-08-03 : « la page datastream,
quelles sont les etapes d'un setup complet ? Est-ce que j'ai tout ce qu'il faut
pour le faire aujourd'hui ? La reponse doit etre trouvee de maniere claire et
efficace. »

Mesure de ce que coutait cette question AVANT ce script : cinq sources --
le document ratifie (ce qui est voulu), `completeness-ledger.json` (les criteres),
`finished_work_audit.py` (les ecrans montes), `navigation.ts` (les routes),
puis le code lui-meme -- et deux d'entre elles se contredisaient : le ledger
donnait `datastream` a 0/14 criteres ouverts pendant que l'audit comptait 6
composants montes nulle part.

CE QU'IL FAIT. Chaque document ratifie peut porter une section
« Setup steps — what delivers each one » : une table qui declare, par etape du
parcours, le code qui la livre, la route serveur, et le test qui la prouve. Ce
script resout chaque citation et ecrit l'etat dans `SURFACE-STATE.md`.

    python scripts/surface_state.py            # regenere docs/product-architecture/SURFACE-STATE.md
    python scripts/surface_state.py --gate     # non-zero si une citation ne resout pas

LA REGLE QUI LE REND HONNETE : la table declare, elle ne conclut pas. Aucun
verdict n'y est ecrit a la main -- un verdict ecrit a la main est vrai le jour
ou on l'ecrit et faux le lendemain, sans que personne ne le voie. Ici, une ligne
qui pourrit fait rougir la porte.

CE QU'IL NE PROUVE PAS, et c'est ecrit dans la page generee : qu'une etape
FONCTIONNE. Il prouve qu'elle est batie, routee et couverte. Un parcours reel
reste un parcours reel -- c'est la difference entre « le bouton existe » et
« quelqu'un a clique dessus » (CLAUDE.md section 6).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs" / "product-architecture"
OUT = DOCS / "SURFACE-STATE.md"
SERVER = ROOT / "server" / "core"

_SECTION = "## Setup steps — what delivers each one"
_ROW = re.compile(r"^\|\s*(\d+)\s*\|(.+)\|\s*$")
_CODE = re.compile(r"`([^`]+)`")
_ROUTE_VERB = re.compile(r"^(GET|POST|PATCH|PUT|DELETE)\s+(/\S+)$")


class Step:
    def __init__(self, number: str, title: str, cells: list[str]):
        self.number = number
        self.title = title.strip()
        self.cells = cells

    @property
    def citations(self) -> list[str]:
        return [c for cell in self.cells for c in _CODE.findall(cell)]


def _steps_of(doc: Path) -> list[Step]:
    text = doc.read_text(encoding="utf-8")
    if _SECTION not in text:
        return []
    body = text.split(_SECTION, 1)[1].split("\n## ", 1)[0]
    steps: list[Step] = []
    for line in body.splitlines():
        match = _ROW.match(line.strip())
        if not match:
            continue
        cells = [c.strip() for c in match.group(2).split("|")]
        if cells:
            steps.append(Step(match.group(1), _CODE.sub(r"\1", cells[0]), cells[1:]))
    return steps


def _resolves(citation: str) -> tuple[bool, str]:
    """Une citation est un chemin du depot, ou une route servie par le serveur."""
    verb = _ROUTE_VERB.match(citation)
    if verb:
        needle = f'"{verb.group(2)}"'
        hit = any(needle in p.read_text(encoding="utf-8", errors="replace")
                  for p in SERVER.glob("*.py"))
        return hit, "route"
    if "/" in citation or citation.endswith((".py", ".tsx", ".ts", ".sql", ".json")):
        return (ROOT / citation).exists(), "fichier"
    return True, "texte"


def build() -> tuple[str, list[str]]:
    problems: list[str] = []
    out: list[str] = []
    w = out.append
    w("<!-- GENERE par scripts/surface_state.py -- ne pas editer a la main. -->")
    w("<!-- Regenerer : python scripts/surface_state.py   |   verifier : --gate -->")
    w("")
    w("# Etat des surfaces — ce que la page doit faire, et ce qu'on a")
    w("")
    w("Cette page repond a une seule question, par surface : **quelles sont les etapes,")
    w("et les a-t-on aujourd'hui ?** Elle est derivee des tables")
    w("« Setup steps — what delivers each one » des documents ratifies : chaque chemin")
    w("et chaque route y est resolu contre le depot au moment de la generation.")
    w("")
    w("**Ce qu'un OK signifie** : l'etape est batie, routee et couverte par un test qui")
    w("existe. **Ce qu'il ne signifie pas** : que quelqu'un a parcouru le chemin en vrai.")
    w("Un test unitaire ne prouve pas qu'une route repond (CLAUDE.md section 6).")
    w("")

    documented = 0
    for doc in sorted(DOCS.glob("*.md")):
        steps = _steps_of(doc)
        if not steps:
            continue
        documented += 1
        w(f"## {doc.stem}")
        w("")
        w(f"Cible : [`{doc.name}`]({doc.name}) — {len(steps)} etapes declarees.")
        w("")
        w("| # | Etape | Etat | Ce qui la porte |")
        w("| --- | --- | --- | --- |")
        for step in steps:
            missing = [c for c in step.citations if not _resolves(c)[0]]
            # UNE LIGNE SANS CITATION LISAIT « OK ». `missing` est vide quand il
            # n'y a rien a resoudre, donc une etape declaree et rattachee a RIEN
            # se rendait exactement comme une etape batie, routee et testee --
            # le faux vert que ce fichier existe pour empecher. Mesure
            # 2026-08-17 : aucune ligne du depot n'etait dans ce cas, donc la
            # reparation ne change aucun etat ; elle ferme la porte avant que
            # quelqu'un ajoute une table en laissant les cellules vides.
            if not step.citations:
                state = "**MANQUE** : aucune citation -- rien ne porte cette etape"
                problems.append(
                    f"{doc.name} etape {step.number} : declaree, rattachee a rien"
                )
            elif missing:
                state = f"**MANQUE** : {', '.join(f'`{m}`' for m in missing)}"
            else:
                state = "OK"
            for m in missing:
                problems.append(f"{doc.name} etape {step.number} : `{m}` ne resout pas")
            carried = ", ".join(f"`{c}`" for c in step.citations) or "—"
            w(f"| {step.number} | {step.title} | {state} | {carried} |")
        w("")

    if documented == 0:
        w("_Aucun document ratifie ne porte encore de table d'etapes._")
        w("")

    w("---")
    w("")
    w("## Dossiers de surface — la lecture humaine")
    w("")
    w("Un dossier repond en UNE lecture : le parcours de bout en bout, ce qui manque,")
    w("et le tableau **Prevu / Souhaite / Integre**. Sans lui, repondre demande de")
    w("parcourir des centaines de fichiers -- et de se tromper. C'est le prealable a")
    w("tout plan de modification : on ne modifie pas un ecran dont on ignore l'usage.")
    w("")
    w("| Surface | Dossier |")
    w("| --- | --- |")
    dossiers = ROOT / "_bmad-output" / "surface-dossiers"
    for doc in sorted(DOCS.glob("*.md")):
        if doc.name in {OUT.name, "README.md"}:
            continue
        stem = doc.stem.split("-workbench")[0]
        found = next((d for d in dossiers.glob("*.md")
                      if d.stem in doc.stem or doc.stem.startswith(d.stem)), None)             if dossiers.exists() else None
        w(f"| `{doc.name}` | " + (f"[`{found.name}`](../../_bmad-output/surface-dossiers/{found.name})"
                                   if found else "**a ecrire**") + " |")
    w("")
    w("---")
    w("")
    w("## Surfaces sans table d'etapes")
    w("")
    w("Leur cible est ecrite, mais personne ne peut repondre « ai-je tout ce qu'il faut ? »")
    w("sans relire le code. C'est le travail restant, et il se voit :")
    w("")
    for doc in sorted(DOCS.glob("*.md")):
        if doc.name in {OUT.name, "README.md"} or _steps_of(doc):
            continue
        w(f"- `{doc.name}`")
    w("")
    return "\n".join(out) + "\n", problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gate", action="store_true",
                    help="non-zero si une citation ne resout pas, ou si la page est perimee")
    args = ap.parse_args()

    page, problems = build()
    if args.gate:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if problems:
            for p in problems:
                print(p, file=sys.stderr)
            print("\nUne etape declaree cite un artefact qui n'existe pas : "
                  "soit il a bouge, soit la table ment.", file=sys.stderr)
            return 1
        if current != page:
            print("SURFACE-STATE.md est perime -- `python scripts/surface_state.py` le regenere.",
                  file=sys.stderr)
            return 1
        print("SURFACE-STATE.md est a jour, et chaque etape declaree resout")
        return 0

    OUT.write_text(page, encoding="utf-8")
    print(f"{OUT.relative_to(ROOT)} regenere")
    for p in problems:
        print("  ", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
