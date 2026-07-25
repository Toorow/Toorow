"""Adjust connector — mobile measurement (Report Service API).

Exposes a ``mcp_app: FastMCP`` instance that the core loader mounts under the
``adjust`` namespace (AD-2). Built to the epic-25 industrial standard from day
one: generated api_catalog.json (Datascape metrics glossary + reports endpoint
dimensions), status-keyed error_map (Adjust publishes no sub-codes), and a
declared single-level app topology (Filters Data discovery).

# AD-12: MCP server reads the fact_daily_kpi mart only — no raw_* tables, no CSV.
# AD-3: token from Nango immediately before use — never stored or logged.
# AD-7: pull_id minted by the core scheduler and passed into pull().
# AD-14: identity/project_id parameter scaffold.
# AD-4: all extracted Adjust metrics are additive at day grain (cost, installs,
#        clicks, impressions, sessions, revenue, ad_revenue, all_revenue) — no
#        aggregation_rule needed. Ratio metrics (ctr, *_rate) are NEVER stored
#        (dropped defensively in transform()).
# AI-03: ASCII-only stdout.
#
# API facts (VERIFIED 2026-07-21 against https://dev.adjust.com/en/api/rs-api/):
#   - GET https://automate.adjust.com/reports-service/report
#     params: dimensions= / metrics= (comma-joined), date_period=from:to,
#     app_token__in= (scope filter), Authorization: Bearer <api_token>.
#   - Metric values arrive as STRINGS inside rows[] (e.g. "64") and as numbers
#     in totals{} — _insert_raw_rows coerces per column type.
#   - Every row carries an "attr_dependency" object — dropped in transform().
#   - 204 = empty report (no rows), NOT an error.
#   - Rate limit: 50 req/s per source IP (burst 100) — 429 raises RateLimitError.
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
mcp_app = FastMCP("adjust")

# Base URL for the Adjust Report Service API (unversioned; snapshot pinned in
# catalog_sources.json api_version).
ADJUST_API_BASE = "https://automate.adjust.com/reports-service"

# ---------------------------------------------------------------------------
# Database connection helpers — env-var driven (no hardcoded paths).
#   TOOROW_DB_MODE     = "duckdb" (default) | "bigquery"
#   TOOROW_DUCKDB_PATH = path to local .duckdb file
# ---------------------------------------------------------------------------

_DEFAULT_DUCKDB_PATH = os.path.join(os.path.dirname(__file__), "seeds", "local.duckdb")


def _get_db_mode() -> str:
    return os.environ.get("TOOROW_DB_MODE", "duckdb")


def _get_duckdb_path() -> str:
    return os.environ.get("TOOROW_DUCKDB_PATH", _DEFAULT_DUCKDB_PATH)


# ---------------------------------------------------------------------------
# Manifest access (cached): error_map (taxonomy 25.2) + profile request fields.
# ---------------------------------------------------------------------------

_MANIFEST: dict | None = None


def _load_manifest() -> dict:
    global _MANIFEST
    if _MANIFEST is None:
        _MANIFEST = json.loads(
            (Path(__file__).parent / "manifest.json").read_text(encoding="utf-8")
        )
    return _MANIFEST


def _load_error_map() -> dict:
    """Return the manifest's ``error_map``, cached.

    Adjust documents HTTP statuses only (no provider sub-codes), so the map is
    documentation of that fact; classification falls through to the pure-HTTP
    rules in core.pull_errors (401/403/400/5xx).
    """
    return _load_manifest().get("error_map") or {}


def _profile_request_fields(profile_id: str) -> tuple[list[str], list[str]]:
    """Return (dimension_tokens, metric_tokens) for the provider request.

    AD-2: the request field lists are DERIVED from the manifest capability
    report (field_id -> source_field), never hardcoded here.
    """
    manifest = _load_manifest()
    caps = manifest.get("source_capabilities", {})
    source_by_id = {
        f["field_id"]: f.get("source_field", f["field_id"])
        for f in caps.get("fields", [])
    }
    report = next(
        (r for r in caps.get("reports", []) if r.get("id") == profile_id), None
    )
    if report is None:
        raise ValueError(f"Unknown adjust report profile: {profile_id!r}")
    dimensions = [source_by_id.get(d, d) for d in report.get("dimensions", [])]
    metrics = [source_by_id.get(m, m) for m in report.get("metrics", [])]
    return dimensions, metrics


# ---------------------------------------------------------------------------
# Raw landing (wide, canonical column names — dbt UNPIVOTs to the long fact).
# ---------------------------------------------------------------------------

_RAW_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_adjust_daily (
    date                  VARCHAR,
    app_token             VARCHAR,
    app                   VARCHAR,
    network               VARCHAR,
    campaign_id           VARCHAR,
    campaign_name         VARCHAR,
    cost                  DOUBLE,
    installs              INTEGER,
    clicks                INTEGER,
    impressions           INTEGER,
    sessions              INTEGER,
    revenue               DOUBLE,
    ad_revenue            DOUBLE,
    all_revenue           DOUBLE,
    pull_id               VARCHAR,
    loaded_at             VARCHAR,
    project_id            VARCHAR,
    cost_source_currency  VARCHAR
)
"""

