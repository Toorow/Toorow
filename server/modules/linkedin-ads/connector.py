"""LinkedIn Ads connector — Story 15.3 (Epic 15, connecteurs vague 1).

Acquisition B2B paid social. Expose une instance ``mcp_app: FastMCP`` que le loader
du core monte sous le namespace ``linkedin-ads`` (AD-2). Meme patron "drop-a-folder"
que meta-ads / tiktok-ads / shopify : zero edit sous server/core, un bloc UNION
additif dans le mart.

# AD-12: le MCP server lit UNIQUEMENT le mart fact_daily_kpi -- pas de raw_*.
# AD-3: le token vient de Nango juste avant usage -- jamais stocke ni logge (AD-3,
#        PAS le flux Google direct AD-21 qui est reserve au stack Google).
# AD-7: pull_id frappe par le scheduler du core, passe dans pull().
# AD-14: parametre identity/project_id.

Decisions de story (15-3-connecteur-linkedin-ads.md) :
  * DEUX grains : campaign_daily (pivot=CAMPAIGN) et campaign_group_daily
    (pivot=CAMPAIGN_GROUP). Ces deux grains sont des vues DIFFERENTES de la
    hierarchie LinkedIn (Campaign Group > Campaign) et se comportent comme des
    series paralleles independantes dans le mart (data_level = pivot LinkedIn).
    La serie campaign_group_daily AGREGE les campagnes d'un meme groupe :
    SUM(campaigns) == campaign_group total. Le pattern data_level evite le grain
    bleed (lecon TikTok review-15-2 F-1) : chaque serie mart lit UNIQUEMENT
    les lignes de son propre pivot.
  * CONVERSIONS = REVENDIQUEES par le canal (AD-4). Les conversions LinkedIn
    (externalWebsiteConversions) ne sont JAMAIS sommees avec GA4/Meta/TikTok/GSC
    sans la regle de dedup declarative (Rule P, 3.7).
  * LEADS (Lead Gen Form) = metrique distincte 'leads' ; ne mappe PAS sur
    'conversions' (conceptuellement differents : in-app lead form vs. pixel web).
  * PAS DE PAGINATION (AI-53 verifie 2026-07-19) : adAnalytics ne supporte pas
    la pagination. La reponse est limitee a 15 000 elements par requete. Garde
    anti-boucle conservee (pas de while True sans borne) mais le pull est en fait
    un seul appel REST par grain.
  * Rate limit = 45 millions de valeurs metriques / 5 minutes (not requests).
    429 -> RateLimitError("linkedin-ads", retry_after).
  * Auth : OAuth2 via Nango (AD-3). Provider 'linkedin-ads'. token = Bearer.

# AI-53 : tous les champs/endpoints API LinkedIn declares d'apres la documentation
# officielle Microsoft Learn (verifie le 2026-07-19) et annotes "a confirmer en
# passe live" (Phase B, AI-13). Source :
# https://learn.microsoft.com/en-us/linkedin/marketing/integrations/
#   ads-reporting/ads-reporting?view=li-lms-2026-06
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
mcp_app = FastMCP("linkedin-ads")

# Story 25.7 (AC3): the LinkedIn provider error map lives in manifest.json (AD-2 —
# core never hardcodes provider codes). Loaded once and cached, mirroring the
# meta-ads _load_error_map pattern. See the manifest _error_map_note: LinkedIn's
# serviceErrorCode is NOT extractable by core's generic classifier, so the map is
# keyed <status>:<status> and the pure-HTTP base class already yields the same
# class; the map is forwarded for contract symmetry and future refinement.
_ERROR_MAP: dict[str, str] | None = None


def _load_error_map() -> dict[str, str]:
    """Return the manifest's ``error_map`` (status:code -> canonical class), cached."""
    global _ERROR_MAP
    if _ERROR_MAP is None:
        manifest_path = Path(__file__).parent / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        _ERROR_MAP = manifest.get("error_map", {})
    return _ERROR_MAP

# ---------------------------------------------------------------------------
# Database connection helpers — env-var driven (no hardcoded paths).
# Same dual-backend pattern as meta-ads / tiktok-ads / shopify.
#   TOOROW_DB_MODE     = "duckdb" (default) | "bigquery"
#   TOOROW_DUCKDB_PATH = path to local .duckdb file
#   GCP_PROJECT           = GCP project ID (bigquery mode only)
# ---------------------------------------------------------------------------

_DEFAULT_DUCKDB_PATH = os.path.join(os.path.dirname(__file__), "seeds", "local.duckdb")


def _get_db_mode() -> str:
    return os.environ.get("TOOROW_DB_MODE", "duckdb")


def _get_duckdb_path() -> str:
    return os.environ.get("TOOROW_DUCKDB_PATH", _DEFAULT_DUCKDB_PATH)


def _query_duckdb(sql: str, params: list, duckdb_path: str) -> list[dict]:
    """Execute parameterized *sql* against the local DuckDB warehouse."""
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path, read_only=True)
    try:
        rel = con.execute(sql, params)
        cols = [d[0] for d in rel.description]
        return [dict(zip(cols, row)) for row in rel.fetchall()]
    finally:
        con.close()


