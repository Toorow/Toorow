"""Meta Ads connector — Story 3.6.

Exposes a ``mcp_app: FastMCP`` instance that the core loader mounts under the
``meta-ads`` namespace (AD-2). This is the second module on the shared base —
the FR2 "drop a folder" proof: zero edits to server/core, one mart UNION.

# AD-12: MCP server reads the fact_daily_kpi mart only — no raw_* tables, no CSV.
# AD-3: token from Nango immediately before use — never stored or logged.
# AD-7: pull_id minted by the core scheduler and passed into pull().
# AD-14: identity/project_id parameter scaffold.
# AI-10: all Meta metrics are additive at day grain; no aggregation_rule needed.
#
# review-15-9 F-1 (data_level, EXACT tiktok-ads post-review-15-2-F-1 pattern):
# Meta exposes THREE report grains (campaign / adset / creative daily). Every grain
# row carries campaign_id (adset/creative rows also carry the parent campaign_id).
# Without a data_level marker, when two grains are pulled the same day the mart's
# campaign_id series would SUM the campaign-grain row AND the campaign_id carried by
# each adset/creative-grain row -> the campaign double-counts inside its own series.
# We now stamp data_level (CAMPAIGN | ADSET | CREATIVE) per grain in the raw row and
# each mart series reads ONLY its own data_level, so the grains never bleed. Legacy
# rows (pre-migration, data_level NULL after the ALTER) are treated as CAMPAIGN grain
# via a documented COALESCE in staging (the historical pull was campaign-level only).
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
# The core does: mcp.mount(loaded.connector_module.mcp_app, namespace=loaded.name)
mcp_app = FastMCP("meta-ads")

# ---------------------------------------------------------------------------
# Database connection helpers — env-var driven (no hardcoded paths).
# Same dual-backend pattern as google-analytics/connector.py.
#   TOOROW_DB_MODE     = "duckdb" (default) | "bigquery"
#   TOOROW_DUCKDB_PATH = path to local .duckdb file
#   GCP_PROJECT           = GCP project ID (bigquery mode only)
# ---------------------------------------------------------------------------

_DEFAULT_DUCKDB_PATH = os.path.join(os.path.dirname(__file__), "seeds", "local.duckdb")


def _get_db_mode() -> str:
    return os.environ.get("TOOROW_DB_MODE", "duckdb")


def _get_duckdb_path() -> str:
    return os.environ.get("TOOROW_DUCKDB_PATH", _DEFAULT_DUCKDB_PATH)


# Story 25.4 (AC5): the Meta provider error map lives in manifest.json (AD-2 —
# core never hardcodes provider codes). Loaded once and cached, mirroring the
# manifest access pattern used by transform().
_ERROR_MAP: dict[str, str] | None = None


def _load_error_map() -> dict[str, str]:
    """Return the manifest's ``error_map`` (status:code -> canonical class), cached.

    Keys are ``"<http_status>:<provider_code_or_subcode>"``. The classifier in
    core.pull_errors extracts the Graph error subcode BEFORE the code, so 190
    auth subcodes (458/459/460/463/464/467) are keyed on their own status.
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
    WHERE connector = 'meta-ads'
      AND project_id = {p_project}
      AND date BETWEEN {p_from} AND {p_to}
    GROUP BY metric, breakdown_dimension, breakdown_value
    ORDER BY metric, breakdown_dimension, breakdown_value
