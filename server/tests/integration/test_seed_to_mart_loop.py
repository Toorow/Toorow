"""Integration smoke test for the full local seed→load→dbt→query loop — Story 1.4, T7.

Runs:
  1. generate_seed.py (import + call)
  2. load_seed.py in DuckDB mode against tmp_path
  3. dbt run --select google_analytics (subprocess)
  4. dbt test --select google_analytics (subprocess)
  5. get_ga4_report via FastMCP in-process client

These tests are slower (~30 s) and skipped when dbt is not available in PATH.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).parents[3]
SEEDS_DIR = REPO_ROOT / "server" / "modules" / "google-analytics" / "seeds"
META_SEEDS_DIR = REPO_ROOT / "server" / "modules" / "meta-ads" / "seeds"
DBT_DIR = REPO_ROOT / "dbt"


# ---------------------------------------------------------------------------
# Skip the whole module when dbt is not installed / not in PATH
# ---------------------------------------------------------------------------
try:  # dbt is invoked via `python -m dbt.cli.main` from the venv
    import dbt.cli.main  # noqa: F401
    _DBT_AVAILABLE = True
except ImportError:
    _DBT_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _DBT_AVAILABLE,
    reason="dbt not found in PATH — skipping full seed-to-mart loop integration tests",
)


@pytest.fixture
def anyio_backend():
    """Use asyncio as the anyio backend for async tests in this file."""
    return "asyncio"


# ---------------------------------------------------------------------------
# Importers
# ---------------------------------------------------------------------------

def _import_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def gen():
    return _import_module("generate_seed", SEEDS_DIR / "generate_seed.py")


@pytest.fixture(scope="module")
def loader():
    return _import_module("load_seed", SEEDS_DIR / "load_seed.py")


@pytest.fixture(scope="module")
def seeded_db(tmp_path_factory, gen, loader):
    """Full pipeline: generate CSV → load DuckDB → dbt run → dbt test.

    Returns the path to the populated DuckDB file so further tests can query it.
    """
    tmp_dir = tmp_path_factory.mktemp("seed_loop")
    csv_path = str(tmp_dir / "ga4_seed.csv")
    db_path = str(tmp_dir / "test.duckdb")

    # 1 — Generate seed CSV (generate 95 days to ensure we cover the start of static seeds)
    from datetime import date, timedelta
    rows1 = gen.generate_rows(date.today())
    rows2 = gen.generate_rows(date.today() - timedelta(days=5))
    seen = set()
    rows = []
    for r in rows1 + rows2:
        key = (r["date"], r["device_category"], r["country"])
        if key not in seen:
            seen.add(key)
            rows.append(r)

    gen.write_csv(rows, csv_path)
    assert Path(csv_path).exists(), "generate_seed must produce a CSV"

    # 2 — Load into DuckDB
    pull_id, count = loader.run(
        csv_path=csv_path,
        mode="duckdb",
        duckdb_path=db_path,
        bq_project=None,
    )
    assert count >= 1350, (
        f"Expected at least 1350 rows (90d × 3 devices × 5 countries), got {count}"
    )
    assert pull_id.startswith("pull_"), f"pull_id must start with pull_: {pull_id!r}"

    # 2b — Story 3.6: also land Meta Ads seed rows so the module-owned staging
    # model (stg_meta_ads_daily) has a source table and the mart proves the join
    # (fact_daily_kpi gains connector='meta-ads' rows). This is the FR2 proof
    # that a second source flows end-to-end through the shared base.
    meta_loader = _import_module("load_meta_seed", META_SEEDS_DIR / "load_meta_seed.py")
    meta_pull_id, meta_count = meta_loader.run(duckdb_path=db_path, days=30)
    assert meta_count == 60, f"Expected 60 Meta rows (30d × 2 campaigns), got {meta_count}"

    # review-15-9 F-1: ALSO land the three-grain coexistence seed (campaign / adset /
    # creative daily) so the mart exercises the data_level filter that fixes the F-1
    # double-count. The campaign-grain load above (60 rows) keeps the retro-compat count
    # assertion; this multigrain load makes the adset_id / ad_id mart series non-empty
    # (they read data_level='ADSET' / 'CREATIVE'). Append-only (AD-7), distinct pull_id.
    meta_loader.run(duckdb_path=db_path, days=30, grains="multi")

    # 2b-bis — Story 6.2: land GSC seed rows so stg_gsc_daily has its source
    # (raw_gsc_daily) and the mart carries connector='gsc' additive metrics.
    gsc_seeds_dir = REPO_ROOT / "server" / "modules" / "gsc" / "seeds"
    gsc_loader = _import_module("load_gsc_seed", gsc_seeds_dir / "load_gsc_seed.py")
    gsc_pull_id, gsc_count = gsc_loader.run(duckdb_path=db_path, days=30)
    assert gsc_count > 0, f"Expected GSC seed rows, got {gsc_count}"

    # Charger le seed de type utilisateur GA4
    ut_loader = _import_module("load_seed_user_type", SEEDS_DIR / "load_seed_user_type.py")
    ut_loader.run(
        csv_path=str(SEEDS_DIR / "ga4_user_type_seed.csv"),
        mode="duckdb",
        duckdb_path=db_path,
        bq_project=None,
    )

    # Charger le seed de landing pages GA4
    pages_loader = _import_module("load_seed_pages", SEEDS_DIR / "load_seed_pages.py")
    pages_loader.run(
        csv_path=str(SEEDS_DIR / "ga4_landing_seed.csv"),
        profile="landing",
        mode="duckdb",
        duckdb_path=db_path,
        bq_project=None,
    )

    # Charger le seed de paths pages GA4
    pages_loader.run(
        csv_path=str(SEEDS_DIR / "ga4_paths_seed.csv"),
        profile="paths",
        mode="duckdb",
        duckdb_path=db_path,
        bq_project=None,
    )

    # Charger le seed d'acquisition session GA4
    acq_loader = _import_module("load_seed_acquisition", SEEDS_DIR / "load_seed_acquisition.py")
    acq_loader.run(
        csv_path=str(SEEDS_DIR / "ga4_acquisition_session_seed.csv"),
        profile="session",
        mode="duckdb",
        duckdb_path=db_path,
        bq_project=None,
    )

    # Charger le seed d'acquisition first_user GA4
    acq_loader.run(
        csv_path=str(SEEDS_DIR / "ga4_acquisition_first_user_seed.csv"),
        profile="first_user",
        mode="duckdb",
        duckdb_path=db_path,
        bq_project=None,
    )

    # Charger le seed de transactions GA4
    tx_loader = _import_module("load_seed_transactions", SEEDS_DIR / "load_seed_transactions.py")
    tx_loader.run(duckdb_path=db_path)

    # Charger le seed d'ordres Shopify
    shopify_seeds_dir = REPO_ROOT / "server" / "modules" / "shopify" / "seeds"
    shopify_loader = _import_module("load_shopify_seed", shopify_seeds_dir / "load_shopify_seed.py")
    shopify_loader.run(duckdb_path=db_path, days=90)

    # Story 15.8 F-4 AJOUT ADDITIF : charger le seed Klaviyo (campagnes + flows)
    # pour prouver que fact_daily_kpi contient des lignes connector='klaviyo'
    # avec les metriques attributed_*. Pattern identique aux loaders GA4/Meta/GSC.
    klaviyo_seeds_dir = REPO_ROOT / "server" / "modules" / "klaviyo" / "seeds"
    klaviyo_loader = _import_module(
        "load_klaviyo_seed", klaviyo_seeds_dir / "load_klaviyo_seed.py"
    )
    klaviyo_pull_id, klaviyo_count = klaviyo_loader.run(duckdb_path=db_path, days=40)
    assert klaviyo_count > 0, (
        f"Klaviyo seed doit produire des lignes, got {klaviyo_count}"
    )

    # Story 15.2 AJOUT ADDITIF : charger le seed TikTok multigrain (les 3 data_level
    # coexistent) -- sans lui, stg_tiktok_ads_daily echoue au dbt build de la fixture
    # (raw_tiktok_ads_daily absent) et test_tiktok_no_grain_bleed n'a pas de matiere.
    tiktok_seeds_dir = REPO_ROOT / "server" / "modules" / "tiktok-ads" / "seeds"
    tiktok_loader = _import_module(
        "load_tiktok_seed", tiktok_seeds_dir / "load_tiktok_seed.py"
    )
    tiktok_pull_id, tiktok_count = tiktok_loader.run(duckdb_path=db_path, grains="multi")
    assert tiktok_count > 0, (
        f"TikTok seed doit produire des lignes, got {tiktok_count}"
    )

    # Story 15.3 AJOUT ADDITIF : charger le seed LinkedIn multigrain (CAMPAIGN +
    # CAMPAIGN_GROUP coexistant) -- sans lui, stg_linkedin_ads_campaign_daily et
    # stg_linkedin_ads_campaign_group_daily echouent au dbt build (raw_linkedin_ads_daily
    # absent) et test_fact_daily_kpi_includes_linkedin_ads n'a pas de matiere.
    linkedin_seeds_dir = REPO_ROOT / "server" / "modules" / "linkedin-ads" / "seeds"
    linkedin_loader = _import_module(
        "load_linkedin_seed", linkedin_seeds_dir / "load_linkedin_seed.py"
    )
    linkedin_pull_id, linkedin_count = linkedin_loader.run(duckdb_path=db_path, grains="multi")
    assert linkedin_count > 0, (
        f"LinkedIn Ads seed doit produire des lignes, got {linkedin_count}"
    )

    # Story 15.7 AJOUT ADDITIF : charger le seed Stripe (charges). Il CORRELE une majorite
    # de charges avec les commandes Shopify deja chargees (meme jour, meme revenue,
    # client_reference_id = order_id) pour prouver la dedup revenue Stripe x Shopify
    # (cross_source_revenue) de facon NON-tautologique + une minorite Stripe-only (SaaS).
    # Doit tourner APRES le loader Shopify (le generateur importe le seed Shopify pour correler).
    stripe_seeds_dir = REPO_ROOT / "server" / "modules" / "stripe" / "seeds"
    stripe_loader = _import_module(
        "load_stripe_seed", stripe_seeds_dir / "load_stripe_seed.py"
    )
    stripe_pull_id, stripe_count = stripe_loader.run(duckdb_path=db_path, days=90)
    assert stripe_count > 0, (
        f"Stripe seed doit produire des lignes, got {stripe_count}"
    )

    # Story 15.5 AJOUT ADDITIF : charger le seed HubSpot CRM (contacts + deals).
    # Prouve que fact_daily_kpi contient des lignes connector='hubspot' avec les
    # metriques CRM (new_contacts, deals_created, deals_closed, deal_amount) et
    # qu'elles sont ISOLEES des totaux cross-source existants (AD-4 CRM).
    hubspot_seeds_dir = REPO_ROOT / "server" / "modules" / "hubspot" / "seeds"
    hubspot_loader = _import_module(
        "load_hubspot_seed", hubspot_seeds_dir / "load_hubspot_seed.py"
    )
    hubspot_pull_id, hubspot_count = hubspot_loader.run(duckdb_path=db_path, days=30)
    assert hubspot_count > 0, (
        f"HubSpot seed doit produire des lignes (contacts + deals), got {hubspot_count}"
    )

    # google-sheets: BEGIN Story 15.6 AJOUT ADDITIF : charger le seed Google Sheets.
    # Prouve que fact_daily_kpi contient des lignes connector='google-sheets' avec les
    # metriques objectifs (budget_declared, target_revenue, target_conversions) et
    # qu'elles sont ISOLEES des totaux cross-source existants (AD-4 -- pas de collision).
    gsheets_seeds_dir = REPO_ROOT / "server" / "modules" / "google-sheets" / "seeds"
    gsheets_loader = _import_module(
        "load_google_sheets_seed", gsheets_seeds_dir / "load_google_sheets_seed.py"
    )
    gsheets_pull_id, gsheets_count = gsheets_loader.run(duckdb_path=db_path, days=30)
    assert gsheets_count > 0, (
        f"Google Sheets seed doit produire des lignes (objectifs), got {gsheets_count}"
    )
    # google-sheets: END Story 15.6 block.

    # 3 — Copy profiles.yml.example → tmp profiles dir for dbt
    profiles_dir = tmp_dir / "profiles"
    profiles_dir.mkdir()
    profiles_dst = profiles_dir / "profiles.yml"

    # Write a profiles.yml pointing at our temp DuckDB
    profiles_dst.write_text(
        f"""connector:
  target: local
  outputs:
    local:
      type: duckdb
      path: "{pathlib.PurePath(db_path).as_posix()}"
      threads: 1