_RAW_INSERT_SQL = """
INSERT INTO raw_adjust_daily
    (date, app_token, app, network, campaign_id, campaign_name,
     cost, installs, clicks, impressions, sessions, revenue, ad_revenue,
     all_revenue, pull_id, loaded_at, project_id, cost_source_currency)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _to_float(value) -> float | None:
    """Coerce an Adjust metric value (string or number) to float; None stays None.

    NULL honnete (AD-9): an absent metric stays NULL — it is never zero-filled,
    so 'missing data' remains distinguishable from 'recorded zero'.
    """
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _to_int(value) -> int | None:
    f = _to_float(value)
    return None if f is None else int(f)


def _insert_raw_rows(
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    project_id: str,
    db_mode: str,
    duckdb_path: str,
) -> int:
    """Insert canonical (post-transform) rows into raw_adjust_daily (DuckDB)."""
    if db_mode != "duckdb":
        raise ValueError(
            f"_insert_raw_rows: unsupported db_mode {db_mode!r} at P-dev "
            "(BigQuery path not yet implemented)"
        )
    from core import warehouse_write  # noqa: PLC0415

    con = warehouse_write.open_raw_writer(duckdb_path, project_id=project_id)
    con.execute(_RAW_CREATE_DDL)
    values = [
        (
            r.get("date", ""),
            r.get("app_token", ""),
            r.get("app", ""),
            r.get("network", ""),
            r.get("campaign_id", ""),
            r.get("campaign_name", ""),
            _to_float(r.get("cost")),
            _to_int(r.get("installs")),
            _to_int(r.get("clicks")),
            _to_int(r.get("impressions")),
            _to_int(r.get("sessions")),
            _to_float(r.get("revenue")),
            _to_float(r.get("ad_revenue")),
            _to_float(r.get("all_revenue")),
            pull_id,
            loaded_at,
            project_id,
            r.get("cost_source_currency", ""),
        )
        for r in rows
    ]
    if values:
        con.executemany(_RAW_INSERT_SQL, values)
    con.close()
    return len(values)


# ---------------------------------------------------------------------------
# transform() — manifest-driven canonical field mapping (AD-2)
# ---------------------------------------------------------------------------

# Ratio metrics are NEVER stored (playbook / AD-4): computed at the semantic
# layer from stored numerators and denominators. attr_dependency is the RS API
# per-row attribution metadata object — not a field.
_DROP_FIELDS: set[str] = {
    "attr_dependency",
    "ctr",
    "click_conversion_rate",
    "impression_conversion_rate",
}


def transform(raw_rows: list[dict]) -> list[dict]:
    """Map raw Report Service fields to canonical names using manifest mappings.

    AD-2: renames driven by canonical_metric_mapping + canonical_dimension_mapping
    (e.g. day -> date, campaign_id_network -> campaign_id, currency_code ->
    cost_source_currency). Fields absent from both mappings pass through
    unchanged (pull_id, connector, ...). Ratio fields and attr_dependency are
    dropped, never stored.
    """
    manifest = _load_manifest()

    rename_map: dict[str, str] = {}
    for src, val in manifest.get("canonical_metric_mapping", {}).items():
        if isinstance(val, str):
            rename_map[src] = val
        elif isinstance(val, dict):
            rename_map[src] = val.get("canonical", src)
    rename_map.update(manifest.get("canonical_dimension_mapping", {}))

    result: list[dict] = []
    for row in raw_rows:
        canonical: dict = {}
        for key, value in row.items():
            if key in _DROP_FIELDS:
                continue
            canonical[rename_map.get(key, key)] = value
        result.append(canonical)
    return result


# ---------------------------------------------------------------------------
# pull — Report Service API (called by the queue worker only, AD-12)
# ---------------------------------------------------------------------------


def _pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    profile: str,
    app_token: str | None = None,
) -> dict:
    """Fetch an Adjust Report Service report and land rows in raw_adjust_daily.

    # AD-3: the token is used immediately as a Bearer header then discarded —
    # it is NEVER stored, logged, or passed further.
    # AD-7: pull_id is minted by the caller; this function never mints its own.

    Parameters
    ----------
    connection_id:
        Nango connection identifier passed to get_fresh_token.
    date_from, date_to:
        ISO-8601 date strings (YYYY-MM-DD) for the date_period window.
    project_id:
        Project binding for the raw rows.
    pull_id:
        Core-minted pull ULID (AD-7).
    profile:
        The report profile id (network_daily).
    app_token:
        Selected reporting app (topology selection_level 'app'). When provided,
        the request is narrowed via app_token__in. When None the pull stays
        scoped to the token's own account (all its apps — every row carries
        app_token as a dimension). NO env-var fallback exists (25.5+ standard).

    Returns
    -------
    dict
        {"pull_id": str, "row_count": int, "date_from": str, "date_to": str}

    Raises
    ------
    RateLimitError
        On HTTP 429 (breaker path).
    core.pull_errors.ConnectorError
        Typed error on any other non-2xx response (401 -> auth_expired,
        403 -> permission_denied, 400 -> invalid_request, 5xx -> transient).
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    dimensions, metrics = _profile_request_fields(profile)

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    # AD-3: token obtained immediately before use; falls out of scope after.
    token = nango_client.get_fresh_token(connection_id, provider="adjust")

    params: dict[str, str] = {
        "dimensions": ",".join(dimensions),
        "metrics": ",".join(metrics),
        "date_period": f"{date_from}:{date_to}",
    }
    if app_token:
        params["app_token__in"] = app_token

    resp = httpx.get(
        f"{ADJUST_API_BASE}/report",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60.0,
    )

    if resp.status_code == 429:
        from core.quota import RateLimitError  # noqa: PLC0415

        retry_after_raw = resp.headers.get("Retry-After", "0")
        try:
            retry_after = int(retry_after_raw) or None
        except (ValueError, TypeError):
            retry_after = None
        raise RateLimitError("adjust", retry_after)

    if resp.status_code == 204:
        # Documented empty report — an honest zero-row pull, not an error.
        logger.info(
            "adjust_pull_completed: pull_id=%s profile=%s row_count=0 (204 empty)",
            pull_id, profile,
        )
        return {
            "pull_id": pull_id,
            "row_count": 0,
            "date_from": date_from,
            "date_to": date_to,
        }

    if resp.status_code != 200:
        # Story 25.2 taxonomy: NEVER a generic RuntimeError. Provider payload
        # preserved as evidence; the manifest error_map documents the (status-
        # only) provider contract.
        from core.pull_errors import classify_http_error  # noqa: PLC0415

        try:
            _body = resp.json()
        except Exception:
            _body = resp.text
        raise classify_http_error(resp.status_code, _body, _load_error_map())

    api_rows = resp.json().get("rows") or []

    canonical_rows = transform(api_rows)

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_raw_rows(
        canonical_rows, pull_id, loaded_at, project_id, db_mode, duckdb_path
    )

    # AD-3: no token in log — only pull_id and row_count (safe metadata).
    logger.info(
        "adjust_pull_completed: pull_id=%s profile=%s row_count=%d",
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
    app_token: str | None = None,
) -> dict:
    """Default pull() = network_daily (AI-58 profile-less default dispatch)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="network_daily", app_token=app_token,
    )


def pull_network_daily(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    app_token: str | None = None,
) -> dict:
    """AI-58 dispatch: network/campaign grain (declared dispatch.callable)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id,
        profile="network_daily", app_token=app_token,
    )