"""


def _query_mart(date_from: str, date_to: str, project_id: str = "default") -> list[dict]:
    """Query fact_daily_kpi mart — DuckDB or BigQuery depending on env.

    # AD-12: MCP server reads marts only — never raw_* tables or CSV
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
            "source_system": "meta-ads",
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
def get_meta_ads_report(
    project_id: str = "default",  # AD-14: identity resolved from OAuth 2.1 + PKCE
    report_profile: str = "campaign_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Meta Ads Performance Report — reads from fact_daily_kpi mart.

    Report profiles: campaign_daily, adset_daily, creative_daily
    Metrics: cost, impressions, clicks, conversions (all additive, AD-4)
    Breakdown dimensions: campaign_id, adset_id, ad_id

    Returns the canonical AD-1 envelope via structuredContent.
    Text channel (this docstring) is the lean LLM summary (<=30 lines).

    # AI-10: all Meta metrics are additive at day grain; no aggregation_rule needed.

    Parameters:
        project_id: Project identifier (default: 'default', AD-14 placeholder)
        report_profile: One of campaign_daily / adset_daily / creative_daily
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
# Meta Marketing API pull — Story 3.6 (AC3, AC4, AC5)
# ---------------------------------------------------------------------------

# Base URL for the Meta Marketing (Graph) API. v20+ per AC4.
META_GRAPH_API_BASE = "https://graph.facebook.com/v20.0"

# review-15-9 F-1: data_level per report grain (mirrors tiktok _DATA_LEVEL_BY_PROFILE).
# The Meta Insights ``level`` param (campaign|adset|ad) drives which grain the API
# returns; we stamp the corresponding data_level so the mart reads one grain per series.
_DATA_LEVEL_BY_PROFILE: dict[str, str] = {
    "campaign_daily": "CAMPAIGN",
    "adset_daily": "ADSET",
    "creative_daily": "CREATIVE",
}

# Meta Insights ``level`` API param per report profile.
_API_LEVEL_BY_PROFILE: dict[str, str] = {
    "campaign_daily": "campaign",
    "adset_daily": "adset",
    "creative_daily": "ad",
}

# review-15-9 F-1: data_level distinguishes WHICH report grain landed each raw row
# (CAMPAIGN | ADSET | CREATIVE). Without it, when several grains are pulled the same day
# the mart cannot tell a campaign-grain row apart from the campaign_id carried by an
# adset/creative-grain row -> the campaign_id series would sum BOTH and double-count the
# campaign inside its own series. Each mart series now reads ONLY the rows of its own
# data_level, so the grains never bleed into one another (EXACT tiktok-ads F-1 pattern).
_RAW_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_meta_ads_daily (
    date                  VARCHAR,
    data_level            VARCHAR,
    campaign_id           VARCHAR,
    campaign_name         VARCHAR,
    adset_id              VARCHAR,
    adset_name            VARCHAR,
    ad_id                 VARCHAR,
    creative_id           VARCHAR,
    spend                 DOUBLE,
    impressions           INTEGER,
    clicks                INTEGER,
    conversions           INTEGER,
    pull_id               VARCHAR,
    loaded_at             VARCHAR,
    project_id            VARCHAR,
    cost_source_currency  VARCHAR DEFAULT 'USD'
)
"""

_RAW_INSERT_SQL = """
INSERT INTO raw_meta_ads_daily
    (date, data_level, campaign_id, campaign_name, adset_id, adset_name, ad_id, creative_id,
     spend, impressions, clicks, conversions, pull_id, loaded_at, project_id,
     cost_source_currency)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _insert_raw_rows(
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    project_id: str,
    db_mode: str,
    duckdb_path: str,
) -> int:
    """Insert canonical rows into raw_meta_ads_daily (DuckDB only at P3-dev).

    Same self-contained pattern as GA4's _insert_raw_rows: the connector owns
    its raw table DDL and never imports from a non-package seeds/ folder.
    """
    if db_mode == "duckdb":
        from core import warehouse_write  # noqa: PLC0415

        con = warehouse_write.open_raw_writer(duckdb_path, project_id=project_id)
        con.execute(_RAW_CREATE_DDL)
        # G-04: add cost_source_currency to existing tables created before this fix.
        con.execute(
            "ALTER TABLE raw_meta_ads_daily ADD COLUMN IF NOT EXISTS "
            "cost_source_currency VARCHAR DEFAULT 'USD'"
        )
        # review-15-9 F-1: additive/idempotent guard for data_level on pre-existing
        # tables (migrates raw_meta_ads_daily created before the 3-grain fix). Legacy
        # rows stay data_level NULL -> staging COALESCEs them to 'CAMPAIGN' (the only
        # grain the historical pull produced). Documented in stg_meta_ads_daily.
        con.execute(
            "ALTER TABLE raw_meta_ads_daily ADD COLUMN IF NOT EXISTS data_level VARCHAR"
        )
        values = [
            (
                r.get("date", ""),
                r.get("data_level"),
                r.get("campaign_id", ""),
                r.get("campaign_name", ""),
                r.get("adset_id", ""),
                r.get("adset_name", ""),
                r.get("ad_id", ""),
                r.get("creative_id", ""),
                float(r.get("spend", 0) or 0),
                int(r.get("impressions", 0) or 0),
                int(r.get("clicks", 0) or 0),
                int(r.get("conversions", 0) or 0),
                pull_id,
                loaded_at,
                project_id,
                r.get("cost_source_currency", "USD"),
            )
            for r in rows
        ]
        con.executemany(_RAW_INSERT_SQL, values)
        con.close()
        return len(values)
    else:
        raise ValueError(
            f"_insert_raw_rows: unsupported db_mode {db_mode!r} at P3-dev "
            "(BigQuery path not yet implemented)"
        )


def _extract_conversions(insight_row: dict) -> int:
    """Derive a conversions count from a Meta insights row.

    Dev Notes (conversions mapping choice): at P3-dev (mocked) we read the
    ``conversions`` field directly from the insights row when present. If it is
    absent, we fall back to summing the ``actions`` array on the pixel purchase
    action type (offsite_conversion.fb_pixel_purchase). This affects Story 3.7's
    cross-source dedup rule (GA4 counts goal completions; Meta counts pixel
    conversions — they measure different things and stay as separate rows).
    """
    if "conversions" in insight_row and insight_row["conversions"] not in (None, ""):
        try:
            return int(float(insight_row["conversions"]))
        except (ValueError, TypeError):
            return 0

    total = 0
    for action in insight_row.get("actions", []) or []:
        if action.get("action_type") == "offsite_conversion.fb_pixel_purchase":
            try:
                total += int(float(action.get("value", 0)))
            except (ValueError, TypeError):
                continue
    return total


def _parse_insight_row(api_row: dict, profile: str = "campaign_daily") -> dict:
    """Map one Meta insights API row to the canonical raw record shape.

    review-15-9 F-1: *profile* stamps data_level (CAMPAIGN | ADSET | CREATIVE) so the
    mart reads one grain per breakdown series (EXACT tiktok-ads _parse_report_row shape).
    """
    return {
        "date": api_row.get("date_start", api_row.get("date", "")),
        # review-15-9 F-1: stamp the report grain so each mart series reads only its own.
        "data_level": _DATA_LEVEL_BY_PROFILE.get(profile),
        "campaign_id": api_row.get("campaign_id", ""),
        "campaign_name": api_row.get("campaign_name", ""),
        "adset_id": api_row.get("adset_id", ""),
        "adset_name": api_row.get("adset_name", ""),
        "ad_id": api_row.get("ad_id", ""),
        "creative_id": api_row.get("creative_id", ""),
        "spend": api_row.get("spend", 0),
        "impressions": api_row.get("impressions", 0),
        "clicks": api_row.get("clicks", 0),
        "conversions": _extract_conversions(api_row),
        # G-04: account_currency is returned by the Graph API insights endpoint
        # when requested via the fields param. Default 'USD' when absent (mocks/backfill).
        "cost_source_currency": api_row.get("account_currency", "USD"),
    }


def _pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    profile: str,
    ad_account_id: str | None = None,
) -> dict:
    """Shared Meta insights pull for a given *profile* grain. Called by the AI-58
    dispatch wrappers below (pull_campaign_daily / pull_adset_daily / pull_creative_daily).

    # AD-12: called by the queue worker only — no synchronous API call during
    # an LLM tool invocation.
    # AD-3: the token is used immediately as a Bearer header then discarded —
    # it is NEVER stored, logged, or passed further.
    # AD-7: pull_id is minted by the caller; this function receives it and never
    # mints its own.

    review-15-9 F-1: *profile* selects both the Insights ``level`` param and the
    data_level stamped on every landed row, so each of the three grains lands under
    its own data_level and the mart never bleeds one grain into another.

    Parameters
    ----------
    connection_id:
        Nango connection identifier passed to get_fresh_token.
    date_from, date_to:
        ISO-8601 date strings (YYYY-MM-DD) for the Meta insights time_range.
    project_id:
        Project binding for the raw rows.
    pull_id:
        Core-minted pull ULID (AD-7). Passed in; not generated here.
    profile:
        One of campaign_daily / adset_daily / creative_daily.
    ad_account_id:
        Meta ad account ID (digits, without the ``act_`` prefix). If None, reads
        the META_ADS_AD_ACCOUNT_ID env var.

    Returns
    -------
    dict
        {"pull_id": str, "row_count": int, "date_from": str, "date_to": str}

    Raises
    ------
    ValueError
        If META_ADS_AD_ACCOUNT_ID env var is missing and ad_account_id is None,
        or if *profile* is not a known Meta report profile.
    RuntimeError
        If the Meta Marketing API returns a non-200 (non-429) response.
    RateLimitError
        If the Meta Marketing API returns HTTP 429 (AC5, HG-4).
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    if profile not in _DATA_LEVEL_BY_PROFILE:
        raise ValueError(f"Unknown meta-ads report profile: {profile!r}")

    # T4.8: AD_ACCOUNT_ID env-var fallback when the parameter is not supplied.
    if ad_account_id is None:
        ad_account_id = os.environ.get("META_ADS_AD_ACCOUNT_ID")
    if not ad_account_id:
        raise ValueError(
            "META_ADS_AD_ACCOUNT_ID env var required for pull "
            "(set it to your Meta ad account id digits, without the 'act_' prefix)"
        )
    # Normalize: accept both 'act_123' and '123'; the API path needs 'act_<digits>'.
    account_digits = ad_account_id[4:] if ad_account_id.startswith("act_") else ad_account_id

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    # AD-3: token obtained immediately before use; falls out of scope after the call.
    token = nango_client.get_fresh_token(connection_id, provider="meta-ads")

    params = {
        "fields": "campaign_id,campaign_name,adset_id,adset_name,ad_id,"
        "spend,impressions,clicks,actions,conversions,account_currency",
        # review-15-9 F-1: the Insights level param selects the report grain.
        "level": _API_LEVEL_BY_PROFILE[profile],
        "time_increment": 1,
        "time_range": json.dumps({"since": date_from, "until": date_to}),
    }
    url = f"{META_GRAPH_API_BASE}/act_{account_digits}/insights"

    resp = httpx.get(
        url,
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )

    if resp.status_code == 429:
        # AC5 / HG-4: raise RateLimitError so the worker trips the breaker.
        # "meta-ads" lives in the MODULE (not in core) -- AD-2 compliant.
        from core.quota import RateLimitError  # noqa: PLC0415

        retry_after_raw = resp.headers.get("Retry-After", "0")
        try:
            retry_after = int(retry_after_raw) or None
        except (ValueError, TypeError):
            retry_after = None
        raise RateLimitError("meta-ads", retry_after)

    if resp.status_code != 200:
        # Story 25.2: canonical typed error (401->auth_expired, 403->permission_denied,
        # 400->invalid_request, 5xx->provider_transient). Provider payload preserved.
        from core.pull_errors import classify_http_error  # noqa: PLC0415

        try:
            _body = resp.json()
        except Exception:
            _body = resp.text
        # Story 25.4 (AC5): pass the manifest's Meta error_map so 190->auth_expired,
        # subcodes 458/... -> auth_revoked, 10/200 -> permission_denied, etc.
        raise classify_http_error(resp.status_code, _body, _load_error_map())

    payload = resp.json()
    api_rows = payload.get("data") or []

    canonical_rows: list[dict] = [_parse_insight_row(r, profile) for r in api_rows]

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_raw_rows(
        canonical_rows, pull_id, loaded_at, project_id, db_mode, duckdb_path
    )

    # AD-3: no token in log — only pull_id and row_count (safe metadata).
    logger.info(
        "meta_ads_pull_completed: pull_id=%s profile=%s row_count=%d",
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
    ad_account_id: str | None = None,
) -> dict:
    """Default pull() = campaign grain (AI-58: the profile-less default dispatch).

    The queue dispatch (core.get_module_pull_fn) prefers pull_<profile_id> when a
    profile is supplied; this bare pull() is the fallback (campaign_daily), mirroring
    the tiktok-ads / shopify default-pull contract.
    """
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="campaign_daily", ad_account_id=ad_account_id,
    )


# ---------------------------------------------------------------------------
# AI-58 per-profile dispatch: pull_<profile_id>. core.get_module_pull_fn resolves
# these by naming convention (source-agnostic, AD-2). Each delegates to _pull() with
# the matching data_level so the three grains share ONE code path (EXACT tiktok pattern).
# ---------------------------------------------------------------------------


def pull_campaign_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, ad_account_id: str | None = None,
) -> dict:
    """AI-58 dispatch: campaign grain (data_level=CAMPAIGN)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="campaign_daily", ad_account_id=ad_account_id,
    )


