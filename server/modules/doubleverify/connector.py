"""DoubleVerify connector.

Media-quality / verification measurement: viewability, fraud/SIVT, brand
suitability, authentic, geo. Exposes ``mcp_app: FastMCP`` mounted by the core
loader under the ``doubleverify`` namespace (AD-2).

# AD-12: the MCP server reads ONLY the fact_daily_kpi mart -- never raw_* tables.
# AD-3: the token (DV Access Token Hash) comes from Nango immediately before use,
#        sent as a Bearer header, then discarded -- never stored or logged.
# AD-7: pull_id is minted by the core scheduler and passed into pull().
# AD-14: identity/project_id parameter scaffold.
# AD-4: every DV *_rate metric is a NON-ADDITIVE ratio. It is DROPPED by
#        transform() and NEVER stored raw -- the rate is recomputed at the
#        semantic layer from its numerator/denominator counts (e.g.
#        viewable_rate = viewable_impressions / measured_impressions).
# AI-03: ASCII-only stdout in all print/log statements.

Decisions (from research; exact wire contract to confirm in the live pass, AI-13):
  * AUTH: DoubleVerify uses a static, long-lived "Access Token Hash" minted in
    the DV Pinnacle UI (no OAuth, no refresh). Sent as
    "Authorization: Bearer <hash>". The secret transits ONLY via Nango (AD-3).
  * REPORTING: asynchronous 3-step -- Data Request (POST, returns a request id)
    -> Poll Status (GET, until ready) -> Data Download (GET, Accept: text/csv,
    optional gzip). The request id is valid for 30 days after data is ready.
  * OUTPUT: CSV (not JSON). The download is parsed into wide rows keyed by the
    requested dimension + metric ids.
  * TOPOLOGY: no account/advertiser enumeration endpoint exists. The token is
    scoped to the "Reporting Programs" selected at creation; advertiser/campaign
    are report dimensions, not list endpoints. No discover_accounts (see the
    manifest _account_topology_note).
  * FRESHNESS: data refreshes at 06:00 CST; same-day data cannot be pulled
    (T-1 earliest, T-3 stable).

Sources: developer.doubleverify.com Report Data API (gated); Supermetrics
DoubleVerify field catalog + connection guide; Improvado; Adverity; Alli;
Salesforce/Datorama DoubleVerify connector.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Module-level FastMCP instance -- the public surface the loader mounts.
mcp_app = FastMCP("doubleverify")

# ---------------------------------------------------------------------------
# DoubleVerify Data API (async report: request -> poll -> download).
#
# The exact host/paths are behind the DV developer-portal login and are
# declared here from the documented operation names; they MUST be confirmed in
# the live-integration pass (AI-13). Overridable via env for the live pass so
# no redeploy is needed to correct the base URL.
# ---------------------------------------------------------------------------
_DEFAULT_API_BASE = "https://api.doubleverify.com/data/v1"


def _api_base() -> str:
    return os.environ.get("DOUBLEVERIFY_API_BASE", _DEFAULT_API_BASE).rstrip("/")


# Poll loop bounds (async report readiness). Conservative; no rate limit is
# published by DV. Overridable for the live pass.
_POLL_INTERVAL_S = float(os.environ.get("DOUBLEVERIFY_POLL_INTERVAL_S", "15"))
_POLL_MAX_ATTEMPTS = int(os.environ.get("DOUBLEVERIFY_POLL_MAX_ATTEMPTS", "40"))

# ---------------------------------------------------------------------------
# Database connection helpers -- env-var driven (same dual-backend pattern as
# gsc / klaviyo / meta-ads).
#   TOOROW_DB_MODE     = "duckdb" (default) | "bigquery"
#   TOOROW_DUCKDB_PATH = path to local .duckdb file
#   GCP_PROJECT        = GCP project ID (bigquery mode only)
# ---------------------------------------------------------------------------

_DEFAULT_DUCKDB_PATH = os.path.join(os.path.dirname(__file__), "seeds", "local.duckdb")


def _get_db_mode() -> str:
    return os.environ.get("TOOROW_DB_MODE", "duckdb")


def _get_duckdb_path() -> str:
    return os.environ.get("TOOROW_DUCKDB_PATH", _DEFAULT_DUCKDB_PATH)


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
    """Fully-qualified fact_daily_kpi reference per engine (AD-12)."""
    if db_mode == "duckdb":
        from core import warehouse_tenancy  # noqa: PLC0415

        return f"{warehouse_tenancy.mart_prefix(None)}fact_daily_kpi"
    dataset = os.environ.get("BQ_MARTS_DATASET", "marts")
    gcp_project = os.environ.get("GCP_PROJECT", "")
    prefix = f"{gcp_project}.{dataset}" if gcp_project else dataset
    return f"{prefix}.fact_daily_kpi"


# ---------------------------------------------------------------------------
# error_map (taxonomy 25.2): provider codes -> canonical classes, read from
# manifest.json and passed to classify_http_error.
# ---------------------------------------------------------------------------

_ERROR_MAP: dict | None = None


def _load_error_map() -> dict:
    global _ERROR_MAP
    if _ERROR_MAP is None:
        _manifest = json.loads(
            (Path(__file__).parent / "manifest.json").read_text(encoding="utf-8")
        )
        _ERROR_MAP = _manifest.get("error_map") or {}
    return _ERROR_MAP


# ---------------------------------------------------------------------------
# Canonical dimension set (grain columns). Everything the API returns that is
# NOT one of these is treated as a numeric metric during the wide->long melt.
# ---------------------------------------------------------------------------

_DIMENSION_FIELDS = (
    "date",
    "advertiser_name",
    "campaign",
    "media_property",
    "media_type",
    "delivery_country",
    "device_delivery_type",
)

# Most-specific-first breakdown priority for the long-format mart. fact_daily_kpi
# stores a single (breakdown_dimension, breakdown_value) pair per row.
_BREAKDOWN_PRIORITY = (
    "campaign",
    "media_property",
    "advertiser_name",
    "delivery_country",
    "device_delivery_type",
)

# ---------------------------------------------------------------------------
# Raw table DDL (long format: one row per (date, metric, breakdown)).
# ---------------------------------------------------------------------------

_RAW_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_doubleverify_daily (
    date                VARCHAR,
    metric              VARCHAR,
    value               DOUBLE,
    breakdown_dimension VARCHAR,
    breakdown_value     VARCHAR,
    pull_id             VARCHAR,
    loaded_at           VARCHAR,
    project_id          VARCHAR
)
"""

