"""Un fait se croise avec une classification A LA DATE DE LA LIGNE (story 69.3).

CE QUE CE TEST PROUVE, et qu'aucune doublure ne pourrait prouver -- le
croisement est du SQL, bati pour de vrai sur une base DuckDB jetable :

  * AS-OF : la MEME entite, le MEME attribut, deux jours differents -> deux
    valeurs differentes. Une reclassification ne reecrit pas l'histoire (AC1) ;
  * le rattachement est LU dans le verdict de 68.3, jamais recalcule ;
  * une classification DERIVEE (68.6) est rendue avec la version de regle qui
    l'a produite (AC2) ;
  * une ligne dont la cle ne resout rien forme le groupe << non rattache >>
    NOMME, avec sa valeur intacte -- jamais fondu dans un vrai libelle, jamais
    jete (AC3) ;
  * un miroir incomplet rend une vue VIDE et le DIT, jamais un build mort (AC4).
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

_LOAD = "dse_01AAA"
_PROJECT = "proj_EXAMPLE"
_NODE = "mdnode_01VIDEO"
_RULE_VERSION = "grsv_01RULEV2"

#: La lignee qu'un projet fichier-seul construit pour atteindre le croisement.
_LINEAGE = (
    "managed_feed_superseding stg_managed_feed_facts fact_daily_kpi "
    "semantic_fact_by_entity_attribute"
)
_SEEDS = "dim_metric"

_needs_dbt = pytest.mark.skipif(
    shutil.which("dbt") is None and "DBT_PROFILES_DIR" not in os.environ,
    reason="dbt absent de cet environnement",
)


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


def _base_warehouse(con) -> None:
    """Le mart, via la branche managed_feed : deux jours, une video, des vues."""
    con.execute("CREATE SCHEMA IF NOT EXISTS mirror")
    con.execute(
        "CREATE TABLE mirror.managed_feed_grain ("
        "project_id TEXT, org_id TEXT, datastream_id TEXT, landing_table TEXT,"
        "mapping_version_id TEXT, grain_columns TEXT, has_grain BOOLEAN,"
        "date_column TEXT, dimension_columns TEXT, measure_columns TEXT,"
        "non_additive_columns TEXT)"
    )
    con.execute(
        "INSERT INTO mirror.managed_feed_grain VALUES "
        f"('{_PROJECT}','org_EXAMPLE','ds_DEMO','managed_feed_ds_DEMO',"
        " 'dsm_1','[\"date\",\"video_id\"]',TRUE,"
        " 'date','[\"video_id\"]','[\"views\"]','[]')"
    )
    con.execute(
        "CREATE TABLE mirror.managed_feed_execution ("
        "project_id TEXT, org_id TEXT, datastream_id TEXT, execution_id TEXT,"
        "loaded_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO mirror.managed_feed_execution VALUES "
        f"('{_PROJECT}','org_EXAMPLE','ds_DEMO','{_LOAD}',"
        " TIMESTAMP '2026-08-10 04:00:00')"
    )
    con.execute(
        "CREATE TABLE main.managed_feed_ds_DEMO ("
        "date TEXT, video_id TEXT, views INTEGER, grain_key TEXT,"
        "execution_id TEXT, plan_version_id TEXT, mapping_version_id TEXT,"
        "project_id TEXT)"
    )
    con.executemany(
        "INSERT INTO main.managed_feed_ds_DEMO VALUES (?,?,?,?,?,?,?,?)",
        [
            # Une video connue, deux jours de part et d'autre du reclassement.
            ("2026-08-01", "v-1", 100, "k1", _LOAD, "dsp_1", "dsm_1", _PROJECT),
            ("2026-08-20", "v-1", 200, "k2", _LOAD, "dsp_1", "dsm_1", _PROJECT),
            # Une video que personne n'a jamais declaree : le groupe non rattache.
            ("2026-08-01", "v-ghost", 7, "k3", _LOAD, "dsp_1", "dsm_1", _PROJECT),
        ],
    )


def _mdm_mirror(con) -> None:
    con.execute(
        "CREATE TABLE mirror.entity_key_match_verdicts_dim ("
        "org_id TEXT, project_id TEXT, datastream_id TEXT, object_kind TEXT,"
        "field_id TEXT, normalized_value TEXT, node_id TEXT, verdict TEXT,"
        "reason_code TEXT, observed_from DATE, observed_to DATE)"
    )
    con.execute(
        "INSERT INTO mirror.entity_key_match_verdicts_dim VALUES "
        f"('org_EXAMPLE','{_PROJECT}','ds_DEMO','video','video_id','v-1',"
        f" '{_NODE}','resolved','exact_lookup',DATE '2026-08-01',DATE '2026-08-31'),"
        # La video fantome a bien un verdict : on a CHERCHE et rien trouve.
        f"('org_EXAMPLE','{_PROJECT}','ds_DEMO','video','video_id','v-ghost',"
        " NULL,'unmatched','no_candidate',DATE '2026-08-01',DATE '2026-08-31')"
    )
    con.execute(
        "CREATE TABLE mirror.master_data_node_attributes_asof ("
        "org_id TEXT, project_id TEXT, registry_id TEXT, node_id TEXT,"
        "version_id TEXT, effective_from DATE, effective_to DATE,"
        "object_kind TEXT, attribute TEXT, value_text TEXT, value_number NUMERIC)"
    )
    con.execute(
        "INSERT INTO mirror.master_data_node_attributes_asof VALUES "
        # La MEME video, le MEME attribut, deux fenetres : c'est tout le sujet.
        f"('org_EXAMPLE','{_PROJECT}','mdr_1','{_NODE}','mdv_1',"
        " DATE '2026-07-01', DATE '2026-08-15','video','format','short',NULL),"
        f"('org_EXAMPLE','{_PROJECT}','mdr_1','{_NODE}','mdv_2',"
        " DATE '2026-08-15', NULL,'video','format','long',NULL)"
    )
    con.execute(
        "CREATE TABLE mirror.master_data_derived_attributes_dim ("
        "org_id TEXT, project_id TEXT, registry_id TEXT, node_id TEXT,"
        "object_kind TEXT, attribute TEXT, value_text TEXT, value_number NUMERIC,"
        "rule_set_id TEXT, rule_set_version_id TEXT, is_current_rule_version BOOLEAN)"
    )
    con.execute(
        "INSERT INTO mirror.master_data_derived_attributes_dim VALUES "
        f"('org_EXAMPLE','{_PROJECT}','mdr_1','{_NODE}','video','content_type',"
        f" 'tutorial',NULL,'grs_1','{_RULE_VERSION}',TRUE),"
        # Une version de regle PERIMEE : elle reste en base, elle ne repond plus.
        f"('org_EXAMPLE','{_PROJECT}','mdr_1','{_NODE}','video','content_type',"
        " 'vlog',NULL,'grs_1','grsv_01RULEV1',FALSE)"
    )


@_needs_dbt
def test_the_same_entity_crosses_to_different_values_at_different_dates(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "cross.duckdb"
    con = duckdb.connect(str(db_path))
    try:
        _base_warehouse(con)
        _mdm_mirror(con)
    finally:
        con.close()
    profiles = _write_profiles(tmp_path, db_path)

    _run_dbt(profiles, "seed", "--select", _SEEDS)
    _run_dbt(profiles, "run", "--select", _LINEAGE)

    con = duckdb.connect(str(db_path))
    try:
        carried = con.execute(
            "SELECT date, attribute_value, value FROM main_marts."
            "semantic_fact_by_entity_attribute "
            "WHERE attribute_origin = 'carried' ORDER BY date"
        ).fetchall()
        # AC1 : LE test. Le 1er aout la video etait `short`, le 20 elle est
        # `long`. Lire la version courante rendrait `long` aux deux dates et
        # reecrirait l'histoire.
        assert [(row[0].isoformat(), row[1], row[2]) for row in carried] == [
            ("2026-08-01", "short", 100.0),
            ("2026-08-20", "long", 200.0),
        ]

        derived = con.execute(
            "SELECT DISTINCT attribute, attribute_value, rule_set_version_id "
            "FROM main_marts.semantic_fact_by_entity_attribute "
            "WHERE attribute_origin = 'derived'"
        ).fetchall()
        # AC2 : la classification derivee NOMME la version de regle qui l'a
        # produite -- et la version perimee ne repond pas.
        assert derived == [("content_type", "tutorial", _RULE_VERSION)]

        unattached = con.execute(
            "SELECT entity_key, resolution_state, attribute_value, value "
            "FROM main_marts.semantic_fact_by_entity_attribute "
            "WHERE attribute_origin = 'unattached'"
        ).fetchall()
        # AC3 : la ligne non rattachee GARDE sa valeur et DIT pourquoi. Elle
        # n'est ni fondue dans `short`, ni jetee.
        assert unattached == [("v-ghost", "unmatched", None, 7.0)]

        # Le total du croisement, groupe non rattache compris, est le total du
        # mart : rien n'a ete perdu en chemin.
        crossed_total = con.execute(
            "SELECT SUM(value) FROM main_marts.semantic_fact_by_entity_attribute "
            "WHERE attribute_origin IN ('carried', 'unattached')"
        ).fetchone()[0]
        mart_total = con.execute(
            "SELECT SUM(value) FROM main_marts.fact_daily_kpi "
            "WHERE breakdown_dimension <> 'day_total'"
        ).fetchone()[0]
        assert crossed_total == mart_total == 307.0
    finally:
        con.close()


@_needs_dbt
def test_the_cross_never_writes_into_the_mart(tmp_path: Path) -> None:
    """FR8 : l'attribut ne descend JAMAIS dans fact_daily_kpi."""
    db_path = tmp_path / "isolated.duckdb"
    con = duckdb.connect(str(db_path))
    try:
        _base_warehouse(con)
        _mdm_mirror(con)
    finally:
        con.close()
    profiles = _write_profiles(tmp_path, db_path)

    _run_dbt(profiles, "seed", "--select", _SEEDS)
    _run_dbt(profiles, "run", "--select", _LINEAGE)

    con = duckdb.connect(str(db_path))
    try:
        columns = [
            row[0]
            for row in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'main_marts' AND table_name = 'fact_daily_kpi'"
            ).fetchall()
        ]
        # Un reclassement ne doit jamais reecrire un chiffre d'aout : la seule
        # facon de le garantir est que le mart ne porte pas l'attribut.
        for forbidden in ("format", "content_type", "attribute", "node_id"):
            assert forbidden not in columns
    finally:
        con.close()


@_needs_dbt
def test_an_incomplete_mirror_is_an_empty_view_that_says_so(tmp_path: Path) -> None:
    """AC4 : une synchro en retard est un RETARD, jamais une panne."""
    db_path = tmp_path / "stale.duckdb"
    con = duckdb.connect(str(db_path))
    try:
        _base_warehouse(con)  # pas de miroir MDM du tout
    finally:
        con.close()
    profiles = _write_profiles(tmp_path, db_path)

    _run_dbt(profiles, "seed", "--select", _SEEDS)
    stdout = _run_dbt(profiles, "run", "--select", _LINEAGE)

    con = duckdb.connect(str(db_path))
    try:
        assert (
            con.execute(
                "SELECT count(*) FROM main_marts.semantic_fact_by_entity_attribute"
            ).fetchone()[0]
            == 0
        )
    finally:
        con.close()
    assert "miroir incomplet" in stdout