# ---------------------------------------------------------------------------
# Account topology discovery (playbook section 5).
# ---------------------------------------------------------------------------


def discover_accounts(connection_id: str) -> list[dict]:
    """List the apps the connection's API token can reach.

    Single-level topology (selection_level 'app'). Calls the official Filters
    Data endpoint (GET /reports-service/filters_data?required_filters=apps)
    and returns the generic hierarchy core's topology flow consumes:

        [{"id": "<app_token>", "label": "<app name>"}, ...]

    The ``id`` strings are opaque to core (AD-2); pulls narrow via
    app_token__in=<id>.

    Raises:
        core.quota.RateLimitError on 429 (breaker path);
        a typed core.pull_errors.ConnectorError on any other non-2xx.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    token = nango_client.get_fresh_token(connection_id, provider="adjust")

    resp = httpx.get(
        f"{ADJUST_API_BASE}/filters_data",
        params={"required_filters": "apps"},
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
        raise RateLimitError("adjust", retry_after)

    if resp.status_code != 200:
        from core.pull_errors import classify_http_error  # noqa: PLC0415

        try:
            _body = resp.json()
        except Exception:
            _body = resp.text
        raise classify_http_error(resp.status_code, _body, _load_error_map())

    accounts: list[dict] = []
    for item in resp.json().get("apps") or []:
        app_id = item.get("id")
        if not app_id:
            continue
        accounts.append({"id": app_id, "label": item.get("name") or app_id})
    return accounts


# ---------------------------------------------------------------------------
# MCP tool — reads from fact_daily_kpi mart (AD-12).
# ---------------------------------------------------------------------------


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
    """Fully-qualified mart table reference per engine (F-02)."""
    if db_mode == "duckdb":
        from core import warehouse_tenancy  # noqa: PLC0415

        return f"{warehouse_tenancy.mart_prefix(None)}fact_daily_kpi"
    dataset = os.environ.get("BQ_MARTS_DATASET", "marts")
    gcp_project = os.environ.get("GCP_PROJECT", "")
    prefix = f"{gcp_project}.{dataset}" if gcp_project else dataset
    return f"{prefix}.fact_daily_kpi"


# SQL body shared by both engines; user-supplied values NEVER enter the string.
_MART_QUERY = """
    SELECT
        metric,
        breakdown_dimension,
        breakdown_value,
        SUM(value) AS value,
        MAX(pull_id) AS pull_id,
        MAX(loaded_at) AS freshness
    FROM {table}
    WHERE connector = 'adjust'
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
            "source_system": "adjust",
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
def get_adjust_report(
    project_id: str = "default",  # AD-14: identity resolved from OAuth 2.1 + PKCE
    report_profile: str = "network_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Adjust Mobile Measurement Report — reads from fact_daily_kpi mart.

    Report profile: network_daily (day x app x network x campaign grain)
    Metrics: cost, installs, clicks, impressions, sessions, revenue,
    ad_revenue, all_revenue (all additive at day grain, AD-4)
    Breakdown dimensions: campaign_id, network, app_token

    Returns the canonical AD-1 envelope via structuredContent.
    Text channel (this docstring) is the lean LLM summary (<=30 lines).

    # AD-4: adjust 'revenue' is app-tracked revenue — NEVER cross-summed with
    # Shopify/Stripe revenue (metric_source_priority: shopify 1, stripe 2,
    # adjust 3; cross_source_revenue picks ONE winner per project/day).

    Parameters:
        project_id: Project identifier (default: 'default', AD-14 placeholder)
        report_profile: 'network_daily'
        date_from: Start date ISO-8601 (e.g. '2026-04-01'). Defaults to 90 days ago.
        date_to: End date ISO-8601 (e.g. '2026-06-30'). Defaults to yesterday.
    """
    # AD-12: MCP server reads fact_daily_kpi mart only — never raw_* tables.
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
# Register this module's raw table name with core.verification.
# module->core direction is allowed by AD-2 (only core->modules is forbidden).
# ---------------------------------------------------------------------------
try:
    from core.verification import register_raw_table_name as _register_raw  # noqa: PLC0415

    _register_raw("raw_adjust_daily", provider="adjust")
except Exception:
    pass  # best-effort; verification will log a warning if table name is missing
