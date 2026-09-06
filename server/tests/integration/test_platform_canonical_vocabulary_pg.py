"""La projection ecrit vraiment, une seule fois, et n'ecrase jamais (AI-288).

Trois proprietes ne se prouvent nulle part ailleurs, et chacune a deja ete la
forme d'un vrai defaut ici :

  * le registre etait VIDE alors que six modules le lisaient -- donc ce qui
    compte est le nombre de lignes qu'un projet neuf VOIT, pas le nombre de
    lignes que la fonction a rendues ;
  * relancer doit etre sans effet : un provisionnement qui re-minte est un
    doublon dans un vocabulaire partage par toute l'instance ;
  * une ligne qu'une personne a modifiee en base doit SURVIVRE au script. Entre
    le depot et un registre vivant, choisir un camp detruit l'autre.

`live_postgres` fait le rollback au teardown.
"""

from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from core import platform_canonical_vocabulary as vocabulary  # noqa: E402
from core.canonical_field_registry import list_visible_canonical_fields  # noqa: E402


@pytest.fixture(autouse=True)
def _no_platform_residue(live_postgres):
    """This suite measures what IT mints, never what another suite left behind.

    Measured 2026-08-17: two tests here were red on a shared disposable base for
    ONE row -- `cost`, PLATFORM scope, `created_by='test'`, `dictionary_field_name`
    NULL. `provision` mints with `ON CONFLICT DO NOTHING`, so a pre-existing row
    is not corrected: the assertion `row["dictionary_field_name"] == name` then
    read `None` and the suite accused the writer, which is correct
    (`platform_canonical_vocabulary.py:255-273` carries the column).

    The residue is not this suite's fault either -- `test_datastream_mapping_constraints.py`
    mints PLATFORM rows (`spend`, `country`) with the same actor and cleans none
    of them. But a suite that only passes when it runs first is a suite that will
    accuse the product for someone else's row, and that is the failure this
    fixture removes. It deletes ONLY rows an actor named `test` minted at
    PLATFORM scope: a real platform vocabulary carries a real actor.
    """
    with live_postgres.cursor() as cur:
        cur.execute(
            "DELETE FROM app.mdm_canonical_fields "
            "WHERE project_id IS NULL AND created_by = 'test'"
        )
    live_postgres.commit()
    yield


def _platform_rows(conn) -> dict[str, dict]:
    return vocabulary.load_platform_registry(conn)


def test_the_dictionary_lands_and_a_project_finally_sees_a_vocabulary(live_postgres):
    """L'ETAT FINAL, jamais le compte de ce qui a ete minte a ce run.

    Assertion posee puis corrigee le 2026-08-15 : la premiere version exigeait
    `minted >= 13`, ce qui n'est vrai que sur un registre vide -- donc une seule
    fois par base. Un test qui ne passe qu'au premier run mesure l'historique du
    cluster, pas le comportement.
    """
    report = vocabulary.provision(live_postgres, actor="tester")

    assert report["divergences"] == []
    stored = _platform_rows(live_postgres)
    #  Tout ce que le depot projette est en base, quel que soit le run qui l'y a mis.
    assert set(report["projected"]) <= set(stored)
    assert len(report["projected"]) >= 13

    visible = list_visible_canonical_fields(
        live_postgres, project_id="proj_EXAMPLE0000000000000000"
    )
    assert {field["canonical_name"] for field in visible} >= set(report["projected"])

    #  Le lien de derivation est LISIBLE en base, pas seulement dans le script --
    #  et il ne se porte QUE sur ce qui vient du dictionnaire. Un champ nomme par
    #  un manifeste ne derive d'aucune ligne du dictionnaire, et le pretendre
    #  casserait la cle etrangere de la migration 032.
    dictionary_names = {row["name"] for row in vocabulary.load_dictionary(live_postgres)}
    for name, row in stored.items():
        if name in dictionary_names:
            assert row["dictionary_field_name"] == name
        else:
            assert row["dictionary_field_name"] is None

    #  Et le vocabulaire deborde largement le dictionnaire : c'est tout le propos.
    assert len(stored) > len(dictionary_names)