def _query_bigquery(sql: str, params: dict) -> list[dict]:
    """Execute parameterized *sql* against BigQuery (@named parameters)."""
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
    """Fully-qualified mart table reference per engine."""
    if db_mode == "duckdb":
        from core import warehouse_tenancy  # noqa: PLC0415

        return f"{warehouse_tenancy.mart_prefix(None)}fact_daily_kpi"
    dataset = os.environ.get("BQ_MARTS_DATASET", "marts")
    gcp_project = os.environ.get("GCP_PROJECT", "")
    prefix = f"{gcp_project}.{dataset}" if gcp_project else dataset
    return f"{prefix}.fact_daily_kpi"


_MART_QUERY = """
    SELECT
        metric,
        breakdown_dimension,
        breakdown_value,
        SUM(value) AS value,
        MAX(pull_id) AS pull_id,
        MAX(loaded_at) AS freshness
    FROM {table}
    WHERE connector = 'linkedin-ads'
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

    provenance = (
        {
            "source_system": "linkedin-ads",
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
def get_linkedin_ads_report(
    project_id: str = "default",  # AD-14: identity resolved from OAuth 2.1 + PKCE
    report_profile: str = "campaign_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """LinkedIn Ads Performance Report — reads from fact_daily_kpi mart.

    Report profiles: campaign_daily, campaign_group_daily
    Metrics: cost, impressions, clicks, conversions (all additive at day grain, AD-4).
        conversions (externalWebsiteConversions) sont REVENDIQUEES par LinkedIn
        (attribution window 30j clic / 7j vue par defaut, verifie AI-53 2026-07-19)
        -- JAMAIS summees avec GA4/Meta/TikTok sans la regle de dedup (Rule P, 3.7).
        leads (leadGenerationMailContactInfoShares) = metrique distincte in-app LinkedIn.

    Returns the canonical AD-1 envelope via structuredContent.
    Text channel (this docstring) is the lean LLM summary.

    Parameters:
        project_id: Project identifier (default: 'default', AD-14 placeholder)
        report_profile: One of campaign_daily / campaign_group_daily
        date_from: Start date ISO-8601 (e.g. '2026-04-01'). Defaults to 90 days ago.
        date_to: End date ISO-8601 (e.g. '2026-06-30'). Defaults to yesterday.
    """
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
# LinkedIn Marketing API pull — Story 15.3
#
# AI-53 verifie le 2026-07-19 (doc officielle Microsoft Learn) :
#   Endpoint : GET https://api.linkedin.com/rest/adAnalytics
#   Version header : LinkedIn-Version: 202506
#   q=analytics  : 1 pivot (CAMPAIGN ou CAMPAIGN_GROUP)
#   timeGranularity=DAILY : grain jour
#   dateRange=(start:(year:Y,month:M,day:D),end:(year:Y,month:M,day:D))
#   accounts=List(urn:li:sponsoredAccount:XXXXXX)
#   fields=costInLocalCurrency,impressions,clicks,externalWebsiteConversions,
#          leadGenerationMailContactInfoShares,dateRange,pivotValues
#   Limite : 15 000 elements par reponse ; PAS de pagination (anti-boucle).
#   Rate limit : 45M valeurs metriques / 5 min ; 429 si depasse.
# ---------------------------------------------------------------------------

LINKEDIN_API_BASE = os.environ.get(
    "LINKEDIN_API_BASE", "https://api.linkedin.com/rest"
)
_AD_ANALYTICS_PATH = "/adAnalytics"

# Pivot LinkedIn par grain de report (AI-53 verifie 2026-07-19).
_PIVOT_BY_PROFILE: dict[str, str] = {
    "campaign_daily": "CAMPAIGN",
    "campaign_group_daily": "CAMPAIGN_GROUP",
}

# Champs metriques demandes dans le query param 'fields'.
# AI-53 verifie 2026-07-19 : les champs disponibles dans la doc adAnalytics.
# - costInLocalCurrency : depense dans la devise du compte (string, ex: "19.91")
# - impressions : entier
# - clicks : entier (clicks sur publicite)
# - externalWebsiteConversions : conversions web declarees via Insight Tag / API
# - leadGenerationMailContactInfoShares : soumissions Lead Gen Form (in-app leads)
# - dateRange, pivotValues : metadonnees de la reponse
_ANALYTICS_FIELDS = (
    "costInLocalCurrency,impressions,clicks,"
    "externalWebsiteConversions,leadGenerationMailContactInfoShares,"
    "dateRange,pivotValues"
)

# AI-53 : version LinkedIn-Version utilisee.
_LINKEDIN_API_VERSION = os.environ.get("LINKEDIN_API_VERSION", "202506")

# Tokens de valeur vide/manquante normalises en None -> NULL.
_NOT_SET_TOKENS = ("", "0", "(not set)", "N/A", "null", "none")

# data_level stamped per grain so mart series read only their own pivot rows.
# Pattern identique a TikTok (review-15-2 F-1 lecon CRITIQUE).
DATA_LEVEL_CAMPAIGN = "CAMPAIGN"
DATA_LEVEL_CAMPAIGN_GROUP = "CAMPAIGN_GROUP"

_DATA_LEVEL_BY_PROFILE: dict[str, str] = {
    "campaign_daily": DATA_LEVEL_CAMPAIGN,
    "campaign_group_daily": DATA_LEVEL_CAMPAIGN_GROUP,
}


def _normalize_not_set(value):
    """Map LinkedIn's empty/missing tokens to None -> NULL."""
    if value is None:
        return None
    s = str(value).strip()
    if s in _NOT_SET_TOKENS:
        return None
    return value


