"""La table brute d'un connecteur existe AVANT qu'une seule ligne n'arrive.

Ce que ces tests tiennent, et pourquoi il a fallu un pull reel pour le voir :
`fact_daily_kpi` fait l'UNION de la cinquantaine de modeles de staging, chacun
lisant la table brute de SON connecteur ; `provision_org_schemas` ne creait que
les SCHEMAS ; et la production lance `dbt build` SANS `--select`. Un client qui
branche deux connecteurs n'avait donc AUCUN mart -- pas un mart partiel, aucun --
donc aucun regroupement transversal, qui est le coeur du produit.
"""

from __future__ import annotations

import duckdb
import pytest
from core.raw_table_provisioning import declared_raw_tables, provision_raw_tables


def test_the_declaration_is_read_from_the_connectors_not_from_a_list_here():
    """Aucun nom de connecteur n'est ecrit dans le module (AD-2).

    La couverture est une MESURE, pas une constante : un connecteur qui cesse de
    declarer sa table le fait baisser, et c'est le signal qu'on veut.
    """
    declared = declared_raw_tables()
    assert len(declared) >= 30, (
        f"seuls {len(declared)} connecteurs declarent une table brute recoltable ; "
        "mesure du 2026-08-01 : 35 sur 38. Une chute veut dire qu'un connecteur a "
        "cesse de declarer sa DDL au niveau module -- et sa table ne sera plus "
        "provisionnee, donc son staging echouera et le mart avec."
    )
    for connector, statements in declared.items():
        assert statements, connector
        assert any("CREATE TABLE" in s.upper() for s in statements), connector


def test_provisioning_creates_the_tables_a_fresh_org_has_never_filled(tmp_path):
    db = str(tmp_path / "fresh_org.duckdb")
    report = provision_raw_tables(db, "org_probe_raw")

    assert report["failed"] == [], report["failed"]
    assert report["tables_created"] > 0

    con = duckdb.connect(db)
    try:
        tables = {
            r[0]
            for r in con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'org_probe_raw'"
            ).fetchall()
        }
    finally:
        con.close()
    assert len(tables) >= 30, f"seulement {len(tables)} tables provisionnees"
    assert all(t.startswith("raw_") for t in tables), sorted(tables)[:5]


def test_provisioning_never_touches_a_table_that_already_holds_rows(tmp_path):
    """L'idempotence n'est pas une commodite : c'est ce qui rend l'appel sur.

    Un provisionnement rejoue apres qu'un connecteur a reellement tire ne doit
    ni recreer ni vider sa table. Sans cette garantie, personne ne pourrait
    appeler cette fonction deux fois -- et une fonction qu'on n'ose pas rappeler
    n'est pas idempotente, elle est dangereuse.
    """
    db = str(tmp_path / "already_pulled.duckdb")
    provision_raw_tables(db, "org_probe_raw")

    con = duckdb.connect(db)
    try:
        target = con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'org_probe_raw' ORDER BY table_name LIMIT 1"
        ).fetchone()[0]
        columns = [
            r[0]
            for r in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'org_probe_raw' AND table_name = ?",
                [target],
            ).fetchall()
        ]
        con.execute(
            f"INSERT INTO org_probe_raw.{target} ({columns[0]}) VALUES ('sentinelle')"
        )
        before = con.execute(f"SELECT count(*) FROM org_probe_raw.{target}").fetchone()[0]
    finally:
        con.close()
    assert before == 1

    provision_raw_tables(db, "org_probe_raw")  # rejoue

    con = duckdb.connect(db)
    try:
        after = con.execute(f"SELECT count(*) FROM org_probe_raw.{target}").fetchone()[0]
    finally:
        con.close()
    assert after == 1, (
        "le provisionnement rejoue a detruit une ligne deja atterrie -- "
        "les DDL declarees doivent porter IF NOT EXISTS"
    )


def test_a_connector_that_declares_nothing_gets_nothing_invented(tmp_path):
    """On ne devine aucune table.

    Trois connecteurs ne declarent pas de DDL et ce n'est PAS un trou :
    `bigquery` lit une table externe qu'il ne cree pas, `generic` n'a pas de
    forme fixe, `github` n'atterrit pas dans cet entrepot. Leur absence du
    resultat est l'information juste ; fabriquer une table pour eux masquerait
    une decision de conception derriere un effet de bord.
    """
    empty = tmp_path / "modules_vides"
    (empty / "rien").mkdir(parents=True)
    (empty / "rien" / "connector.py").write_text("X = 1\n", encoding="utf-8")

    assert declared_raw_tables(empty) == {}

    db = str(tmp_path / "aucune.duckdb")
    report = provision_raw_tables(db, "org_probe_raw", modules_dir=empty)
    assert report["connectors_declared"] == 0
    assert report["tables_created"] == 0
    assert report["failed"] == []


@pytest.mark.parametrize("schema", ["org_a_raw", "org_b_raw"])
def test_two_orgs_get_their_own_tables_in_their_own_schema(tmp_path, schema):
    """Le schema est un parametre, jamais compose dans ce module."""
    db = str(tmp_path / "multi_org.duckdb")
    provision_raw_tables(db, schema)

    con = duckdb.connect(db)
    try:
        n = con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = ?",
            [schema],
        ).fetchone()[0]
    finally:
        con.close()
    assert n >= 30
