#!/usr/bin/env python3
r"""AI-288 -- projeter le dictionnaire gouverne dans le registre canonique de plateforme.

POURQUOI CE SCRIPT EXISTE. Mesure du 2026-08-15 sur une base a jour de toutes ses
migrations : `app.mdm_canonical_fields` porte **0 ligne** a la portee plateforme,
et six modules de production valident chaque liaison mdm contre elle. Le
dictionnaire gouverne (`app.target_fields`, 13 lignes, migration 023) n'etait
projete nulle part, alors que la migration 032 avait deja pose le lien --
`dictionary_field_name`, cle etrangere vers `target_fields(name)`, dont le
commentaire dit « optional derivation link to the 8.5 dictionary ».

Un registre vide est le pire des trois etats possibles : l'ecran rend zero ligne,
zero conflit et zero erreur, ce qui se lit exactement comme un vocabulaire sain.

CE QUE CE SCRIPT NE FAIT PAS, ET C'EST LE POINT. Il ne modifie **jamais** une
ligne qui existe. Une ligne stockee qui contredit la projection du depot est une
DIVERGENCE : elle est imprimee, pas resolue. Entre le depot et un registre vivant,
un script qui choisit un camp detruit l'autre sans laisser de trace -- c'est la
retenue que `declare_platform_clocks.py` s'impose deja pour une cadence, et elle
vaut ici pour le vocabulaire que tous les projets partagent.

    python scripts/provision_platform_canonical_fields.py            # minte ce qui manque
    python scripts/provision_platform_canonical_fields.py --check    # ne rend compte que

`--check` sort non-zero si le registre n'est pas a jour OU si une divergence
existe. C'est la forme que prend la porte : elle constate, elle n'ecrit pas.

BASE : `PLATFORM_DB_URL` (ou `TEST_POSTGRES_DSN`), comme `apply_migrations.py`.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from core.platform_canonical_vocabulary import (  # noqa: E402
    load_dictionary,
    load_platform_registry,
    provision,
)


def _dsn() -> str:
    for name in ("PLATFORM_DB_URL", "TEST_POSTGRES_DSN"):
        value = os.environ.get(name)
        if value:
            return value
    raise SystemExit(
        "Aucune base : poser PLATFORM_DB_URL (ou TEST_POSTGRES_DSN pour un cluster "
        "jetable -- python scripts/disposable_postgres.py up)."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="ne rien ecrire ; non-zero si le registre est incomplet ou divergent",
    )
    parser.add_argument(
        "--actor",
        default="system",
        help="qui declare (inscrit en `created_by`) ; defaut : system",
    )
    args = parser.parse_args()

    import psycopg  # noqa: PLC0415

    with psycopg.connect(_dsn()) as conn:
        dictionary = load_dictionary(conn)
        stored = load_platform_registry(conn)

        print(f"dictionnaire gouverne : {len(dictionary)} lignes (app.target_fields)")
        print(f"registre plateforme   : {len(stored)} lignes deja declarees")

        report = provision(conn, actor=args.actor, dry_run=args.check)

        #  Le compte des DEUX sources, parce que la seconde est la reponse a
        #  « borne par quoi ? » : par rien. Un 40e connecteur l'agrandit.
        print(
            f"projete               : {len(report['projected'])} "
            f"({report['from_dictionary']} du dictionnaire, "
            f"{report['from_connectors']} des manifestes de connecteur)"
        )
        for refusal in report["refused"]:
            #  Ni minte, ni tu : ce sont les decisions que le depot ne peut pas
            #  prendre a la place de quelqu un.
            print(f"  A DECIDER {refusal}")

        if args.check:
            missing = report["would_mint"]
            for name in missing:
                print(f"  MANQUE  {name}")
            for problem in report["divergences"]:
                print(f"  DIVERGE {problem}")
            if not missing and not report["divergences"]:
                print("OK : le registre porte tout ce que le depot declare.")
                return 0
            print(
                f"\n{len(missing)} champ(s) a declarer, "
                f"{len(report['divergences'])} divergence(s)."
            )
            return 1

        for name in report["minted"]:
            print(f"  DECLARE {name}")
        for problem in report["divergences"]:
            #  Imprime APRES l'ecriture, et jamais corrige : voir l'en-tete.
            print(f"  DIVERGE {problem}")
        conn.commit()
        print(
            f"\n{len(report['minted'])} champ(s) declares, "
            f"{len(report['present'])} deja presents, "
            f"{len(report['divergences'])} divergence(s) a arbitrer a la main."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
