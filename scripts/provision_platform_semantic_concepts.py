#!/usr/bin/env python3
r"""AI-288 -- provisionner le referentiel de plateforme du Modele Semantique.

POURQUOI CE SCRIPT EXISTE. La formule d un ratio existe en TROIS copies :
`dbt/seeds/dim_metric.csv` la declare, `dbt/models/marts/semantic_*.sql` la
calcule, `app.semantic_concept_versions.expression` la gouverne. Les deux
premieres sont au depot et tenues par `make check-metric-formula-parity` ; la
troisieme vit en base et **rien ne la provisionnait**. Une instance neuve ne
gouverne donc aucune formule, et la garde a trois sources ne peut comparer que
deux copies -- ce qu elle DIT, plutot que de laisser croire a une parite qu elle
n a pas verifiee.

CE QU IL NE FAIT PAS, ET C EST LE POINT. Il ne modifie **jamais** une ligne qui
existe. Un concept stocke qui contredit la projection est une DIVERGENCE : elle
est imprimee, pas resolue. Entre le depot et un registre vivant, un script qui
choisit un camp detruit l autre sans laisser de trace -- meme retenue que
`provision_platform_canonical_fields.py`, et pour la meme raison.

    python scripts/provision_platform_semantic_concepts.py                # minte ce qui manque
    python scripts/provision_platform_semantic_concepts.py --check        # ne rend compte que
    python scripts/provision_platform_semantic_concepts.py --repair-only  # corrige les semis 142
                                                                          # sans rien minter

`--check` sort non-zero si le referentiel n est pas a jour OU si une divergence
existe. C est la forme que prend la porte : elle constate, elle n ecrit pas.

BASE : `PLATFORM_DB_URL` (ou `TEST_POSTGRES_DSN`), comme `apply_migrations.py`.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from core.platform_semantic_concepts import provision  # noqa: E402


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
        "--check", action="store_true", help="ne rend compte que ; non-zero si a faire"
    )
    parser.add_argument("--actor", default="platform-provisioner", help="qui declare")
    parser.add_argument(
        "--repair-seeded",
        action="store_true",
        help=(
            "publie une version CORRIGEE des concepts que la migration 142 a semes "
            "contre une table vide. Jamais une declaration humaine."
        ),
    )
    parser.add_argument(
        "--repair-only",
        action="store_true",
        help=(
            "corrige les semis de la migration 142 SANS minter les concepts manquants. "
            "Deux actes, deux decisions : remettre une definition existante sur ce que le "
            "catalogue livre declare n est pas agrandir le vocabulaire que tout projet lit."
        ),
    )
    args = parser.parse_args()

    import psycopg  # noqa: PLC0415

    with psycopg.connect(_dsn()) as conn:
        report = provision(
            conn,
            actor=args.actor,
            dry_run=args.check,
            repair_seeded=args.repair_seeded or args.repair_only,
            mint=not args.repair_only,
        )
        if not args.check:
            conn.commit()

    print(f"{len(report['projected'])} concept(s) projete(s) depuis dbt/seeds/dim_metric.csv")
    print(f"  deja en base : {len(report['present'])}")
    if args.check:
        pending = report.get("would_mint") or []
        print(f"  a minter     : {len(pending)}")
        if pending:
            print("    " + ", ".join(pending))
    else:
        print(f"  mintes       : {len(report['minted'])}")
        if report["minted"]:
            print("    " + ", ".join(report["minted"]))

    if report["refused"]:
        print("\nREFUSES PAR LEUR NOM (jamais approximes) :")
        for refusal in report["refused"]:
            print(f"  - {refusal}")

    #  RETENUS, JAMAIS TUS. Un catalogue qui se tait sur ce qu'il ne mint pas se
    #  lit comme un catalogue complet -- et ces metriques-la ne sont pas absentes
    #  du produit, ce sont des presets qu'un projet ADOPTE. Chaque ligne porte le
    #  geste qui les rend disponibles, pas la cause technique.
    if report.get("withheld"):
        print("\nRETENUS A LA PORTEE PLATEFORME (le geste, pas la cause) :")
        for line in report["withheld"]:
            print(f"  - {line}")

    if report["seeded_divergences"]:
        print("\nSEMIS DE LA MIGRATION 142, ecrits contre une table VIDE :")
        for divergence in report["seeded_divergences"]:
            print(f"  - {divergence}")
        if report["repaired"]:
            print("  corriges (version 2 publiee) : " + ", ".join(report["repaired"]))
        elif args.check:
            print("  a corriger : " + ", ".join(report.get("would_repair") or []))
        else:
            print(
                "  NON corriges : relancer avec --repair-seeded. La version 1 reste "
                "immuable ; la correction est une version 2 et un pointeur qui bouge."
            )

    if report["divergences"]:
        print("\nDIVERGENCES DECLAREES (rapportees, JAMAIS resolues) :")
        for divergence in report["divergences"]:
            print(f"  - {divergence}")
        print(
            "\nQuelqu un a publie ces versions. Entre le depot et un registre "
            "vivant, choisir un camp detruit l autre : ces lignes se tranchent a la main."
        )

    if args.check and (
        (report.get("would_mint") or [])
        or (report.get("would_repair") or [])
        or report["divergences"]
    ):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
