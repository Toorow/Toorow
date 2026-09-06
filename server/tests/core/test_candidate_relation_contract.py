"""La reference publiee designe la relation qui existe -- les DEUX bouts, ensemble.

POURQUOI CE FICHIER EXISTE, et pourquoi il ne ressemble a aucun test voisin.
Trois contrats se rencontrent sur une seule chaine, `artifact_ref`, et chacun
etait teste SEUL :

  1. `raw_landing.candidate_table` NOMME la relation isolee reellement ecrite.
  2. `datastream_activation` REFUSE une reference qui ne porte pas l'execution_id
     (`:1069`) -- la tracabilite de l'isolement.
  3. `query_execution._safe_identifier` REFUSE ce qui n'est pas un identifiant SQL
     -- l'anti-injection.

Chacun avait ses tests, tous verts, et la chaine ne pouvait pas fonctionner : le
driver publiait `execution/<id>/candidate/relation`, qui satisfait (2) et jamais
(3). Le defaut etait ecrit noir sur blanc a ses deux extremites et invisible tant
qu'on regardait une extremite a la fois. Aucune execution n'etait necessaire pour
le voir -- seulement lire les deux cotes ensemble, ce qu'aucun test ne faisait.

C'est ce que ce fichier fait, et c'est tout ce qu'il fait.
"""

from __future__ import annotations

import glob
import os
import re

import pytest
from core.query_execution import _IDENTIFIER
from core.raw_landing import candidate_execution, candidate_table, landed_candidate_relations

#: Un execution_id de la forme reellement mintee (`datastream_publication:210`).
REAL_EXECUTION_ID = "dse_01KYMBJX3B8YSJZA9TGTXZN5GF"

_CREATE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z0-9_]+)", re.IGNORECASE)


def _declared_raw_tables() -> list[str]:
    """Les tables brutes que les connecteurs declarent, lues a la source."""
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "modules")
    names: set[str] = set()
    for path in glob.glob(os.path.join(root, "*", "connector.py")):
        with open(path, encoding="utf-8") as handle:
            for match in _CREATE.finditer(handle.read()):
                names.add(match.group(1))
    return sorted(names)


def test_every_declared_raw_table_survives_both_ends():
    """Le nom isole de CHAQUE connecteur satisfait l'activation ET la lecture.

    C'est la garde qui manquait. Le 2026-08-01, avec la borne d'identifiant a 62,
    CINQ des 51 tables echouaient ici -- et seulement les noms les plus longs,
    donc le defaut ne serait apparu que chez le client ayant branche le mauvais
    connecteur.
    """
    tables = _declared_raw_tables()
    assert tables, "aucune table brute declaree -- l'instrument est casse, pas le code"

    refused_by_reader: list[str] = []
    missing_execution: list[str] = []
    for table in tables:
        isolated = candidate_table(table, REAL_EXECUTION_ID)
        if REAL_EXECUTION_ID not in isolated:
            missing_execution.append(isolated)
        if not _IDENTIFIER.match(isolated):
            refused_by_reader.append(f"{isolated} ({len(isolated)} car.)")

    assert not missing_execution, (
        "l'activation refusera ces references : elles ne portent pas l'execution_id "
        f"({datastream_activation_rule()}) -> {missing_execution}"
    )
    assert not refused_by_reader, (
        "query_execution._safe_identifier refusera ces relations, donc elles seront "
        "silencieusement illisibles alors qu'elles existent dans l'entrepot -> "
        f"{refused_by_reader}"
    )


def datastream_activation_rule() -> str:
    return "datastream_activation.py:1071, `execution_id not in artifact_ref`"


def test_the_file_family_landing_survives_both_ends_too():
    """La meme garde pour les sources FICHIER -- c'etait une reparation de classe.

    Le defaut avait ete corrige pour `connector_pull` seul. Le driver
    `managed_feed` publiait toujours `execution/<id>/candidate/relation`, donc
    tout Datastream fichier publie restait illisible au maillon 9 alors que le
    connecteur, lui, etait lisible. Un test qui ne regarde qu'une famille laisse
    passer exactement cela : ce cas tient l'autre bout du meme contrat.

    Le nom d'atterrissage vient de `managed_feed_ledger.allocate_landing_relation`
    (`managed_feed_<datastream_id>`), qui le rend qualifie ; `_land_managed_rows`
    ne passe que la table nue a `land_raw_rows`, et c'est elle qui est isolee.
    """
    from core.managed_feed_ledger import allocate_landing_relation

    datastream_id = "ds_01KYMBJX3B8YSJZA9TGTXZN5GF"
    qualified = allocate_landing_relation(
        datastream_id=datastream_id, project_id="proj_EXAMPLE", raw_schema=None
    )
    table = qualified.rsplit(".", 1)[-1]
    isolated = candidate_table(table, REAL_EXECUTION_ID)

    assert REAL_EXECUTION_ID in isolated, (
        f"l'activation refusera cette reference ({datastream_activation_rule()}) : {isolated}"
    )
    assert _IDENTIFIER.match(isolated), (
        "query_execution._safe_identifier refusera cette relation, donc tout "
        f"Datastream fichier publie sera illisible -> {isolated} ({len(isolated)} car.)"
    )


