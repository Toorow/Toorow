#!/usr/bin/env python3
r"""Une metrique non additive ne se somme pas. Le garde le verifiait sur UN nom.

CE QU IL ETAIT (Makefile, jusqu au 2026-08-16) :

    grep -rP "^[^-].*SUM\(average_position\)" dbt/models/marts/

Trois faiblesses, et la troisieme est la seule qui compte vraiment :

  1. `^[^-]` prend pour du code toute ligne dont le PREMIER caractere n est pas
     un tiret. Un commentaire INDENTE -- `    -- SUM(average_position)` -- passe
     donc le filtre et fait rougir la porte sur de la prose. La grande majorite
     des commentaires SQL de ce depot sont indentes.
  2. Le motif est litteral : `sum(average_position)` en minuscules,
     `SUM( average_position )`, `SUM(f.average_position)` passent tous.
  3. **IL NE CONNAIT QU UN SEUL NOM.** La regle est AD-4 et elle vaut pour TOUTE
     metrique non additive. `dbt/seeds/dim_metric.csv` en declare **sept**
     (`roas`, `ctr`, `cpa`, `average_position`, `unique_reach`,
     `average_frequency`, `viewability_rate`) et le garde en surveillait une.
     Six pouvaient etre sommees sans que rien ne rougisse.

CE QU IL EST MAINTENANT. Les noms sont DERIVES du catalogue livre -- la meme
lecture que `platform_canonical_vocabulary.non_additive_metric_names`, importee
plutot que recopiee, parce qu une seconde liste divergerait le jour ou une
huitieme metrique est declaree. Un nom ajoute au seed entre dans le garde sans
que personne y pense.

CE QU IL NE REFUSE PAS, ET C EST LE POINT DELICAT.
`SUM(average_position * impressions)` est la formule PONDEREE, et elle est
CORRECTE : c est le numerateur de `semantic_avg_position`. Ce qui est faux, c est
sommer la metrique SEULE. Le garde ne refuse donc que `SUM(<metrique>)` -- un
argument qui est exactement la metrique, eventuellement qualifiee par un alias --
et laisse passer toute expression qui la combine. Un garde qui refuserait la
ponderation aurait interdit la seule facon juste de la calculer.

    python scripts/check_non_additive_guard.py           # rapport
    python scripts/check_non_additive_guard.py --gate    # non-zero si une somme nue

Le meme code est appele par
`server/tests/conformance/test_non_additive_guard.py` : une regle qui vit dans
deux codes finit par vivre dans deux verites.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "dbt" / "seeds" / "dim_metric.csv"
MARTS = ROOT / "dbt" / "models" / "marts"

#: Une ligne de commentaire SQL, INDENTATION COMPRISE -- c est ce que le `^[^-]`
#: de l ancien garde ratait. Les blocs Jinja `{# ... #}` sont traites a part.
_COMMENT_LINE = re.compile(r"^\s*--")
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)


def non_additive_metric_names(path: Path | None = None) -> frozenset[str]:
    """Les metriques que le catalogue livre declare NON additives.

    Lu, jamais recopie : une seconde liste ici serait libre de diverger de celle
    sur laquelle dbt construit.
    """
    source = path or SEED
    with source.open(encoding="utf-8", newline="") as handle:
        return frozenset(
            (row["name"] or "").strip()
            for row in csv.DictReader(handle)
            if (row.get("additive") or "").strip().lower() == "false"
            and (row.get("name") or "").strip()
        )


def _bare_sum(metric: str) -> re.Pattern[str]:
    """`SUM(metric)` -- et RIEN d autre entre les parentheses.

    Une qualification par alias (`f.average_position`) compte : c est la meme
    somme nue. Une expression (`average_position * impressions`) ne compte pas :
    c est la ponderation, et elle est juste.
    """
    return re.compile(
        r"\bSUM\s*\(\s*(?:[A-Za-z_][A-Za-z0-9_]*\s*\.\s*)?" + re.escape(metric) + r"\s*\)",
        re.IGNORECASE,
    )


def code_lines(text: str) -> list[tuple[int, str]]:
    """Les lignes qui sont du CODE, numerotees comme dans le fichier.

    Les commentaires de fin de ligne sont coupes : `value, -- SUM(ctr)` ne doit
    pas rougir, et le code qui le precede doit rester lisible.
    """
    without_jinja = _JINJA_COMMENT.sub("", text)
    lines: list[tuple[int, str]] = []
    for number, line in enumerate(without_jinja.splitlines(), start=1):
        if _COMMENT_LINE.match(line):
            continue
        code = line.split("--", 1)[0]
        if code.strip():
            lines.append((number, code))
    return lines


def _readable(path: Path) -> str:
    """Le chemin depuis la racine du depot, ou tel quel s il est ailleurs.

    Un repertoire de test hors du depot est un chemin legitime a rapporter :
    `relative_to` levait dessus, et une porte qui plante sur son propre test
    n est pas une porte.
    """
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def offences(marts: Path | None = None, metrics: frozenset[str] | None = None) -> list[str]:
    """Chaque somme nue d une metrique non additive, fichier et ligne."""
    directory = marts or MARTS
    names = metrics if metrics is not None else non_additive_metric_names()
    patterns = {metric: _bare_sum(metric) for metric in sorted(names)}

    found: list[str] = []
    for path in sorted(directory.rglob("*.sql")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in code_lines(text):
            for metric, pattern in patterns.items():
                if pattern.search(line):
                    found.append(
                        f"{_readable(path)}:{number}: "
                        f"SUM({metric}) -- {metric} is declared non-additive in "
                        f"dbt/seeds/dim_metric.csv"
                    )
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", action="store_true", help="non-zero si une somme nue existe")
    args = parser.parse_args()

    names = non_additive_metric_names()
    problems = offences(metrics=names)

    print(f"{len(names)} metrique(s) non additive(s) declaree(s) dans dbt/seeds/dim_metric.csv")
    print("  " + ", ".join(sorted(names)))
    if problems:
        print("\nSOMMES NUES :")
        for problem in problems:
            print(f"  - {problem}")
        print(
            "\nUne metrique non additive se lit par sa vue semantique "
            "(`semantic_<metrique>`), jamais par une somme."
        )
    else:
        print("\nOK : aucune de ces metriques n est sommee seule dans dbt/models/marts/.")
    print(
        "\nNON REFUSE, ET DELIBEREMENT : `SUM(average_position * impressions)` est la "
        "ponderation, et c est la seule facon juste de la calculer."
    )
    return 1 if (args.gate and problems) else 0


if __name__ == "__main__":
    sys.exit(main())