_RAW_INSERT_SQL = """
INSERT INTO raw_doubleverify_daily
    (date, metric, value, breakdown_dimension, breakdown_value,
     pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""


def _melt_wide_to_long(wide_rows: list[dict]) -> list[dict]:
    """Melt canonical wide rows into long (date, metric, value, breakdown) rows.

    Each wide row carries dimension columns (a subset of _DIMENSION_FIELDS) plus
    one column per numeric metric. The long form emits one row per metric, with
    the single most-specific present dimension as the breakdown pair
    (fact_daily_kpi stores exactly one breakdown dimension per row).

    AD-4: rate metrics have already been dropped by transform() before this runs.
    """
    long_rows: list[dict] = []
    for row in wide_rows:
        date_val = str(row.get("date", ""))
        breakdown_dim = ""
        breakdown_val = ""
        for dim in _BREAKDOWN_PRIORITY:
            if row.get(dim) not in (None, ""):
                breakdown_dim = dim
                breakdown_val = str(row[dim])
                break
        for key, value in row.items():
            if key in _DIMENSION_FIELDS:
                continue
            if value is None:
                continue  # AD-9: honest NULL -- absent metric is not zero.
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue  # non-numeric non-dimension column -- skip defensively.
            long_rows.append(
                {
                    "date": date_val,
                    "metric": key,
                    "value": numeric,
                    "breakdown_dimension": breakdown_dim,
                    "breakdown_value": breakdown_val,
                }
            )
    return long_rows


def _insert_raw_rows(long_rows: list[dict], pull_id: str, project_id: str) -> int:
    """Insert long-format rows into raw_doubleverify_daily (DuckDB at P3-dev)."""
    db_mode = _get_db_mode()
    if db_mode != "duckdb":
        raise ValueError(
            f"_insert_raw_rows: unsupported db_mode {db_mode!r} at P3-dev "
            "(BigQuery landing path not yet implemented)"
        )

    from core import warehouse_write  # noqa: PLC0415

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    con = warehouse_write.open_raw_writer(_get_duckdb_path(), project_id=project_id)
    con.execute(_RAW_CREATE_DDL)
    values = [
        (
            r.get("date", ""),
            r.get("metric", ""),
            float(r.get("value", 0.0) or 0.0),
            r.get("breakdown_dimension", ""),
            r.get("breakdown_value", ""),
            pull_id,
            loaded_at,
            project_id,
        )
        for r in long_rows
    ]
    if values:
        con.executemany(_RAW_INSERT_SQL, values)
    con.close()
    return len(values)


# ---------------------------------------------------------------------------
# Async DV report: Data Request -> Poll Status -> Data Download (CSV).
# ---------------------------------------------------------------------------


def _parse_csv(text: str) -> list[dict]:
    """Parse a DV Data Download CSV into a list of wide dict rows."""
    reader = csv.DictReader(io.StringIO(text))
    return [dict(row) for row in reader]


def pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    report_type: str = "standard",
    metrics: list[str] | None = None,
    dimensions: list[str] | None = None,
) -> dict:
    """Run a DoubleVerify async report and land rows into raw_doubleverify_daily.

    # AD-12: called by the queue worker only -- no synchronous API call during an
    #         LLM tool invocation.
    # AD-3: the token is used immediately as a Bearer header then discarded.
    # AD-7: pull_id is passed in by the caller; never minted here.

    Parameters
    ----------
    connection_id:
        Nango connection identifier passed to get_fresh_token.
    date_from, date_to:
        ISO-8601 date strings (YYYY-MM-DD). DV data is T-1 at the earliest.
    project_id:
        Project binding for the raw rows.
    pull_id:
        Core-minted pull ULID (AD-7).
    report_type:
        DV Report Request Type id (e.g. 'standard'). Confirm ids in the live pass.
    metrics, dimensions:
        Field ids (from api_catalog.json) sent in the Data Request. Default to a
        core viewability grain when omitted.

    Returns ``{"pull_id", "row_count", "date_from", "date_to"}``.

    Raises a typed core.pull_errors.ConnectorError on non-2xx (refined by the
    manifest error_map). Raises RateLimitError on HTTP 429.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2

    if metrics is None:
        metrics = ["monitored_ads", "measured_impressions", "viewable_impressions"]
    if dimensions is None:
        dimensions = ["date", "advertiser_name", "campaign"]

    # AD-3: token obtained immediately before use; falls out of scope after use.
    token = nango_client.get_fresh_token(connection_id, provider="doubleverify")
    headers = {"Authorization": f"Bearer {token}"}
    base = _api_base()

    # --- Step 1: Data Request (POST) -> request id ---
    request_body = {
        "type": report_type,
        "dimensions": dimensions,
        "metrics": metrics,
        "dateRange": {"start": date_from, "end": date_to},
    }
    resp = httpx.post(
        f"{base}/reports", json=request_body, headers=headers, timeout=30.0
    )
    _raise_for_status(resp, "doubleverify")
    request_id = (resp.json() or {}).get("id") or (resp.json() or {}).get("requestId")
    if not request_id:
        raise RuntimeError("doubleverify: Data Request returned no request id")

    # --- Step 2: Poll Status (GET) until ready ---
    for _ in range(_POLL_MAX_ATTEMPTS):
        status_resp = httpx.get(
            f"{base}/reports/{request_id}/status", headers=headers, timeout=30.0
        )
        _raise_for_status(status_resp, "doubleverify")
        state = str((status_resp.json() or {}).get("status", "")).lower()
        if state in ("ready", "complete", "completed", "success"):
            break
        if state in ("failed", "error"):
            from core.pull_errors import classify_http_error  # noqa: PLC0415

            raise classify_http_error(400, status_resp.json(), _load_error_map())
        time.sleep(_POLL_INTERVAL_S)
    else:
        raise RuntimeError(
            f"doubleverify: report {request_id} not ready after "
            f"{_POLL_MAX_ATTEMPTS} polls"
        )

    # --- Step 3: Data Download (GET, CSV) ---
    dl_resp = httpx.get(
        f"{base}/reports/{request_id}/data",
        headers={**headers, "Accept": "text/csv"},
        timeout=60.0,
    )
    _raise_for_status(dl_resp, "doubleverify")

    wide_rows = _parse_csv(dl_resp.text)
    canonical_rows = transform(wide_rows)
    long_rows = _melt_wide_to_long(canonical_rows)
    row_count = _insert_raw_rows(long_rows, pull_id, project_id)

    # AD-3: no token in log -- only safe metadata.
    logger.info(
        "doubleverify_pull_completed: pull_id=%s row_count=%d type=%s",
        pull_id, row_count, report_type,
    )

    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
    }