def pull_adset_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, ad_account_id: str | None = None,
) -> dict:
    """AI-58 dispatch: ad set grain (data_level=ADSET)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="adset_daily", ad_account_id=ad_account_id,
    )


def pull_creative_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str,
    pull_id: str, ad_account_id: str | None = None,
) -> dict:
    """AI-58 dispatch: creative/ad grain (data_level=CREATIVE)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="creative_daily", ad_account_id=ad_account_id,
    )


# ---------------------------------------------------------------------------
# Epic 31.6 — campaign_launch event profile (landing: context_events).
#
# Generalises the YouTube 31.3 video_upload pattern to the ads family: each Meta
# campaign becomes a canonical marker `meta > campaign_launch > <name>` that
# annotates the spend/conversion curves AND feeds the MMM exogenous-regressor
# feature builder (epic-31 §10). This profile writes ONLY context_events -- never
# raw_meta_ads_daily nor fact_daily_kpi (HG-2 inverted per profile).
# ---------------------------------------------------------------------------

# Canonical event mapping mirrors canonical_event_mapping in the manifest. Pure
# lookup (no I/O) -- unit-testable via golden_events/expected_events.
_CANONICAL_EVENT_MAPPING = {"campaign_launch": "campaign_launch"}


def transform_events(
    raw_rows: list[dict],
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict]:
    """Map raw Graph campaign dicts to canonical event dicts (AD-2, pure function).

    Input: a list of campaign resource dicts from /act_<id>/campaigns, each with
    ``id``/``name``/``start_time`` (and optionally ``created_time``). Output:
    canonical event dicts with keys event_type, event_date, label, platform,
    source -- ready for persist_context_event() (value = None, pulse).

    event_date = start_time truncated to YYYY-MM-DD (fallback created_time).
    label      = name.
    event_type = "campaign_launch" (via _CANONICAL_EVENT_MAPPING).
    platform   = "meta".
    source     = "meta-ads".

    Date window (H1, symmetric to youtube): the campaigns edge returns every
    campaign on the account, so an unfiltered transform would emit the full
    account history on every pull. When ``date_from``/``date_to`` are supplied,
    campaigns whose launch date falls OUTSIDE [date_from, date_to] are dropped.
    Both None (the golden-replay contract) means "no window" -> every campaign
    passes, so golden_events.json -> expected_events.json stays a pure 1:1 map.

    Validity (M1): a campaign with no usable start_time/created_time (missing, or
    shorter than a 10-char ISO date prefix) is SKIPPED rather than persisted with
    an empty event_date (validate_event_input would reject it downstream anyway).
    """
    event_type = _CANONICAL_EVENT_MAPPING.get("campaign_launch", "campaign_launch")
    result: list[dict] = []
    for campaign in raw_rows:
        raw_date = campaign.get("start_time") or campaign.get("created_time") or ""
        # M1: reject a campaign without a valid ISO date prefix (>=10 chars).
        if len(raw_date) < 10:
            logger.debug(
                "transform_events: skipping campaign with invalid start_time=%r", raw_date
            )
            continue
        event_date = raw_date[:10]
        # H1: bounded window filter (no-op when both bounds are None).
        if date_from is not None and event_date < date_from:
            continue
        if date_to is not None and event_date > date_to:
            continue
        result.append(
            {
                "event_type": event_type,
                "event_date": event_date,
                "label": campaign.get("name") or "",
                "platform": "meta",
                "source": "meta-ads",
            }
        )
    return result


