"""TikTok Ads connector — Story 15.2 (Epic 15, connecteurs vague 1).

Troisieme regie du trio paid social (aux cotes de Meta et Google). Expose une
instance ``mcp_app: FastMCP`` que le loader du core monte sous le namespace
``tiktok-ads`` (AD-2). Meme patron "drop-a-folder" que meta-ads / shopify : zero
edit sous server/core, un bloc UNION additif dans le mart.

# AD-12: le MCP server lit UNIQUEMENT le mart fact_daily_kpi -- pas de raw_*, pas de CSV.
# AD-3: le token vient de Nango juste avant usage -- jamais stocke ni logge (AD-3, PAS
#        le flux Google direct AD-21 qui est reserve au stack Google).
# AD-7: pull_id frappe par le scheduler du core, passe dans pull().
# AD-14: parametre identity/project_id.
# AI-10: toutes les metriques TikTok sont additives au grain jour ; pas d'aggregation_rule.

Decisions de story (Dev Agent Record 15-2-connecteur-tiktok-ads.md) :
  * CONVERSIONS = REVENDIQUEES par le canal (AD-4). Les conversions TikTok ne sont
    JAMAIS sommees avec GA4/Meta/GSC sans la regle de dedup declarative (Rule P, 3.7).
    Elles restent des lignes SEPAREES keyed par connector='tiktok-ads' dans le mart.
  * Trois grains report BASIC (campaign / adgroup / ad) daily, dispatch AI-58 via des
    fonctions pull_<profile_id> qui delegent a _pull() avec le data_level idoine.
  * Fenetre d'attribution declaree dans le manifest (7D CTA / 1D VTA par defaut TikTok,
    verifie 2026-07-19). L'API renvoie les conversions deja attribuees selon le reglage
    du compte -- aucune fenetre passee a l'extraction sur les comptes SAN.

# AI-53: tous les champs/endpoints/quota API TikTok sont declares d'apres la doc
# officielle (verifie le 2026-07-19) et annotes "a confirmer en passe live" (AI-53/AI-13).
# Rien de non verifiable n'est affirme.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Module-level FastMCP instance — the public surface the loader mounts.
mcp_app = FastMCP("tiktok-ads")

# ---------------------------------------------------------------------------
# Database connection helpers — env-var driven (no hardcoded paths).
# Same dual-backend pattern as meta-ads / shopify / gsc connectors.
#   TOOROW_DB_MODE     = "duckdb" (default) | "bigquery"
#   TOOROW_DUCKDB_PATH = path to local .duckdb file
#   GCP_PROJECT           = GCP project ID (bigquery mode only)
# ---------------------------------------------------------------------------

_DEFAULT_DUCKDB_PATH = os.path.join(os.path.dirname(__file__), "seeds", "local.duckdb")


def _get_db_mode() -> str:
    return os.environ.get("TOOROW_DB_MODE", "duckdb")


def _get_duckdb_path() -> str:
    return os.environ.get("TOOROW_DUCKDB_PATH", _DEFAULT_DUCKDB_PATH)


# Story 25.7 (AC3): the TikTok provider error map lives in manifest.json (AD-2 --
# core never hardcodes provider codes). Loaded once and cached, mirroring the
# meta-ads _load_error_map pattern.
_ERROR_MAP: dict[str, str] | None = None


def _load_error_map() -> dict[str, str]:
    """Return the manifest's ``error_map`` (status:code -> canonical class), cached.

    Keys are ``"<http_status>:<provider_code>"`` where provider_code is the TikTok
    top-level logical ``code`` echoed in an error body (core.pull_errors reads the
    ``{"code": X}`` shape directly). Used ONLY at the HTTP raise site (non-200,
    non-429). The logical code!=0 path on an HTTP 200 body still raises RuntimeError
    today -- see catalog_sources/ROLLOUT_NOTES.md for the planned adoption.
    """
    global _ERROR_MAP
    if _ERROR_MAP is None:
        manifest_path = Path(__file__).parent / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        _ERROR_MAP = manifest.get("error_map", {})
    return _ERROR_MAP


def _query_duckdb(sql: str, params: list, duckdb_path: str) -> list[dict]:
    """Execute parameterized *sql* against the local DuckDB warehouse (F-01)."""
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path, read_only=True)
    try:
        rel = con.execute(sql, params)
        cols = [d[0] for d in rel.description]
        return [dict(zip(cols, row)) for row in rel.fetchall()]
    finally:
        con.close()


def _query_bigquery(sql: str, params: dict) -> list[dict]:
    """Execute parameterized *sql* against BigQuery (F-01: @named parameters)."""
    from google.cloud import bigquery  # noqa: PLC0415

    project = os.environ.get("GCP_PROJECT", "")
    client = bigquery.Client(project=project or None)
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter(name, "STRING", value)
            for name, value in params.items()
        ]
    )
    result = client.query(sql, job_config=job_config).result()
    cols = [f.name for f in result.schema]
    return [dict(zip(cols, row)) for row in result]


def _get_mart_table(db_mode: str) -> str:
    """Fully-qualified mart table reference per engine.

    DuckDB: dbt materialises marts into the main_marts schema.
    BigQuery (F-02): a dataset qualifier is mandatory — BQ_MARTS_DATASET
    (default "marts"), optionally project-qualified via GCP_PROJECT.
    """
    if db_mode == "duckdb":
        from core import warehouse_tenancy  # noqa: PLC0415

        return f"{warehouse_tenancy.mart_prefix(None)}fact_daily_kpi"
    dataset = os.environ.get("BQ_MARTS_DATASET", "marts")
    gcp_project = os.environ.get("GCP_PROJECT", "")
    prefix = f"{gcp_project}.{dataset}" if gcp_project else dataset
    return f"{prefix}.fact_daily_kpi"


# SQL body shared by both engines; only the table reference and the parameter
# placeholder style differ. User-supplied values NEVER enter the string (F-01).
_MART_QUERY = """
    SELECT
        metric,
        breakdown_dimension,
        breakdown_value,
        SUM(value) AS value,
        MAX(pull_id) AS pull_id,
        MAX(loaded_at) AS freshness
    FROM {table}
    WHERE connector = 'tiktok-ads'
      AND project_id = {p_project}
      AND date BETWEEN {p_from} AND {p_to}
    GROUP BY metric, breakdown_dimension, breakdown_value
    ORDER BY metric, breakdown_dimension, breakdown_value