def _raise_for_status(resp: httpx.Response, provider: str) -> None:
    """Raise the canonical typed error on 429 / non-2xx (taxonomy 25.2)."""
    if resp.status_code == 429:
        from core.quota import RateLimitError  # noqa: PLC0415

        retry_after_raw = resp.headers.get("Retry-After", "0")
        try:
            retry_after = int(retry_after_raw) or None
        except (ValueError, TypeError):
            retry_after = None
        raise RateLimitError(provider, retry_after)

    if resp.status_code >= 300:
        from core.pull_errors import classify_http_error  # noqa: PLC0415

        try:
            _body = resp.json()
        except Exception:
            _body = resp.text
        raise classify_http_error(resp.status_code, _body, _load_error_map())


def pull_catalog_daily(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    metrics: list[str] | None = None,
    dimensions: list[str] | None = None,
) -> dict:
    """Catalog-driven dispatch (manifest catalog_daily.dispatch.callable).

    Any non-excluded catalog field id may be selected. Selection resolution and
    the exclusion of rate/attention sections are enforced by the catalog contract
    (core.catalog_contract) before this runs.
    """
    return pull(
        connection_id=connection_id,
        date_from=date_from,
        date_to=date_to,
        project_id=project_id,
        pull_id=pull_id,
        report_type="standard",
        metrics=metrics,
        dimensions=dimensions,
    )