def pull_campaign_launch(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    ad_account_id: str | None = None,
) -> dict:
    """Fetch the ad account's campaigns and persist each launch as a context_event.

    Graph API path (reuses the existing meta-ads token/ad-account scope -- no new
    scope): GET /act_<digits>/campaigns?fields=id,name,start_time,created_time,
    status, following ``paging.next`` (same cursor idiom as discover_accounts).

    Idempotence (Epic 31.3/H1): the campaigns edge has no server-side date filter,
    so two guards make a daily re-pull idempotent (no double-counted markers /
    doubled MMM regressor):
      1. transform_events() filters campaigns to [date_from, date_to] client-side.
      2. delete_connector_events_in_window() clears this project's prior meta-ads
         campaign_launch events in the SAME window BEFORE the re-insert.
    No new migration is required (context_events.type/source already exist).

    AD-3:  token fetched immediately before use, then discarded.
    AI-03: ASCII-only in log strings.
    AD-2:  canonical event mapping via transform_events().
    AD-8:  project-scoped (delete + insert both bound to project_id).
    """
    from core import nango_client  # noqa: PLC0415
    from core.context_events import (  # noqa: PLC0415
        delete_connector_events_in_window,
        persist_context_event,
    )

    if ad_account_id is None:
        ad_account_id = os.environ.get("META_ADS_AD_ACCOUNT_ID")
    if not ad_account_id:
        raise ValueError(
            "META_ADS_AD_ACCOUNT_ID env var required for pull "
            "(set it to your Meta ad account id digits, without the 'act_' prefix)"
        )
    account_digits = ad_account_id[4:] if ad_account_id.startswith("act_") else ad_account_id

    token = nango_client.get_fresh_token(connection_id, provider="meta-ads")

    # Paginate the campaigns edge (Graph cursor paging via paging.next).
    raw_rows: list[dict] = []
    url: str | None = f"{META_GRAPH_API_BASE}/act_{account_digits}/campaigns"
    params: dict | None = {
        "fields": "id,name,start_time,created_time,status",
        "limit": "100",
    }
    pages = 0
    while url is not None:
        if pages >= _MAX_ADACCOUNT_PAGES:
            logger.warning(
                "pull_campaign_launch: page cap %d reached, stopping", _MAX_ADACCOUNT_PAGES
            )
            break
        pages += 1

        resp = httpx.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )

        if resp.status_code == 429:
            from core.quota import RateLimitError  # noqa: PLC0415

            retry_after_raw = resp.headers.get("Retry-After", "0")
            try:
                retry_after = int(retry_after_raw) or None
            except (ValueError, TypeError):
                retry_after = None
            raise RateLimitError("meta-ads", retry_after)

        if resp.status_code != 200:
            from core.pull_errors import classify_http_error  # noqa: PLC0415

            try:
                _body = resp.json()
            except Exception:
                _body = resp.text
            raise classify_http_error(resp.status_code, _body, _load_error_map())

        payload = resp.json()
        raw_rows.extend(payload.get("data") or [])
        url = (payload.get("paging") or {}).get("next")
        params = None  # the next URL already embeds the cursor + fields

    logger.info(
        "pull_campaign_launch: fetched %d campaign(s) in %d page(s) for connection_id=%s",
        len(raw_rows), pages, connection_id,
    )

    canonical_events = transform_events(raw_rows, date_from=date_from, date_to=date_to)

    # H1: delete-by-source-window BEFORE re-insert -> idempotent daily re-pull.
    event_type = _CANONICAL_EVENT_MAPPING.get("campaign_launch", "campaign_launch")
    delete_connector_events_in_window(
        project_id=project_id,
        source="meta-ads",
        event_type=event_type,
        date_from=date_from,
        date_to=date_to,
    )

    event_count = 0
    for ev in canonical_events:
        label = ev["label"][:120]  # validate_event_input enforces max 120.
        persist_context_event(
            project_id=project_id,
            event_date=ev["event_date"],
            type=ev["event_type"],
            label=label,
            description=label,
            created_by=f"meta-ads_pull:{pull_id}",
            platform=ev["platform"],
            value=None,
            source=ev["source"],
        )
        event_count += 1

    logger.info(
        "pull_campaign_launch: persisted %d context_events: pull_id=%s date_from=%s date_to=%s",
        event_count, pull_id, date_from, date_to,
    )
    return {
        "pull_id": pull_id,
        "event_count": event_count,
        "date_from": date_from,
        "date_to": date_to,
    }


