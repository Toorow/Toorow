"""Provisionner les tables brutes d'un org depuis la DECLARATION des connecteurs.

POURQUOI CE MODULE EXISTE. Mesure du 2026-08-01, sur un entrepot portant deux
pulls REELS (GSC, GA4) plus 39 seeds :

    dbt run --select staging   -> 37 construits, 13 en ECHEC (table brute absente)
    dbt build --select fact_daily_kpi
        -> Catalog Error: Table with name stg_ga4_user_type_daily does not exist

`fact_daily_kpi` fait l'UNION de la cinquantaine de modeles de staging, et chaque
staging lit la table brute de SON connecteur. Or `warehouse_tenancy.provision_org_schemas`
ne cree que les SCHEMAS : la table n'apparait que lorsqu'un connecteur a
reellement tourne (`CREATE TABLE IF NOT EXISTS` dans son propre code
d'atterrissage). Et la production lance `dbt build --vars '{"org": ...}'` SANS
`--select` (`core/scheduler.py:1386-1392`), donc tous les modeles.

Consequence pour un vrai client : il branche deux connecteurs, les trente-six
autres tables n'existent pas, `fact_daily_kpi` echoue -- et il n'a AUCUN mart.
Pas un mart partiel : aucun. Donc aucun regroupement transversal, qui est le
coeur du produit.

CE QUE CE MODULE FAIT. Il recolte le DDL que chaque connecteur DECLARE deja et
le rejoue a la creation de l'org. La table brute n'est donc plus le residu du
premier pull : c'est la forme declaree par la plateforme, disponible avant qu'une
seule ligne n'arrive. Son pendant semantique existe deja et n'est pas duplique
ici -- `core.metric_semantics_bootstrap.bootstrap_org_source_mappings` seme les
definitions de metriques par defaut depuis les memes manifestes.

AD-2 : aucun nom de connecteur n'apparait dans ce fichier. Le repertoire des
modules est parcouru, les constantes SQL sont lues sur le module importe -- donc
les f-strings de niveau module sont deja resolues, comme le fait deja
`tests/conformance/test_raw_landing_translates.py` pour le traducteur BigQuery.
Une declaration, deux consommateurs.

CE QU'IL NE FAIT PAS. Il ne devine aucune table : un connecteur qui ne declare
ni `CREATE TABLE` ni couple `<PREFIXE>_TABLE`/`<PREFIXE>_COLUMNS` n'en obtient
pas. Mesure du 2026-08-17 : 37 connecteurs sur 39 sont recoltables ; les deux
autres sont explicables et ne sont pas des trous -- `generic` n'a pas de forme
fixe, `github` n'atterrit pas dans cet entrepot. (`bigquery` s'y est ajoute : il
LIT une table externe qu'il ne cree pas, mais il ATTERRIT dans
`raw_bigquery_daily`, qui est bien la sienne et que son staging lit.)
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MODULES_DIR = Path(__file__).resolve().parent.parent / "modules"

#: Une instruction de creation de table, et le nom qu'elle cree.
_CREATE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z0-9_.\"]+)", re.IGNORECASE
)


def _harvest(connector_py: Path) -> list[str]:
    """Les instructions CREATE TABLE d'un connecteur, APRES import.

    Apres import et non a l'AST : une DDL assemblee en f-string de niveau module
    n'existe pas dans le texte source. C'est le meme choix -- et pour la meme
    raison -- que la garde de traduction BigQuery.
    """
    name = f"_rawprov_{connector_py.parent.name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(name, connector_py)
    if spec is None or spec.loader is None:
        return []
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 -- un module qui ne s'importe pas seul
        logger.debug(
            "raw_table_provisioning: %s n'importe pas isolement (%s)",
            connector_py.parent.name,
            exc,
        )
        return []
    finally:
        sys.modules.pop(name, None)

    members = vars(module)
    statements: list[str] = []
    for value in members.values():
        if isinstance(value, str) and _CREATE.search(value):
            statements.append(value)

    # LES TABLES DECLAREES, rendues comme le connecteur les rend.
    #
    # Depuis la reparation « une seule declaration » (2026-08-17), un connecteur
    # n'expose plus sa DDL comme constante : il declare `<PREFIXE>_TABLE` et
    # `<PREFIXE>_COLUMNS`, et c'est `core.raw_landing` qui rend l'instruction.
    # Ne lire que les constantes SQL faisait DISPARAITRE huit connecteurs de ce
    # provisionnement -- sans erreur, puisqu'un connecteur absent du resultat est
    # une information legitime ici. Leurs tables n'auraient plus ete creees a la
    # naissance de l'org, et `fact_daily_kpi` aurait re-echoue exactement comme
    # le decrit l'en-tete de ce fichier.
    for name, value in members.items():
        if not (name.endswith("_TABLE") and isinstance(value, str)):
            continue
        columns = members.get(name[: -len("_TABLE")] + "_COLUMNS")
        if not _is_column_declaration(columns):
            continue
        from core.raw_landing import duckdb_ddl  # noqa: PLC0415 -- import tardif

        statements.append(duckdb_ddl(value, columns))
    return statements


def _is_column_declaration(value: Any) -> bool:
    """`[(nom, type), ...]` -- la forme que `core.raw_landing` sait rendre."""
    return (
        isinstance(value, list)
        and bool(value)
        and all(
            isinstance(item, tuple)
            and len(item) == 2
            and all(isinstance(part, str) for part in item)
            for item in value
        )
    )


def declared_raw_tables(modules_dir: str | Path | None = None) -> dict[str, list[str]]:
    """{connecteur: [instructions CREATE TABLE declarees]} -- PURE, aucune base.

    Un connecteur absent du resultat n'a rien declare : ce n'est pas une erreur,
    c'est une information, et l'appelant la journalise plutot que de la combler.
    """
    root = Path(modules_dir) if modules_dir else _MODULES_DIR
    found: dict[str, list[str]] = {}
    for connector_py in sorted(root.glob("*/connector.py")):
        statements = _harvest(connector_py)
        if statements:
            found[connector_py.parent.name] = statements
    return found


def _qualify(statement: str, schema: str) -> str:
    """Prefixer la table creee par le schema de l'org, sans toucher au reste."""

    def repl(match: re.Match) -> str:
        table = match.group(1).strip('"')
        if "." in table:  # deja qualifie -- on ne re-qualifie pas
            return match.group(0)
        return match.group(0).replace(match.group(1), f"{schema}.{table}")

    return _CREATE.sub(repl, statement, count=1)