# ---------------------------------------------------------------------------
# transform() -- manifest-driven canonical mapping (AD-2: no source names here).
# ---------------------------------------------------------------------------


def transform(raw_rows: list[dict]) -> list[dict]:
    """Map raw DoubleVerify fields to canonical names using manifest mappings.

    AD-2: renames driven by canonical_metric_mapping + canonical_dimension_mapping.
    AD-4: every non-additive ratio is DROPPED -- never stored. DV names every
    ratio '<...>_rate' (viewable_rate, fraud_sivt_rate, brand_suitable_rate, ...),
    plus a handful of explicit mean fields. These are recomputed at the semantic
    layer from their numerator/denominator counts and must not be summed.
    """
    _manifest = json.loads(
        (Path(__file__).parent / "manifest.json").read_text(encoding="utf-8")
    )

    rename_map: dict[str, str] = {}
    for src, val in _manifest.get("canonical_metric_mapping", {}).items():
        if isinstance(val, str):
            rename_map[src] = val
        elif isinstance(val, dict):
            rename_map[src] = val.get("canonical", src)
    rename_map.update(_manifest.get("canonical_dimension_mapping", {}))

    def _is_ratio(field_id: str) -> bool:
        # AD-4: any DV rate/ratio/mean metric is non-additive -> dropped.
        return (
            field_id.endswith("_rate")
            or field_id.startswith("rate_")
            or field_id.startswith("average_")
            or field_id.startswith("avg_")
        )

    result: list[dict] = []
    for row in raw_rows:
        canonical: dict = {}
        for key, value in row.items():
            if _is_ratio(key):
                continue  # AD-4: never store a ratio.
            canonical[rename_map.get(key, key)] = value
        result.append(canonical)
    return result


# ---------------------------------------------------------------------------
# MCP tool -- reads from fact_daily_kpi mart only (AD-12).
# ---------------------------------------------------------------------------