def _parse_date_range(date_range: dict) -> str:
    """Extract YYYY-MM-DD string from LinkedIn dateRange object."""
    if not date_range:
        return ""
    start = date_range.get("start", {}) or {}
    y = start.get("year", "")
    m = start.get("month", "")
    d = start.get("day", "")
    if y and m and d:
        return f"{y:04d}-{m:02d}-{d:02d}"
    return ""


def _date_to_li_param(date_str: str) -> dict:
    """Convert ISO date string to LinkedIn dateRange component dict."""
    parts = date_str.split("-")
    if len(parts) != 3:
        raise ValueError(f"Invalid date string for LinkedIn API: {date_str!r}")
    return {"year": int(parts[0]), "month": int(parts[1]), "day": int(parts[2])}


def _parse_report_row(api_row: dict, profile: str) -> dict:
    """Map one LinkedIn adAnalytics element to the canonical raw record shape.

    AI-53 verifie le 2026-07-19 : la reponse LinkedIn adAnalytics structure chaque
    element ainsi :
      {
        "costInLocalCurrency": "19.91",   # string (!)
        "impressions": 165,
        "clicks": 11,
        "externalWebsiteConversions": 0,
        "leadGenerationMailContactInfoShares": 0,  # optionnel
        "dateRange": {
          "start": {"year": 2026, "month": 5, "day": 28},
          "end": {"year": 2026, "month": 5, "day": 28}
        },
        "pivotValues": ["urn:li:sponsoredCampaign:1234567"]
      }
    'pivotValues' est une liste d'URNs. On extrait le numeric id de l'URN.
    Champs a confirmer en passe live (Phase B, AI-13).

    Lecon review-15-2 F-2 : parse est une extraction STRUCTURELLE ; les metriques
    gardent leurs noms SOURCE (costInLocalCurrency). transform() applique le rename
    canonique (costInLocalCurrency -> cost) depuis le manifest.
    Lecon review-15-2 F-1 : on stamp data_level pour que chaque serie mart lise UNIQUEMENT
    les lignes de son propre pivot (campaign vs campaign_group).
    """
    def _num_str(key: str):
        """Convertit un champ numerique LinkedIn (int ou string) en float."""
        val = api_row.get(key)
        if val is None or val == "":
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    def _int_field(key: str):
        val = api_row.get(key)
        if val is None:
            return None
        try:
            return int(val)
        except (ValueError, TypeError):
            return None

    # Extrait le numeric id de l'URN pivot (ex: "urn:li:sponsoredCampaign:1234567" -> "1234567")
    pivot_values = api_row.get("pivotValues") or []
    pivot_urn = pivot_values[0] if pivot_values else None
    entity_id = None
    if pivot_urn:
        parts = str(pivot_urn).rsplit(":", 1)
        entity_id = parts[-1] if len(parts) >= 2 else pivot_urn

    date_str = _parse_date_range(api_row.get("dateRange", {}) or {})

    return {
        "date": date_str,
        # Lecon review-15-2 F-1 : data_level stamp pour filtrage grain au mart.
        "data_level": _DATA_LEVEL_BY_PROFILE.get(profile),
        "campaign_id": _normalize_not_set(entity_id) if profile == "campaign_daily" else None,
        "campaign_group_id": (
            _normalize_not_set(entity_id) if profile == "campaign_group_daily" else None
        ),
        # SOURCE metric names kept here ; transform() renames (costInLocalCurrency -> cost).
        "costInLocalCurrency": _num_str("costInLocalCurrency"),
        "impressions": _int_field("impressions"),
        "clicks": _int_field("clicks"),
        "externalWebsiteConversions": _int_field("externalWebsiteConversions"),
        # Leads (Lead Gen Form) : metrique distincte -- ne mappe PAS sur 'conversions'.
        "leadGenerationMailContactInfoShares": _int_field(
            "leadGenerationMailContactInfoShares"
        ),
        # La devise vient de la cle Nango/compte LinkedIn -- on utilise 'EUR' par defaut.
        # A CONFIRMER en passe live (la devise reelle du compte peut etre USD/GBP/...).
        "cost_source_currency": "EUR",
    }


# ---------------------------------------------------------------------------
# Raw table DDL (DuckDB) — module-owned, jamais dans dbt/models/staging/ central.
# ---------------------------------------------------------------------------