# ---------------------------------------------------------------------------
# Story 25.8 — catalog_driven execution: pull_catalog_daily.
#
# Principle (Jean): the catalog IS the execution contract — any cataloged field a
# datastream selects must actually be extracted. core validates the selection
# against api_catalog.json and passes selection={"metrics","dimensions",
# "source_fields": {...}} into this callable; here we translate that generic
# selection into the concrete Meta Insights request (fields param + breakdowns
# param), chunk the fields param, merge chunk rows on the grain key, parse
# action-family selections from the base arrays, and land in the SAME wide
# raw_meta_ads_daily via _insert_raw_rows (landing shape unchanged — the dbt
# marts UNPIVOT it to the long fact_daily_kpi downstream).
# ---------------------------------------------------------------------------

# Structure/grain fields ALWAYS present in the fields param regardless of the
# selection, so every chunk row carries the same merge key + landing columns.
# (These are the wide raw table's identity columns; source_field == field_id.)
_CATALOG_STRUCTURE_FIELDS = (
    "date_start",
    "campaign_id",
    "campaign_name",
    "adset_id",
    "adset_name",
    "ad_id",
    "account_currency",
)

# Meta's practical ceiling on the comma-joined fields param. We chunk WELL under
# it (batch ~90) and merge the per-chunk rows back on the grain key so a very
# wide selection never trips the provider's field-count limit.
_CATALOG_FIELDS_BATCH = 90

# The AdsActionStats families whose catalog ids are '<family>_<action_type>'
# expansions. A selected expansion id maps back to its base array field +
# an action_type filter parsed at parse time (mirrors _extract_conversions).
_ACTION_FAMILIES = (
    "actions",
    "action_values",
    "cost_per_action_type",
    "cost_per_conversion",
    "conversions",
    "conversion_values",
)


def _load_catalog_sources() -> dict:
    """Load and return catalog_sources/catalog_sources.json (cached per process)."""
    global _CATALOG_SOURCES
    try:
        return _CATALOG_SOURCES  # type: ignore[name-defined]
    except NameError:
        pass
    path = Path(__file__).parent / "catalog_sources" / "catalog_sources.json"
    globals()["_CATALOG_SOURCES"] = json.loads(path.read_text(encoding="utf-8"))
    return globals()["_CATALOG_SOURCES"]