def test_running_it_twice_declares_nothing_the_second_time(live_postgres):
    """Un vocabulaire partage par toute l'instance ne prend pas de doublon."""
    vocabulary.provision(live_postgres, actor="tester")
    first = _platform_rows(live_postgres)

    second_report = vocabulary.provision(live_postgres, actor="tester")

    assert second_report["minted"] == []
    assert _platform_rows(live_postgres).keys() == first.keys()


def test_a_row_someone_edited_survives_the_script_and_is_reported(live_postgres):
    """La retenue du script est sa raison d'etre : il constate, il ne tranche pas."""
    vocabulary.provision(live_postgres, actor="tester")
    with live_postgres.cursor() as cur:
        cur.execute(
            "UPDATE app.mdm_canonical_fields SET value_type = 'decimal' "
            "WHERE project_id IS NULL AND canonical_name = 'cost'"
        )

    report = vocabulary.provision(live_postgres, actor="tester")

    assert report["minted"] == []
    assert any("cost" in problem and "decimal" in problem for problem in report["divergences"])
    #  Et surtout : la valeur editee est TOUJOURS la.
    assert _platform_rows(live_postgres)["cost"]["value_type"] == "decimal"


def test_the_non_additive_metric_carries_no_aggregation_in_the_database(live_postgres):
    """La CHECK de la migration 032 accepte l'un OU l'autre ; la projection choisit."""
    vocabulary.provision(live_postgres, actor="tester")

    stored = _platform_rows(live_postgres)
    assert stored["average_position"]["non_additive"] is True
    assert stored["average_position"]["aggregation"] is None
    assert stored["cost"]["aggregation"] == "sum"
    assert stored["cost"]["non_additive"] is False


def test_a_binding_can_now_name_a_platform_field(live_postgres):
    """Le point de tout ceci : la enumeration fermee n'est plus fermee sur rien."""
    vocabulary.provision(live_postgres, actor="tester")

    visible = {
        field["canonical_name"]
        for field in list_visible_canonical_fields(
            live_postgres, project_id="proj_EXAMPLE0000000000000000"
        )
    }

    assert {"date", "country", "cost", "clicks", "impressions"} <= visible


def test_minting_the_platform_vocabulary_leaves_a_journal(live_postgres):
    """LE MINT N ECRIVAIT RIEN, mesure du 2026-08-16.

    Un champ de plateforme change ce que TOUT projet de l instance aligne -- c est
    la phrase par laquelle `canonical_field_registry` refuse cette porte a un
    client. Une decision de cette portee ne laissait aucune ligne : `d ou vient ce
    nom` n avait pas de reponse dans le depot.

    L action est DISTINCTE de celle du mint projet, a dessein : lire le journal ne
    doit pas demander de deduire la portee d un `project_id` absent.
    """
    report = vocabulary.provision(live_postgres, actor="tester@example.com")
    assert report["minted"], "rien n a ete minte -- le test ne prouverait rien"

    with live_postgres.cursor() as cur:
        cur.execute(
            """
            SELECT identity, metadata->>'scope', metadata->>'canonical_name',
                   metadata->>'canonical_field_id', metadata->>'derived_from'
              FROM app.audit_log
             WHERE action = 'platform_canonical_field.declared'
            """
        )
        rows = cur.fetchall()

    assert len(rows) == len(report["minted"]), "un champ minte, une ligne de journal"
    assert {row[0] for row in rows} == {"tester@example.com"}
    assert {row[1] for row in rows} == {"platform"}
    # Ce que la ligne doit permettre de retrouver : le nom, l identite mintee, et
    # D OU il vient -- le dictionnaire gouverne ou l extension par les connecteurs.
    journalled = {row[2] for row in rows}
    assert {"cost", "clicks", "impressions"} <= journalled
    assert all(row[3] and row[3].startswith("mdm_") for row in rows)
    assert {row[4] for row in rows} <= {"target_fields", "connector_manifests"}