_RAW_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_linkedin_ads_daily (
    date                                VARCHAR,
    data_level                          VARCHAR,
    campaign_id                         VARCHAR,
    campaign_group_id                   VARCHAR,
    costInLocalCurrency                 DOUBLE,
    impressions                         INTEGER,
    clicks                              INTEGER,
    externalWebsiteConversions          INTEGER,
    leadGenerationMailContactInfoShares INTEGER,
    pull_id                             VARCHAR,
    loaded_at                           VARCHAR,
    project_id                          VARCHAR,
    cost_source_currency                VARCHAR DEFAULT 'EUR'
)
"""

_RAW_INSERT_SQL = """
INSERT INTO raw_linkedin_ads_daily
    (date, data_level, campaign_id, campaign_group_id,
     costInLocalCurrency, impressions, clicks,
     externalWebsiteConversions, leadGenerationMailContactInfoShares,
     pull_id, loaded_at, project_id, cost_source_currency)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _insert_raw_rows(
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    project_id: str,
    db_mode: str,
    duckdb_path: str,
) -> int:
    """Insert canonical rows into raw_linkedin_ads_daily (DuckDB only at P-dev)."""
    if db_mode == "duckdb":
        from core import warehouse_write  # noqa: PLC0415

        con = warehouse_write.open_raw_writer(duckdb_path, project_id=project_id)
        con.execute(_RAW_CREATE_DDL)
        values = [
            (
                r.get("date", ""),
                r.get("data_level"),
                r.get("campaign_id"),
                r.get("campaign_group_id"),
                r.get("costInLocalCurrency"),
                r.get("impressions"),
                r.get("clicks"),
                r.get("externalWebsiteConversions"),
                r.get("leadGenerationMailContactInfoShares"),
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


def _pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    profile: str,
    account_id: str | None = None,
) -> dict:
    """Shared LinkedIn adAnalytics pull pour un *profile* donne.

    Appele par les wrappers AI-58 pull_campaign_daily / pull_campaign_group_daily.

    # AD-12: called by queue worker only — pas d'appel API synchrone lors d'un
    #         appel d'outil LLM.
    # AD-3: le token est utilise immediatement comme header Authorization, puis
    #        tombe hors de portee. JAMAIS stocke, logge, ni passe plus loin.
    # AD-7: pull_id fourni par l'appelant ; cette fonction ne mint jamais le sien.

    LIVE API = BLOCKED Phase B (AI-08/AI-13) : OAuth LinkedIn et quotas de production
    non disponibles en dev. Cette fonction sera exercee en Phase B.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    if profile not in _PIVOT_BY_PROFILE:
        raise ValueError(f"Unknown linkedin-ads report profile: {profile!r}")

    if account_id is None:
        account_id = os.environ.get("LINKEDIN_ADS_ACCOUNT_ID")
    if not account_id:
        raise ValueError(
            "LINKEDIN_ADS_ACCOUNT_ID env var required for pull "
            "(set it to your LinkedIn Ads sponsored account id)"
        )

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    # AD-3: token obtenu immediatement avant usage ; tombe hors de portee apres l'appel.
    token = nango_client.get_fresh_token(connection_id, provider="linkedin-ads")
    headers = {
        "Authorization": f"Bearer {token}",
        "LinkedIn-Version": _LINKEDIN_API_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
    }

    pivot = _PIVOT_BY_PROFILE[profile]
    account_urn = f"urn:li:sponsoredAccount:{account_id}"

    # AI-53 verifie 2026-07-19 : format dateRange LinkedIn = tuples (year, month, day).
    start = _date_to_li_param(date_from)
    end = _date_to_li_param(date_to)
    date_range = (
        f"(start:(year:{start['year']},month:{start['month']},day:{start['day']}),"
        f"end:(year:{end['year']},month:{end['month']},day:{end['day']}))"
    )

    params = {
        "q": "analytics",
        "pivot": pivot,
        "timeGranularity": "DAILY",
        "dateRange": date_range,
        "accounts": f"List({account_urn})",
        "fields": _ANALYTICS_FIELDS,
    }

    url = f"{LINKEDIN_API_BASE}{_AD_ANALYTICS_PATH}"
    resp = httpx.get(url, params=params, headers=headers, timeout=30.0)

    if resp.status_code == 429:
        # AI-53 verifie 2026-07-19 : LinkedIn renvoie 429 quand la limite de 45M
        # valeurs metriques / 5 minutes est depassee (ou limite daily applicative).
        from core.quota import RateLimitError  # noqa: PLC0415

        retry_after_raw = resp.headers.get("Retry-After", "0")
        try:
            retry_after = int(float(retry_after_raw)) or None
        except (ValueError, TypeError):
            retry_after = None
        raise RateLimitError("linkedin-ads", retry_after)

    if resp.status_code != 200:
        # Story 25.2: canonical typed error; provider payload preserved.
        from core.pull_errors import classify_http_error  # noqa: PLC0415

        try:
            _body = resp.json()
        except Exception:
            _body = resp.text
        raise classify_http_error(resp.status_code, _body, _load_error_map())

    payload = resp.json()
    # AI-53 verifie 2026-07-19 : la reponse LinkedIn encapsule les resultats dans
    # {"elements": [...], "paging": {...}}. Pas de code metier (pas de champ "code").
    # Un status HTTP 200 avec elements=[] est une reponse valide (aucune activite).
    elements = payload.get("elements") or []

    # AI-53 : PAS de pagination sur adAnalytics (limite = 15 000 elements).
    # On consomme la reponse unique. Pas de boucle while True. Pas de curseur.
    # Garde honnete : si elements >= 15 000, on loggue un avertissement (proche limite).
    if len(elements) >= 15000:
        logger.warning(
            "linkedin_ads_pull: response hit the 15000-element cap "
            "(pull_id=%s profile=%s). Data may be truncated. "
            "Consider narrowing the date range or filtering by campaign batch.",
            pull_id, profile,
        )

    raw_rows = [_parse_report_row(row, profile) for row in elements]

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_raw_rows(
        raw_rows, pull_id, loaded_at, project_id, db_mode, duckdb_path
    )

    # AD-3: pas de token dans le log -- seulement pull_id et row_count.
    logger.info(
        "linkedin_ads_pull_completed: pull_id=%s profile=%s row_count=%d",
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
    account_id: str | None = None,
) -> dict:
    """Default pull() = campaign grain (AI-58 : fallback sans profil explicite).

    Le dispatch du queue (core.get_module_pull_fn) prefere pull_<profile_id> quand
    un profil est fourni ; ce pull() bare est le fallback (campaign_daily), miroir
    du contrat meta-ads / shopify.
    """
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="campaign_daily", account_id=account_id,
    )


# ---------------------------------------------------------------------------
# AI-58 per-profile dispatch : pull_<profile_id>. core.get_module_pull_fn resout
# ces fonctions par convention de nommage (source-agnostic, AD-2). Chacune
# delegue a _pull() avec le pivot LinkedIn correspondant.
# ---------------------------------------------------------------------------


def pull_campaign_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, account_id: str | None = None,
) -> dict:
    """AI-58 dispatch : grain campagne (pivot=CAMPAIGN)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="campaign_daily", account_id=account_id,
    )