def provision_raw_tables(
    duckdb_path: str,
    schema: str,
    *,
    modules_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Creer, dans *schema*, la table brute declaree par chaque connecteur.

    Idempotent : les DDL declarees portent `IF NOT EXISTS`. Une table qui existe
    deja -- parce qu'un pull a tourne -- n'est ni recreee ni modifiee, donc cette
    fonction ne peut pas ecraser de donnee.

    Non bloquante par contrat, comme le provisionnement de schemas : l'echec d'un
    connecteur est journalise et NOMME dans le rapport, il n'interrompt pas les
    autres. Un org a qui il manque une table le saura par le rapport, pas par un
    mart qui echoue trois heures plus tard.
    """
    import duckdb  # noqa: PLC0415

    declared = declared_raw_tables(modules_dir)
    created: list[str] = []
    failed: list[dict[str, str]] = []

    conn = duckdb.connect(duckdb_path)
    try:
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        for connector, statements in declared.items():
            for statement in statements:
                try:
                    conn.execute(_qualify(statement, schema))
                    created.append(connector)
                except Exception as exc:  # noqa: BLE001
                    failed.append({"connector": connector, "error": str(exc)[:200]})
                    logger.warning(
                        "raw_table_provisioning: %s a echoue dans %s: %s",
                        connector,
                        schema,
                        exc,
                    )
    finally:
        conn.close()

    return {
        "schema": schema,
        "connectors_declared": len(declared),
        "tables_created": len(created),
        "failed": failed,
    }