"""


def _query_mart(date_from: str, date_to: str, project_id: str = "default") -> list[dict]:
    """Query fact_daily_kpi mart — DuckDB or BigQuery depending on env.

    # AD-12: MCP server reads marts only — never raw_* tables or CSV.
    """
    db_mode = _get_db_mode()
    table = _get_mart_table(db_mode)

    if db_mode == "duckdb":
        sql = _MART_QUERY.format(table=table, p_project="?", p_from="?", p_to="?")
        return _query_duckdb(sql, [project_id, date_from, date_to], _get_duckdb_path())
    elif db_mode == "bigquery":
        sql = _MART_QUERY.format(
            table=table, p_project="@project_id", p_from="@date_from", p_to="@date_to"
        )
        return _query_bigquery(
            sql, {"project_id": project_id, "date_from": date_from, "date_to": date_to}
        )
    else:
        raise ValueError(f"Unknown TOOROW_DB_MODE: {db_mode!r}")


def _build_envelope(
    rows: list[dict],
    report_profile: str,
    date_from: str,
    date_to: str,
    project_id: str,
) -> dict:
    """Build the canonical AD-1 envelope from mart rows."""
    pull_ids = {r["pull_id"] for r in rows if r.get("pull_id")}
    freshness_values = [r["freshness"] for r in rows if r.get("freshness")]

    latest_pull_id = max(pull_ids) if pull_ids else None
    latest_freshness = max(freshness_values) if freshness_values else None

    data_by_metric: dict[str, list[dict]] = {}
    for r in rows:
        metric = r["metric"]
        if metric not in data_by_metric:
            data_by_metric[metric] = []
        data_by_metric[metric].append(
            {
                "breakdown_dimension": r["breakdown_dimension"],
                "breakdown_value": r["breakdown_value"],
                "value": r["value"],
            }
        )

    # NFR8: provenance is a full dict, not a scalar pull_id string.
    provenance = (
        {
            "source_system": "tiktok-ads",
            "source_field": "fact_daily_kpi",
            "pull_id": latest_pull_id,
        }
        if latest_pull_id is not None
        else None
    )

    return {
        "schema_version": "1",
        "meta": {
            "freshness": latest_freshness,
            "provenance": provenance,
            "alerts": [],
        },
        "data": {
            "project_id": project_id,
            "report_profile": report_profile,
            "date_from": date_from,
            "date_to": date_to,
            "metrics": data_by_metric,
        },
    }


@mcp_app.tool()
def get_tiktok_ads_report(
    project_id: str = "default",  # AD-14: identity resolved from OAuth 2.1 + PKCE
    report_profile: str = "campaign_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """TikTok Ads Performance Report — reads from fact_daily_kpi mart.

    Report profiles: campaign_daily, adgroup_daily, ad_daily
    Metrics: cost, impressions, clicks, conversions (all additive at day grain, AD-4).
        conversions are CLAIMED by TikTok (attributed by the account window, default
        7D click / 1D view) -- NEVER summed with GA4/Meta conversions without the
        cross-source dedup rule (Rule P, 3.7). They stay distinct connector='tiktok-ads'.
    Breakdown dimensions: campaign_id, adgroup_id, ad_id.

    Returns the canonical AD-1 envelope via structuredContent.
    Text channel (this docstring) is the lean LLM summary (<=30 lines).

    Parameters:
        project_id: Project identifier (default: 'default', AD-14 placeholder)
        report_profile: One of campaign_daily / adgroup_daily / ad_daily
        date_from: Start date ISO-8601 (e.g. '2026-04-01'). Defaults to 90 days ago.
        date_to: End date ISO-8601 (e.g. '2026-06-30'). Defaults to yesterday.
    """
    # AD-12: MCP server reads fact_daily_kpi mart only — never raw_* tables or CSV.
    from datetime import date, timedelta  # noqa: PLC0415

    if not date_to:
        date_to = (date.today() - timedelta(days=1)).isoformat()
    if not date_from:
        date_from = (date.today() - timedelta(days=90)).isoformat()

    try:
        rows = _query_mart(date_from, date_to, project_id)
    except Exception as exc:
        return {
            "schema_version": "1",
            "meta": {
                "freshness": None,
                "provenance": None,
                "alerts": [{"level": "error", "message": str(exc)}],
            },
            "data": {
                "project_id": project_id,
                "report_profile": report_profile,
                "date_from": date_from,
                "date_to": date_to,
                "metrics": {},
            },
        }

    return _build_envelope(rows, report_profile, date_from, date_to, project_id)


# ---------------------------------------------------------------------------
# TikTok Marketing API pull — Story 15.2
# ---------------------------------------------------------------------------

# AI-53 verifie le 2026-07-19 (doc officielle business-api.tiktok.com): endpoint
# synchrone de reporting integre. La version d'API (v1.3) fait partie du chemin.
TIKTOK_API_BASE = os.environ.get(
    "TIKTOK_API_BASE", "https://business-api.tiktok.com/open_api/v1.3"
)
_INTEGRATED_REPORT_PATH = "/report/integrated/get/"

# data_level TikTok par grain de report BASIC (AI-53 verifie 2026-07-19).
# Le report BASIC agrege au niveau de l'entite selectionnee.
_DATA_LEVEL_BY_PROFILE: dict[str, str] = {
    "campaign_daily": "AUCTION_CAMPAIGN",
    "adgroup_daily": "AUCTION_ADGROUP",
    "ad_daily": "AUCTION_AD",
}

# Dimension d'entite TikTok par grain (renvoyee dans row['dimensions']).
_ENTITY_DIMENSION_BY_PROFILE: dict[str, str] = {
    "campaign_daily": "campaign_id",
    "adgroup_daily": "adgroup_id",
    "ad_daily": "ad_id",
}

# TikTok pagination: la reponse porte data.page_info {page, total_page}. On avance
# page par page jusqu'a total_page. Garde anti-boucle (pattern shopify F-2): borne
# dure _MAX_PAGES + set de pages deja vues (cursor non-progressant / mock casse).
# 1000 pages x page_size 1000 = 1M lignes/fenetre, tres au-dessus de tout pull reel.
_MAX_PAGES = 1000
_PAGE_SIZE = 1000

# TikTok renvoie parfois la chaine litterale "-" ou "" pour un nom/valeur absente
# (equivalent du "(not set)" GA4). On la normalise en None -> NULL (AI-05/qualite).
_NOT_SET_TOKENS = ("", "-", "(not set)", "N/A", "null")


def _normalize_not_set(value):
    """Map TikTok's empty/placeholder tokens to None -> NULL (mirrors GA4 (not set))."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip() in _NOT_SET_TOKENS:
        return None
    return value