def test_the_managed_import_records_the_relation_the_driver_publishes(monkeypatch, tmp_path):
    """Le driver fichier LIT l'atterrissage, il ne le reconstruit pas.

    Meme geste que le cote connecteur : l'observation est enregistree a la
    couture partagee `land_raw_rows`, que l'import gere emprunte
    (`import_landing._land_managed_rows`), et le ledger ADOPTE l'execution
    ambiante -- c'est ce qui fait coincider la cle du registre avec l'execution
    d'activation.
    """
    from core import raw_landing

    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "landing.duckdb"))

    execution_id = "dse_01FILELANDINGRECORD00000000"
    table = "managed_feed_ds_01KYMBJX3B8YSJZA9TGTXZN5GF"
    with candidate_execution(execution_id):
        raw_landing.land_raw_rows(
            table,
            [{"date": "2026-08-01", "clicks": 3}],
            columns=[("date", "STRING"), ("clicks", "INTEGER")],
            project_id="proj_EXAMPLE",
        )

    landed = landed_candidate_relations(execution_id)
    assert landed == [candidate_table(table, execution_id)], landed
    assert _IDENTIFIER.match(landed[0]), landed[0]


def test_the_landing_records_what_it_actually_wrote(monkeypatch, tmp_path):
    """La reference vient d'une OBSERVATION, pas d'une reconstruction.

    Reconstruire le nom chez l'appelant marche jusqu'au jour ou la regle de
    nommage change d'un seul cote. L'enregistrer a l'atterrissage ne peut pas
    deriver : c'est le nom qui a servi.
    """
    from core import raw_landing

    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "landing.duckdb"))

    execution_id = "dse_01TESTLANDINGRECORD00000000"
    with candidate_execution(execution_id):
        raw_landing.land_raw_rows(
            "raw_probe_daily",
            [{"date": "2026-08-01", "value": 1}],
            columns=[("date", "STRING"), ("value", "INTEGER")],
            project_id="proj_EXAMPLE",
        )

    landed = landed_candidate_relations(execution_id)
    assert landed == [candidate_table("raw_probe_daily", execution_id)], landed
    assert _IDENTIFIER.match(landed[0]), landed[0]


def test_an_execution_that_landed_nothing_publishes_no_relation():
    """Une fenetre sans donnee n'invente pas de relation.

    Elle ne doit pas non plus etre confondue avec un echec : le driver garde alors
    l'ancienne reference de tracabilite, et `query_execution` la refusera -- ce
    qui est le comportement JUSTE, puisqu'il n'y a rien a lire. Ce test tient la
    distinction, pas une valeur.
    """
    assert landed_candidate_relations("dse_01NEVERLANDEDANYTHING0000") == []


@pytest.mark.parametrize(
    "hostile",
    [
        "execution/dse_01ABC/candidate/relation",
        'raw_gsc_daily"; DROP TABLE users; --',
        "raw gsc daily",
        "1_starts_with_a_digit",
        "",
    ],
)
def test_the_reader_still_refuses_what_is_not_an_identifier(hostile):
    """La borne de longueur a bouge ; la classe de caracteres n'a pas bouge.

    C'est elle qui protege de l'injection -- un nom sans guillemet, sans espace et
    sans point-virgule ne peut pas s'echapper de sa position. Relacher la longueur
    ne relache pas la garde, et ce test le prouve plutot que de l'affirmer.
    """
    assert not _IDENTIFIER.match(hostile)


def test_the_length_bound_admits_the_longest_real_name_and_not_an_unbounded_one():
    longest = max(_declared_raw_tables(), key=len)
    isolated = candidate_table(longest, REAL_EXECUTION_ID)
    assert _IDENTIFIER.match(isolated), f"{isolated} ({len(isolated)} car.)"
    assert not _IDENTIFIER.match("a" * 200)
