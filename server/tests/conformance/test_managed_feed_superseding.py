"""Un fichier renvoye supersede les jours qu'il recouvre, il ne les empile pas.

CE QUE CE TEST TIENT. Renvoyer un fichier qui couvre des jours deja recus doit
n'ajouter que ce qui est nouveau et laisser gagner la version la plus recente
sur ce qui se recouvre -- exactement ce qu'un connecteur fait quand il refait ses
trois derniers jours chaque nuit. Avant ce modele, le chemin fichier empilait :
`land_raw_rows` est un INSERT, le MERGE de promotion a pour cle l'`execution_id`
(il empeche de promouvoir deux fois le meme run, jamais de reinserer les memes
jours), et aucun modele dbt ne couvrait `managed_feed`.

POURQUOI UN VRAI BUILD ET PAS UN MOCK. Le modele est du Jinja qui interroge le
miroir a la COMPILATION pour composer une requete par flux : une doublure
pytest prouverait que la doublure est d'accord avec elle-meme. Ici dbt tourne
pour de vrai sur une base DuckDB jetable, et l'assertion porte sur les lignes
rendues.

LE JEU D'ESSAI EST CELUI DE LA DEMANDE. Un premier fichier couvre trois jours ;
un second renvoie deux de ces jours -- dont une valeur CORRIGEE -- et en ajoute
un quatrieme. Six lignes brutes, quatre lignes lues.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DBT = _REPO_ROOT / "dbt"

duckdb = pytest.importorskip("duckdb")

# Measured 6.01 seconds on the reference Windows workstation (2026-08-18).
# One minute leaves a 10x loaded-machine margin while still failing this file
# promptly instead of inheriting pytest's session-killing 180-second default.
pytestmark = pytest.mark.timeout(60)

#: Deux chargements. `dse_` est un ULID monotone, donc `MAX` = le plus recent --
#: la meme propriete que `pull_id DESC` chez les connecteurs.
_FIRST_LOAD = "dse_01AAA"
_SECOND_LOAD = "dse_01BBB"


def _write_fixture(db_path: Path) -> None:
    con = duckdb.connect(str(db_path))
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS mirror")
        con.execute(
            "CREATE TABLE mirror.managed_feed_grain ("
            "project_id TEXT, org_id TEXT, datastream_id TEXT, landing_table TEXT,"
            "mapping_version_id TEXT, grain_columns TEXT, has_grain BOOLEAN)"
        )
        con.execute(
            "INSERT INTO mirror.managed_feed_grain VALUES "
            "('proj_EXAMPLE','org_EXAMPLE','ds_DEMO','managed_feed_ds_DEMO',"
            " 'dsm_1','[\"date\",\"campaign\"]',TRUE),"
            # Un flux SANS maille publiee : il doit etre exclu, pas inclus en
            # silence avec ses doublons.
            "('proj_EXAMPLE','org_EXAMPLE','ds_NOKEY','managed_feed_ds_NOKEY',"
            " 'dsm_2','[]',FALSE)"
        )
        con.execute(
            "CREATE TABLE main.managed_feed_ds_DEMO ("
            "date TEXT, campaign TEXT, clicks INTEGER, execution_id TEXT)"
        )
        con.execute(
            "INSERT INTO main.managed_feed_ds_DEMO VALUES "
            f"('2026-08-01','Summer',100,'{_FIRST_LOAD}'),"
            f"('2026-08-02','Summer',110,'{_FIRST_LOAD}'),"
            f"('2026-08-03','Summer',120,'{_FIRST_LOAD}'),"
            # Le second fichier recouvre les 02 et 03 -- avec une CORRECTION sur
            # le 02 -- et ajoute le 04.
            f"('2026-08-02','Summer',999,'{_SECOND_LOAD}'),"
            f"('2026-08-03','Summer',121,'{_SECOND_LOAD}'),"
            f"('2026-08-04','Summer',130,'{_SECOND_LOAD}')"
        )
    finally:
        con.close()


def _run_dbt(profiles_dir: Path) -> None:
    target_dir = profiles_dir.parent / "target"
    result = subprocess.run(  # noqa: S603
        [
            sys.executable, "-m", "dbt.cli.main", "run",
            "--no-version-check",
            "--profiles-dir", str(profiles_dir),
            "--project-dir", str(_DBT),
            "--target-path", str(target_dir),
            "--select", "managed_feed_superseding",
        ],
        capture_output=True,
        text=True,
        cwd=str(_DBT),
    )
    if result.returncode != 0:
        pytest.fail(f"dbt run a echoue:\n{result.stdout[-3000:]}\n{result.stderr[-2000:]}")


@pytest.mark.skipif(
    shutil.which("dbt") is None and "DBT_PROFILES_DIR" not in os.environ,
    reason="dbt absent de cet environnement",
)
def test_a_resent_file_supersedes_the_days_it_covers(tmp_path: Path) -> None:
    db_path = tmp_path / "superseding.duckdb"
    _write_fixture(db_path)

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "profiles.yml").write_text(
        "connector:\n  target: local\n  outputs:\n    local:\n"
        f"      type: duckdb\n      path: '{db_path.as_posix()}'\n      threads: 1\n",
        encoding="utf-8",
    )
    _run_dbt(profiles)

    con = duckdb.connect(str(db_path))
    try:
        index = con.execute(
            "SELECT grain_key, winning_execution_id, superseded_loads "
            "FROM main_marts.managed_feed_superseding ORDER BY grain_key"
        ).fetchall()

        # Six lignes brutes, quatre cles: le recouvrement est resolu, pas empile.
        assert con.execute(
            "SELECT count(*) FROM main.managed_feed_ds_DEMO"
        ).fetchone()[0] == 6
        assert len(index) == 4

        winners = {row[0].split(chr(31))[0]: row[1] for row in index}
        covered = {row[0].split(chr(31))[0]: row[2] for row in index}

        # Le jour que seul le PREMIER fichier portait garde son chargement.
        assert winners["2026-08-01"] == _FIRST_LOAD
        assert covered["2026-08-01"] == 1
        # Les jours recouverts passent au plus recent -- c'est ce qui fait qu'un
        # fichier corrige corrige vraiment.
        assert winners["2026-08-02"] == _SECOND_LOAD
        assert winners["2026-08-03"] == _SECOND_LOAD
        assert covered["2026-08-02"] == 2
        # Le jour nouveau est simplement ajoute.
        assert winners["2026-08-04"] == _SECOND_LOAD
        assert covered["2026-08-04"] == 1

        # La donnee qui fait foi, jointe sur l'index: la CORRECTION gagne.
        winning_rows = con.execute(
            "SELECT l.date, l.clicks FROM main.managed_feed_ds_DEMO l "
            "JOIN main_marts.managed_feed_superseding s "
            "  ON s.datastream_id = 'ds_DEMO' "
            " AND s.grain_key = COALESCE(CAST(l.date AS VARCHAR),'') || CHR(31) "
            "                   || COALESCE(CAST(l.campaign AS VARCHAR),'') "
            " AND s.winning_execution_id = l.execution_id "
            "ORDER BY l.date"
        ).fetchall()
        assert winning_rows == [
            ("2026-08-01", 100),
            ("2026-08-02", 999),
            ("2026-08-03", 121),
            ("2026-08-04", 130),
        ]

        # Le flux sans maille n'est pas dedoublonne EN SILENCE: il est absent de
        # l'index, et c'est `has_grain` qui porte la raison.
        assert all(row[0] for row in index)
        assert con.execute(
            "SELECT count(*) FROM main_marts.managed_feed_superseding "
            "WHERE datastream_id = 'ds_NOKEY'"
        ).fetchone()[0] == 0
    finally:
        con.close()