# review-15-2 F-1: data_level distinguishes WHICH report grain landed each raw row
# (AUCTION_CAMPAIGN | AUCTION_ADGROUP | AUCTION_AD). Without it, when several grains are
# pulled the same day the mart cannot tell a campaign-grain row (adgroup_id NULL) apart
# from the campaign_id carried by an adgroup-grain row -> the campaign_id series would sum
# BOTH and double-count the campaign inside its own series. Each mart series now reads ONLY
# the rows of its own data_level, so the grains never bleed into one another.
_RAW_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_tiktok_ads_daily (
    date                  VARCHAR,
    data_level            VARCHAR,
    campaign_id           VARCHAR,
    campaign_name         VARCHAR,
    adgroup_id            VARCHAR,
    adgroup_name          VARCHAR,
    ad_id                 VARCHAR,
    spend                 DOUBLE,
    impressions           INTEGER,
    clicks                INTEGER,
    conversions           INTEGER,
    pull_id               VARCHAR,
    loaded_at             VARCHAR,
    project_id            VARCHAR,
    cost_source_currency  VARCHAR DEFAULT 'EUR'
)
"""

_RAW_INSERT_SQL = """
INSERT INTO raw_tiktok_ads_daily
    (date, data_level, campaign_id, campaign_name, adgroup_id, adgroup_name, ad_id,
     spend, impressions, clicks, conversions, pull_id, loaded_at, project_id,
     cost_source_currency)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _insert_raw_rows(
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    project_id: str,
    db_mode: str,
    duckdb_path: str,
) -> int:
    """Insert canonical rows into raw_tiktok_ads_daily (DuckDB only at P-dev).

    Same self-contained pattern as meta-ads/shopify _insert_raw_rows: the connector
    owns its raw table DDL and never imports from a non-package seeds/ folder.
    """
    if db_mode == "duckdb":
        from core import warehouse_write  # noqa: PLC0415

        con = warehouse_write.open_raw_writer(duckdb_path, project_id=project_id)
        con.execute(_RAW_CREATE_DDL)
        # Additive/idempotent guard for tables created before cost_source_currency existed.
        con.execute(
            "ALTER TABLE raw_tiktok_ads_daily ADD COLUMN IF NOT EXISTS "
            "cost_source_currency VARCHAR DEFAULT 'EUR'"
        )
        # review-15-2 F-1: same additive guard for data_level on pre-existing tables.
        con.execute(
            "ALTER TABLE raw_tiktok_ads_daily ADD COLUMN IF NOT EXISTS "
            "data_level VARCHAR"
        )
        values = [
            (
                r.get("date", ""),
                r.get("data_level"),
                r.get("campaign_id", ""),
                r.get("campaign_name"),
                r.get("adgroup_id", ""),
                r.get("adgroup_name"),
                r.get("ad_id", ""),
                float(r.get("spend", 0) or 0),
                int(r.get("impressions", 0) or 0),
                int(r.get("clicks", 0) or 0),
                int(r.get("conversions", 0) or 0),
                pull_id,
                loaded_at,
                project_id,
                r.get("cost_source_currency", "EUR"),
            )
            for r in rows
        ]
        if values:
            con.executemany(_RAW_INSERT_SQL, values)
        con.close()
        return len(values)
    else:
        raise ValueError(
            f"_insert_raw_rows: unsupported db_mode {db_mode!r} at P-dev "
            "(BigQuery path not yet implemented)"
        )


