"""Le staging managed_feed lit le superseding : seul le chargement qui fait foi passe.

Story 69.1 (AC1-AC4). Le mart `managed_feed_superseding` n'avait aucun
consommateur (audit R1) ; `stg_managed_feed_facts` est ce consommateur. Ce
test est le jumeau de `test_managed_feed_superseding.py` : dbt tourne pour de
vrai sur une base DuckDB jetable (le modele est du Jinja qui interroge le
miroir et la landing a la COMPILATION -- une doublure prouverait qu'elle est
d'accord avec elle-meme), et les assertions portent sur les lignes rendues.

LE JEU D'ESSAI. Un premier fichier couvre trois jours ; un second renvoie
deux de ces jours -- dont une valeur CORRIGEE -- et en ajoute un quatrieme.
La landing porte les colonnes epinglees (`date`, `campaign`, `clicks`) plus
la provenance du writer (`execution_id`, `plan_version_id`,
`mapping_version_id`, `project_id`) ; la colonne rejetee `operator_note`
n'atterrit jamais, et le contrat de colonnes du staging est epingle par une
assertion exacte. Un flux SANS maille publiee (ds_NOKEY) est exclu.
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

# Same loaded-machine margin as the superseding mart's test.
pytestmark = pytest.mark.timeout(60)

#: Deux chargements. `dse_` est un ULID monotone, donc `MAX` = le plus recent.
_FIRST_LOAD = "dse_01AAA"
_SECOND_LOAD = "dse_01BBB"

#: Le contrat de colonnes du staging : la maille epinglee + les colonnes de
#: donnee epinglees + la provenance. `operator_note` -- rejetee par le mapping
#: epingle -- n'y figure pas et ne peut pas y figurer (elle n'a jamais atterri).
_EXPECTED_COLUMNS = [
    "project_id",
    "datastream_id",
    "campaign",
    "clicks",
    "date",
    "grain_key",
    "execution_id",
    "plan_version_id",
    "mapping_version_id",
    "loaded_at",
]


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
            # Un flux SANS maille publiee : exclu, jamais dedoublonne en silence.
            "('proj_EXAMPLE','org_EXAMPLE','ds_NOKEY','managed_feed_ds_NOKEY',"
            " 'dsm_2','[]',FALSE)"
        )
        # La landing porte les colonnes EPINGLEES par le mapping (date, campaign,
        # clicks) + grain_key (colonne reservee) + la provenance du writer. La
        # colonne rejetee `operator_note` n'atterrit jamais (c'est le point AC2).
        con.execute(
            "CREATE TABLE main.managed_feed_ds_DEMO ("
            "date TEXT, campaign TEXT, clicks INTEGER, grain_key TEXT,"
            "execution_id TEXT, plan_version_id TEXT, mapping_version_id TEXT,"
            "project_id TEXT)"
        )
        rows = [
            # Le premier fichier couvre trois jours.
            ("2026-08-01", "Summer", 100, _FIRST_LOAD),
            ("2026-08-02", "Summer", 110, _FIRST_LOAD),
            ("2026-08-03", "Summer", 120, _FIRST_LOAD),
            # Le second recouvre les 02 (CORRECTION) et 03, et ajoute le 04.
            ("2026-08-02", "Summer", 999, _SECOND_LOAD),
            ("2026-08-03", "Summer", 121, _SECOND_LOAD),
            ("2026-08-04", "Summer", 130, _SECOND_LOAD),
        ]
        con.executemany(
            "INSERT INTO main.managed_feed_ds_DEMO VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    date,
                    campaign,
                    clicks,
                    f"grainhash-{date}",  # empreinte reservee, jamais la cle du mart
                    execution_id,
                    "dsp_1",
                    "dsm_1",
                    "proj_EXAMPLE",
                )
                for date, campaign, clicks, execution_id in rows
            ],
        )
    finally:
        con.close()


def _run_dbt(profiles_dir: Path, *args: str) -> None:
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
            f"dbt {' '.join(args)} a echoue:\n{result.stdout[-3000:]}\n{result.stderr[-2000:]}"
        )


def _write_profiles(tmp_path: Path, db_path: Path) -> Path:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "profiles.yml").write_text(
        "connector:\n  target: local\n  outputs:\n    local:\n"
        f"      type: duckdb\n      path: '{db_path.as_posix()}'\n      threads: 1\n",
        encoding="utf-8",
    )
    return profiles


@pytest.mark.skipif(
    shutil.which("dbt") is None and "DBT_PROFILES_DIR" not in os.environ,
    reason="dbt absent de cet environnement",
)
def test_the_staging_reads_only_the_superseding_execution(tmp_path: Path) -> None:
    db_path = tmp_path / "staging.duckdb"
    _write_fixture(db_path)
    profiles = _write_profiles(tmp_path, db_path)

    # `+` : le mart est le parent du staging (le staging le `ref()`), il est
    # construit d'abord -- exactement comme dans un build reel.
    _run_dbt(profiles, "run", "--select", "+stg_managed_feed_facts")
    # Les tests de schema (grain unique AI-05, not_null sur la provenance)
    # s'executent sur la sortie reelle.
    _run_dbt(profiles, "test", "--select", "stg_managed_feed_facts")

    con = duckdb.connect(str(db_path))
    try:
        # Le contrat de colonnes est epingle : la colonne rejetee
        # `operator_note` n'atteint jamais le staging (AC2).
        columns = [
            row[0]
            for row in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'main_staging' "
                "AND table_name = 'stg_managed_feed_facts' "
                "ORDER BY ordinal_position"
            ).fetchall()
        ]
        assert columns == _EXPECTED_COLUMNS

        rows = con.execute(
            "SELECT date, campaign, clicks, execution_id, project_id,"
            "       plan_version_id, mapping_version_id, loaded_at "
            "FROM main_staging.stg_managed_feed_facts ORDER BY date"
        ).fetchall()

        # Six lignes brutes, QUATRE lignes lues : le recouvrement est resolu
        # par le superseding, pas empile (AC1) -- et la CORRECTION gagne.
        assert [(row[0], row[2]) for row in rows] == [
            ("2026-08-01", 100),
            ("2026-08-02", 999),
            ("2026-08-03", 121),
            ("2026-08-04", 130),
        ]

        # La provenance est celle du chargement qui fait foi (AC3) : le jour
        # porte par le seul premier fichier garde son execution ; les jours
        # recouverts et le jour nouveau portent le second.
        assert [row[3] for row in rows] == [
            _FIRST_LOAD,
            _SECOND_LOAD,
            _SECOND_LOAD,
            _SECOND_LOAD,
        ]
        assert all(row[4] == "proj_EXAMPLE" for row in rows)
        assert all(row[5] == "dsp_1" for row in rows)
        assert all(row[6] == "dsm_1" for row in rows)
        # loaded_at : NULL honnete, present et requetable (AD-9) -- le chemin
        # fichier ne tamponne pas d'heure de chargement.
        assert all(row[7] is None for row in rows)

        # Le flux sans maille n'est pas dedoublonne EN SILENCE : absent.
        assert (
            con.execute(
                "SELECT count(*) FROM main_staging.stg_managed_feed_facts "
                "WHERE datastream_id = 'ds_NOKEY'"
            ).fetchone()[0]
            == 0
        )
    finally:
        con.close()


@pytest.mark.skipif(
    shutil.which("dbt") is None and "DBT_PROFILES_DIR" not in os.environ,
    reason="dbt absent de cet environnement",
)
def test_a_project_without_managed_feed_builds_the_staging_empty(
    tmp_path: Path,
) -> None:
    """AC4 : zero fait fichier n'est pas une panne -- le modele se construit vide."""
    db_path = tmp_path / "empty.duckdb"
    con = duckdb.connect(str(db_path))
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS mirror")
        con.execute(
            "CREATE TABLE mirror.managed_feed_grain ("
            "project_id TEXT, org_id TEXT, datastream_id TEXT, landing_table TEXT,"
            "mapping_version_id TEXT, grain_columns TEXT, has_grain BOOLEAN)"
        )
    finally:
        con.close()
    profiles = _write_profiles(tmp_path, db_path)

    _run_dbt(profiles, "run", "--select", "+stg_managed_feed_facts")
    _run_dbt(profiles, "test", "--select", "stg_managed_feed_facts")

    con = duckdb.connect(str(db_path))
    try:
        assert (
            con.execute("SELECT count(*) FROM main_staging.stg_managed_feed_facts").fetchone()[0]
            == 0
        )
        columns = [
            row[0]
            for row in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'main_staging' "
                "AND table_name = 'stg_managed_feed_facts' "
                "ORDER BY ordinal_position"
            ).fetchall()
        ]
        # La forme vide porte l'identite et la provenance, les seules colonnes
        # connues sans aucun flux.
        assert columns == [
            "project_id",
            "datastream_id",
            "grain_key",
            "execution_id",
            "plan_version_id",
            "mapping_version_id",
            "loaded_at",
        ]
    finally:
        con.close()
