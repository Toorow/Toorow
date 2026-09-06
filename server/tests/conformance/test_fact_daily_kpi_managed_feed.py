"""Les faits d'un fichier entrent dans le mart canonique (story 69.2).

CE QUE CE TEST FERME. L'audit du 2026-08-20 (R1) : la donnee d'un managed feed
atterrissait, se dedoublonnait (69.1) et s'arretait la. Aucun rapport, aucune
alerte, aucun insight ne la voyait. Ici `fact_daily_kpi` est BATI pour de vrai
sur une base DuckDB jetable -- la branche est du Jinja qui interroge le miroir
a la compilation, une doublure prouverait seulement qu'elle est d'accord avec
elle-meme -- et les assertions portent sur les lignes du mart.

CE QUI EST PROUVE :

  * les lignes du fichier deviennent des lignes de mart, au jour, avec la
    mesure declaree et l'axe declare (AC1) ;
  * `connector` porte le DATASTREAM, pas une famille : deux fichiers d'un meme
    projet portant la meme metrique le meme jour ne s'ecrasent pas et ne
    s'additionnent pas -- le test de maille unique (AI-05) tourne pour de vrai
    dessus (AC2) ;
  * une mesure declaree non additive n'entre JAMAIS (AD-4), et ce qui entre
    porte l'`execution_id` du chargement qui fait foi (AC3) ;
  * un projet sans fichier, un flux sans jour, un miroir anterieur a la
    migration 299 : trois absences ordinaires, aucun echec de build (AC4).
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

pytestmark = pytest.mark.timeout(120)

_FIRST_LOAD = "dse_01AAA"
_SECOND_LOAD = "dse_01BBB"

_MIRROR_DDL = (
    "CREATE TABLE mirror.managed_feed_grain ("
    "project_id TEXT, org_id TEXT, datastream_id TEXT, landing_table TEXT,"
    "mapping_version_id TEXT, grain_columns TEXT, has_grain BOOLEAN,"
    "date_column TEXT, dimension_columns TEXT, measure_columns TEXT,"
    "non_additive_columns TEXT)"
)

_EXECUTION_DDL = (
    "CREATE TABLE mirror.managed_feed_execution ("
    "project_id TEXT, org_id TEXT, datastream_id TEXT, execution_id TEXT,"
    "loaded_at TIMESTAMP)"
)

#: Le miroir TEL QU'IL ETAIT avant la migration 299 : la maille, sans la
#: classification ni les heures de chargement. Un build doit s'en accommoder.
_MIRROR_DDL_PRE_299 = (
    "CREATE TABLE mirror.managed_feed_grain ("
    "project_id TEXT, org_id TEXT, datastream_id TEXT, landing_table TEXT,"
    "mapping_version_id TEXT, grain_columns TEXT, has_grain BOOLEAN)"
)


def _landing_ddl(table: str) -> str:
    return (
        f"CREATE TABLE main.{table} ("
        "date TEXT, campaign TEXT, clicks INTEGER, ctr DOUBLE, grain_key TEXT,"
        "execution_id TEXT, plan_version_id TEXT, mapping_version_id TEXT,"
        "project_id TEXT)"
    )


def _insert_landing(con, table: str, rows) -> None:
    con.executemany(
        f"INSERT INTO main.{table} VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                date,
                campaign,
                clicks,
                0.5,  # ctr : declaree non additive, ne doit JAMAIS etre sommee
                f"grainhash-{date}-{campaign}",
                execution_id,
                "dsp_1",
                "dsm_1",
                "proj_EXAMPLE",
            )
            for date, campaign, clicks, execution_id in rows
        ],
    )


def _write_fixture(db_path: Path) -> None:
    con = duckdb.connect(str(db_path))
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS mirror")
        con.execute(_MIRROR_DDL)
        con.execute(
            "INSERT INTO mirror.managed_feed_grain VALUES "
            # Deux flux du MEME projet, meme mesure, memes jours : la preuve que
            # `connector` doit porter le Datastream.
            "('proj_EXAMPLE','org_EXAMPLE','ds_DEMO','managed_feed_ds_DEMO',"
            " 'dsm_1','[\"date\",\"campaign\"]',TRUE,"
            " 'date','[\"campaign\"]','[\"clicks\"]','[\"ctr\"]'),"
            "('proj_EXAMPLE','org_EXAMPLE','ds_OTHER','managed_feed_ds_OTHER',"
            " 'dsm_1','[\"date\",\"campaign\"]',TRUE,"
            " 'date','[\"campaign\"]','[\"clicks\"]','[]'),"
            # Un flux dont la maille ne porte AUCUN jour : exclu en le disant.
            "('proj_EXAMPLE','org_EXAMPLE','ds_NODATE','managed_feed_ds_NODATE',"
            " 'dsm_1','[\"campaign\"]',TRUE,"
            " NULL,'[\"campaign\"]','[\"clicks\"]','[]'),"
            # Un flux sans maille publiee : deja exclu par le superseding.
            "('proj_EXAMPLE','org_EXAMPLE','ds_NOKEY','managed_feed_ds_NOKEY',"
            " 'dsm_2','[]',FALSE,NULL,'[]','[]','[]')"
        )
        con.execute(_EXECUTION_DDL)
        con.execute(
            "INSERT INTO mirror.managed_feed_execution VALUES "
            f"('proj_EXAMPLE','org_EXAMPLE','ds_DEMO','{_FIRST_LOAD}',"
            " TIMESTAMP '2026-08-01 04:00:00'),"
            f"('proj_EXAMPLE','org_EXAMPLE','ds_DEMO','{_SECOND_LOAD}',"
            " TIMESTAMP '2026-08-03 04:00:00'),"
            f"('proj_EXAMPLE','org_EXAMPLE','ds_OTHER','{_FIRST_LOAD}',"
            " TIMESTAMP '2026-08-01 05:00:00'),"
            f"('proj_EXAMPLE','org_EXAMPLE','ds_NODATE','{_FIRST_LOAD}',"
            " TIMESTAMP '2026-08-01 06:00:00')"
        )
        for table in ("managed_feed_ds_DEMO", "managed_feed_ds_OTHER", "managed_feed_ds_NODATE"):
            con.execute(_landing_ddl(table))
        _insert_landing(
            con,
            "managed_feed_ds_DEMO",
            [
                ("2026-08-01", "Summer", 100, _FIRST_LOAD),
                ("2026-08-02", "Summer", 110, _FIRST_LOAD),
                # Le second chargement CORRIGE le 02 -- seule la correction doit
                # atteindre le mart.
                ("2026-08-02", "Summer", 999, _SECOND_LOAD),
            ],
        )
        _insert_landing(
            con,
            "managed_feed_ds_OTHER",
            [("2026-08-01", "Summer", 7, _FIRST_LOAD)],
        )
        _insert_landing(
            con,
            "managed_feed_ds_NODATE",
            [("2026-08-01", "Summer", 42, _FIRST_LOAD)],
        )
    finally:
        con.close()


def _run_dbt(profiles_dir: Path, *args: str) -> str:
    target_dir = profiles_dir.parent / "target"
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "dbt.cli.main",
            *args,
            "--no-version-check",
            "--profiles-dir",
            str(profiles_dir),
            "--project-dir",
            str(_DBT),
            "--target-path",
            str(target_dir),
        ],
        capture_output=True,
        text=True,
        cwd=str(_DBT),
    )
    if result.returncode != 0:
        pytest.fail(
            f"dbt {' '.join(args)} a echoue:\n{result.stdout[-4000:]}\n{result.stderr[-2000:]}"
        )
    return result.stdout


def _write_profiles(tmp_path: Path, db_path: Path) -> Path:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "profiles.yml").write_text(
        "connector:\n  target: local\n  outputs:\n    local:\n"
        f"      type: duckdb\n      path: '{db_path.as_posix()}'\n      threads: 1\n",
        encoding="utf-8",
    )
    return profiles


#: La lignee que construit un projet dont la seule source est un fichier.
_LINEAGE = "managed_feed_superseding stg_managed_feed_facts fact_daily_kpi"

#: Les graines que les GARDES du mart lisent -- `dim_metric` porte la liste des
#: metriques non additives sur laquelle `fact_kpi_metric_additive_only`
#: s'appuie. Sans elle, le garde ne mesure rien et se croit vert.
_SEEDS = "dim_metric"

#: Les tests de SCHEMA du mart -- maille unique (AI-05) et interdit
#: d'additivite (AD-4). Les tests singuliers du depot croisent d'autres marts
#: (stripe, tiktok...) qu'un projet fichier-seul n'a pas ; les inclure
#: mesurerait l'absence de ces connecteurs, pas cette branche.
_MART_TESTS = "fact_daily_kpi,test_type:generic"

_needs_dbt = pytest.mark.skipif(
    shutil.which("dbt") is None and "DBT_PROFILES_DIR" not in os.environ,
    reason="dbt absent de cet environnement",
)


@_needs_dbt
def test_imported_facts_reach_the_canonical_mart(tmp_path: Path) -> None:
    db_path = tmp_path / "mart.duckdb"
    _write_fixture(db_path)
    profiles = _write_profiles(tmp_path, db_path)

    # Le vrai runner EXCLUT les stagings des connecteurs absents du projet
    # (`fact_daily_kpi` en-tete) : selectionner `+fact_daily_kpi` ici ferait
    # echouer 52 stagings dont aucun n'appartient a cette story. On construit la
    # lignee de la branche, telle qu'un projet fichier-seul la construit.
    _run_dbt(profiles, "seed", "--select", _SEEDS)
    stdout = _run_dbt(profiles, "run", "--select", _LINEAGE)
    # Le test de maille unique (AI-05) et l'interdit d'additivite (AD-4)
    # tournent sur la sortie REELLE.
    _run_dbt(profiles, "test", "--select", _MART_TESTS)

    con = duckdb.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT connector, date, metric, breakdown_dimension, breakdown_value,"
            "       value, pull_id "
            "FROM main_marts.fact_daily_kpi ORDER BY connector, date"
        ).fetchall()

        # AC1 + AC3 : trois lignes, la correction gagne, la provenance est celle
        # du chargement qui fait foi.
        assert rows == [
            ("ds_DEMO", __import__("datetime").date(2026, 8, 1), "clicks",
             "campaign", "Summer", 100.0, _FIRST_LOAD),
            ("ds_DEMO", __import__("datetime").date(2026, 8, 2), "clicks",
             "campaign", "Summer", 999.0, _SECOND_LOAD),
            ("ds_OTHER", __import__("datetime").date(2026, 8, 1), "clicks",
             "campaign", "Summer", 7.0, _FIRST_LOAD),
        ]

        # AC2 : `connector` porte le Datastream. Deux fichiers du meme projet,
        # meme metrique, meme jour, meme axe : deux lignes distinctes -- et le
        # test de maille unique vient de passer dessus.
        assert (
            con.execute(
                "SELECT count(DISTINCT connector) FROM main_marts.fact_daily_kpi "
                "WHERE date = DATE '2026-08-01'"
            ).fetchone()[0]
            == 2
        )

        # AD-4 : la mesure non additive n'est jamais stockee.
        assert (
            con.execute(
                "SELECT count(*) FROM main_marts.fact_daily_kpi WHERE metric = 'ctr'"
            ).fetchone()[0]
            == 0
        )
        # ...et elle est NOMMEE, jamais tue (AD-9).
        assert "ds_DEMO.ctr" in stdout

        # Le flux sans jour est exclu EN LE DISANT.
        assert (
            con.execute(
                "SELECT count(*) FROM main_marts.fact_daily_kpi WHERE connector = 'ds_NODATE'"
            ).fetchone()[0]
            == 0
        )
        assert "ds_NODATE" in stdout
    finally:
        con.close()


@_needs_dbt
def test_a_project_without_managed_feed_builds_the_mart_without_the_branch(
    tmp_path: Path,
) -> None:
    """AC4 : zero fait fichier n'est pas une panne."""
    db_path = tmp_path / "empty.duckdb"
    con = duckdb.connect(str(db_path))
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS mirror")
        con.execute(_MIRROR_DDL)
        con.execute(_EXECUTION_DDL)
    finally:
        con.close()
    profiles = _write_profiles(tmp_path, db_path)

    _run_dbt(profiles, "seed", "--select", _SEEDS)
    _run_dbt(profiles, "run", "--select", _LINEAGE)
    _run_dbt(profiles, "test", "--select", _MART_TESTS)

    con = duckdb.connect(str(db_path))
    try:
        assert con.execute("SELECT count(*) FROM main_marts.fact_daily_kpi").fetchone()[0] == 0
    finally:
        con.close()