def _parse_report_row(api_row: dict, profile: str) -> dict:
    """Map one TikTok /report/integrated/get row to the canonical raw record shape.

    AI-53 verifie le 2026-07-19: la reponse TikTok structure chaque ligne en deux
    sous-objets: ``dimensions`` (campaign_id/adgroup_id/ad_id + stat_time_day) et
    ``metrics`` (spend, impressions, clicks, conversions, plus les *_name). Les noms
    d'entite absents sur les grains superieurs (ex: ad_id sur campaign_daily) restent
    vides -> normalises via _normalize_not_set. Champs/version a confirmer en passe live.

    review-15-2: parse est une extraction STRUCTURELLE -- les metriques gardent leurs
    noms SOURCE (spend). transform() applique le rename canonique (spend -> cost) depuis
    le manifest, donc le rename map est exerce sur CHAQUE pull ET sur la golden fixture.
    """
    dims = api_row.get("dimensions", {}) or {}
    mets = api_row.get("metrics", {}) or {}

    # stat_time_day est un timestamp "YYYY-MM-DD HH:MM:SS" -> grain jour = la date.
    stat_day = dims.get("stat_time_day") or mets.get("stat_time_day") or ""
    date_str = str(stat_day)[:10] if stat_day else ""

    def _num(key: str):
        val = mets.get(key, 0)
        return val if val not in (None, "") else 0

    return {
        "date": date_str,
        # review-15-2 F-1: stamp the report grain so each mart series reads only its own
        # data_level (AUCTION_CAMPAIGN | AUCTION_ADGROUP | AUCTION_AD).
        "data_level": _DATA_LEVEL_BY_PROFILE.get(profile),
        "campaign_id": _normalize_not_set(dims.get("campaign_id") or mets.get("campaign_id")),
        "campaign_name": _normalize_not_set(mets.get("campaign_name")),
        "adgroup_id": _normalize_not_set(dims.get("adgroup_id") or mets.get("adgroup_id")),
        "adgroup_name": _normalize_not_set(mets.get("adgroup_name")),
        "ad_id": _normalize_not_set(dims.get("ad_id") or mets.get("ad_id")),
        # SOURCE metric names kept here; transform() renames spend -> cost (manifest).
        "spend": _num("spend"),
        "impressions": _num("impressions"),
        "clicks": _num("clicks"),
        "conversions": _num("conversions"),
        # AI-53: la devise du compte n'est pas dans /report/integrated/get ; elle vient
        # de l'endpoint advertiser/info. Defaut 'EUR' (normalise en staging via fx_rates).
        "cost_source_currency": _normalize_not_set(api_row.get("account_currency")) or "EUR",
    }


def _metrics_for_profile(profile: str) -> list[str]:
    """Return the TikTok metric field names to request for *profile*.

    The *_name fields are requested alongside the ids so the raw row carries a human
    label (nullable -- normalized via _normalize_not_set).
    """
    base = ["spend", "impressions", "clicks", "conversions"]
    if profile == "campaign_daily":
        return base + ["campaign_name"]
    if profile == "adgroup_daily":
        return base + ["campaign_id", "adgroup_name"]
    if profile == "ad_daily":
        return base + ["campaign_id", "adgroup_id"]
    return base


def _dimensions_for_profile(profile: str) -> list[str]:
    """Return the TikTok dimension field names (entity id + stat_time_day)."""
    entity = _ENTITY_DIMENSION_BY_PROFILE[profile]
    return [entity, "stat_time_day"]