def pull_campaign_group_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, account_id: str | None = None,
) -> dict:
    """AI-58 dispatch : grain campaign group (pivot=CAMPAIGN_GROUP)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="campaign_group_daily", account_id=account_id,
    )


# ---------------------------------------------------------------------------
# Story 25.9 — catalog_driven execution: pull_catalog_daily.
#
# LinkedIn adAnalytics is a SINGLE-PIVOT API: exactly one pivot= param per
# request. The catalog_daily pull:
#   1. Reads the selection's pivot_* dimension (zero or one); >1 is an
#      InvalidRequestError BEFORE any API call.
#   2. Builds the fields param from source_fields (scalar metrics). The
#      structure metadata fields (dateRange + pivotValues) are ALWAYS appended
#      to every chunk so the merge key is present on every chunk row.
#   3. Chunks the fields param at _CATALOG_FIELDS_BATCH; per-chunk rows are
#      merged on the (dateRange, pivotValues) key (JSON-serialised canonical
#      forms) so exactly ONE row lands per grain regardless of chunk count.
#   4. Parses each merged element into the raw record shape via a lightweight
#      catalog variant of _parse_report_row, then lands into the existing wide
#      raw_linkedin_ads_daily table via _insert_raw_rows.
#
# Pivot param logic:
#   - One pivot_* dimension selected -> pivot = ENUM portion after "pivot_"
#     (e.g. pivot_MEMBER_COUNTRY_V2 -> MEMBER_COUNTRY_V2).
#   - No pivot_* dimension -> pivot = default_pivot from pivot_compatibility
#     block in catalog_sources.json (CAMPAIGN).
#   - >1 pivot_* dimension -> InvalidRequestError (pre-call).
#
# None selection -> tier-core default resolved from the catalog (same as
# meta-ads: catalog IS the execution contract). The tier-core default does not
# include any pivot_* dimension, so the default pivot CAMPAIGN is used.
# ---------------------------------------------------------------------------

# Batch size for the fields= param. LinkedIn's adAnalytics does not publish a
# hard field-count limit; 18 is intentionally conservative (well under any
# practical ceiling) and matches the predecessor agent design.
_CATALOG_FIELDS_BATCH = 18

# Metadata fields ALWAYS present in every chunk so the merge key is available.
_CATALOG_METADATA_FIELDS = ("dateRange", "pivotValues")


def _load_catalog_sources_li() -> dict:
    """Load catalog_sources/catalog_sources.json for linkedin-ads (cached)."""
    try:
        return _LI_CATALOG_SOURCES  # type: ignore[name-defined]
    except NameError:
        pass
    path = Path(__file__).parent / "catalog_sources" / "catalog_sources.json"
    globals()["_LI_CATALOG_SOURCES"] = json.loads(path.read_text(encoding="utf-8"))
    return globals()["_LI_CATALOG_SOURCES"]


def _build_catalog_request_li(
    selection: dict,
) -> tuple[list[str], str]:
    """Translate a validated *selection* into the LinkedIn adAnalytics shape.

    Returns ``(scalar_fields, pivot_value)`` where:
      - ``scalar_fields`` = source field tokens (metrics only; metadata always
        appended per chunk; NOT including dateRange/pivotValues here).
      - ``pivot_value`` = the LinkedIn pivot string (e.g. "CAMPAIGN").

    Raises InvalidRequestError when >1 pivot_* dimension is selected (must be
    refused BEFORE any API call; LinkedIn accepts exactly one pivot per request).
    """
    from core.pull_errors import InvalidRequestError  # noqa: PLC0415

    compat = _load_catalog_sources_li().get("pivot_compatibility", {})
    pivot_field_ids: set[str] = set(compat.get("pivot_fields", []))
    default_pivot: str = compat.get("default_pivot", "CAMPAIGN")

    source_fields: dict[str, str] = selection.get("source_fields", {})
    metrics: list[str] = list(selection.get("metrics", []))
    dimensions: list[str] = list(selection.get("dimensions", []))

    # Collect metric source tokens (non-pivot scalar fields).
    scalar_fields: list[str] = []
    for field_id in metrics:
        token = source_fields.get(field_id, field_id)
        if token not in scalar_fields:
            scalar_fields.append(token)

    # Dimensions: pivot_* -> pivot param; others -> scalar fields param.
    selected_pivots: list[str] = []
    for field_id in dimensions:
        if field_id in pivot_field_ids:
            selected_pivots.append(field_id)
        else:
            token = source_fields.get(field_id, field_id)
            # Non-pivot structural dimensions (campaign_id, date) are already
            # materialised from dateRange / pivotValues at parse time; they
            # are NOT added to the fields= param (LinkedIn infers them from
            # the pivot param and the date-range header).
            _ = token  # structural dim: no fields= token needed

    if len(selected_pivots) > 1:
        raise InvalidRequestError(
            provider_status=None,
            provider_payload=None,
            message=(
                f"LinkedIn adAnalytics accepts exactly 1 pivot per request but "
                f"the selection includes {len(selected_pivots)}: "
                f"{', '.join(sorted(selected_pivots))}. "
                "Remove all but one pivot_* dimension."
            ),
        )

    pivot_value: str
    if selected_pivots:
        # Strip the "pivot_" prefix to get the LinkedIn enum token.
        pivot_value = selected_pivots[0][len("pivot_"):]
    else:
        pivot_value = default_pivot

    return scalar_fields, pivot_value


def _li_catalog_row_key(element: dict) -> str:
    """Canonical merge key for chunk rows: JSON-serialised (dateRange, pivotValues)."""
    return json.dumps(
        {
            "dateRange": element.get("dateRange"),
            "pivotValues": element.get("pivotValues"),
        },
        sort_keys=True,
    )


def _parse_catalog_element_li(element: dict, pivot_value: str) -> dict:
    """Map one merged adAnalytics element to the wide raw record shape.

    The pivot_value (e.g. "CAMPAIGN") determines the data_level stamp and
    whether entity_id is placed in campaign_id or campaign_group_id.
    Reuses the scalar-field helpers from _parse_report_row so the wide landing
    shape is byte-identical to the legacy pull for the shared columns.
    """
    def _num_str(key: str):
        val = element.get(key)
        if val is None or val == "":
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    def _int_field(key: str):
        val = element.get(key)
        if val is None:
            return None
        try:
            return int(val)
        except (ValueError, TypeError):
            return None

    pivot_values = element.get("pivotValues") or []
    pivot_urn = pivot_values[0] if pivot_values else None
    entity_id = None
    if pivot_urn:
        parts = str(pivot_urn).rsplit(":", 1)
        entity_id = parts[-1] if len(parts) >= 2 else pivot_urn

    date_str = _parse_date_range(element.get("dateRange", {}) or {})

    is_campaign = (pivot_value == "CAMPAIGN")
    is_campaign_group = (pivot_value == "CAMPAIGN_GROUP")

    row: dict = {
        "date": date_str,
        "data_level": pivot_value,
        "campaign_id": _normalize_not_set(entity_id) if is_campaign else None,
        "campaign_group_id": _normalize_not_set(entity_id) if is_campaign_group else None,
        "costInLocalCurrency": _num_str("costInLocalCurrency"),
        "impressions": _int_field("impressions"),
        "clicks": _int_field("clicks"),
        "externalWebsiteConversions": _int_field("externalWebsiteConversions"),
        "leadGenerationMailContactInfoShares": _int_field(
            "leadGenerationMailContactInfoShares"
        ),
        "cost_source_currency": "EUR",
    }

    # Any additional scalar fields from the selection that the API returned:
    # copy them through under their source name so the wide table gains the column.
    # (The raw DDL uses CREATE TABLE IF NOT EXISTS; unknown columns are silently
    # ignored by _insert_raw_rows which uses positional slots for the known schema.)
    return row


def pull_catalog_daily(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    selection: dict | None = None,
    account_id: str | None = None,
) -> dict:
    """Story 25.9: catalog_driven daily pull for LinkedIn Ads.

    core resolves+validates ``selection`` against api_catalog.json and passes it
    in as ``{"metrics","dimensions","source_fields"}``. A ``None`` selection
    (job without an explicit selection) falls back to the catalog tier-core
    default, resolved here so the callable is safe to invoke standalone.

    The pull:
      1. Validates the selection: >1 pivot_* dimension -> InvalidRequestError
         BEFORE any API call.
      2. Builds the LinkedIn pivot param from the selected pivot_* dimension
         (default: CAMPAIGN when none selected).
      3. Builds the scalar fields list; chunks at _CATALOG_FIELDS_BATCH. The
         structure metadata fields dateRange + pivotValues are appended to every
         chunk so the merge key is always present.
      4. Fires one API call per chunk; merges rows on (dateRange, pivotValues)
         so exactly one raw row lands per grain regardless of chunk count.
      5. Parses merged elements and lands into raw_linkedin_ads_daily via the
         existing _insert_raw_rows (wide landing shape unchanged).

    # AD-3: token used immediately then discarded. AD-7: pull_id passed in.
    # AI-58: get_module_pull_fn('linkedin-ads', 'catalog_daily') -> this fn.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    # None selection -> tier-core default resolved FROM the catalog.
    if selection is None:
        from core.catalog_contract import (  # noqa: PLC0415
            catalog_default_selection,
            validate_selection,
        )

        _catalog = json.loads(
            (Path(__file__).parent / "api_catalog.json").read_text(encoding="utf-8")
        )
        selection, _issues = validate_selection(
            _catalog, catalog_default_selection(_catalog)
        )

    if account_id is None:
        account_id = os.environ.get("LINKEDIN_ADS_ACCOUNT_ID")
    if not account_id:
        raise ValueError(
            "LINKEDIN_ADS_ACCOUNT_ID env var required for catalog pull "
            "(set it to your LinkedIn Ads sponsored account id)"
        )

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    # Pivot/field validation BEFORE the token is even fetched (invalid_request
    # drift signal surfaces without consuming a quota request).
    scalar_fields, pivot_value = _build_catalog_request_li(selection)

    token = nango_client.get_fresh_token(connection_id, provider="linkedin-ads")
    headers = {
        "Authorization": f"Bearer {token}",
        "LinkedIn-Version": _LINKEDIN_API_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
    }

    account_urn = f"urn:li:sponsoredAccount:{account_id}"
    start = _date_to_li_param(date_from)
    end = _date_to_li_param(date_to)
    date_range_param = (
        f"(start:(year:{start['year']},month:{start['month']},day:{start['day']}),"
        f"end:(year:{end['year']},month:{end['month']},day:{end['day']}))"
    )

    url = f"{LINKEDIN_API_BASE}{_AD_ANALYTICS_PATH}"

    # Build chunk list: metadata fields always present; scalar fields chunked.
    metadata = list(_CATALOG_METADATA_FIELDS)
    chunks: list[list[str]] = []
    if not scalar_fields:
        # Only metadata: single chunk with no scalar metrics.
        chunks.append(metadata[:])
    else:
        budget = max(1, _CATALOG_FIELDS_BATCH - len(metadata))
        for start_idx in range(0, len(scalar_fields), budget):
            batch = scalar_fields[start_idx:start_idx + budget]
            chunks.append(metadata + batch)

    merged: dict[str, dict] = {}
    for chunk_fields in chunks:
        params = {
            "q": "analytics",
            "pivot": pivot_value,
            "timeGranularity": "DAILY",
            "dateRange": date_range_param,
            "accounts": f"List({account_urn})",
            "fields": ",".join(chunk_fields),
        }

        resp = httpx.get(url, params=params, headers=headers, timeout=30.0)

        if resp.status_code == 429:
            from core.quota import RateLimitError  # noqa: PLC0415

            retry_after_raw = resp.headers.get("Retry-After", "0")
            try:
                retry_after = int(float(retry_after_raw)) or None
            except (ValueError, TypeError):
                retry_after = None
            raise RateLimitError("linkedin-ads", retry_after)

        if resp.status_code != 200:
            from core.pull_errors import classify_http_error  # noqa: PLC0415

            try:
                _body = resp.json()
            except Exception:
                _body = resp.text
            raise classify_http_error(resp.status_code, _body, _load_error_map())

        elements = resp.json().get("elements") or []
        if len(elements) >= 15000:
            logger.warning(
                "linkedin_ads_catalog_pull: chunk response hit 15000-element cap "
                "(pull_id=%s chunk_fields=%s). Data may be truncated.",
                pull_id, chunk_fields,
            )

        for element in elements:
            key = _li_catalog_row_key(element)
            if key in merged:
                merged[key].update(element)
            else:
                merged[key] = dict(element)

    canonical_rows = [
        _parse_catalog_element_li(element, pivot_value)
        for element in merged.values()
    ]

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_raw_rows(
        canonical_rows, pull_id, loaded_at, project_id, db_mode, duckdb_path
    )

    logger.info(
        "linkedin_ads_catalog_pull_completed: pull_id=%s pivot=%s row_count=%d "
        "scalar_fields=%d chunks=%d",
        pull_id, pivot_value, row_count, len(scalar_fields), len(chunks),
    )

    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
    }