def _load_api_catalog() -> dict:
    """Load and return api_catalog.json (cached per process)."""
    try:
        return _API_CATALOG  # type: ignore[name-defined]
    except NameError:
        pass
    path = Path(__file__).parent / "api_catalog.json"
    globals()["_API_CATALOG"] = json.loads(path.read_text(encoding="utf-8"))
    return globals()["_API_CATALOG"]


def _catalog_action_type_map() -> dict[str, str]:
    """Return ``{field_id: dotted_action_type}`` for action-family expansions.

    The DOTTED action_type is not recoverable from the underscore-normalised
    field_id (the normalisation is lossy), so we read it from the catalog field
    description which carries it verbatim: ``... for action_type '<dotted>'.``.
    The catalog is the source of truth (AD-2). Cached per process.
    """
    try:
        return _ACTION_TYPE_MAP  # type: ignore[name-defined]
    except NameError:
        pass
    import re  # noqa: PLC0415

    pattern = re.compile(r"action_type '([^']+)'")
    mapping: dict[str, str] = {}
    for field in _load_api_catalog().get("fields", []):
        match = pattern.search(field.get("description", ""))
        if match:
            mapping[field["field_id"]] = match.group(1)
    globals()["_ACTION_TYPE_MAP"] = mapping
    return mapping


def _split_action_family(field_id: str) -> tuple[str, str] | None:
    """If *field_id* is an action-family expansion, return (base_family, action_type).

    e.g. 'actions_offsite_conversion_fb_pixel_purchase'
         -> ('actions', 'offsite_conversion.fb_pixel_purchase').
    The DOTTED action_type is read from the catalog description (the id's
    underscore normalisation is lossy). Bare family fields (id == family, e.g.
    'actions') return None (no filter). A prefixed id whose action_type cannot be
    recovered from the catalog falls back to the naive de-normalisation so the
    request still carries the base array.
    """
    for family in _ACTION_FAMILIES:
        if field_id == family:
            return None
        if field_id.startswith(family + "_"):
            action_type = _catalog_action_type_map().get(field_id)
            if action_type is None:
                action_type = field_id[len(family) + 1:].replace("_", ".")
            return family, action_type
    return None


def _build_catalog_request(
    selection: dict,
) -> tuple[list[str], list[str], dict[str, tuple[str, str]]]:
    """Translate a validated *selection* into the Meta Insights request shape.

    Returns ``(fields, breakdowns, action_family_map)``:
      - ``fields``: the source field tokens for the Insights ``fields`` param
        (structure fields always included; scalar metrics/dims; the BASE array
        for any selected action-family expansion, de-duplicated).
      - ``breakdowns``: the source tokens for the ``breakdowns`` param (only the
        selection's dimensions that are declared breakdowns in catalog_sources).
      - ``action_family_map``: ``{selected_field_id: (base_family, action_type)}``
        so the parser can read the right row out of the base array.

    Raises core.pull_errors.InvalidRequestError when the chosen breakdowns are an
    incompatible combination (validated BEFORE the API call — the catalog's
    breakdown_compatibility block is the source of truth).
    """
    from core.pull_errors import InvalidRequestError  # noqa: PLC0415 -- AD-2

    compat = _load_catalog_sources().get("breakdown_compatibility", {})
    breakdown_ids = set(compat.get("breakdown_fields", []))
    incompatible_pairs = {
        frozenset(pair) for pair in compat.get("incompatible_pairs", [])
    }
    max_breakdowns = compat.get("max_breakdowns", 4)

    source_fields: dict[str, str] = selection.get("source_fields", {})
    metrics: list[str] = list(selection.get("metrics", []))
    dimensions: list[str] = list(selection.get("dimensions", []))

    fields: list[str] = list(_CATALOG_STRUCTURE_FIELDS)
    breakdowns: list[str] = []
    action_family_map: dict[str, tuple[str, str]] = {}

    def _add_field(token: str) -> None:
        if token and token not in fields:
            fields.append(token)

    # Metrics -> fields param (action-family expansions collapse to the base array).
    for field_id in metrics:
        parsed = _split_action_family(field_id)
        if parsed is not None:
            base_family, action_type = parsed
            action_family_map[field_id] = (base_family, action_type)
            _add_field(base_family)
        else:
            _add_field(source_fields.get(field_id, field_id))

    # Dimensions -> either a breakdown (breakdowns param) or a fields-param column.
    selected_breakdowns: list[str] = []
    for field_id in dimensions:
        if field_id in breakdown_ids:
            token = source_fields.get(field_id, field_id)
            if token not in breakdowns:
                breakdowns.append(token)
                selected_breakdowns.append(field_id)
        else:
            _add_field(source_fields.get(field_id, field_id))

    # Breakdown compatibility validated BEFORE any API call (invalid_request-class).
    if len(selected_breakdowns) > max_breakdowns:
        raise InvalidRequestError(
            provider_status=None,
            provider_payload=None,
            message=(
                f"selection requests {len(selected_breakdowns)} breakdowns but Meta "
                f"Insights allows at most {max_breakdowns} per request: "
                f"{', '.join(sorted(selected_breakdowns))}"
            ),
        )
    for i in range(len(selected_breakdowns)):
        for j in range(i + 1, len(selected_breakdowns)):
            pair = frozenset({selected_breakdowns[i], selected_breakdowns[j]})
            if pair in incompatible_pairs:
                a, b = sorted(pair)
                raise InvalidRequestError(
                    provider_status=None,
                    provider_payload=None,
                    message=(
                        f"breakdowns {a!r} and {b!r} cannot be combined in one Meta "
                        "Insights request (breakdown_compatibility)"
                    ),
                )

    return fields, breakdowns, action_family_map