def _pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    profile: str,
    advertiser_id: str | None = None,
) -> dict:
    """Shared TikTok report pull for a given *profile* grain. Called by the AI-58
    dispatch wrappers below (pull_campaign_daily / pull_adgroup_daily / pull_ad_daily).

    # AD-12: called by the queue worker only — no synchronous API call during an LLM tool call.
    # AD-3: the token is used immediately as the Access-Token header then discarded —
    #        it is NEVER stored, logged, or passed further (AD-3, NOT the Google direct flow AD-21).
    # AD-7: pull_id is minted by the caller; this function receives it and never mints its own.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    if profile not in _DATA_LEVEL_BY_PROFILE:
        raise ValueError(f"Unknown tiktok-ads report profile: {profile!r}")

    if advertiser_id is None:
        advertiser_id = os.environ.get("TIKTOK_ADS_ADVERTISER_ID")
    if not advertiser_id:
        raise ValueError(
            "TIKTOK_ADS_ADVERTISER_ID env var required for pull "
            "(set it to your TikTok Ads advertiser id)"
        )

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    # AD-3: token obtained immediately before use; falls out of scope after the call.
    token = nango_client.get_fresh_token(connection_id, provider="tiktok-ads")
    headers = {"Access-Token": token}

    url = f"{TIKTOK_API_BASE}{_INTEGRATED_REPORT_PATH}"
    data_level = _DATA_LEVEL_BY_PROFILE[profile]
    # AI-53 verifie 2026-07-19: TikTok attend les dimensions/metrics en JSON-array
    # serialise dans les query params, report_type=BASIC, page/page_size pour paginer.
    base_params = {
        "advertiser_id": advertiser_id,
        "report_type": "BASIC",
        "data_level": data_level,
        "dimensions": json.dumps(_dimensions_for_profile(profile)),
        "metrics": json.dumps(_metrics_for_profile(profile)),
        "start_date": date_from,
        "end_date": date_to,
        "page_size": _PAGE_SIZE,
    }

    all_rows: list[dict] = []
    seen_pages: set[int] = set()
    page = 1
    pages_fetched = 0
    while True:
        # review-15-2 (pattern shopify F-2): boucle BORNEE. Un serveur/mock renvoyant
        # une page non-progressante ou un total_page incoherent ne doit JAMAIS bloquer
        # le worker. Borne _MAX_PAGES + garde sur page deja vue (no silent unbounded loop).
        if page in seen_pages:
            logger.warning(
                "tiktok_ads_pull: non-progressing page cursor detected, stopping "
                "(pull_id=%s page=%d)", pull_id, page,
            )
            break
        seen_pages.add(page)
        if pages_fetched >= _MAX_PAGES:
            logger.warning(
                "tiktok_ads_pull: page cap %d reached, stopping (pull_id=%s)",
                _MAX_PAGES, pull_id,
            )
            break
        pages_fetched += 1

        params = dict(base_params, page=page)
        resp = httpx.get(url, params=params, headers=headers, timeout=30.0)

        if resp.status_code == 429:
            # AI-53 verifie 2026-07-19: rate limit sur fenetre glissante 1 min -> 429.
            # "tiktok-ads" vit dans le MODULE (pas dans core) -- AD-2 compliant.
            from core.quota import RateLimitError  # noqa: PLC0415

            retry_after_raw = resp.headers.get("Retry-After", "0")
            try:
                retry_after = int(float(retry_after_raw)) or None
            except (ValueError, TypeError):
                retry_after = None
            raise RateLimitError("tiktok-ads", retry_after)

        if resp.status_code != 200:
            # Story 25.2 / 25.7 (AC3): canonical typed error; provider payload preserved.
            # Pass the manifest error_map so 40105/40002/40100 -> auth_expired,
            # 40001 -> auth_revoked, 40300/40003 -> permission_denied, 40002/40001
            # (on 400) -> invalid_request. This is the HTTP raise site ONLY (the
            # logical code!=0 path below still raises RuntimeError -- see ROLLOUT_NOTES.md).
            from core.pull_errors import classify_http_error  # noqa: PLC0415

            try:
                _body = resp.json()
            except Exception:
                _body = resp.text
            raise classify_http_error(resp.status_code, _body, _load_error_map())

        payload = resp.json()
        # AI-53 verifie 2026-07-19: TikTok encapsule le resultat metier dans
        # {"code": 0, "message": "OK", "data": {...}}. review-15-2 F-4: on EXIGE code==0
        # explicitement. Un code manquant (payload sans 'code') n'est PAS un succes implicite
        # -- c'est une reponse inattendue/malformee -> RuntimeError. Un code != 0 (ex: 40100
        # deja gere en 429, ou 40002 params) est une erreur logique -> RuntimeError.
        if "code" not in payload:
            raise RuntimeError(
                "TikTok API response missing 'code' field (unexpected/malformed body): "
                f"{str(payload)[:200]}"
            )
        api_code = payload.get("code")
        if api_code != 0:
            raise RuntimeError(
                f"TikTok API logical error code={api_code}: "
                f"{str(payload.get('message', ''))[:200]}"
            )

        data = payload.get("data") or {}
        page_rows = data.get("list") or []
        all_rows.extend(page_rows)

        page_info = data.get("page_info") or {}
        total_page = page_info.get("total_page")
        # Avance page par page ; stop quand on a atteint total_page (ou pas d'info).
        if not total_page or page >= int(total_page):
            break
        page += 1

    # review-15-2: like meta-ads, the raw table keeps SOURCE column names (spend), and the
    # canonical rename (spend -> cost) is performed by dbt staging (manifest-mirrored). So we
    # land the PARSED rows here (source names), NOT the transform() output. transform() is
    # still genuinely exercised by conformance Layer 4: test_golden_pull_through_parse_and_
    # transform runs the API-shaped golden fixture through _parse_report_row + transform (F-2).
    raw_rows = [_parse_report_row(r, profile) for r in all_rows]

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_raw_rows(
        raw_rows, pull_id, loaded_at, project_id, db_mode, duckdb_path
    )

    # AD-3: no token in log — only pull_id and row_count (safe metadata).
    logger.info(
        "tiktok_ads_pull_completed: pull_id=%s profile=%s row_count=%d",
        pull_id, profile, row_count,
    )

    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
    }


def pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    advertiser_id: str | None = None,
) -> dict:
    """Default pull() = campaign grain (AI-58: the profile-less default dispatch).

    The queue dispatch (core.get_module_pull_fn) prefers pull_<profile_id> when a
    profile is supplied; this bare pull() is the fallback (campaign_daily), mirroring
    the meta-ads / shopify default-pull contract.
    """
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="campaign_daily", advertiser_id=advertiser_id,
    )


# ---------------------------------------------------------------------------
# AI-58 per-profile dispatch: pull_<profile_id>. core.get_module_pull_fn resolves
# these by naming convention (source-agnostic, AD-2). Each delegates to _pull() with
# the matching data_level so the three grains share ONE bounded/paginated code path.
# ---------------------------------------------------------------------------


def pull_campaign_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, advertiser_id: str | None = None,
) -> dict:
    """AI-58 dispatch: campaign grain (data_level=AUCTION_CAMPAIGN)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="campaign_daily", advertiser_id=advertiser_id,
    )


def pull_adgroup_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, advertiser_id: str | None = None,
) -> dict:
    """AI-58 dispatch: ad group grain (data_level=AUCTION_ADGROUP)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="adgroup_daily", advertiser_id=advertiser_id,
    )


def pull_ad_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, advertiser_id: str | None = None,
) -> dict:
    """AI-58 dispatch: ad grain (data_level=AUCTION_AD)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="ad_daily", advertiser_id=advertiser_id,
    )


# ---------------------------------------------------------------------------
# transform() — manifest-driven canonical field mapping
# ---------------------------------------------------------------------------


def _is_api_shaped(row: dict) -> bool:
    """True if *row* is a raw /report/integrated/get row ({dimensions, metrics})."""
    return "dimensions" in row or "metrics" in row


def transform(raw_rows: list[dict]) -> list[dict]:
    """Map source fields to canonical names using manifest mappings.

    # AD-4: canonical field mapping driven by manifest — do not hardcode source names.

    review-15-2 F-2: transform() is the module's canonicalization entrypoint (the shared
    conformance Layer 4 harness calls transform() directly on golden_pull.json). golden_pull
    .json is now the REAL API payload shape ({dimensions, metrics}), so transform() first
    routes API-shaped rows through _parse_report_row() (structural extraction + data_level
    stamp, keeping SOURCE names like spend) THEN applies the canonical rename (spend -> cost)
    from the manifest. This genuinely exercises _parse_report_row on the golden fixture, not
    just the respx mocks. Already-parsed rows (source-name dicts, e.g. from the live pull
    path) pass straight to the rename step. Structural fields pass through unchanged.
    """
    _manifest_path = Path(__file__).parent / "manifest.json"
    _manifest = json.loads(_manifest_path.read_text(encoding="utf-8"))

    rename_map: dict[str, str] = {}
    for src, val in _manifest.get("canonical_metric_mapping", {}).items():
        if isinstance(val, str):
            rename_map[src] = val
        elif isinstance(val, dict):
            rename_map[src] = val.get("canonical", src)
    rename_map.update(_manifest.get("canonical_dimension_mapping", {}))

    # F-2: parse API-shaped rows to the canonical source-name record first. The golden
    # fixture is campaign-grain (AUCTION_CAMPAIGN) — the profile the conformance replay uses.
    parsed_rows = [
        _parse_report_row(row, "campaign_daily") if _is_api_shaped(row) else row
        for row in raw_rows
    ]

    result: list[dict] = []
    for row in parsed_rows:
        canonical_row: dict = {}
        for key, value in row.items():
            canonical_row[rename_map.get(key, key)] = value
        result.append(canonical_row)
    return result


