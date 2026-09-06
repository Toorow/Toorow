#!/usr/bin/env python3
r"""Une cible canonique se CLASSE, ou elle se refuse. Le cliquet est ici.

LA DOCTRINE, arbitrage Jean du 2026-08-15 : « maximiser la classification par
enforcement, justement pour une meilleure comprehension et automatisation ».

Ce qui n'est pas classe ne peut etre ni compris ni automatise. Un champ dont
personne n'a dit s'il est une mesure ou une dimension, dans quel type il vit, et
comment il s'agrege, sera somme par le premier qui le lit -- et le nombre faux
qui en sort a l'air d'un nombre juste. La classification ne s'obtient pas en
demandant gentiment : elle s'obtient parce qu'une porte REFUSE ce qui n'est pas
classe, au moment ou quelqu'un l'ecrit.

CE QUE CETTE PORTE VERIFIE, sur les 39 manifestes du depot (499 declarations,
286 cibles canoniques distinctes) :

  * un `kind` que le registre canonique sait porter -- `metric` ou `dimension` ;
  * un `physical_type` qui a un `value_type` canonique ;
  * une metrique qui declare son agregation OU sa non-additivite ;
  * et l'accord entre connecteurs : deux modules qui nomment la MEME cible
    canonique avec deux formes differentes n'ont pas classe la meme chose.

CLIQUET, PAS MUR. Les 20 cas ci-dessous existaient avant cette porte ; les
refuser tous d'un coup arreterait le depot sans rien classer. Ils sont donc
inscrits, dates et nommes, et la porte rougit sur ce qui n'est pas dans la liste
-- une declaration NEUVE non classee est refusee, une ancienne reste visible
jusqu'a ce que quelqu'un la tranche. Retirer une ligne de la liste sans avoir
corrige le manifeste fait rougir aussi : le cliquet tourne dans un seul sens.

    python scripts/check_canonical_target_classification.py          # rapport
    python scripts/check_canonical_target_classification.py --gate   # non-zero sur du neuf
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from core.platform_canonical_vocabulary import (  # noqa: E402
    connector_declarations,
    project_connectors,
)

#: Les cibles NON CLASSEES au 2026-08-15, avec ce qui manque a chacune. Elles ne
#: font pas rougir la porte ; elles sont la pour etre tranchees et retirees.
#:
#: Trois familles, et elles n'appellent pas le meme geste :
#:
#:   DESACCORD (12) deux connecteurs decrivent la meme cible autrement. Le plus
#:       lourd est `campaign_id` : SEIZE connecteurs le portent, et deux ne
#:       s'accordent pas sur son type. Trancher = editer le manifeste perdant.
#:   TYPE ABSENT (3) `json` n'a pas de `value_type` canonique. Trancher = soit
#:       ajouter le type au vocabulaire semantique, soit declarer la colonne
#:       autrement.
#:   GENRE ABSENT (5) `kind: event`. Ce n'est pas une erreur de manifeste :
#:       l'epic 31 declare l'evenement comme un objet de premiere classe, et
#:       `app.mdm_canonical_fields` n'a que `metric` et `dimension` dans sa
#:       CHECK. Trancher demande une migration, donc une decision produit.
UNCLASSIFIED_AT_2026_08_15: frozenset[str] = frozenset(
    {
        # DESACCORD entre connecteurs
        "campaign_id",
        "clicks",
        "comments",
        "conversions",
        "conversions_value",
        "cost",
        "events",
        "impressions",
        "opens",
        "page_views",
        "sessions",
        "shares",
        # TYPE sans equivalent canonique
        "column_raw_value",
        "column_values",
        "segment_ids",
        # GENRE que le registre ne porte pas (epic 31)
        "campaign_launch",
        "milestone",
        "product_launch",
        "social_post",
        "video_upload",
    }
)


def unclassified() -> dict[str, str]:
    """`{cible -> la phrase qui dit ce qui manque}`, sur tout le depot."""
    _fields, refused = project_connectors(connector_declarations(), governed=[])
    named: dict[str, str] = {}
    for refusal in refused:
        name, _, reason = refusal.partition(": ")
        named[name] = reason
    return named


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", action="store_true", help="non-zero sur une non-classee NEUVE")
    args = parser.parse_args()

    declared = connector_declarations()
    found = unclassified()
    classified = len(declared) - len(found)

    print(f"cibles canoniques declarees par les manifestes : {len(declared)}")
    print(f"  classees                                     : {classified}")
    print(f"  non classees                                 : {len(found)}")

    new = sorted(set(found) - UNCLASSIFIED_AT_2026_08_15)
    fixed = sorted(UNCLASSIFIED_AT_2026_08_15 - set(found))

    for name in sorted(found):
        mark = "NEUF " if name in new else "connu"
        print(f"  [{mark}] {name}: {found[name]}")

    if fixed:
        print("\nCLASSEES DEPUIS -- retirer ces lignes de la liste du script :")
        for name in fixed:
            print(f"  {name}")

    if args.gate:
        if new:
            print(
                f"\nREFUS : {len(new)} cible(s) canonique(s) NEUVE(s) sans classification. "
                "Un champ que personne ne classe est somme par le premier qui le lit."
            )
            return 1
        if fixed:
            print(
                f"\nREFUS : {len(fixed)} cible(s) sont classees et encore inscrites. "
                "Le cliquet tourne dans un seul sens : retirer la ligne."
            )
            return 1
        print("\nOK : aucune non-classee neuve, aucune ligne perimee.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