def _catalog_row_key(api_row: dict, breakdowns: list[str]) -> tuple:
    """Merge key for chunk rows: grain + breakdown values (deterministic order)."""
    key = (
        api_row.get("date_start", api_row.get("date", "")),
        api_row.get("campaign_id", ""),
        api_row.get("adset_id", ""),
        api_row.get("ad_id", ""),
    )
    return key + tuple(api_row.get(bd, "") for bd in sorted(breakdowns))


def _parse_action_value(api_row: dict, base_family: str, action_type: str) -> float:
    """Sum the value of *action_type* out of the base array *base_family*."""
    total = 0.0
    for entry in api_row.get(base_family, []) or []:
        if entry.get("action_type") == action_type:
            try:
                total += float(entry.get("value", 0))
            except (ValueError, TypeError):
                continue
    return total


def _parse_catalog_row(
    api_row: dict,
    breakdowns: list[str],
    action_family_map: dict[str, tuple[str, str]],
) -> dict:
    """Map one merged Insights row to the canonical wide raw record (catalog grain).

    Reuses _parse_insight_row for the structure + core-scalar columns so the wide
    raw_meta_ads_daily landing is byte-identical to campaign_daily for the shared
    columns. Selected action-family expansions are parsed from the base arrays;
    the wide table lands their sum into conversions (the schema-covered slot),
    matching _extract_conversions' contract. data_level is CAMPAIGN (the account
    grain of the catalog profile).
    """
    row = _parse_insight_row(api_row, profile="campaign_daily")
    # Action-family selections: fold pixel-purchase style conversions into the
    # conversions column (landing shape unchanged); other families are requested
    # and parsed here so the value is available, and land via the same slot.
    extra_conversions = 0.0
    for _field_id, (base_family, action_type) in action_family_map.items():
        if base_family in ("actions", "conversions"):
            extra_conversions += _parse_action_value(api_row, base_family, action_type)
    if extra_conversions and not api_row.get("conversions"):
        row["conversions"] = int(extra_conversions)
    return row