def test_a_second_run_journals_nothing_because_it_mints_nothing(live_postgres):
    """Une ligne de journal par ECRITURE, jamais par passage du script."""
    vocabulary.provision(live_postgres, actor="tester@example.com")
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.audit_log "
            "WHERE action = 'platform_canonical_field.declared'"
        )
        after_first = cur.fetchone()[0]

    second = vocabulary.provision(live_postgres, actor="tester@example.com")
    assert second["minted"] == []

    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.audit_log "
            "WHERE action = 'platform_canonical_field.declared'"
        )
        assert cur.fetchone()[0] == after_first


def test_minting_and_archiving_a_project_field_leave_a_journal(live_postgres):
    """La moitie PROJET du meme constat -- elle n ecrivait rien non plus.

    Le mint et l archivage comptent chacun pour une raison distincte : le mint
    ajoute un nom que six modules de production valideront, l archivage LIBERE
    ce nom (l index unique partiel exclut les lignes archivees), donc deux champs
    successifs peuvent le porter sans que rien ne raconte le passage.
    """
    from core.canonical_field_registry import archive_project_field, declare_project_field

    project_id = "proj_EXAMPLE0000000000000000"
    minted = declare_project_field(
        live_postgres,
        project_id=project_id,
        canonical_name="episode_id",
        concept_kind="dimension",
        value_type="string",
        actor="owner@example.com",
    )

    # LU SUR LE CHAMP QUE CE TEST VIENT DE MINTER, jamais sur l action entiere :
    # `app.audit_log` est append-only (aucun `_no_platform_residue` ne peut le
    # nettoyer) et d autres suites de ce repertoire y laissent leurs propres
    # `canonical_field.declared` -- des `probe_dimension_*` d une base partagee.
    # Compter toute l action mesurait l ordre des fichiers, pas l ecriture.
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT identity, metadata->>'scope', metadata->>'canonical_name', "
            "       metadata->>'canonical_field_id', metadata->>'concept_kind' "
            "  FROM app.audit_log WHERE action = 'canonical_field.declared' "
            "   AND metadata->>'canonical_field_id' = %s",
            (minted["id"],),
        )
        rows = cur.fetchall()
    assert rows == [
        ("owner@example.com", "project", "episode_id", minted["id"], "dimension")
    ]

    archive_project_field(
        live_postgres, project_id=project_id, field_id=minted["id"], actor="owner@example.com"
    )
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT metadata->>'released_name', metadata->>'canonical_field_id' "
            "  FROM app.audit_log WHERE action = 'canonical_field.archived' "
            "   AND metadata->>'canonical_field_id' = %s",
            (minted["id"],),
        )
        assert cur.fetchall() == [("episode_id", minted["id"])]


def test_a_refused_mint_writes_no_journal_line(live_postgres):
    """Une ligne de journal sans champ est aussi fausse qu un champ sans ligne.

    Les deux ecritures partagent la transaction de l appelant -- c est pour cela
    que `insert_audit_row` prend la connexion plutot que `write_audit_row`, qui
    aurait ouvert la sienne et commite seule.
    """
    from core.canonical_field_registry import CanonicalFieldError, declare_project_field

    project_id = "proj_EXAMPLE0000000000000000"
    minted = declare_project_field(
        live_postgres,
        project_id=project_id,
        canonical_name="episode_id",
        concept_kind="dimension",
        value_type="string",
        actor="owner@example.com",
    )
    with pytest.raises(CanonicalFieldError):
        declare_project_field(
            live_postgres,
            project_id=project_id,
            canonical_name="episode_id",
            concept_kind="dimension",
            value_type="string",
            actor="owner@example.com",
        )

    # Meme raison qu au-dessus : la ligne comptee est celle du champ minte ici.
    # Le refus ne peut pas en ecrire une deuxieme sous cet id, et il n a pas
    # d id a lui -- c est exactement ce que dit "aucune trace".
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.audit_log "
            " WHERE action = 'canonical_field.declared' "
            "   AND metadata->>'project_id' = %s "
            "   AND metadata->>'canonical_name' = 'episode_id'",
            (project_id,),
        )
        assert cur.fetchone()[0] == 1, "le mint refuse a laisse une trace"
    assert minted["canonical_name"] == "episode_id"