# ---------------------------------------------------------------------------
# Story 25.7 stitch: account topology discovery (playbook section 5).
# ---------------------------------------------------------------------------

# oauth2/advertiser/get is unpaged (returns the full advertiser list for the
# token) -- no page cap needed, unlike meta-ads /me/adaccounts.
_ADVERTISER_LIST_PATH = "/oauth2/advertiser/get/"


def discover_accounts(connection_id: str) -> list[dict]:
    """List the advertisers (ad accounts) the connection's token can reach.

    Story 25.7 stitch (single-level topology, selection_level 'advertiser').
    Calls the official TikTok Business API v1.3 endpoint
    ``GET /open_api/v1.3/oauth2/advertiser/get/`` (app_id + secret + Access-Token
    header) and returns the generic hierarchy core's topology flow consumes -- one
    flat level:

        [{"id": advertiser_id, "label": advertiser_name}, ...]

    The ``id`` strings are opaque to core (AD-2); pulls target
    /report/integrated/get with advertiser_id so the id is stored verbatim.

    app_id / secret are the OAuth APP credentials (module-owned config, env
    TIKTOK_APP_ID / TIKTOK_APP_SECRET) -- NOT an account id env var (those are
    forbidden by the playbook; the advertiser id now comes from selection, not env).

    Raises:
        core.quota.RateLimitError on 429 (breaker path, unchanged contract);
        a typed core.pull_errors.ConnectorError on any other non-2xx, refined by
        the manifest error_map (40105 -> auth_expired, 40300 -> permission_denied).
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    token = nango_client.get_fresh_token(connection_id, provider="tiktok-ads")
    headers = {"Access-Token": token}

    # OAuth app credentials (module config, not an account id). The advertiser
    # discovery endpoint authenticates the app AND the token.
    app_id = os.environ.get("TIKTOK_APP_ID", "")
    secret = os.environ.get("TIKTOK_APP_SECRET", "")

    url = f"{TIKTOK_API_BASE}{_ADVERTISER_LIST_PATH}"
    params: dict = {"app_id": app_id, "secret": secret}

    resp = httpx.get(url, params=params, headers=headers, timeout=30.0)

    if resp.status_code == 429:
        from core.quota import RateLimitError  # noqa: PLC0415

        retry_after_raw = resp.headers.get("Retry-After", "0")
        try:
            retry_after = int(float(retry_after_raw)) or None
        except (ValueError, TypeError):
            retry_after = None
        raise RateLimitError("tiktok-ads", retry_after)

    if resp.status_code != 200:
        from core.pull_errors import classify_http_error  # noqa: PLC0415

        try:
            _body = resp.json()
        except Exception:
            _body = resp.text
        raise classify_http_error(resp.status_code, _body, _load_error_map())

    payload = resp.json()
    # TikTok wraps success in {"code": 0, "message": "OK", "data": {"list": [...]}}.
    # A non-zero logical code on an HTTP 200 is a business error (same discipline as
    # the pull path -- see ROLLOUT_NOTES.md for planned taxonomy adoption).
    if payload.get("code") not in (0, None):
        raise RuntimeError(
            f"TikTok advertiser/get logical error code={payload.get('code')}: "
            f"{str(payload.get('message', ''))[:200]}"
        )

    data = payload.get("data") or {}
    accounts: list[dict] = []
    for item in data.get("list") or []:
        advertiser_id = item.get("advertiser_id") or item.get("id")
        if not advertiser_id:
            continue
        label = item.get("advertiser_name") or item.get("name") or str(advertiser_id)
        accounts.append({"id": str(advertiser_id), "label": label})

    return accounts


# ---------------------------------------------------------------------------
# Story 25.9 — catalog_driven execution: pull_catalog_daily.
#
# TikTok param-style: the API accepts arbitrary metric lists, so the request is
# built directly from the selection's source_fields. KEY contract: TikTok
# source_field == field_id for every metric (event-family expansions like
# conversion_add_to_cart, total_app_install, onsite_on_web_order are REAL metric
# names on the wire — no lossy re-expansion like Meta action_type arrays).
#
# Chunking: metrics chunked ~50/request (_CATALOG_METRICS_BATCH); per-chunk rows
# merged on (date + entity_id) grain key so a wide selection produces one landed
# row per grain. Level routing: ad_id → AUCTION_AD, adgroup_id → AUCTION_ADGROUP,
# else AUCTION_CAMPAIGN (deepest structural id wins). Lands in raw_tiktok_ads_daily
# (shape unchanged — same wide table as the legacy exact_bundle profiles).
# ---------------------------------------------------------------------------

# TikTok /report/integrated/get accepts up to ~100+ metrics but we chunk well under
# it so a very wide catalog selection never trips any undocumented per-request limit.
_CATALOG_METRICS_BATCH = 50

# Structural dimension ids that map to TikTok data levels (deepest wins).
# Order matters: we check ad_id first (deepest), then adgroup_id, else campaign.
_CATALOG_LEVEL_PRIORITY = [
    ("ad_id", "AUCTION_AD"),
    ("adgroup_id", "AUCTION_ADGROUP"),
    ("campaign_id", "AUCTION_CAMPAIGN"),
]

# Dimension ids that are structural grain fields (entity id + date).
# These go into the TikTok ``dimensions`` param, NOT the ``metrics`` param.
_CATALOG_STRUCTURAL_DIMS = frozenset(
    {"date", "stat_time_day", "campaign_id", "adgroup_id", "ad_id"}
)


def _catalog_level_from_selection(selection: dict) -> tuple[str, str]:
    """Return (entity_dim_source_field, data_level) from the selection's dimensions.

    Deepest structural id wins: ad_id → AUCTION_AD, adgroup_id → AUCTION_ADGROUP,
    else AUCTION_CAMPAIGN. The entity_dim_source_field is the TikTok dimension name
    to put in the ``dimensions`` param (source_field — equal to field_id for TikTok).
    """
    source_fields: dict[str, str] = selection.get("source_fields", {})
    dims: list[str] = list(selection.get("dimensions", []))

    for field_id, data_level in _CATALOG_LEVEL_PRIORITY:
        if field_id in dims:
            return source_fields.get(field_id, field_id), data_level

    # Fallback: campaign level
    return "campaign_id", "AUCTION_CAMPAIGN"


def _build_catalog_report_params(
    selection: dict,
    advertiser_id: str,
    date_from: str,
    date_to: str,
    metric_chunk: list[str],
    entity_dim: str,
    data_level: str,
) -> dict:
    """Build the TikTok /report/integrated/get query params for one metric chunk.

    The ``dimensions`` param is always [entity_dim, stat_time_day] (grain key).
    The ``metrics`` param carries the current chunk's metric source_fields.
    report_type is always BASIC (synchronous, AD-22 compatible).
    """
    return {
        "advertiser_id": advertiser_id,
        "report_type": "BASIC",
        "data_level": data_level,
        "dimensions": json.dumps([entity_dim, "stat_time_day"]),
        "metrics": json.dumps(metric_chunk),
        "start_date": date_from,
        "end_date": date_to,
        "page_size": _PAGE_SIZE,
    }


def _catalog_row_key(api_row: dict, entity_dim: str) -> tuple:
    """Merge key for chunk rows: (date, entity_id) grain (deterministic)."""
    dims = api_row.get("dimensions", {}) or {}
    mets = api_row.get("metrics", {}) or {}
    stat_day = dims.get("stat_time_day", mets.get("stat_time_day", ""))
    date_str = str(stat_day)[:10] if stat_day else ""
    entity_id = dims.get(entity_dim, mets.get(entity_dim, ""))
    return (date_str, str(entity_id))


def _merge_catalog_row(base: dict, addition: dict) -> None:
    """Merge *addition* into *base* in-place (both are raw /report/integrated/get rows).

    Merges the ``metrics`` sub-dict of *addition* into that of *base*; ``dimensions``
    are assumed identical across chunks (same grain key — entity + date).
    """
    base_metrics = base.setdefault("metrics", {})
    for k, v in (addition.get("metrics") or {}).items():
        if k not in base_metrics:
            base_metrics[k] = v


def _parse_catalog_report_row(api_row: dict, data_level: str, source_fields: dict) -> dict:
    """Map a merged catalog /report/integrated/get row to the raw_tiktok_ads_daily shape.

    Unlike _parse_report_row (which uses _DATA_LEVEL_BY_PROFILE for legacy profiles),
    the data_level here is determined by the selection's structural dimensions
    (_catalog_level_from_selection). Source field names are used verbatim (TikTok
    source_field == field_id — no re-expansion needed).

    The wide raw table covers the fixed columns (spend, impressions, clicks,
    conversions, campaign/adgroup/ad ids and names). The catalog selection may request
    additional metrics — those are stored in the ``extra_metrics`` key and ignored by
    the fixed-schema INSERT (future: wide raw table expansion).
    """
    dims = api_row.get("dimensions", {}) or {}
    mets = api_row.get("metrics", {}) or {}

    stat_day = dims.get("stat_time_day", mets.get("stat_time_day", ""))
    date_str = str(stat_day)[:10] if stat_day else ""

    def _num(key: str):
        val = mets.get(key, 0)
        return val if val not in (None, "") else 0

    def _str_dim(key: str):
        return _normalize_not_set(dims.get(key) or mets.get(key))

    return {
        "date": date_str,
        "data_level": data_level,
        "campaign_id": _str_dim("campaign_id"),
        "campaign_name": _normalize_not_set(mets.get("campaign_name")),
        "adgroup_id": _str_dim("adgroup_id"),
        "adgroup_name": _normalize_not_set(mets.get("adgroup_name")),
        "ad_id": _str_dim("ad_id"),
        "spend": _num("spend"),
        "impressions": _num("impressions"),
        "clicks": _num("clicks"),
        "conversions": _num("conversions"),
        "cost_source_currency": "EUR",
    }


def pull_catalog_daily(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    selection: dict | None = None,
    advertiser_id: str | None = None,
) -> dict:
    """Story 25.9: catalog_driven daily pull for TikTok Ads (param-style).

    core resolves+validates ``selection`` against api_catalog.json and passes it
    in as ``{"metrics","dimensions","source_fields"}``. A ``None`` selection (job
    without an explicit selection) falls back to the catalog tier-core default,
    resolved here so the callable is safe to invoke standalone in tests too.

    The pull:
      1. resolves the data_level and entity dimension from the selection's structural
         dimensions (deepest wins: ad_id→AUCTION_AD, adgroup_id→AUCTION_ADGROUP,
         else AUCTION_CAMPAIGN);
      2. builds the TikTok metrics list from selection.source_fields (source_field ==
         field_id for TikTok — event-family expansions are real metric names, no
         re-expansion needed);
      3. chunks the metrics list (~50/request) and merges per-chunk rows on the
         (date, entity_id) grain key;
      4. parses merged rows and lands them via _insert_raw_rows into raw_tiktok_ads_daily
         (same wide table as legacy exact_bundle profiles — shape unchanged).

    # AD-3: token used immediately then discarded. AD-7: pull_id passed in.
    # AD-22: the three legacy exact_bundle profiles are byte-identical (untouched).
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    # Default selection resolved FROM the catalog (tier-core) when absent.
    if selection is None:
        from core.catalog_contract import (  # noqa: PLC0415
            catalog_default_selection,
            validate_selection,
        )

        catalog = json.loads(
            (Path(__file__).parent / "api_catalog.json").read_text(encoding="utf-8")
        )
        selection, _issues = validate_selection(
            catalog, catalog_default_selection(catalog)
        )

    if advertiser_id is None:
        advertiser_id = os.environ.get("TIKTOK_ADS_ADVERTISER_ID")
    if not advertiser_id:
        raise ValueError(
            "advertiser_id (or TIKTOK_ADS_ADVERTISER_ID env var) required for "
            "pull_catalog_daily"
        )

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    # Determine data level + entity dimension from the selection.
    entity_dim, data_level = _catalog_level_from_selection(selection)

    # Build the metrics list: source_fields of selected metrics that are NOT
    # structural dimensions (structural dims go into the TikTok dimensions param).
    source_fields: dict[str, str] = selection.get("source_fields", {})
    selected_metrics: list[str] = list(selection.get("metrics", []))

    metric_source_fields: list[str] = []
    seen: set[str] = set()
    for field_id in selected_metrics:
        sf = source_fields.get(field_id, field_id)
        # Skip structural/dimension tokens (they belong in the dimensions param).
        if sf in ("stat_time_day", "campaign_id", "adgroup_id", "ad_id"):
            continue
        if sf not in seen:
            metric_source_fields.append(sf)
            seen.add(sf)

    # Chunk the metrics list so each request stays under _CATALOG_METRICS_BATCH.
    if not metric_source_fields:
        chunks: list[list[str]] = [["spend"]]
    else:
        batch = max(1, _CATALOG_METRICS_BATCH)
        chunks = [
            metric_source_fields[i: i + batch]
            for i in range(0, len(metric_source_fields), batch)
        ]

    # AD-3: token obtained immediately before use.
    token = nango_client.get_fresh_token(connection_id, provider="tiktok-ads")
    headers = {"Access-Token": token}
    url = f"{TIKTOK_API_BASE}{_INTEGRATED_REPORT_PATH}"

    merged: dict[tuple, dict] = {}

    for chunk in chunks:
        base_params = _build_catalog_report_params(
            selection, advertiser_id, date_from, date_to, chunk, entity_dim, data_level
        )

        seen_pages: set[int] = set()
        page = 1
        pages_fetched = 0

        while True:
            if page in seen_pages:
                logger.warning(
                    "tiktok_ads_catalog_pull: non-progressing page detected, "
                    "stopping (pull_id=%s page=%d)", pull_id, page,
                )
                break
            seen_pages.add(page)
            if pages_fetched >= _MAX_PAGES:
                logger.warning(
                    "tiktok_ads_catalog_pull: page cap %d reached, "
                    "stopping (pull_id=%s)", _MAX_PAGES, pull_id,
                )
                break
            pages_fetched += 1

            params = dict(base_params, page=page)
            resp = httpx.get(url, params=params, headers=headers, timeout=30.0)

            if resp.status_code == 429:
                from core.quota import RateLimitError  # noqa: PLC0415

                retry_after_raw = resp.headers.get("Retry-After", "0")
                try:
                    retry_after = int(float(retry_after_raw)) or None
                except (ValueError, TypeError):
                    retry_after = None
                raise RateLimitError("tiktok-ads", retry_after)

            if resp.status_code != 200:
                from core.pull_errors import classify_http_error  # noqa: PLC0415

                try:
                    _body = resp.json()
                except Exception:
                    _body = resp.text
                raise classify_http_error(resp.status_code, _body, _load_error_map())

            payload = resp.json()
            if "code" not in payload:
                raise RuntimeError(
                    "TikTok API response missing 'code' field (unexpected/malformed body): "
                    f"{str(payload)[:200]}"
                )
            api_code = payload.get("code")
            if api_code != 0:
                raise RuntimeError(
                    f"TikTok API logical error code={api_code}: "
                    f"{str(payload.get('message', ''))[:200]}"
                )

            data = payload.get("data") or {}
            page_rows = data.get("list") or []

            for api_row in page_rows:
                key = _catalog_row_key(api_row, entity_dim)
                if key in merged:
                    _merge_catalog_row(merged[key], api_row)
                else:
                    merged[key] = {
                        "dimensions": dict(api_row.get("dimensions") or {}),
                        "metrics": dict(api_row.get("metrics") or {}),
                    }

            page_info = data.get("page_info") or {}
            total_page = page_info.get("total_page")
            if not total_page or page >= int(total_page):
                break
            page += 1

    canonical_rows = [
        _parse_catalog_report_row(api_row, data_level, source_fields)
        for api_row in merged.values()
    ]

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_raw_rows(
        canonical_rows, pull_id, loaded_at, project_id, db_mode, duckdb_path
    )

    logger.info(
        "tiktok_ads_catalog_pull_completed: pull_id=%s profile=catalog_daily "
        "row_count=%d metrics=%d chunks=%d data_level=%s",
        pull_id, row_count, len(metric_source_fields), len(chunks), data_level,
    )

    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
    }


# ---------------------------------------------------------------------------
# Story 15.2: register this module's raw table name with core.verification.
# module->core direction is allowed by AD-2 (only core->modules is forbidden).
# The raw table name never appears in core source code (AD-2).
# ---------------------------------------------------------------------------
try:
    from core.verification import register_raw_table_name as _register_raw  # noqa: PLC0415

    _register_raw("raw_tiktok_ads_daily", provider="tiktok-ads")
except Exception:
    pass  # best-effort; verification will log a warning if table name is missing