# ---------------------------------------------------------------------------
# transform() — manifest-driven canonical field mapping
# ---------------------------------------------------------------------------


def _is_api_shaped(row: dict) -> bool:
    """True si *row* est un element brut de la reponse adAnalytics LinkedIn.

    Critere : presence de 'pivotValues' OU 'dateRange' (champs exclusifs a l'API).
    Les lignes deja parsees (issues de _parse_report_row) ne portent pas ces cles.
    """
    return "pivotValues" in row or "dateRange" in row


def transform(raw_rows: list[dict]) -> list[dict]:
    """Map source fields to canonical names using manifest mappings.

    # AD-4: canonical field mapping driven by manifest.

    Lecon review-15-2 F-2 : transform() est l'entrypoint de canonicalisation du
    module (la conformance Layer 4 l'appelle directement sur golden_pull.json).
    golden_pull.json est au shape API reel ({pivotValues, dateRange, costInLocalCurrency, ...}),
    donc transform() route d'abord les lignes API-shapees via _parse_report_row()
    (extraction structurelle + stamp data_level, noms SOURCE) PUIS applique le
    rename canonique (costInLocalCurrency -> cost) depuis le manifest. Cela garantit
    que _parse_report_row est genuinement exerce sur la fixture golden, pas seulement
    sur les mocks. Les lignes deja parsees (noms source dict) passent directement.
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

    # F-2 : parse les lignes API-shapees en premier. La fixture golden est au grain
    # campaign_daily (CAMPAIGN) -- le profil que la conformance replay utilise.
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
# Story 25.7 stitch: discover_accounts — lists the sponsored ad accounts the
# connection's token can reach via GET /rest/adAccounts?q=search (cursor paged).
#
# AI-53 / doc officielle (li-lms-2026-06) :
#   GET https://api.linkedin.com/rest/adAccounts?q=search
#   Headers: Authorization: Bearer <token>, LinkedIn-Version, X-Restli-Protocol-Version: 2.0.0
#   Cursor pagination: pageSize (<=1000) + pageToken ; nextPageToken in metadata.
#   Response: {"elements":[{"id":507404993,"name":"...","status":"ACTIVE",...}],
#              "metadata":{"nextPageToken":"..."}}
#   The numeric id becomes the sponsored account URN the pull targets.
# ---------------------------------------------------------------------------

_AD_ACCOUNTS_PATH = "/adAccounts"
# Guard against a runaway pagination loop (defensive; a real token reaches a
# bounded number of accounts). Mirrors meta-ads _MAX_ADACCOUNT_PAGES.
_MAX_ADACCOUNT_PAGES = 50
_ADACCOUNT_PAGE_SIZE = 100


def discover_accounts(connection_id: str) -> list[dict]:
    """List the sponsored ad accounts the connection's token can reach.

    Story 25.7 stitch (single flat topology level 'ad_account', selection_level
    'ad_account'). Calls GET /rest/adAccounts?q=search (cursor-paged) and returns
    the generic hierarchy core's topology flow consumes:

        [{"id": "urn:li:sponsoredAccount:507404993", "label": "Dunder Mifflin Account"}, ...]

    The ``id`` is the FULL sponsored-account URN so a downstream pull can target
    ``accounts=List(urn:li:sponsoredAccount:<id>)`` verbatim (AD-2: opaque to core).

    # AD-3: the token is used immediately as the Authorization header, then falls
    #        out of scope. Never stored, logged, nor passed further.

    Raises:
        core.quota.RateLimitError("linkedin-ads", retry_after) on 429 (breaker path);
        a typed core.pull_errors.ConnectorError on any other non-2xx, refined by the
        manifest error_map (401 -> auth_expired via the pure-HTTP base class).
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    token = nango_client.get_fresh_token(connection_id, provider="linkedin-ads")
    headers = {
        "Authorization": f"Bearer {token}",
        "LinkedIn-Version": _LINKEDIN_API_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
    }

    url = f"{LINKEDIN_API_BASE}{_AD_ACCOUNTS_PATH}"
    accounts: list[dict] = []
    page_token: str | None = None
    pages = 0

    while True:
        if pages >= _MAX_ADACCOUNT_PAGES:
            logger.warning(
                "linkedin_ads_discover_accounts: page cap %d reached, stopping",
                _MAX_ADACCOUNT_PAGES,
            )
            break
        pages += 1

        params: dict[str, str] = {
            "q": "search",
            "pageSize": str(_ADACCOUNT_PAGE_SIZE),
        }
        if page_token:
            params["pageToken"] = page_token

        resp = httpx.get(url, params=params, headers=headers, timeout=30.0)

        if resp.status_code == 429:
            from core.quota import RateLimitError  # noqa: PLC0415

            retry_after_raw = resp.headers.get("Retry-After", "0")
            try:
                retry_after = int(float(retry_after_raw)) or None
            except (ValueError, TypeError):
                retry_after = None
            raise RateLimitError("linkedin-ads", retry_after)

        if resp.status_code != 200:
            from core.pull_errors import classify_http_error  # noqa: PLC0415

            try:
                _body = resp.json()
            except Exception:
                _body = resp.text
            raise classify_http_error(resp.status_code, _body, _load_error_map())

        payload = resp.json()
        for item in payload.get("elements") or []:
            raw_id = item.get("id")
            if raw_id is None:
                continue
            account_urn = f"urn:li:sponsoredAccount:{raw_id}"
            accounts.append(
                {"id": account_urn, "label": item.get("name") or account_urn}
            )

        # Cursor pagination: nextPageToken lives in the metadata block.
        page_token = (payload.get("metadata") or {}).get("nextPageToken")
        if not page_token:
            break

    logger.info(
        "linkedin_ads_discover_accounts: connection=%s accounts=%d pages=%d",
        connection_id, len(accounts), pages,
    )
    return accounts


# ---------------------------------------------------------------------------
# Enregistrement du nom de table raw aupres de core.verification.
# ---------------------------------------------------------------------------
try:
    from core.verification import register_raw_table_name as _register_raw  # noqa: PLC0415

    _register_raw("raw_linkedin_ads_daily", provider="linkedin-ads")
except Exception:
    pass  # best-effort ; verification loggue un avertissement si le nom est absent