""",
        encoding="utf-8",
    )

    # 2c — Story 4.4: populate mirror schema in DuckDB so dim_project can materialise.
    # In production, mirror_sync.py does this. In CI without Postgres, we create the
    # mirror.project_preferences table directly from the seed CSV default row.
    import duckdb as _duckdb
    _mirror_conn = _duckdb.connect(db_path)
    _mirror_conn.execute("CREATE SCHEMA IF NOT EXISTS mirror")
    _mirror_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mirror.project_preferences (
            project_id VARCHAR,
            canonical_currency VARCHAR,
            reporting_timezone VARCHAR,
            -- Story 17.1: colonnes source de vérification (nullable, opt-in).
            verification_source_type VARCHAR,
            verification_source_id VARCHAR,
            lead_event_name VARCHAR,
            created_at TIMESTAMP,
            updated_at TIMESTAMP
        )
        """
    )
    _mirror_conn.execute(
        """
        INSERT INTO mirror.project_preferences
            (project_id, canonical_currency, reporting_timezone,
             verification_source_type, verification_source_id, lead_event_name,
             created_at, updated_at)
        VALUES ('default', 'EUR', 'Europe/Paris', NULL, NULL, NULL,
                current_timestamp, current_timestamp)
        """
    )
    _mirror_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mirror.context_events (
            id VARCHAR,
            project_id VARCHAR,
            event_date DATE,
            type VARCHAR,
            label VARCHAR,
            description VARCHAR,
            created_by VARCHAR,
            created_at TIMESTAMP
        )
        """
    )
    _mirror_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mirror.connection_ref_dim (
            project_id VARCHAR,
            connector_name VARCHAR,
            display_name VARCHAR
        )
        """
    )
    # Story 13.2: staging models LEFT JOIN mirror.fx_conflict_resolutions (AD-6).
    # mirror_sync.py creates it in production (empty when no resolutions). Empty
    # here -> JOIN matches nothing -> COALESCE falls back to raw currency.
    _mirror_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mirror.fx_conflict_resolutions (
            id VARCHAR,
            project_id VARCHAR,
            target_field VARCHAR,
            source_module VARCHAR,
            resolved_source_currency VARCHAR,
            decided_by VARCHAR,
            decided_at TIMESTAMP,
            note VARCHAR
        )
        """
    )
    _mirror_conn.close()

    # 3b — dbt seed (metric_source_priority, Story 3.7)
    env = {**os.environ, "TOOROW_DUCKDB_PATH": db_path}
    seed_result = subprocess.run(
        [
            sys.executable, "-m", "dbt.cli.main", "seed",
            "--profiles-dir", str(profiles_dir),
            "--project-dir", str(DBT_DIR),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert seed_result.returncode == 0, (
        f"dbt seed failed:\nSTDOUT:\n{seed_result.stdout}\nSTDERR:\n{seed_result.stderr}"
    )

    # 4 — dbt run
    run_result = subprocess.run(
        [
            sys.executable, "-m", "dbt.cli.main", "run",
            "--profiles-dir", str(profiles_dir),
            "--project-dir", str(DBT_DIR),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert run_result.returncode == 0, (
        f"dbt run failed:\nSTDOUT:\n{run_result.stdout}\nSTDERR:\n{run_result.stderr}"
    )

    # 5 — dbt test
    test_result = subprocess.run(
        [
            sys.executable, "-m", "dbt.cli.main", "test",
            "--profiles-dir", str(profiles_dir),
            "--project-dir", str(DBT_DIR),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert test_result.returncode == 0, (
        f"dbt test failed:\nSTDOUT:\n{test_result.stdout}\nSTDERR:\n{test_result.stderr}"
    )

    return {
        "db_path": db_path,
        "pull_id": pull_id,
        "row_count": count,
        "meta_pull_id": meta_pull_id,
        "meta_row_count": meta_count,
    }


# ---------------------------------------------------------------------------
# T7.1 — fact_daily_kpi has correct schema and row counts
# ---------------------------------------------------------------------------

def test_fact_daily_kpi_has_rows(seeded_db):
    """fact_daily_kpi must have rows after dbt run (AC4)."""
    import duckdb

    db_path = seeded_db["db_path"]
    con = duckdb.connect(db_path, read_only=True)
    try:
        # dbt-duckdb materialises marts in main_marts schema
        count = con.execute("SELECT COUNT(*) FROM main_marts.fact_daily_kpi").fetchone()[0]
    finally:
        con.close()
    assert count > 0, "fact_daily_kpi must have rows after dbt run"


def test_fact_daily_kpi_schema(seeded_db):
    """fact_daily_kpi must have the canonical star-schema columns (AC4)."""
    import duckdb

    db_path = seeded_db["db_path"]
    con = duckdb.connect(db_path, read_only=True)
    try:
        con.execute("SELECT * FROM main_marts.fact_daily_kpi LIMIT 1").fetchone()
        cols = [d[0] for d in con.description]
    finally:
        con.close()

    required = {
        "project_id", "date", "connector", "metric",
        "breakdown_dimension", "breakdown_value", "value", "pull_id", "loaded_at",
    }
    assert required.issubset(set(cols)), (
        f"Missing columns in fact_daily_kpi: {required - set(cols)}"
    )


def test_fact_daily_kpi_pull_id_propagated(seeded_db):
    """pull_id in fact_daily_kpi must match the pull_id from the load batch (AD-7)."""
    import duckdb

    db_path = seeded_db["db_path"]
    expected_pull_id = seeded_db["pull_id"]
    con = duckdb.connect(db_path, read_only=True)
    try:
        ids = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT pull_id FROM main_marts.fact_daily_kpi"
            ).fetchall()
        }
    finally:
        con.close()

    assert expected_pull_id in ids, (
        f"Expected pull_id {expected_pull_id!r} in fact_daily_kpi, found: {ids}"
    )


def test_fact_daily_kpi_no_ratio_metrics(seeded_db):
    """fact_daily_kpi must not contain ratio metrics — additive only (AC6, AD-4)."""
    import duckdb

    db_path = seeded_db["db_path"]
    con = duckdb.connect(db_path, read_only=True)
    try:
        metrics = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT metric FROM main_marts.fact_daily_kpi"
            ).fetchall()
        }
    finally:
        con.close()

    forbidden = {"cvr", "ctr", "roas", "sessions_per_user"}
    found = forbidden & metrics
    assert not found, f"Ratio metrics found in fact_daily_kpi (violates AD-4): {found}"


def test_fact_daily_kpi_no_null_pull_id(seeded_db):
    """No NULL pull_id in fact_daily_kpi (AC6)."""
    import duckdb

    db_path = seeded_db["db_path"]
    con = duckdb.connect(db_path, read_only=True)
    try:
        nulls = con.execute(
            "SELECT COUNT(*) FROM main_marts.fact_daily_kpi WHERE pull_id IS NULL"
        ).fetchone()[0]
    finally:
        con.close()
    assert nulls == 0, f"Found {nulls} NULL pull_id rows in fact_daily_kpi"


# ---------------------------------------------------------------------------
# Story 3.6 — Meta Ads rows flow through the shared base into fact_daily_kpi
# ---------------------------------------------------------------------------

def test_fact_daily_kpi_includes_meta_ads(seeded_db):
    """fact_daily_kpi must contain connector='meta-ads' rows after dbt run (FR2 join proof)."""
    import duckdb

    db_path = seeded_db["db_path"]
    con = duckdb.connect(db_path, read_only=True)
    try:
        connectors = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT connector FROM main_marts.fact_daily_kpi"
            ).fetchall()
        }
        meta_metrics = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT metric FROM main_marts.fact_daily_kpi "
                "WHERE connector = 'meta-ads'"
            ).fetchall()
        }
        meta_dims = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT breakdown_dimension FROM main_marts.fact_daily_kpi "
                "WHERE connector = 'meta-ads'"
            ).fetchall()
        }
        # Both sources coexist in the single canonical fact table.
        both = con.execute(
            "SELECT COUNT(DISTINCT connector) FROM main_marts.fact_daily_kpi"
        ).fetchone()[0]
    finally:
        con.close()

    assert "meta-ads" in connectors, (
        f"fact_daily_kpi must include connector='meta-ads' rows, found: {connectors}"
    )
    assert "google-analytics" in connectors, "GA4 rows must remain present (no regression)"
    assert both >= 2, "google-analytics and meta-ads must coexist in the mart (gsc may join too)"
    # Meta canonical metrics (spend->cost) and dimensions land correctly.
    assert {"cost", "impressions", "clicks", "conversions"}.issubset(meta_metrics), (
        f"Meta metrics missing from mart: {meta_metrics}"
    )
    assert {"campaign_id", "adset_id", "ad_id"}.issubset(meta_dims), (
        f"Meta breakdown dimensions missing from mart: {meta_dims}"
    )


# ---------------------------------------------------------------------------
# T7.2 — get_ga4_report returns non-empty data via FastMCP in-process client
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_get_ga4_report_returns_real_data(seeded_db):
    """get_ga4_report returns non-empty mart data for the seeded date range (T7.2, AC5)."""
    from datetime import date, timedelta
    from unittest.mock import patch

    db_path = seeded_db["db_path"]

    # Import connector with env pointing to the seeded DuckDB (main_marts schema)
    with patch.dict(
        os.environ,
        {
            "TOOROW_DB_MODE": "duckdb",
            "TOOROW_DUCKDB_PATH": db_path,
        },
    ):
        connector = _import_module(
            "connector_ga4_live",
            REPO_ROOT / "server" / "modules" / "google-analytics" / "connector.py",
        )

        # Date range within the 90-day seed window
        date_to = (date.today() - timedelta(days=1)).isoformat()
        date_from = (date.today() - timedelta(days=30)).isoformat()

        envelope = connector.get_ga4_report(
            project_id="default",
            report_profile="standard_daily",
            date_from=date_from,
            date_to=date_to,
        )

    # Shape checks
    assert "schema_version" in envelope
    assert "meta" in envelope
    assert "data" in envelope

    meta = envelope["meta"]
    assert meta["provenance"] is not None, (
        "meta.provenance must be non-None when mart has data (NFR6, AD-7)"
    )
    # Story 2.7 AC5 / NFR8: provenance is now a dict (not a scalar string).
    prov = meta["provenance"]
    assert isinstance(prov, dict), (
        f"meta.provenance must be a dict (NFR8 Story 2.7), got: {type(prov)!r}"
    )
    assert prov["source_system"] == "google-analytics"
    assert prov["source_field"] == "fact_daily_kpi"
    assert prov["pull_id"].startswith("pull_"), (
        f"provenance.pull_id {prov['pull_id']!r} must start with 'pull_'"
    )
    assert meta["freshness"] is not None, "meta.freshness must be non-None when mart has data"

    data = envelope["data"]
    metrics = data.get("metrics", {})
    assert len(metrics) > 0, "data.metrics must be non-empty for seeded date range (AC5)"
    for m in ("sessions", "active_users", "conversions"):
        assert m in metrics, f"Metric '{m}' missing from data.metrics"


# ---------------------------------------------------------------------------
# T7.3 — No code under server/ reads CSV or raw_ga4_standard_daily directly
# ---------------------------------------------------------------------------

def test_connector_does_not_read_csv_or_raw_table():
    """Connector MCP tools must not read CSV or raw tables directly (AD-12).

    Story 2.7 adds pull() which WRITES to raw_ga4_standard_daily — that
    is the landing function, not an MCP tool handler read path. The AD-12 rule
    is: MCP server reads marts only. The MCP tools (get_ga4_report, _query_mart)
    must not read raw tables. The write path in pull() is allowed.

    This test does not require a seeded DB — it inspects source code.
    """
    connector_py = REPO_ROOT / "server" / "modules" / "google-analytics" / "connector.py"
    content = connector_py.read_text(encoding="utf-8")

    # Should not open .csv files (seed files are not part of the connector)
    assert "ga4_seed.csv" not in content, (
        "connector.py must not reference ga4_seed.csv directly (AD-12)"
    )
    # AD-12: MCP read path (_MART_QUERY) must reference fact_daily_kpi (not raw tables).
    # Story 2.7 adds pull() which WRITES to raw_ga4_standard_daily — this is allowed.
    # Verify the mart query SQL only touches fact_daily_kpi.
    assert "fact_daily_kpi" in content, (
        "_MART_QUERY must reference fact_daily_kpi (AD-12 mart-only reads)"
    )
    # The _MART_QUERY block itself must not query raw tables
    mart_query_block = (
        content.split("_MART_QUERY =")[1].split("def ")[0]
        if "_MART_QUERY =" in content
        else ""
    )
    assert "raw_ga4" not in mart_query_block, (
        "_MART_QUERY SQL must NOT reference raw_ga4 (AD-12)"
    )


# ---------------------------------------------------------------------------
# Story 15.8 F-4 AJOUT ADDITIF : klaviyo dans fact_daily_kpi apres dbt build
# ---------------------------------------------------------------------------

def test_fact_daily_kpi_includes_klaviyo(seeded_db):
    """Story 15.8 F-4 : fact_daily_kpi doit contenir connector='klaviyo' avec les
    metriques attributed_* apres le chargement du seed klaviyo et dbt run.
    Prouve le flux complet seed -> raw_klaviyo_daily -> staging -> mart.
    """
    import duckdb

    db_path = seeded_db["db_path"]
    con = duckdb.connect(db_path, read_only=True)
    try:
        connectors = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT connector FROM main_marts.fact_daily_kpi"
            ).fetchall()
        }
        klaviyo_metrics = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT metric FROM main_marts.fact_daily_kpi "
                "WHERE connector = 'klaviyo'"
            ).fetchall()
        }
        klaviyo_dims = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT breakdown_dimension FROM main_marts.fact_daily_kpi "
                "WHERE connector = 'klaviyo'"
            ).fetchall()
        }
        klaviyo_count = con.execute(
            "SELECT COUNT(*) FROM main_marts.fact_daily_kpi WHERE connector = 'klaviyo'"
        ).fetchone()[0]
    finally:
        con.close()

    assert "klaviyo" in connectors, (
        f"fact_daily_kpi doit inclure connector='klaviyo' apres dbt run, "
        f"connecteurs presents: {connectors}"
    )
    assert klaviyo_count > 0, (
        "fact_daily_kpi doit avoir des lignes klaviyo non nulles apres dbt run"
    )
    # Metriques canoniques Klaviyo (AD-4 : noms explicites, pas 'conversions'/'revenue').
    expected_metrics = {
        "sends", "opens", "clicks", "attributed_conversions", "attributed_revenue"
    }
    assert expected_metrics.issubset(klaviyo_metrics), (
        f"Metriques Klaviyo manquantes dans le mart: "
        f"{expected_metrics - klaviyo_metrics}. Presentes: {klaviyo_metrics}"
    )
    # Les deux sous-dimensions klaviyo (campaigns et flows) doivent etre presentes.
    assert {"campaign_id", "flow_id"}.issubset(klaviyo_dims), (
        f"Dimensions klaviyo manquantes dans le mart: {klaviyo_dims}. "
        "Attendu: campaign_id ET flow_id (deux sous-modeles de staging)"
    )
    # Regle de non-agregation (AD-4) : 'conversions' et 'revenue' generiques interdits.
    assert "conversions" not in klaviyo_metrics, (
        "fact_daily_kpi ne doit PAS contenir metric='conversions' pour connector='klaviyo' "
        "(utiliser 'attributed_conversions' -- AD-4 non-agregation)"
    )
    assert "revenue" not in klaviyo_metrics, (
        "fact_daily_kpi ne doit PAS contenir metric='revenue' pour connector='klaviyo' "
        "(utiliser 'attributed_revenue' -- AD-4 non-agregation)"
    )


# ---------------------------------------------------------------------------
# Story 15.3 AJOUT ADDITIF : linkedin-ads dans fact_daily_kpi apres dbt build
# ---------------------------------------------------------------------------

def test_fact_daily_kpi_includes_linkedin_ads(seeded_db):
    """Story 15.3 : fact_daily_kpi doit contenir connector='linkedin-ads' avec les
    metriques canoniques (cost, impressions, clicks, conversions, leads) et les deux
    dimensions (campaign_id, campaign_group_id) apres le chargement du seed LinkedIn
    multigrain et dbt run.
    Prouve le flux complet seed -> raw_linkedin_ads_daily -> staging -> mart.
    Prouve aussi la discipline double-compte (data_level) : campaign + campaign_group
    coexistant dans raw, les deux series sont presentes SEPAREES dans le mart.
    """
    import duckdb

    db_path = seeded_db["db_path"]
    con = duckdb.connect(db_path, read_only=True)
    try:
        connectors = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT connector FROM main_marts.fact_daily_kpi"
            ).fetchall()
        }
        linkedin_metrics = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT metric FROM main_marts.fact_daily_kpi "
                "WHERE connector = 'linkedin-ads'"
            ).fetchall()
        }
        linkedin_dims = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT breakdown_dimension FROM main_marts.fact_daily_kpi "
                "WHERE connector = 'linkedin-ads'"
            ).fetchall()
        }
        linkedin_count = con.execute(
            "SELECT COUNT(*) FROM main_marts.fact_daily_kpi WHERE connector = 'linkedin-ads'"
        ).fetchone()[0]
    finally:
        con.close()

    assert "linkedin-ads" in connectors, (
        f"fact_daily_kpi doit inclure connector='linkedin-ads' apres dbt run, "
        f"connecteurs presents: {connectors}"
    )
    assert linkedin_count > 0, (
        "fact_daily_kpi doit avoir des lignes linkedin-ads non nulles apres dbt run"
    )
    # Metriques canoniques LinkedIn (AD-4 : cost, impressions, clicks, conversions, leads).
    expected_metrics = {"cost", "impressions", "clicks", "conversions"}
    assert expected_metrics.issubset(linkedin_metrics), (
        f"Metriques LinkedIn manquantes dans le mart: "
        f"{expected_metrics - linkedin_metrics}. Presentes: {linkedin_metrics}"
    )
    # 'leads' est optionnel (emis uniquement si non-NULL au grain campaign).
    # Pas d'assertion strict : le seed multigrain en produit, donc on verifie.
    assert "leads" in linkedin_metrics, (
        "Metrique 'leads' absente du mart linkedin-ads -- le seed multigrain doit en produire"
    )
    # Les deux dimensions LinkedIn doivent etre presentes (grain campaign ET campaign_group).
    assert "campaign_id" in linkedin_dims, (
        f"breakdown_dimension='campaign_id' absent pour linkedin-ads: {linkedin_dims}"
    )
    assert "campaign_group_id" in linkedin_dims, (
        f"breakdown_dimension='campaign_group_id' absent pour linkedin-ads: {linkedin_dims}"
        " -- prouve que stg_linkedin_ads_campaign_group_daily est bien monte"
    )
    # Discipline double-compte (lecon review-15-2 F-1) : les conversions linkedin-ads
    # restent SEPAREES du cross_source_conversions (connector distingue les series).
    # La presence de 'conversions' sous connector='linkedin-ads' est correcte et attendue.
    assert "conversions" in linkedin_metrics, (
        "fact_daily_kpi doit contenir metric='conversions' pour connector='linkedin-ads' "
        "(conversions revendiquees, separees par la cle connector -- AD-4)"
    )
    # Verifier que les totaux GA4/Meta/TikTok/Klaviyo/Shopify existants sont inchanges
    # (reconciliation "totaux existants inchanges" -- regle commune epic-15).
    assert "google-analytics" in connectors, "GA4 rows doivent rester presentes (pas de regression)"
    assert "meta-ads" in connectors, "Meta rows doivent rester presentes (pas de regression)"


# ---------------------------------------------------------------------------
# Story 15.7 AJOUT ADDITIF : stripe dans fact_daily_kpi + dedup revenue vs shopify
# ---------------------------------------------------------------------------

def test_fact_daily_kpi_includes_stripe(seeded_db):
    """Story 15.7 : fact_daily_kpi doit contenir connector='stripe' avec les metriques
    canoniques (revenue, refunds, fees, transaction_count, order_count) au grain 'day_total'
    apres le chargement du seed Stripe et dbt run. Prouve le flux complet
    seed -> raw_stripe_payments -> stg_stripe_payments_daily -> mart, sans regression sur
    les totaux existants. Prouve aussi la regle AD-4 : 'conversions' n'apparait JAMAIS pour
    stripe (un paiement est une vente, pas une conversion regie)."""
    import duckdb

    db_path = seeded_db["db_path"]
    con = duckdb.connect(db_path, read_only=True)
    try:
        connectors = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT connector FROM main_marts.fact_daily_kpi"
            ).fetchall()
        }
        stripe_metrics = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT metric FROM main_marts.fact_daily_kpi "
                "WHERE connector = 'stripe'"
            ).fetchall()
        }
        stripe_dims = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT breakdown_dimension FROM main_marts.fact_daily_kpi "
                "WHERE connector = 'stripe'"
            ).fetchall()
        }
        stripe_count = con.execute(
            "SELECT COUNT(*) FROM main_marts.fact_daily_kpi WHERE connector = 'stripe'"
        ).fetchone()[0]
    finally:
        con.close()

    assert "stripe" in connectors, (
        f"fact_daily_kpi doit inclure connector='stripe' apres dbt run, "
        f"connecteurs presents: {connectors}"
    )
    assert stripe_count > 0, (
        "fact_daily_kpi doit avoir des lignes stripe non nulles apres dbt run"
    )
    expected_metrics = {"revenue", "refunds", "fees", "transaction_count", "order_count"}
    assert expected_metrics.issubset(stripe_metrics), (
        f"Metriques Stripe manquantes dans le mart: "
        f"{expected_metrics - stripe_metrics}. Presentes: {stripe_metrics}"
    )
    # Une seule serie day-total (pas de partition par charge -- charge_id reste detail).
    assert stripe_dims == {"day_total"}, (
        f"stripe doit n'emettre que 'day_total', trouve: {stripe_dims}"
    )
    # AD-4 : pas de 'conversions' pour stripe (un paiement est une vente).
    assert "conversions" not in stripe_metrics, (
        "fact_daily_kpi ne doit PAS contenir metric='conversions' pour connector='stripe' "
        "(un paiement Stripe est une VENTE, pas une conversion regie -- AD-4)"
    )
    # Regression : les totaux existants restent presents.
    assert "shopify" in connectors, "Shopify rows doivent rester presentes (pas de regression)"
    assert "google-analytics" in connectors, "GA4 rows doivent rester presentes (pas de regression)"


def test_cross_source_revenue_dedups_stripe_and_shopify(seeded_db):
    """Story 15.7 (AD-4 dedup revenue) : sur les jours ou stripe ET shopify emettent du
    revenue (le seed correle DELIBEREMENT une majorite de charges Stripe a des commandes
    Shopify), cross_source_revenue.revenue_total choisit UNE source gagnante (shopify,
    priorite 1) et est STRICTEMENT INFERIEUR a la somme naive shopify+stripe -- preuve
    NON-tautologique que les deux revenue ne fusionnent JAMAIS dans un total croise."""
    import duckdb

    db_path = seeded_db["db_path"]
    con = duckdb.connect(db_path, read_only=True)
    try:
        # jours de chevauchement (shopify ET stripe > 0).
        overlap = con.execute(
            """
            WITH per_source AS (
                SELECT project_id, date, connector, SUM(value) AS rev
                FROM main_marts.fact_daily_kpi
                WHERE metric = 'revenue' AND connector IN ('shopify', 'stripe')
                GROUP BY project_id, date, connector
            ),
            by_day AS (
                SELECT project_id, date,
                    MAX(CASE WHEN connector='shopify' THEN rev END) AS shop,
                    MAX(CASE WHEN connector='stripe'  THEN rev END) AS strp
                FROM per_source GROUP BY project_id, date
            )
            SELECT COUNT(*) FROM by_day
            WHERE shop IS NOT NULL AND strp IS NOT NULL AND shop > 0 AND strp > 0
            """
        ).fetchone()[0]

        # pour chaque jour de chevauchement, revenue_total < somme naive et source = shopify.
        violations = con.execute(
            """
            WITH per_source AS (
                SELECT project_id, date, connector, SUM(value) AS rev
                FROM main_marts.fact_daily_kpi
                WHERE metric = 'revenue' AND connector IN ('shopify', 'stripe')
                GROUP BY project_id, date, connector
            ),
            by_day AS (
                SELECT project_id, date,
                    MAX(CASE WHEN connector='shopify' THEN rev END) AS shop,
                    MAX(CASE WHEN connector='stripe'  THEN rev END) AS strp
                FROM per_source GROUP BY project_id, date
            ),
            overlap AS (
                SELECT project_id, date, shop + strp AS naive_sum
                FROM by_day
                WHERE shop IS NOT NULL AND strp IS NOT NULL AND shop > 0 AND strp > 0
            )
            SELECT COUNT(*)
            FROM overlap o
            JOIN main_marts.cross_source_revenue c
              ON c.project_id = o.project_id AND c.date = o.date
            WHERE c.revenue_total >= o.naive_sum - 0.001
               OR c.revenue_source <> 'shopify'
            """
        ).fetchone()[0]

        # F-1+F-2 (Story 15.7 review fix) : preuve de VRAIE correlation (jointure reelle).
        # Au moins 10 charges Stripe ont un client_reference_id qui existe dans
        # raw_shopify_orders. Si les fenetres sont desalignees (Stripe avec end_date fige,
        # Shopify avec today()), les order_ids Stripe ne matchent AUCUNE ligne Shopify et ce
        # compteur tombe a 0 -- ce qui detecte la regression de fenetre immediatement.
        real_corr = con.execute(
            """
            SELECT COUNT(*) FROM raw_stripe_payments s
            WHERE s.client_reference_id IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM raw_shopify_orders o
                  WHERE o.order_id = s.client_reference_id
              )
            """
        ).fetchone()[0]
    finally:
        con.close()

    assert overlap > 0, (
        "Le seed doit produire des jours ou stripe ET shopify emettent du revenue "
        "(sinon la preuve de dedup est tautologique) -- verifier la correlation du seed Stripe"
    )
    assert violations == 0, (
        f"{violations} jour(s) ou cross_source_revenue somme les deux sources ou choisit "
        "une source != shopify -- la dedup revenue Stripe x Shopify (AD-4) est violee"
    )
    assert real_corr >= 10, (
        f"Correlation Stripe x Shopify INSUFFISANTE : seulement {real_corr} charges Stripe "
        "ont un client_reference_id present dans raw_shopify_orders. "
        "Verifier que les deux seeds partagent la meme fenetre end_date (today() par defaut). "
        "Indice : charger Shopify AVANT Stripe (dependance de generation)."
    )


# ---------------------------------------------------------------------------
# Story 15.5 AJOUT ADDITIF : hubspot dans fact_daily_kpi + isolation CRM
# ---------------------------------------------------------------------------

def test_fact_daily_kpi_includes_hubspot(seeded_db):
    """Story 15.5 : fact_daily_kpi doit contenir connector='hubspot' avec les metriques
    CRM (new_contacts, deals_created, deals_closed, deal_amount) apres le chargement du
    seed HubSpot et dbt run. Prouve le flux complet :
      seed -> raw_hubspot_contacts_daily + raw_hubspot_deals_daily
           -> stg_hubspot_contacts_daily + stg_hubspot_deals_daily -> mart.
    Prouve aussi la regle AD-4 CRM : 'conversions' et 'revenue' n'apparaissent JAMAIS
    pour connector='hubspot' (noms CRM distincts = protection nominale AD-4).
    """
    import duckdb

    db_path = seeded_db["db_path"]
    con = duckdb.connect(db_path, read_only=True)
    try:
        connectors = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT connector FROM main_marts.fact_daily_kpi"
            ).fetchall()
        }
        hubspot_metrics = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT metric FROM main_marts.fact_daily_kpi "
                "WHERE connector = 'hubspot'"
            ).fetchall()
        }
        hubspot_dims = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT breakdown_dimension FROM main_marts.fact_daily_kpi "
                "WHERE connector = 'hubspot'"
            ).fetchall()
        }
        hubspot_count = con.execute(
            "SELECT COUNT(*) FROM main_marts.fact_daily_kpi WHERE connector = 'hubspot'"
        ).fetchone()[0]
    finally:
        con.close()

    assert "hubspot" in connectors, (
        f"fact_daily_kpi doit inclure connector='hubspot' apres dbt run, "
        f"connecteurs presents: {connectors}"
    )
    assert hubspot_count > 0, (
        "fact_daily_kpi doit avoir des lignes hubspot non nulles apres dbt run"
    )
    # Metriques canoniques HubSpot CRM (AD-4 : noms CRM distincts des regies).
    expected_metrics = {"new_contacts", "deals_created", "deals_closed"}
    assert expected_metrics.issubset(hubspot_metrics), (
        f"Metriques HubSpot CRM manquantes dans le mart: "
        f"{expected_metrics - hubspot_metrics}. Presentes: {hubspot_metrics}"
    )
    # deal_amount peut etre absent si tous les jours ont deals_closed=0 (AD-9).
    # On ne l'assert pas comme obligatoire.

    # breakdown_dimension doit etre 'date' (grain journalier CRM, pas par entite).
    assert hubspot_dims == {"date"}, (
        f"hubspot doit n'emettre que breakdown_dimension='date', trouve: {hubspot_dims}"
    )

    # REGLE D'ISOLATION CRM (AD-4, CRITIQUE) : pas de collision nominale avec les regies.
    assert "conversions" not in hubspot_metrics, (
        "fact_daily_kpi ne doit PAS contenir metric='conversions' pour connector='hubspot' "
        "(new_contacts / deals != conversions regies -- AD-4 CRM isolation)"
    )
    assert "revenue" not in hubspot_metrics, (
        "fact_daily_kpi ne doit PAS contenir metric='revenue' pour connector='hubspot' "
        "(deal_amount != revenue Shopify/Stripe -- AD-4 CRM isolation)"
    )
    assert "cost" not in hubspot_metrics, (
        "fact_daily_kpi ne doit PAS contenir metric='cost' pour connector='hubspot'"
    )

    # Regression : les totaux existants (GA4, Meta, Shopify) restent presents.
    assert "google-analytics" in connectors, "GA4 rows doivent rester presentes (pas de regression)"
    assert "meta-ads" in connectors, "Meta rows doivent rester presentes (pas de regression)"
    assert "shopify" in connectors, "Shopify rows doivent rester presentes (pas de regression)"


# ---------------------------------------------------------------------------
# google-sheets: BEGIN Story 15.6 AJOUT ADDITIF : google-sheets dans fact_daily_kpi
# ---------------------------------------------------------------------------

def test_fact_daily_kpi_includes_google_sheets(seeded_db):
    """Story 15.6 : fact_daily_kpi doit contenir connector='google-sheets' avec les
    metriques objectifs (budget_declared, target_revenue, target_conversions) et
    breakdown_dimension='sheet_row_id' apres le chargement du seed Google Sheets et dbt run.
    Prouve le flux complet :
      seed -> raw_google_sheets_daily -> stg_google_sheets_daily -> mart.
    Prouve aussi la regle AD-4 Objectifs : 'conversions' et 'revenue' n'apparaissent JAMAIS
    pour connector='google-sheets' (noms objectifs distincts = protection nominale AD-4).
    """
    import duckdb

    db_path = seeded_db["db_path"]
    con = duckdb.connect(db_path, read_only=True)
    try:
        connectors = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT connector FROM main_marts.fact_daily_kpi"
            ).fetchall()
        }
        gsheets_metrics = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT metric FROM main_marts.fact_daily_kpi "
                "WHERE connector = 'google-sheets'"
            ).fetchall()
        }
        gsheets_dims = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT breakdown_dimension FROM main_marts.fact_daily_kpi "
                "WHERE connector = 'google-sheets'"
            ).fetchall()
        }
        gsheets_count = con.execute(
            "SELECT COUNT(*) FROM main_marts.fact_daily_kpi WHERE connector = 'google-sheets'"
        ).fetchone()[0]
        # AI-56 seam : valeurs reelles (pas juste HTTP 200 ou presence colonne).
        gsheets_value_check = con.execute(
            "SELECT COUNT(*) FROM main_marts.fact_daily_kpi "
            "WHERE connector = 'google-sheets' AND value IS NOT NULL AND value > 0"
        ).fetchone()[0]
        # Grain uniqueness : chaque (project_id, date, sheet_row_id) doit etre unique par metrique.
        grain_violations = con.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT project_id, date, breakdown_value, metric, COUNT(*) AS n
                FROM main_marts.fact_daily_kpi
                WHERE connector = 'google-sheets'
                GROUP BY project_id, date, breakdown_value, metric
                HAVING n > 1
            )
            """
        ).fetchone()[0]
    finally:
        con.close()

    assert "google-sheets" in connectors, (
        f"fact_daily_kpi doit inclure connector='google-sheets' apres dbt run, "
        f"connecteurs presents: {connectors}"
    )
    assert gsheets_count > 0, (
        "fact_daily_kpi doit avoir des lignes google-sheets non nulles apres dbt run"
    )
    # Metriques canoniques Objectifs (AD-4 : noms objectifs distincts des regies).
    expected_metrics = {"budget_declared", "target_revenue", "target_conversions"}
    assert expected_metrics.issubset(gsheets_metrics), (
        f"Metriques Google Sheets manquantes dans le mart: "
        f"{expected_metrics - gsheets_metrics}. Presentes: {gsheets_metrics}"
    )

    # breakdown_dimension doit etre 'sheet_row_id' (grain par canal/ligne objectif).
    assert gsheets_dims == {"sheet_row_id"}, (
        f"google-sheets doit n'emettre que breakdown_dimension='sheet_row_id', "
        f"trouve: {gsheets_dims}"
    )

    # AI-56 seam : les valeurs doivent etre reelles (pas juste des lignes vides).
    assert gsheets_value_check > 0, (
        "fact_daily_kpi google-sheets doit avoir des valeurs > 0 (AD-9 : pas de zero silencieux)"
    )

    # Grain uniqueness : (project_id, date, breakdown_value, metric) uniques.
    assert grain_violations == 0, (
        f"{grain_violations} violation(s) de grain dans fact_daily_kpi "
        "pour connector='google-sheets' "
        "(QUALIFY dans stg_google_sheets_daily doit deduplicater par pull_id DESC)"
    )

    # REGLE D'ISOLATION OBJECTIFS (AD-4, CRITIQUE) : pas de collision nominale avec les regies.
    assert "conversions" not in gsheets_metrics, (
        "fact_daily_kpi ne doit PAS contenir metric='conversions' pour connector='google-sheets' "
        "(target_conversions != conversions regies -- AD-4 isolation objectifs)"
    )
    assert "revenue" not in gsheets_metrics, (
        "fact_daily_kpi ne doit PAS contenir metric='revenue' pour connector='google-sheets' "
        "(target_revenue != revenue Shopify/Stripe -- AD-4 isolation objectifs)"
    )
    assert "cost" not in gsheets_metrics, (
        "fact_daily_kpi ne doit PAS contenir metric='cost' pour connector='google-sheets'"
    )

    # Regression : les totaux existants (GA4, HubSpot, Shopify) restent presents.
    assert "google-analytics" in connectors, "GA4 rows doivent rester presentes (pas de regression)"
    assert "hubspot" in connectors, "HubSpot rows doivent rester presentes (pas de regression)"
    assert "shopify" in connectors, "Shopify rows doivent rester presentes (pas de regression)"

# ---------------------------------------------------------------------------
# google-sheets: END Story 15.6 block.
# ---------------------------------------------------------------------------