@_needs_dbt
def test_a_mirror_older_than_the_migration_names_itself_rather_than_failing(
    tmp_path: Path,
) -> None:
    """Un miroir anterieur a la 299 est un RETARD, jamais une panne."""
    db_path = tmp_path / "stale.duckdb"
    con = duckdb.connect(str(db_path))
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS mirror")
        con.execute(_MIRROR_DDL_PRE_299)
        con.execute(
            "INSERT INTO mirror.managed_feed_grain VALUES "
            "('proj_EXAMPLE','org_EXAMPLE','ds_DEMO','managed_feed_ds_DEMO',"
            " 'dsm_1','[\"date\",\"campaign\"]',TRUE)"
        )
        con.execute(_landing_ddl("managed_feed_ds_DEMO"))
        _insert_landing(
            con, "managed_feed_ds_DEMO", [("2026-08-01", "Summer", 100, _FIRST_LOAD)]
        )
    finally:
        con.close()
    profiles = _write_profiles(tmp_path, db_path)

    # Le vrai runner EXCLUT les stagings des connecteurs absents du projet
    # (`fact_daily_kpi` en-tete) : selectionner `+fact_daily_kpi` ici ferait
    # echouer 52 stagings dont aucun n'appartient a cette story. On construit la
    # lignee de la branche, telle qu'un projet fichier-seul la construit.
    stdout = _run_dbt(profiles, "run", "--select", _LINEAGE)

    con = duckdb.connect(str(db_path))
    try:
        assert con.execute("SELECT count(*) FROM main_marts.fact_daily_kpi").fetchone()[0] == 0
    finally:
        con.close()
    # Le retard se DIT : sinon l'absence de lignes se lirait comme "ce projet
    # n'a pas de fichier".
    assert "migration 299" in stdout
