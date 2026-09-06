#!/usr/bin/env python3
r"""La formule d'un ratio est ecrite TROIS fois. Ce script exige qu'elles disent la meme chose.

POURQUOI CE SCRIPT EXISTE. Audit MDM & Gouvernance du 2026-08-14 : la definition
d'un ratio existe en plusieurs copies, et rien ne verifiait qu'elles coincident.

    dbt/seeds/dim_metric.csv                    `cpa,false,ratio,cost,conversions`
    dbt/models/marts/semantic_cpa.sql           SUM(cost) / NULLIF(SUM(conversions), 0)
    app.semantic_concept_versions.expression    {"op":"ratio","numerator":...}

Le seed DECLARE le numerateur et le denominateur ; le mart les CALCULE ; le
Concept les GOUVERNE. Changer l'un sans l'autre ne casse aucun test : dbt
construit le mart sans jamais lire la ligne du seed, le seed se charge sans jamais
lire le mart, et la base ne lit ni l'un ni l'autre. Un CPA calcule sur `clicks`
alors que le catalogue annonce `conversions` rend un nombre plausible et faux, sur
tous les ecrans a la fois.

LA TROISIEME COPIE EST DESORMAIS COUVERTE, et c'est ce qui a change le 2026-08-16.
Elle vit en BASE, donc elle ne se compare pas comme un fichier :

  - `platform_ratio_expressions()` PROJETTE depuis `dim_metric.csv` l'arbre typé
    exact que la migration 142 ecrit pour un ratio. C'est le chemin versionne du
    referentiel plateforme que `docs/product-architecture/governance.md` portait
    en `Incomplete if` : la reference est DERIVEE du catalogue livre, jamais une
    seconde liste qu'une personne aurait ecrite a cote -- meme doctrine que
    `server/core/platform_canonical_vocabulary.py`, et pour la meme raison.
  - `stored_divergences()` lit les Concepts reellement declares et les confronte a
    cette projection. Les divergences sont RAPPORTEES, JAMAIS resolues : entre un
    depot et un registre vivant, choisir un camp en silence detruit l'autre.

Deux categories distinctes, parce qu'elles n'ont pas le meme sens :

  - Un Concept de PLATEFORME (`project_id IS NULL`) qui contredit le catalogue est
    une DIVERGENCE. C'est le meme objet dit deux fois.
  - Un Concept de PROJET qui redefinit un ratio que le mart sert est un OVERRIDE
    CLIENT : le client a le droit de definir sa metrique, mais personne ne doit
    croire que `semantic_cpa` implemente SA formule. Rapporte, jamais compte
    comme une faute.

    python scripts/check_metric_formula_parity.py               # les 2 copies du depot
    python scripts/check_metric_formula_parity.py --gate        # non-zero si divergence
    python scripts/check_metric_formula_parity.py --dsn ...     # + la copie gouvernee
    python scripts/check_metric_formula_parity.py --require-db  # l'absence de base ECHOUE

Sans base, le script le DIT plutot que de laisser croire a une parite a trois : la
CI tourne sans base et sa porte reste celle des deux copies du depot.

Le meme code est appele par `server/tests/conformance/test_metric_formula_parity.py`
pour que la regle ait UNE implementation et deux portes.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "dbt" / "seeds" / "dim_metric.csv"
MARTS = ROOT / "dbt" / "models" / "marts"

# LA REGLE VIT DANS `server/core`, PAS ICI, et depuis le 2026-08-31. Elle avait
# besoin d'un SECOND lecteur -- le modele de lecture de la gouvernance, qui doit
# porter le verdict jusqu'a l'onglet Semantic Model -- et `scripts/` n'est pas
# dans l'image de deploiement (`infra/docker/mcp-server/Dockerfile` copie
# `server/` et `dbt/`). Une seconde copie cote serveur aurait ete exactement le
# defaut que ce script surveille : deux ecritures d'une meme formule.
sys.path.insert(0, str(ROOT / "server"))

from core.platform_semantic_concepts import (  # noqa: E402
    PARITY_PROJECT_OVERRIDE,
    declared_ratios,
    formula_parity,
    project_delivered_catalogue,
    ratio_operand_names,
)

#: `metric = 'cost'` -- comment le mart nomme une composante dans `fact_daily_kpi`.
_METRIC_LITERAL = re.compile(r"metric\s*=\s*'([a-z0-9_]+)'")

#: Un mart `semantic_*` qui ne nomme aucune composante par `metric = '...'` ne se
#: compare pas ainsi : il ponder par une colonne (`SUM(average_position *
#: impressions)`) au lieu de filtrer des lignes. Ces regles-la sont declarees ici
#: pour que leur absence soit un CHOIX inscrit et pas un oubli du detecteur.
_NON_LITERAL_RULES = frozenset(
    {"impression_weighted_average", "impression_weighted", "weighted_ratio", "max", "sum"}
)


def computed_components(metric: str) -> set[str] | None:
    """Les composantes que le mart de *metric* nomme, ou None s'il n'existe pas."""
    mart = MARTS / f"semantic_{metric}.sql"
    if not mart.exists():
        return None
    text = mart.read_text(encoding="utf-8", errors="replace")
    # Les lignes de commentaire citent les formules a l'envi -- les compter
    # ferait rougir la porte sur de la prose exacte.
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))
    return set(_METRIC_LITERAL.findall(code))


def divergences() -> list[str]:
    """Ce qui ne coincide pas. Vide = les deux copies disent la meme chose."""
    problems: list[str] = []
    ratios = declared_ratios()

    for metric, (numerator, denominator) in sorted(ratios.items()):
        if not numerator or not denominator:
            problems.append(
                f"`{metric}` est declare `ratio` dans dim_metric.csv sans numerateur "
                f"ni denominateur : la formule n'est ecrite nulle part."
            )
            continue
        components = computed_components(metric)
        if components is None:
            problems.append(
                f"`{metric}` est declare `ratio` dans dim_metric.csv et aucun mart "
                f"dbt/models/marts/semantic_{metric}.sql ne le calcule."
            )
            continue
        expected = {numerator, denominator}
        if components != expected:
            problems.append(
                f"`{metric}` : dim_metric.csv declare {sorted(expected)}, "
                f"semantic_{metric}.sql calcule sur {sorted(components)}."
            )

    # L'autre sens : un mart `semantic_*` qui nomme des composantes sans qu'aucune
    # ligne du seed ne le declare est une formule que le catalogue ignore.
    for mart in sorted(MARTS.glob("semantic_*.sql")):
        metric = mart.stem[len("semantic_") :]
        if metric in ratios:
            continue
        components = computed_components(metric) or set()
        if components:
            problems.append(
                f"semantic_{metric}.sql calcule sur {sorted(components)} et "
                f"dim_metric.csv ne declare aucun ratio `{metric}`."
            )
    return problems


# --- la troisieme copie : la formule gouvernee, en base ----------------------


def platform_ratio_expressions() -> dict[str, dict[str, Any]]:
    """`{metrique -> arbre typé}` : la reference plateforme PROJETEE depuis le seed.

    L'arbre est celui que la migration 142 ecrit (`142_semantic_model.sql:825-835`)
    pour un ratio dont les operandes sont portes par NOM. Il n'est pas invente ici :
    il est derive du catalogue livre, comme `platform_canonical_vocabulary` derive
    les champs canoniques du dictionnaire ratifie.

    `zero_denominator` vaut `'null'` parce que c'est ce que les marts CALCULENT --
    `NULLIF(SUM(denominateur), 0)` rend NULL, jamais zero ni une erreur. La
    politique n'est donc pas un defaut choisi ici, c'est la lecture du SQL servi.
    """
    concepts, _refused = project_delivered_catalogue()
    return {
        concept.name: concept.expression
        for concept in concepts
        if concept.expression.get("op") == "ratio"
    }


def expression_operands(
    expression: Mapping[str, Any] | None,
    names_by_version_id: Mapping[str, str] | None = None,
) -> tuple[str | None, str | None] | None:
    """Les DEUX operandes d'un arbre `ratio`, par nom -- le nom local de LA regle.

    Aucune logique ici : `core.platform_semantic_concepts.ratio_operand_names` la
    porte, et le modele de lecture de la gouvernance lit la meme. Ce nom reste
    parce que la garde de conformite et ce fichier l'appellent ainsi depuis
    2026-08-16.
    """
    return ratio_operand_names(expression, names_by_version_id)


def load_stored_ratios(conn) -> list[dict[str, Any]]:
    """Les Concepts-metriques dont l'expression est un ratio, operandes resolus par nom.

    Ne lit que les versions VIVANTES (`draft`, `candidate`, `published`) : une
    version `superseded` ou `archived` est une trace, pas une definition en
    vigueur, et la faire rougir demanderait de reecrire l'immuable.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT v.id, v.concept_id, v.project_id, v.name, v.status,
                   v.version_number, v.expression
            FROM app.semantic_concept_versions v
            WHERE v.kind = 'metric'
              AND v.status IN ('draft', 'candidate', 'published')
              AND v.expression IS NOT NULL
              AND v.expression ->> 'op' = 'ratio'
            ORDER BY v.project_id NULLS FIRST, v.name, v.version_number
            """
        )
        columns = [column.name for column in cur.description]
        rows = [dict(zip(columns, row)) for row in cur.fetchall()]

        # Un `concept_ref` porte un `version_id`, pas un nom. Sans cette
        # resolution, tout ratio PUBLIE serait lu comme « operandes inconnus ».
        cur.execute("SELECT id, name FROM app.semantic_concept_versions")
        names_by_version_id = {row[0]: row[1] for row in cur.fetchall()}

    for row in rows:
        operands = expression_operands(row.get("expression"), names_by_version_id)
        row["numerator"], row["denominator"] = operands or (None, None)
    return rows


def stored_divergences(
    stored: list[Mapping[str, Any]], ratios: Mapping[str, tuple[str, str]] | None = None
) -> tuple[list[str], list[str]]:
    """`(divergences, overrides_client)` entre la copie gouvernee et le catalogue.

    Rapporte, JAMAIS resolu : ce script n'ecrit dans aucune des trois copies.
    """
    catalogue = ratios if ratios is not None else declared_ratios()
    problems: list[str] = []
    overrides: list[str] = []

    for row in stored:
        name = str(row.get("name") or "")
        # LE VERDICT VIENT DE LA REGLE, pas d'une seconde comparaison ecrite ici :
        # `core.platform_semantic_concepts.formula_parity` est celle que le modele
        # de lecture de la gouvernance porte jusqu'a l'onglet Semantic Model. Les
        # PHRASES ci-dessous restent locales -- c'est la sortie d'un script, pas
        # ce qu'un ecran affiche -- mais la CLASSIFICATION est partagee.
        #
        # Les operandes ont deja ete resolus par nom dans `load_stored_ratios`, on
        # rejoue donc l'arbre minimal que la regle sait lire plutot que de relire
        # la base -- et sans consulter `row["expression"]`, que les appelants de
        # cette fonction (la garde de conformite comprise) ne portent pas tous.
        numerator, denominator = row.get("numerator"), row.get("denominator")
        verdict = formula_parity(
            name=name,
            project_id=row.get("project_id"),
            expression=_named_ratio(numerator, denominator),
            ratios=catalogue,
        )
        if verdict is None or verdict["verdict"] == "aligned":
            continue
        expected = verdict["declared"]
        if verdict["governed"] is None:
            problems.append(
                f"`{name}` (version {row.get('id')}) : son expression est un `ratio` "
                f"dont les operandes ne se lisent pas par nom. La formule gouvernee "
                f"ne peut pas etre confrontee au catalogue."
            )
        elif verdict["verdict"] == PARITY_PROJECT_OVERRIDE:
            overrides.append(
                f"`{name}` : le projet {row.get('project_id')} gouverne "
                f"`{verdict['governed']}` la ou le catalogue et le mart servent "
                f"`{expected}`. Le client a le droit de sa definition -- mais "
                f"semantic_{name} n'implemente PAS la sienne."
            )
        else:
            problems.append(
                f"`{name}` : dim_metric.csv declare `{expected}`, le Concept de "
                f"PLATEFORME {row.get('id')} gouverne `{verdict['governed']}`. Le "
                f"meme objet dit deux choses."
            )
    return problems, overrides


def _named_ratio(numerator: str | None, denominator: str | None) -> dict[str, Any]:
    """L'arbre `ratio` reecrit avec ses operandes DEJA resolus en noms.

    `load_stored_ratios` fait la resolution `concept_ref -> nom` une fois, en
    base, pour toutes les lignes, et n'en rend que des lignes dont l'expression
    EST un ratio. Rendre cet arbre a la regle lui evite un second aller-retour et
    ne change pas ce qu'elle juge : elle lit des noms.

    `None` a la place d'un operande devient un noeud d'une TROISIEME forme, que
    la regle rend `unreadable` -- c'est exactement le cas << ses operandes ne se
    lisent pas par nom >>, et le taire ferait passer la formule pour confrontee.
    """
    return {
        "op": "ratio",
        "numerator": {"op": "concept_name", "name": numerator} if numerator else {"op": "sum"},
        "denominator": (
            {"op": "concept_name", "name": denominator} if denominator else {"op": "sum"}
        ),
    }


def _dsn(explicit: str | None) -> str | None:
    """Le DSN, ou None. L'absence de base n'est pas une erreur ici -- c'est la CI."""
    if explicit:
        return explicit
    url = os.environ.get("PLATFORM_DB_URL")
    if url:
        return url
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("PLATFORM_DB_URL="):
                return line.split("=", 1)[1].strip().strip('"')
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", action="store_true", help="non-zero si une copie diverge")
    parser.add_argument("--dsn", default=None, help="la base qui porte la copie gouvernee")
    parser.add_argument(
        "--require-db",
        action="store_true",
        help="echoue si la troisieme copie n'a pas pu etre consultee",
    )
    args = parser.parse_args()

    ratios = declared_ratios()
    problems = divergences()

    print(f"{len(ratios)} ratio(s) declares dans dbt/seeds/dim_metric.csv")
    for metric, (numerator, denominator) in sorted(ratios.items()):
        print(f"  {metric:<20} {numerator} / {denominator}")
    if problems:
        print("\nDIVERGENCES depot (seed <-> mart) :")
        for problem in problems:
            print(f"  - {problem}")
    else:
        print("\nOK : chaque ratio declare est calcule sur les composantes declarees.")

    # --- la troisieme copie -------------------------------------------------
    dsn = _dsn(args.dsn)
    consulted = False
    if dsn:
        try:
            import psycopg  # noqa: PLC0415 -- un import pour un seul chemin

            with psycopg.connect(dsn) as conn:
                stored = load_stored_ratios(conn)
            consulted = True
        except Exception as error:  # noqa: BLE001 -- la raison est ce qu'on imprime
            print(f"\nCOPIE GOUVERNEE NON LUE : {type(error).__name__}: {error}")
            stored = []
        if consulted:
            governed, overrides = stored_divergences(stored, ratios)
            print(
                f"\n{len(stored)} Concept(s)-metrique(s) vivant(s) portant un `ratio` "
                f"dans app.semantic_concept_versions"
            )
            if governed:
                print("\nDIVERGENCES gouvernees (catalogue <-> Concept de plateforme) :")
                for problem in governed:
                    print(f"  - {problem}")
            else:
                print(
                    "  OK : aucun Concept de plateforme ne contredit le catalogue."
                )
            if overrides:
                print("\nOVERRIDES CLIENT (rapportes, jamais comptes comme une faute) :")
                for override in overrides:
                    print(f"  - {override}")
            problems = problems + governed
    else:
        print(
            "\nTROISIEME COPIE NON CONSULTEE : `app.semantic_concept_versions.expression` "
            "vit en base et aucun DSN n'a ete fourni (--dsn / PLATFORM_DB_URL). Cette "
            "execution ne prouve QUE la parite des deux copies du depot."
        )
        if args.require_db:
            print(
                "  --require-db etait demande : l'absence de base est l'echec, pas un "
                "silence."
            )
            return 1

    print(
        "\nREFERENCE PLATEFORME projetee depuis le seed (le chemin versionne) : "
        f"{len(platform_ratio_expressions())} arbre(s) `ratio`."
    )
    return 1 if (args.gate and problems) else 0


if __name__ == "__main__":
    sys.exit(main())
