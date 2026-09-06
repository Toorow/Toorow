#!/usr/bin/env python3
r"""L'index du modele de donnees -- ce qu'une table EXIGE, et qui la possede.

POURQUOI. Troisieme index de la meme famille, apres les documents
(`_bmad-output/INDEX.md`) et l'outillage (`TOOLBOX.md`). Jean, 2026-08-03 :
« faut que tu trouves la bonne methode pour scinder les informations utiles
aussi, par exemple comment trouver rapidement les profils des bases ».

Le cout de son absence, mesure le meme jour : pour creer UNE organisation et UN
projet en local, j'ai interroge `information_schema` a la main quatre fois, et
j'ai quand meme rate deux choses que ce fichier aurait donnees d'un coup :

  * `app.org_members.role` doit etre pose -- un membre sans role n'a aucun droit ;
  * `app.org_members.identity` attend le **person_id** canonique, PAS un e-mail.
    Le refus rendu est « Project not found », qui envoie chercher ailleurs.

    python scripts/schema_index.py            # regenere SCHEMA.md
    python scripts/schema_index.py --gate     # non-zero si perime

CE QU'IL INDEXE, et pourquoi ces trois-la seulement : les colonnes OBLIGATOIRES
(celles qui font echouer une insertion), les cles etrangeres (la chaine de
possession), et les tables les plus liees (par ou tout passe). Une liste
exhaustive de 276 tables ne se lit pas ; ces trois angles se lisent.

IL LIT UNE BASE VIVANTE. `PLATFORM_DB_URL` ou `TEST_POSTGRES_DSN` -- jamais la
production : le contenu ne l'interesse pas, seulement la forme, et une base
jetable migree la porte a l'identique.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "SCHEMA.md"


def dsn() -> str | None:
    for name in ("PLATFORM_DB_URL", "TEST_POSTGRES_DSN"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def build(url: str) -> str:
    import psycopg

    out: list[str] = []
    w = out.append
    with psycopg.connect(url, connect_timeout=15) as conn:
        required = conn.execute("""
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema='app' AND is_nullable='NO' AND column_default IS NULL
            ORDER BY table_name, ordinal_position
        """).fetchall()
        links = conn.execute("""
            SELECT tc.table_name, kcu.column_name, ccu.table_name AS cible
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON kcu.constraint_name = tc.constraint_name
            JOIN information_schema.constraint_column_usage ccu
              ON ccu.constraint_name = tc.constraint_name
            WHERE tc.constraint_type='FOREIGN KEY' AND tc.table_schema='app'
            ORDER BY 1, 2
        """).fetchall()
        total = conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='app'"
        ).fetchone()[0]

    by_table: dict[str, list[str]] = {}
    for table, column, kind in required:
        by_table.setdefault(table, []).append(f"{column} ({kind.split()[0]})")

    incoming: dict[str, int] = {}
    for _, _, target in links:
        incoming[target] = incoming.get(target, 0) + 1

    w("<!-- GENERE par scripts/schema_index.py -- ne pas editer a la main. -->")
    w("")
    w("# Le modèle de données — ce qu'une table exige, et qui la possède")
    w("")
    w(f"Schéma `app` : **{total} tables**. Une liste exhaustive ne se lit pas ;")
    w("trois angles se lisent, et ce sont les trois qui font échouer une écriture.")
    w("")
    w("## Les tables les plus référencées — par où tout passe")
    w("")
    w("| Table | Références entrantes | Colonnes obligatoires |")
    w("| --- | --- | --- |")
    for table, count in sorted(incoming.items(), key=lambda kv: -kv[1])[:12]:
        w(f"| `app.{table}` | {count} | {', '.join(by_table.get(table, [])) or '—'} |")
    w("")
    w("## Le piège qui a coûté une session")
    w("")
    w("`app.org_members.identity` attend le **`person_id` canonique**, pas un e-mail.")
    w("L'authentification crée la personne dans `app.persons` au premier appel")
    w("authentifié ; tous les résolveurs comparent dessus. Insérer un e-mail ne donne")
    w("aucun droit — et le refus rendu est `Project not found`, qui envoie chercher")
    w("ailleurs. Voir `e2e/AUTONOMIE.md`, procédure de liaison, étape 6.")
    w("")
    w("## Colonnes obligatoires, par table")
    w("")
    w("Celles qui font échouer une insertion : `NOT NULL` sans valeur par défaut.")
    w("")
    w("<details><summary>Les " + str(len(by_table)) + " tables concernées</summary>")
    w("")
    w("| Table | À fournir |")
    w("| --- | --- |")
    for table in sorted(by_table):
        w(f"| `{table}` | {', '.join(by_table[table])} |")
    w("")
    w("</details>")
    w("")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gate", action="store_true")
    args = ap.parse_args()

    url = dsn()
    if not url:
        # Ne PAS echouer : sans base, ce script n'a rien a dire, et une porte
        # rouge faute d'environnement est une porte qu'on apprend a ignorer.
        print("ni PLATFORM_DB_URL ni TEST_POSTGRES_DSN -- rien à indexer "
              "(voir TOOLBOX.md : disposable_postgres.py)")
        return 0

    page = build(url)
    if args.gate:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != page:
            print("SCHEMA.md est perime -- `python scripts/schema_index.py` le regenere.",
                  file=sys.stderr)
            return 1
        print("SCHEMA.md est a jour")
        return 0
    OUT.write_text(page, encoding="utf-8")
    print(f"{OUT.relative_to(ROOT)} regenere")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