_MART_QUERY = """
    SELECT
        metric,
        breakdown_dimension,
        breakdown_value,
        SUM(value) AS value,
        MAX(pull_id) AS pull_id,
        MAX(loaded_at) AS freshness
    FROM {table}
    WHERE connector = 'doubleverify'
      AND project_id = {p_project}
      AND date BETWEEN {p_from} AND {p_to}
    GROUP BY metric, breakdown_dimension, breakdown_value
    ORDER BY metric, breakdown_dimension, breakdown_value
"""


def _query_mart(date_from: str, date_to: str, project_id: str) -> list[dict]:
    db_mode = _get_db_mode()
    table = _get_mart_table(db_mode)
    if db_mode == "duckdb":
        sql = _MART_QUERY.format(table=table, p_project="?", p_from="?", p_to="?")
        return _query_duckdb(sql, [project_id, date_from, date_to], _get_duckdb_path())
    if db_mode == "bigquery":
        sql = _MART_QUERY.format(
            table=table, p_project="@project_id", p_from="@date_from", p_to="@date_to"
        )
        return _query_bigquery(
            sql, {"project_id": project_id, "date_from": date_from, "date_to": date_to}
        )
    raise ValueError(f"Unknown TOOROW_DB_MODE: {db_mode!r}")


@mcp_app.tool()
def get_doubleverify_report(
    project_id: str = "default",
    report_profile: str = "viewability_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """DoubleVerify media-quality report -- reads from fact_daily_kpi mart.

    Report profiles: viewability_daily, fraud_sivt_daily, brand_suitability_daily,
                     catalog_daily.
    Metrics (additive counts, AD-4): monitored_ads, measured_impressions,
        viewable_impressions, eligible_impressions, video_viewable_impressions,
        fraud_sivt_incidents, fraud_sivt_free_ads, brand_suitability_incidents,
        brand_suitable_ads, authentic_ads, ...
    Rates (viewable_rate, fraud_sivt_rate, ...) are NON-ADDITIVE: never stored;
    recompute at the semantic layer from their numerator/denominator counts.

    Returns the canonical AD-1 envelope via structuredContent.

    Parameters:
        project_id: Project identifier (AD-14 placeholder).
        report_profile: Report profile id from the manifest.
        date_from: Start date ISO-8601. Defaults to 90 days ago.
        date_to: End date ISO-8601. Defaults to yesterday (DV data is T-1).
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
            "meta": {"freshness": None, "provenance": None,
                     "alerts": [{"level": "error", "message": str(exc)}]},
            "data": {"project_id": project_id, "report_profile": report_profile,
                     "date_from": date_from, "date_to": date_to, "metrics": {}},
        }

    pull_ids = {r["pull_id"] for r in rows if r.get("pull_id")}
    freshness_values = [r["freshness"] for r in rows if r.get("freshness")]
    latest_pull_id = max(pull_ids) if pull_ids else None
    latest_freshness = max(freshness_values) if freshness_values else None

    data_by_metric: dict[str, list[dict]] = {}
    for r in rows:
        data_by_metric.setdefault(r["metric"], []).append(
            {
                "breakdown_dimension": r["breakdown_dimension"],
                "breakdown_value": r["breakdown_value"],
                "value": r["value"],
            }
        )

    provenance = (
        {"source_system": "doubleverify", "source_field": "fact_daily_kpi",
         "pull_id": latest_pull_id}
        if latest_pull_id is not None
        else None
    )

    return {
        "schema_version": "1",
        "meta": {"freshness": latest_freshness, "provenance": provenance, "alerts": []},
        "data": {
            "project_id": project_id,
            "report_profile": report_profile,
            "date_from": date_from,
            "date_to": date_to,
            "metrics": data_by_metric,
        },
    }


# ---------------------------------------------------------------------------
# Story 6.2 pattern: register this module's raw table name with core.verification.
# module->core direction is allowed by AD-2 (only core->modules is forbidden).
# ---------------------------------------------------------------------------
try:
    from core.verification import register_raw_table_name as _register_raw  # noqa: PLC0415

    _register_raw("raw_doubleverify_daily", provider="doubleverify")
except Exception:
    pass  # best-effort; verification logs a warning if the table name is missing