def pull_catalog_daily(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    selection: dict | None = None,
    ad_account_id: str | None = None,
) -> dict:
    """Story 25.8 reference: catalog_driven daily pull for Meta Ads.

    core resolves+validates ``selection`` against api_catalog.json and passes it
    in as ``{"metrics","dimensions","source_fields"}``. A ``None`` selection (job
    without an explicit selection) falls back to the catalog tier-core default,
    resolved here so the callable is safe to invoke standalone in tests too.

    The pull:
      1. builds the Insights ``fields`` param from the selection's source_fields
         (structure fields always included), and the ``breakdowns`` param from
         the selection's declared-breakdown dimensions;
      2. validates breakdown compatibility BEFORE any API call (incompatible pair
         or >max -> InvalidRequestError, the invalid_request drift signal);
      3. chunks the fields param (~90 fields) and merges per-chunk rows on the
         grain+breakdown key so a very wide selection never trips Meta's limit;
      4. parses action-family selections from the base arrays at parse time;
      5. lands rows via the existing _insert_raw_rows (wide raw_meta_ads_daily —
         landing shape unchanged; dbt UNPIVOTs to the long fact_daily_kpi).

    # AD-3: token used immediately then discarded. AD-7: pull_id passed in.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    # Default selection resolved FROM the catalog (tier-core) when absent, so the
    # callable is honest standalone (core normally passes a validated selection).
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

    if ad_account_id is None:
        ad_account_id = os.environ.get("META_ADS_AD_ACCOUNT_ID")
    if not ad_account_id:
        raise ValueError(
            "META_ADS_AD_ACCOUNT_ID env var required for pull "
            "(set it to your Meta ad account id digits, without the 'act_' prefix)"
        )
    account_digits = ad_account_id[4:] if ad_account_id.startswith("act_") else ad_account_id

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    # Breakdown compatibility validated here, BEFORE the token is even fetched.
    fields, breakdowns, action_family_map = _build_catalog_request(selection)

    token = nango_client.get_fresh_token(connection_id, provider="meta-ads")
    url = f"{META_GRAPH_API_BASE}/act_{account_digits}/insights"

    # Chunk the fields param; structure fields repeat in every chunk so rows merge.
    variable_fields = [f for f in fields if f not in _CATALOG_STRUCTURE_FIELDS]
    chunks: list[list[str]] = []
    if not variable_fields:
        chunks.append(list(_CATALOG_STRUCTURE_FIELDS))
    else:
        struct = list(_CATALOG_STRUCTURE_FIELDS)
        budget = max(1, _CATALOG_FIELDS_BATCH - len(struct))
        for start in range(0, len(variable_fields), budget):
            chunks.append(struct + variable_fields[start:start + budget])

    merged: dict[tuple, dict] = {}
    for chunk_fields in chunks:
        params = {
            "fields": ",".join(chunk_fields),
            "level": "campaign",
            "time_increment": 1,
            "time_range": json.dumps({"since": date_from, "until": date_to}),
        }
        if breakdowns:
            params["breakdowns"] = ",".join(breakdowns)

        resp = httpx.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )

        if resp.status_code == 429:
            from core.quota import RateLimitError  # noqa: PLC0415

            retry_after_raw = resp.headers.get("Retry-After", "0")
            try:
                retry_after = int(retry_after_raw) or None
            except (ValueError, TypeError):
                retry_after = None
            raise RateLimitError("meta-ads", retry_after)

        if resp.status_code != 200:
            from core.pull_errors import classify_http_error  # noqa: PLC0415

            try:
                _body = resp.json()
            except Exception:
                _body = resp.text
            raise classify_http_error(resp.status_code, _body, _load_error_map())

        for api_row in resp.json().get("data") or []:
            key = _catalog_row_key(api_row, breakdowns)
            if key in merged:
                merged[key].update(api_row)
            else:
                merged[key] = dict(api_row)

    canonical_rows = [
        _parse_catalog_row(api_row, breakdowns, action_family_map)
        for api_row in merged.values()
    ]

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_raw_rows(
        canonical_rows, pull_id, loaded_at, project_id, db_mode, duckdb_path
    )

    logger.info(
        "meta_ads_pull_completed: pull_id=%s profile=catalog_daily row_count=%d "
        "fields=%d breakdowns=%d chunks=%d",
        pull_id, row_count, len(fields), len(breakdowns), len(chunks),
    )

    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
    }


# ---------------------------------------------------------------------------
# transform() — manifest-driven canonical field mapping (AC3)
# ---------------------------------------------------------------------------


def transform(raw_rows: list[dict]) -> list[dict]:
    """Map raw source fields to canonical names using manifest mappings.

    # AD-4: canonical field mapping driven by manifest — do not hardcode source names.
    Reads ``canonical_metric_mapping`` and ``canonical_dimension_mapping`` from
    ``manifest.json`` and renames fields accordingly. The key rename for Meta is
    ``spend -> cost``. Fields not present in either mapping are passed through
    unchanged (e.g. ``pull_id``, ``connector``, ``date``, ``impressions``).
    """
    _manifest_path = Path(__file__).parent / "manifest.json"
    _manifest = json.loads(_manifest_path.read_text(encoding="utf-8"))

    rename_map: dict[str, str] = {}
    rename_map.update(_manifest.get("canonical_metric_mapping", {}))
    rename_map.update(_manifest.get("canonical_dimension_mapping", {}))

    result: list[dict] = []
    for row in raw_rows:
        canonical_row: dict = {}
        for key, value in row.items():
            canonical_row[rename_map.get(key, key)] = value
        result.append(canonical_row)
    return result


# ---------------------------------------------------------------------------
# Story 25.5 stitch: account topology discovery (playbook section 5).
# ---------------------------------------------------------------------------

_MAX_ADACCOUNT_PAGES = 50


def discover_accounts(connection_id: str) -> list[dict]:
    """List the ad accounts the connection's token can reach.

    Story 25.5 stitch (meta-ads reference for the single-level topology).
    Calls Graph API /me/adaccounts (paged) and returns the generic hierarchy
    core's topology flow consumes -- one flat level (selection_level
    'ad_account'):

        [{"id": "act_123", "label": "Acme Ads"}, ...]

    The ``id`` strings are opaque to core (AD-2); pulls target
    act_<digits>/insights so the account id is stored verbatim ('act_...').

    Raises:
        core.quota.RateLimitError on 429 (breaker path, unchanged contract);
        a typed core.pull_errors.ConnectorError on any other non-2xx, refined
        by the manifest error_map (190 -> auth_expired, subcodes -> revoked).
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    token = nango_client.get_fresh_token(connection_id, provider="meta-ads")

    accounts: list[dict] = []
    url: str | None = f"{META_GRAPH_API_BASE}/me/adaccounts"
    params: dict | None = {"fields": "id,name,account_status", "limit": "100"}
    pages = 0

    while url is not None:
        if pages >= _MAX_ADACCOUNT_PAGES:
            logger.warning(
                "meta_ads_discover_accounts: page cap %d reached, stopping",
                _MAX_ADACCOUNT_PAGES,
            )
            break
        pages += 1

        resp = httpx.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )

        if resp.status_code == 429:
            from core.quota import RateLimitError  # noqa: PLC0415

            retry_after_raw = resp.headers.get("Retry-After", "0")
            try:
                retry_after = int(retry_after_raw) or None
            except (ValueError, TypeError):
                retry_after = None
            raise RateLimitError("meta-ads", retry_after)

        if resp.status_code != 200:
            from core.pull_errors import classify_http_error  # noqa: PLC0415

            try:
                _body = resp.json()
            except Exception:
                _body = resp.text
            raise classify_http_error(resp.status_code, _body, _load_error_map())

        payload = resp.json()
        for item in payload.get("data") or []:
            account_id = item.get("id")
            if not account_id:
                continue
            accounts.append(
                {"id": account_id, "label": item.get("name") or account_id}
            )

        # Graph paging: follow paging.next verbatim (it embeds the cursor);
        # params only apply to the first request.
        url = (payload.get("paging") or {}).get("next")
        params = None

    return accounts


# ---------------------------------------------------------------------------
# Story 3.5/3.6: register this module's raw table name with core.verification.
# module->core direction is allowed by AD-2 (only core->modules is forbidden).
# The raw table name never appears in core source code (AD-2).
# ---------------------------------------------------------------------------
try:
    from core.verification import register_raw_table_name as _register_raw  # noqa: PLC0415

    _register_raw("raw_meta_ads_daily", provider="meta-ads")  # review-3-6: per-provider
except Exception:
    pass  # best-effort; verification will log a warning if table name is missing
