"""GA4 connector — Story 1.4.

Exposes a ``mcp_app: FastMCP`` instance that the core loader mounts under
the ``google-analytics`` namespace (AD-2).

# P0: reads fact_daily_kpi mart — local DuckDB or BigQuery per env config
# AD-12: MCP server reads marts only — no raw_* tables, no CSV
# NFR6: API result → BigQuery raw → semantic-layer read (at P0: CSV → raw → dbt mart → MCP)
# AD-14: identity/project_id parameter scaffold — OAuth 2.1 + PKCE in Story 2.3.
# AD-7: pull_id will be minted by core scheduler from Story 3.2
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

# Module-level FastMCP instance — this is the public surface the loader uses.
# The core does: mcp.mount(loaded.connector_module.mcp_app, namespace=loaded.name)
mcp_app = FastMCP("google-analytics")

# AD-4: canonical dimension vocabulary enforced at Story 4.1.
# country is passed through as-is (GA4 full name, e.g. "France") at P0.

# ---------------------------------------------------------------------------
# Database connection helpers — env-var driven (no hardcoded paths)
# ---------------------------------------------------------------------------
# TOOROW_DB_MODE    = "duckdb" (default) | "bigquery"
# TOOROW_DUCKDB_PATH = path to local .duckdb file
# GCP_PROJECT          = GCP project ID (bigquery mode only)
# ---------------------------------------------------------------------------

_DEFAULT_DUCKDB_PATH = os.path.join(
    os.path.dirname(__file__), "seeds", "local.duckdb"
)


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
    WHERE connector = 'google-analytics'
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
    # Derive provenance and freshness from the result set
    pull_ids = {r["pull_id"] for r in rows if r.get("pull_id")}
    freshness_values = [r["freshness"] for r in rows if r.get("freshness")]

    latest_pull_id = max(pull_ids) if pull_ids else None
    latest_freshness = max(freshness_values) if freshness_values else None

    # Group data by metric for the envelope
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

    # NFR8 (AC5 Story 2.7): provenance is a full dict, not a scalar pull_id string.
    provenance = (
        {
            "source_system": "google-analytics",
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
def get_ga4_report(
    project_id: str = "default",  # AD-14: identity resolved from OAuth 2.1 + PKCE in Story 2.3
    report_profile: str = "standard_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """GA4 Standard Daily Report — reads from fact_daily_kpi mart.

    Report profile: standard_daily
    Metrics: sessions, active_users, conversions (additive only, AD-4)
    Breakdown dimensions: device_category, country

    Returns the canonical AD-1 envelope via structuredContent.
    Text channel (this docstring) is the lean LLM summary (≤30 lines).

    Parameters:
        project_id: Project identifier (default: 'default', AD-14 placeholder)
        report_profile: Report type (currently only 'standard_daily')
        date_from: Start date ISO-8601 (e.g. '2026-04-01'). Defaults to 90 days ago.
        date_to: End date ISO-8601 (e.g. '2026-06-30'). Defaults to yesterday.
    """
    # P0: reads fact_daily_kpi mart — local DuckDB or BigQuery per env config
    # AD-12: MCP server reads marts only — never raw_* tables or CSV

    # Default date range: last 90 days
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
# GA4 Data API pull — Story 2.7 (AC1, AC2, HG-1, HG-2, HG-3, AD-12)
# ---------------------------------------------------------------------------

# Base URL for the GA4 Data API v1beta
GA4_DATA_API_BASE = "https://analyticsdata.googleapis.com/v1beta"

# Raw INSERT SQL for pull_ga4 (mirrors load_seed.py shape but includes project_id).
# Story 39.7: report_timezone is captured as per-row provenance (the GENERIC time-context
# capture contract, generalizing GAM's report_timezone). It records the exact IANA zone the
# GA4 property used to draw its reporting-day boundaries (time_context.locus='property').
# It is IMMUTABLE provenance (E39-AD2) -- never rewritten, never used to convert_timezone()
# at day grain (HG-4). The value is resolved via core.report_timezone.resolve_capture; when
# the property zone is undetermined it lands NULL (fail-closed -> a read-time TIMEZONE_GAP),
# never a silent 'UTC'. The live property-metadata read is a recorded follow-up (AI-13
# blocked -- no live GA4 property, no-connector-test-accounts); today the column is fed from
# the row/fixture (r.get("report_timezone")) so the staging carries it end-to-end.
_RAW_INSERT_SQL = """
INSERT INTO raw_ga4_standard_daily
    (date, device_category, country, sessions, active_users, conversions,
     pull_id, loaded_at, project_id, report_timezone)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_RAW_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_ga4_standard_daily (
    date            VARCHAR,
    device_category VARCHAR,
    country         VARCHAR,
    sessions        INTEGER,
    active_users    INTEGER,
    conversions     INTEGER,
    pull_id         VARCHAR,
    loaded_at       VARCHAR,
    project_id      VARCHAR,
    report_timezone VARCHAR
)
"""


def _insert_raw_rows(
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    project_id: str,
    db_mode: str,
    duckdb_path: str,
) -> int:
    """Insert canonical rows into raw_ga4_standard_daily.

    Private helper for pull_ga4 (T8.2 Story 2.7). Keeps the connector
    self-contained without importing from seeds/ (seeds/ is not a package).

    Supports duckdb mode only at P2; bigquery extension follows Epic 3.
    """
    if db_mode == "duckdb":
        from core import warehouse_write  # noqa: PLC0415

        con = warehouse_write.open_raw_writer(duckdb_path, project_id=project_id)
        con.execute(_RAW_CREATE_DDL)
        # Migration guard: add project_id column to existing tables created
        # before Story 2.7 (DuckDB 1.5.4 supports ADD COLUMN IF NOT EXISTS).
        con.execute(
            "ALTER TABLE raw_ga4_standard_daily ADD COLUMN IF NOT EXISTS "
            "project_id VARCHAR DEFAULT 'default'"
        )
        # Story 39.7 migration guard: additive report_timezone provenance column on
        # tables created before 39.7. Idempotent; NULL default (fail-closed -> TIMEZONE_GAP,
        # never a silent 'UTC'). E39-NFR06: additive column, no existing total moves.
        con.execute(
            "ALTER TABLE raw_ga4_standard_daily ADD COLUMN IF NOT EXISTS "
            "report_timezone VARCHAR"
        )
        # Story 39.7: resolve the per-row report timezone through the GENERIC time-context
        # capture contract (core owns validate/fallback/gap; the connector supplies its own
        # captured zone). GA4 declares locus='property'/fallback='gap'; the live property
        # zone read is an AI-13-blocked follow-up, so today captured_zone comes from the
        # row/fixture (r.get("report_timezone")). Undetermined => resolved None => NULL
        # (a read-time TIMEZONE_GAP), NEVER a silent default (E39-NFR02).
        from core import report_timezone as _rtz  # noqa: PLC0415

        _tz_declared = {"locus": "property", "fallback": "gap"}
        values = [
            (
                r["date"],
                r["device_category"],
                r["country"],
                int(r["sessions"]),
                int(r["active_users"]),
                int(r["conversions"]),
                pull_id,
                loaded_at,
                project_id,
                _rtz.resolve_capture(_tz_declared, r.get("report_timezone"))["report_timezone"],
            )
            for r in rows
        ]
        con.executemany(_RAW_INSERT_SQL, values)
        con.close()
        return len(values)
    else:
        raise ValueError(
            f"_insert_raw_rows: unsupported db_mode {db_mode!r} at P2 "
            "(BigQuery path not yet implemented; use load_seed.py for BQ)"
        )


# ---------------------------------------------------------------------------
# Epic 10, story 10.1: user_type_daily profile (GA4 newVsReturning dimension).
#
# Parallel, non-interfering with pull()/raw_ga4_standard_daily. This profile
# carries only (date, user_type) -- no device_category or country columns. The
# raw table is append-only (AD-7) and lands breakdown_dimension='user_type' in
# the mart WITHOUT altering the existing device_category/country totals (the
# 9.9 CRITICAL scenario -- reconciliation proven by
# test_ga4_user_type_reconciliation.py).
# ---------------------------------------------------------------------------

_RAW_INSERT_SQL_USER_TYPE = """
INSERT INTO raw_ga4_user_type_daily
    (date, user_type, sessions, active_users, conversions,
     pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

_RAW_CREATE_DDL_USER_TYPE = """
CREATE TABLE IF NOT EXISTS raw_ga4_user_type_daily (
    date         VARCHAR,
    user_type    VARCHAR,
    sessions     INTEGER,
    active_users INTEGER,
    conversions  INTEGER,
    pull_id      VARCHAR,
    loaded_at    VARCHAR,
    project_id   VARCHAR
)
"""


def _insert_raw_rows_user_type(
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    project_id: str,
    db_mode: str,
    duckdb_path: str,
) -> int:
    """Insert canonical rows into raw_ga4_user_type_daily (Story 10.1).

    Mirrors _insert_raw_rows() but targets the user_type profile table. Each row
    dict carries the canonical keys: date, user_type, sessions, active_users,
    conversions. Append-only (AD-7). DuckDB only at P2 (BigQuery path follows the
    same extension as the standard profile).
    """
    if db_mode == "duckdb":
        from core import warehouse_write  # noqa: PLC0415

        con = warehouse_write.open_raw_writer(duckdb_path, project_id=project_id)
        con.execute(_RAW_CREATE_DDL_USER_TYPE)
        # Migration guard (mirrors the standard-profile pattern): ensure project_id
        # exists on tables created before this column was added.
        con.execute(
            "ALTER TABLE raw_ga4_user_type_daily ADD COLUMN IF NOT EXISTS "
            "project_id VARCHAR DEFAULT 'default'"
        )
        values = [
            (
                r["date"],
                r["user_type"],
                int(r["sessions"]),
                int(r["active_users"]),
                int(r["conversions"]),
                pull_id,
                loaded_at,
                project_id,
            )
            for r in rows
        ]
        con.executemany(_RAW_INSERT_SQL_USER_TYPE, values)
        con.close()
        return len(values)
    else:
        raise ValueError(
            f"_insert_raw_rows_user_type: unsupported db_mode {db_mode!r} at P2 "
            "(BigQuery path not yet implemented; use load_seed_user_type.py for BQ)"
        )


# ---------------------------------------------------------------------------
# Epic 10, story 10.3: pages_daily profiles (GA4 landingPage + pagePath).
#
# DESIGN DECISION (Task 1): landingPage and pagePath are DISTINCT GA4 dimensions.
# A single runReport with BOTH would return the landingPage x pagePath CROSS-JOIN
# grain (cardinality explosion, not what we want). Entry pages = sessions by
# landingPage; most-viewed pages = screen_page_views by pagePath -- two INDEPENDENT
# marginal top-N lists with NO join needed (unlike GSC query>page in 10.5). So we
# ship TWO report profiles, each a single-dimension top-N runReport:
#   * pages_daily_landing -> raw_ga4_landing_daily (sessions by landing_page)
#   * pages_daily_paths   -> raw_ga4_paths_daily   (screen_page_views by page)
#
# TOP-N IS A BOUNDED PARTITION, NOT A FULL RECONCILIATION: unlike user_type (10.1),
# where the 3 buckets sum to the connector daily total, the page partitions are
# top-N bounded -- the long tail beyond page_top_n is intentionally DROPPED, so
# they do NOT sum to the day total. This is correct and documented on the mart
# block + not asserted as a full reconciliation (test_ga4_pages_reconciliation.py).
#
# Parallel, non-interfering with pull()/pull_user_type_daily() and their raw tables.
# Append-only (AD-7). Lands breakdown_dimension='landing_page' / ='page' in the mart
# WITHOUT altering the existing device_category/country/user_type totals.
# ---------------------------------------------------------------------------

# page_top_n bound: default 50, clamped to [10, 200] (mirrors the manifest
# extraction_capabilities row_limit + row_limit_min/max). Env override:
# GA4_PAGE_TOP_N. This is the LIMIT applied to the GA4 runReport (orderBys metric
# DESC + limit=page_top_n) -- the tail beyond N is dropped by design (documented).
_PAGE_TOP_N_DEFAULT = 50
_PAGE_TOP_N_MIN = 10
_PAGE_TOP_N_MAX = 200


def _get_page_top_n() -> int:
    """Resolve the top-N page bound (env GA4_PAGE_TOP_N, clamped to [10, 200])."""
    try:
        n = int(os.environ.get("GA4_PAGE_TOP_N", str(_PAGE_TOP_N_DEFAULT)))
    except (ValueError, TypeError):
        n = _PAGE_TOP_N_DEFAULT
    return max(_PAGE_TOP_N_MIN, min(_PAGE_TOP_N_MAX, n))


_RAW_INSERT_SQL_LANDING = """
INSERT INTO raw_ga4_landing_daily
    (date, landing_page, sessions, pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?)
"""

_RAW_CREATE_DDL_LANDING = """
CREATE TABLE IF NOT EXISTS raw_ga4_landing_daily (
    date         VARCHAR,
    landing_page VARCHAR,
    sessions     INTEGER,
    pull_id      VARCHAR,
    loaded_at    VARCHAR,
    project_id   VARCHAR
)
"""

_RAW_INSERT_SQL_PATHS = """
INSERT INTO raw_ga4_paths_daily
    (date, page, screen_page_views, pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?)
"""

_RAW_CREATE_DDL_PATHS = """
CREATE TABLE IF NOT EXISTS raw_ga4_paths_daily (
    date              VARCHAR,
    page              VARCHAR,
    screen_page_views INTEGER,
    pull_id           VARCHAR,
    loaded_at         VARCHAR,
    project_id        VARCHAR
)
"""


def _insert_raw_rows_landing(
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    project_id: str,
    db_mode: str,
    duckdb_path: str,
) -> int:
    """Insert canonical entry-page rows into raw_ga4_landing_daily (Story 10.3).

    Each row dict carries: date, landing_page, sessions. Append-only (AD-7).
    DuckDB only at P2 (mirrors the standard/user_type profiles).
    """
    if db_mode == "duckdb":
        from core import warehouse_write  # noqa: PLC0415

        con = warehouse_write.open_raw_writer(duckdb_path, project_id=project_id)
        con.execute(_RAW_CREATE_DDL_LANDING)
        con.execute(
            "ALTER TABLE raw_ga4_landing_daily ADD COLUMN IF NOT EXISTS "
            "project_id VARCHAR DEFAULT 'default'"
        )
        values = [
            (
                r["date"],
                r["landing_page"],
                int(r["sessions"]),
                pull_id,
                loaded_at,
                project_id,
            )
            for r in rows
        ]
        con.executemany(_RAW_INSERT_SQL_LANDING, values)
        con.close()
        return len(values)
    else:
        raise ValueError(
            f"_insert_raw_rows_landing: unsupported db_mode {db_mode!r} at P2 "
            "(BigQuery path not yet implemented; use load_seed_pages.py for BQ)"
        )


def _insert_raw_rows_paths(
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    project_id: str,
    db_mode: str,
    duckdb_path: str,
) -> int:
    """Insert canonical top-page rows into raw_ga4_paths_daily (Story 10.3).

    Each row dict carries: date, page, screen_page_views. Append-only (AD-7).
    DuckDB only at P2.
    """
    if db_mode == "duckdb":
        from core import warehouse_write  # noqa: PLC0415

        con = warehouse_write.open_raw_writer(duckdb_path, project_id=project_id)
        con.execute(_RAW_CREATE_DDL_PATHS)
        con.execute(
            "ALTER TABLE raw_ga4_paths_daily ADD COLUMN IF NOT EXISTS "
            "project_id VARCHAR DEFAULT 'default'"
        )
        values = [
            (
                r["date"],
                r["page"],
                int(r["screen_page_views"]),
                pull_id,
                loaded_at,
                project_id,
            )
            for r in rows
        ]
        con.executemany(_RAW_INSERT_SQL_PATHS, values)
        con.close()
        return len(values)
    else:
        raise ValueError(
            f"_insert_raw_rows_paths: unsupported db_mode {db_mode!r} at P2 "
            "(BigQuery path not yet implemented; use load_seed_pages.py for BQ)"
        )


def _ga4_date(date_raw: str) -> str:
    """GA4 YYYYMMDD -> YYYY-MM-DD (same slice pattern as pull())."""
    return (
        date_raw[:4] + "-" + date_raw[4:6] + "-" + date_raw[6:]
        if len(date_raw) == 8
        else date_raw
    )


def _ga4_runreport(
    property_id: str,
    token: str,
    request_body: dict,
) -> dict:
    """POST a GA4 runReport and return the parsed JSON payload.

    Shared helper for the page pulls. Raises RateLimitError on 429 and
    RuntimeError on any other non-200 (mirrors pull()/pull_user_type_daily()).

    # HG-2 (AD-3): token used immediately as a Bearer header, never stored/logged.
    """
    url = f"{GA4_DATA_API_BASE}/properties/{property_id}:runReport"
    resp = httpx.post(
        url,
        json=request_body,
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
        raise RateLimitError("google-analytics", retry_after)
    if resp.status_code != 200:
        # Story 25.2: canonical typed error; provider payload preserved.
        from core.pull_errors import classify_http_error  # noqa: PLC0415

        try:
            _body = resp.json()
        except Exception:
            _body = resp.text
        raise classify_http_error(resp.status_code, _body)
    return resp.json()


def pull_pages_daily_landing(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    property_id: str | None = None,
) -> dict:
    """Fetch entry pages (sessions by GA4 landingPage) top-N into the raw table.

    Story 10.3. Parallel to pull()/pull_user_type_daily() -- SAME signature. Hits the
    GA4 Data API with dimensions [date, landingPage], metric [sessions], orderBys
    sessions DESC + limit=page_top_n (the top-N bound). Lands rows in
    raw_ga4_landing_daily (NOT the standard/user_type tables). Existing pulls
    UNTOUCHED.

    TOP-N is a BOUNDED partition: the tail beyond page_top_n is DROPPED by design --
    the returned rows do NOT sum to the connector daily total (this is correct;
    documented on the mart, not asserted as reconciliation).

    GA4 mapping before insert:
      * date: YYYYMMDD -> YYYY-MM-DD.
      * landingPage '(not set)' -> 'unknown' (never silently dropped).

    Returns {"pull_id": str, "row_count": int, "date_from": str, "date_to": str,
             "page_top_n": int}.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    if property_id is None:
        property_id = os.environ.get("GA4_PROPERTY_ID")
    if not property_id:
        raise ValueError(
            "GA4_PROPERTY_ID env var required for pull_pages_daily_landing "
            "(set it to your GA4 property numeric ID)"
        )

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()
    page_top_n = _get_page_top_n()

    token = nango_client.get_fresh_token(connection_id, provider="google-analytics")

    request_body = {
        "dimensions": [
            {"name": "date"},
            {"name": "landingPage"},
        ],
        "metrics": [
            {"name": "sessions"},
        ],
        "dateRanges": [{"startDate": date_from, "endDate": date_to}],
        # Top-N bound: order by the metric DESC and cap the row count. The GA4
        # `limit` applies per request; combined with the DESC order this returns
        # the highest-traffic landing pages first and drops the long tail (Task 1).
        "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
        "limit": str(page_top_n),
    }

    payload = _ga4_runreport(property_id, token, request_body)
    raw_rows = payload.get("rows") or []

    canonical_rows: list[dict] = []
    for api_row in raw_rows:
        dim_values = [d["value"] for d in api_row.get("dimensionValues", [])]
        met_values = [m["value"] for m in api_row.get("metricValues", [])]
        date_str = _ga4_date(dim_values[0] if len(dim_values) > 0 else "")
        landing_raw = dim_values[1] if len(dim_values) > 1 else ""
        landing_page = "unknown" if landing_raw == "(not set)" else landing_raw
        canonical_rows.append(
            {
                "date": date_str,
                "landing_page": landing_page,
                "sessions": met_values[0] if len(met_values) > 0 else "0",
            }
        )

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_raw_rows_landing(
        canonical_rows, pull_id, loaded_at, project_id, db_mode, duckdb_path
    )

    # AD-3: no token in log. Log the top-N bound so the dropped tail is explicit.
    logger.info(
        "ga4_pull_landing_done: pull_id=%s row_count=%d page_top_n=%d",
        pull_id,
        row_count,
        page_top_n,
    )

    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
        "page_top_n": page_top_n,
    }


def pull_pages_daily_paths(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    property_id: str | None = None,
) -> dict:
    """Fetch top pages (screen_page_views by GA4 pagePath) top-N into the raw table.

    Story 10.3. Parallel to pull()/pull_user_type_daily() -- SAME signature. Hits the
    GA4 Data API with dimensions [date, pagePath], metric [screenPageViews], orderBys
    screenPageViews DESC + limit=page_top_n. Lands rows in raw_ga4_paths_daily.
    Existing pulls UNTOUCHED.

    AI-53 / AI-13 HONESTY: the `screenPageViews` metric name is DECLARED per the
    api-schema but NOT verified against a live GA4 property in this story (no account
    connected). The first live pass (AC 11) confirms the exact metric name; until
    then the profile ships but the metric is unverified (Phase B gate). Fallback if
    absent: sessions by pagePath (confirmed metric).

    TOP-N is a BOUNDED partition (tail beyond page_top_n dropped by design).

    GA4 mapping before insert:
      * date: YYYYMMDD -> YYYY-MM-DD.
      * pagePath '(not set)' -> 'unknown'.

    Returns {"pull_id": str, "row_count": int, "date_from": str, "date_to": str,
             "page_top_n": int}.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    if property_id is None:
        property_id = os.environ.get("GA4_PROPERTY_ID")
    if not property_id:
        raise ValueError(
            "GA4_PROPERTY_ID env var required for pull_pages_daily_paths "
            "(set it to your GA4 property numeric ID)"
        )

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()
    page_top_n = _get_page_top_n()

    token = nango_client.get_fresh_token(connection_id, provider="google-analytics")

    request_body = {
        "dimensions": [
            {"name": "date"},
            {"name": "pagePath"},
        ],
        "metrics": [
            # AI-13: exact metric name confirmed at first live pass; declared here.
            {"name": "screenPageViews"},
        ],
        "dateRanges": [{"startDate": date_from, "endDate": date_to}],
        "orderBys": [{"metric": {"metricName": "screenPageViews"}, "desc": True}],
        "limit": str(page_top_n),
    }

    payload = _ga4_runreport(property_id, token, request_body)
    raw_rows = payload.get("rows") or []

    canonical_rows: list[dict] = []
    for api_row in raw_rows:
        dim_values = [d["value"] for d in api_row.get("dimensionValues", [])]
        met_values = [m["value"] for m in api_row.get("metricValues", [])]
        date_str = _ga4_date(dim_values[0] if len(dim_values) > 0 else "")
        page_raw = dim_values[1] if len(dim_values) > 1 else ""
        page = "unknown" if page_raw == "(not set)" else page_raw
        canonical_rows.append(
            {
                "date": date_str,
                "page": page,
                "screen_page_views": met_values[0] if len(met_values) > 0 else "0",
            }
        )

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_raw_rows_paths(
        canonical_rows, pull_id, loaded_at, project_id, db_mode, duckdb_path
    )

    logger.info(
        "ga4_pull_paths_done: pull_id=%s row_count=%d page_top_n=%d",
        pull_id,
        row_count,
        page_top_n,
    )

    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
        "page_top_n": page_top_n,
    }


# ---------------------------------------------------------------------------
# Epic 16, story 16.1: acquisition profiles (GA4 last-click + first-click).
#
# DESIGN DECISION (Task 1): TWO report profiles, each a SEPARATE runReport, exactly
# like pages_daily_landing vs pages_daily_paths (10.3). The forbidden cross-join is
# session x first-user (combining sessionSourceMedium AND firstUserSourceMedium in a
# single runReport would explode cardinality and be semantic nonsense). So:
#   * acquisition_daily_session   -> raw_ga4_acquisition_session (last click)
#       dims [date, sessionSourceMedium, sessionCampaignName], metrics conversions+sessions.
#       The mart derives TWO MARGINAL partitions (session_source_medium, session_campaign)
#       from the single raw row -- like country/device_category are two marginals of the
#       standard row. NOT a cross-join between attribution axes.
#   * acquisition_daily_first_user -> raw_ga4_acquisition_first_user (first click)
#       dims [date, firstUserSourceMedium], metric conversions.
#
# AI-53 / AI-13 HONESTY: sessionSourceMedium, sessionCampaignName, firstUserSourceMedium
# are DECLARED per the GA4 Data API doc but NOT verified against a live GA4 property in
# this story (no account connected). The first live pass (AC11) confirms the exact names
# via properties/{p}/metadata (Phase B gate). Fallback if sessionCampaignName is absent:
# the pull still lands the session_source_medium axis (campaign -> 'unknown').
#
# TOP-N BOUNDED PARTITION (reuses _get_page_top_n): source/medium/campaign is a long
# tail. orderBys conversions DESC + limit=top_n; the tail beyond N is DROPPED by design
# (partition bounded, NOT a full reconciliation -- same status as pages 10.3).
#
# CONVERSION-EVENT FILTER (lead_event_name) is a PROJECT PREFERENCE, never hardcoded --
# BLOCKED Phase B in this story (default = all conversions). The dimensionFilter seam is
# noted but not wired to a project-preference source here (out of scope 16.1).
#
# (not set) -> unknown, (direct) -> direct: the direct/unknown bucket must land (never
# silently dropped) so the acquisition axes stay honest (leçon 10.1 unknown bucket).
#
# Parallel, non-interfering: appended AFTER the existing pulls; does NOT touch the
# standard / user_type / pages raw tables. Append-only (AD-7).
# ---------------------------------------------------------------------------

_RAW_INSERT_SQL_ACQ_SESSION = """
INSERT INTO raw_ga4_acquisition_session
    (date, session_source_medium, session_campaign, conversions, sessions,
     pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

_RAW_CREATE_DDL_ACQ_SESSION = """
CREATE TABLE IF NOT EXISTS raw_ga4_acquisition_session (
    date                  VARCHAR,
    session_source_medium VARCHAR,
    session_campaign      VARCHAR,
    conversions           INTEGER,
    sessions              INTEGER,
    pull_id               VARCHAR,
    loaded_at             VARCHAR,
    project_id            VARCHAR
)
"""

_RAW_INSERT_SQL_ACQ_FIRST_USER = """
INSERT INTO raw_ga4_acquisition_first_user
    (date, first_user_source_medium, conversions,
     pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?)
"""

_RAW_CREATE_DDL_ACQ_FIRST_USER = """
CREATE TABLE IF NOT EXISTS raw_ga4_acquisition_first_user (
    date                     VARCHAR,
    first_user_source_medium VARCHAR,
    conversions              INTEGER,
    pull_id                  VARCHAR,
    loaded_at                VARCHAR,
    project_id               VARCHAR
)
"""


def _map_acquisition_value(raw: str) -> str:
    """Map GA4 acquisition dimension sentinels to canonical values (never drop).

    '(not set)' -> 'unknown' and '(direct)' -> 'direct' (the direct/unknown bucket
    must land so the axes stay honest -- leçon 10.1). Any other value passes through.
    """
    if raw == "(not set)":
        return "unknown"
    if raw == "(direct)":
        return "direct"
    return raw


def _insert_raw_rows_acq_session(
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    project_id: str,
    db_mode: str,
    duckdb_path: str,
) -> int:
    """Insert canonical last-click rows into raw_ga4_acquisition_session (Story 16.1).

    Each row dict carries: date, session_source_medium, session_campaign,
    conversions, sessions. Append-only (AD-7). DuckDB only at P2.
    """
    if db_mode == "duckdb":
        from core import warehouse_write  # noqa: PLC0415

        con = warehouse_write.open_raw_writer(duckdb_path, project_id=project_id)
        con.execute(_RAW_CREATE_DDL_ACQ_SESSION)
        con.execute(
            "ALTER TABLE raw_ga4_acquisition_session ADD COLUMN IF NOT EXISTS "
            "project_id VARCHAR DEFAULT 'default'"
        )
        values = [
            (
                r["date"],
                r["session_source_medium"],
                r["session_campaign"],
                int(r["conversions"]),
                int(r["sessions"]),
                pull_id,
                loaded_at,
                project_id,
            )
            for r in rows
        ]
        con.executemany(_RAW_INSERT_SQL_ACQ_SESSION, values)
        con.close()
        return len(values)
    else:
        raise ValueError(
            f"_insert_raw_rows_acq_session: unsupported db_mode {db_mode!r} at P2 "
            "(BigQuery path not yet implemented; use load_seed_acquisition.py for BQ)"
        )


def _insert_raw_rows_acq_first_user(
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    project_id: str,
    db_mode: str,
    duckdb_path: str,
) -> int:
    """Insert canonical first-click rows into raw_ga4_acquisition_first_user (Story 16.1).

    Each row dict carries: date, first_user_source_medium, conversions.
    Append-only (AD-7). DuckDB only at P2.
    """
    if db_mode == "duckdb":
        from core import warehouse_write  # noqa: PLC0415

        con = warehouse_write.open_raw_writer(duckdb_path, project_id=project_id)
        con.execute(_RAW_CREATE_DDL_ACQ_FIRST_USER)
        con.execute(
            "ALTER TABLE raw_ga4_acquisition_first_user ADD COLUMN IF NOT EXISTS "
            "project_id VARCHAR DEFAULT 'default'"
        )
        values = [
            (
                r["date"],
                r["first_user_source_medium"],
                int(r["conversions"]),
                pull_id,
                loaded_at,
                project_id,
            )
            for r in rows
        ]
        con.executemany(_RAW_INSERT_SQL_ACQ_FIRST_USER, values)
        con.close()
        return len(values)
    else:
        raise ValueError(
            f"_insert_raw_rows_acq_first_user: unsupported db_mode {db_mode!r} at P2 "
            "(BigQuery path not yet implemented; use load_seed_acquisition.py for BQ)"
        )


def pull_acquisition_daily_session(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    property_id: str | None = None,
) -> dict:
    """Fetch last-click acquisition (conversions + sessions by GA4 sessionSourceMedium
    x sessionCampaignName) top-N into the raw table.

    Story 16.1. Parallel to the other pulls -- SAME signature. Hits the GA4 Data API
    with dimensions [date, sessionSourceMedium, sessionCampaignName], metrics
    [conversions, sessions], orderBys conversions DESC + limit=top_n. Lands rows in
    raw_ga4_acquisition_session (NOT the other raw tables). Existing pulls UNTOUCHED.

    AI-53 / AI-13 HONESTY: the sessionSourceMedium / sessionCampaignName dimension
    names are DECLARED per the GA4 Data API doc but NOT verified live in this story
    (Phase B gate, AC11 confirms via properties/{p}/metadata). Fallback if
    sessionCampaignName is absent live: campaign -> 'unknown' (the session_source_medium
    axis still lands).

    TOP-N is a BOUNDED partition: the tail beyond top_n is DROPPED by design (the
    returned rows do NOT sum to the connector daily total -- correct, documented on
    the mart, not asserted as reconciliation).

    GA4 mapping before insert:
      * date: YYYYMMDD -> YYYY-MM-DD.
      * sessionSourceMedium / sessionCampaignName: '(not set)' -> 'unknown',
        '(direct)' -> 'direct' (never silently dropped).

    Returns {"pull_id": str, "row_count": int, "date_from": str, "date_to": str,
             "page_top_n": int}.

    # HG-2 (AD-3): token used immediately as a Bearer header, never stored/logged.
    # HG-3 (AD-7): pull_id minted by the caller; never generated here.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    if property_id is None:
        property_id = os.environ.get("GA4_PROPERTY_ID")
    if not property_id:
        raise ValueError(
            "GA4_PROPERTY_ID env var required for pull_acquisition_daily_session "
            "(set it to your GA4 property numeric ID)"
        )

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()
    top_n = _get_page_top_n()

    token = nango_client.get_fresh_token(connection_id, provider="google-analytics")

    request_body = {
        "dimensions": [
            {"name": "date"},
            # AI-13: exact dimension names confirmed at first live pass; declared here.
            {"name": "sessionSourceMedium"},
            {"name": "sessionCampaignName"},
        ],
        "metrics": [
            {"name": "conversions"},
            {"name": "sessions"},
        ],
        "dateRanges": [{"startDate": date_from, "endDate": date_to}],
        # Top-N bound: order by conversions DESC and cap the row count -- the highest
        # converting source/medium x campaign combos first, long tail dropped (Task 1).
        #
        # review-16-5 fix-11 SEMANTICS: `limit=top_n` applies to the ENTIRE WINDOW
        # (dateRanges spans date_from..date_to), NOT per day. GA4 returns the top-N
        # (source_medium x campaign) combos over the full range; the 'date' dimension
        # then multiplies rows per day (one row per combo per day). The resulting
        # n_distinct(source_medium x campaign) per day is <= top_n, but live it is
        # typically LESS (most combos are only active on a subset of days).
        # The attribution card (16.3) top-N marginals (session_source_medium,
        # session_campaign, first_user_source_medium) inherit this truncation bias --
        # the share_of_top_N (Part) is a share of the returned subset, never of the
        # full GA4 day total. Calibration (Phase B): compare with unfiltered reports.
        "orderBys": [{"metric": {"metricName": "conversions"}, "desc": True}],
        "limit": str(top_n),
        # BLOCKED Phase B (AC10): a dimensionFilter on eventName / keyEvents to a
        # project-configured lead_event_name goes HERE. Not wired in 16.1 (project
        # preference registry out of scope); default = all conversions.
    }

    payload = _ga4_runreport(property_id, token, request_body)
    raw_rows = payload.get("rows") or []

    canonical_rows: list[dict] = []
    for api_row in raw_rows:
        dim_values = [d["value"] for d in api_row.get("dimensionValues", [])]
        met_values = [m["value"] for m in api_row.get("metricValues", [])]
        date_str = _ga4_date(dim_values[0] if len(dim_values) > 0 else "")
        src_raw = dim_values[1] if len(dim_values) > 1 else ""
        camp_raw = dim_values[2] if len(dim_values) > 2 else ""
        canonical_rows.append(
            {
                "date": date_str,
                "session_source_medium": _map_acquisition_value(src_raw),
                "session_campaign": _map_acquisition_value(camp_raw),
                "conversions": met_values[0] if len(met_values) > 0 else "0",
                "sessions": met_values[1] if len(met_values) > 1 else "0",
            }
        )

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_raw_rows_acq_session(
        canonical_rows, pull_id, loaded_at, project_id, db_mode, duckdb_path
    )

    # AD-3: no token in log. Log the top-N bound so the dropped tail is explicit.
    logger.info(
        "ga4_pull_acq_session_done: pull_id=%s row_count=%d top_n=%d",
        pull_id,
        row_count,
        top_n,
    )

    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
        "page_top_n": top_n,
    }


def pull_acquisition_daily_first_user(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    property_id: str | None = None,
) -> dict:
    """Fetch first-click acquisition (conversions by GA4 firstUserSourceMedium) top-N
    into the raw table.

    Story 16.1. Parallel to the other pulls -- SAME signature. Hits the GA4 Data API
    with dimensions [date, firstUserSourceMedium], metric [conversions], orderBys
    conversions DESC + limit=top_n. Lands rows in raw_ga4_acquisition_first_user.
    Existing pulls UNTOUCHED. SEPARATE runReport from pull_acquisition_daily_session
    (no session x first-user cross-join).

    AI-53 / AI-13 HONESTY: firstUserSourceMedium is DECLARED but NOT verified live
    (Phase B gate, AC11 confirms via metadata).

    TOP-N is a BOUNDED partition (tail beyond top_n dropped by design).

    GA4 mapping before insert:
      * date: YYYYMMDD -> YYYY-MM-DD.
      * firstUserSourceMedium: '(not set)' -> 'unknown', '(direct)' -> 'direct'.

    Returns {"pull_id": str, "row_count": int, "date_from": str, "date_to": str,
             "page_top_n": int}.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    if property_id is None:
        property_id = os.environ.get("GA4_PROPERTY_ID")
    if not property_id:
        raise ValueError(
            "GA4_PROPERTY_ID env var required for pull_acquisition_daily_first_user "
            "(set it to your GA4 property numeric ID)"
        )

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()
    top_n = _get_page_top_n()

    token = nango_client.get_fresh_token(connection_id, provider="google-analytics")

    request_body = {
        "dimensions": [
            {"name": "date"},
            # AI-13: exact dimension name confirmed at first live pass; declared here.
            {"name": "firstUserSourceMedium"},
        ],
        "metrics": [
            {"name": "conversions"},
        ],
        "dateRanges": [{"startDate": date_from, "endDate": date_to}],
        "orderBys": [{"metric": {"metricName": "conversions"}, "desc": True}],
        "limit": str(top_n),
        # BLOCKED Phase B (AC10): lead_event_name dimensionFilter goes HERE (project
        # preference, not hardcoded). Default = all conversions.
    }

    payload = _ga4_runreport(property_id, token, request_body)
    raw_rows = payload.get("rows") or []

    canonical_rows: list[dict] = []
    for api_row in raw_rows:
        dim_values = [d["value"] for d in api_row.get("dimensionValues", [])]
        met_values = [m["value"] for m in api_row.get("metricValues", [])]
        date_str = _ga4_date(dim_values[0] if len(dim_values) > 0 else "")
        src_raw = dim_values[1] if len(dim_values) > 1 else ""
        canonical_rows.append(
            {
                "date": date_str,
                "first_user_source_medium": _map_acquisition_value(src_raw),
                "conversions": met_values[0] if len(met_values) > 0 else "0",
            }
        )

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_raw_rows_acq_first_user(
        canonical_rows, pull_id, loaded_at, project_id, db_mode, duckdb_path
    )

    logger.info(
        "ga4_pull_acq_first_user_done: pull_id=%s row_count=%d top_n=%d",
        pull_id,
        row_count,
        top_n,
    )

    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
        "page_top_n": top_n,
    }


# ---------------------------------------------------------------------------
# Epic 17, story 17.4: transactions_daily profile (GA4 transactionId e-commerce).
#
# PURPOSE: land GA4 e-commerce transactions per (date, transaction_id) so the
# transaction_reconciliation mart (dbt/models/marts/transaction_reconciliation.sql)
# can FULL OUTER JOIN them against stg_shopify_orders_daily.transaction_id and MEASURE
# the real coverage rate (matched transactions / Shopify orders with a txn) -- the §3.2
# Shopify x GA4 reconciliation. This is a MEASURED overlap, NOT the aggregate estimation
# of 17.2 (dedup_estimate).
#
# GRAIN DISCIPLINE (RULE ABSOLUE, lesson epic-10 + decision 15.4): the TRANSACTION grain
# NEVER enters fact_daily_kpi. transaction_id is NOT a breakdown partition (a partition
# must total the connector day; per-transaction rows do not -- they are a join key, like
# order_id on the Shopify side). So there is NO fact_daily_kpi UNION block for this raw
# table (contrast the acquisition/user_type/pages profiles). This raw table feeds ONLY
# its module-owned staging model stg_ga4_transactions_daily, consumed by the dedicated
# reconciliation mart. This is the central discipline point of the story.
#
# AI-53 / AI-13 HONESTY: the transactionId dimension (GA4 Data API v1, e-commerce) and the
# conversions / purchaseRevenue metrics are DECLARED per the GA4 Data API doc but NOT
# verified against a live GA4 property in this story (no account connected, and e-commerce
# tracking is a property-level setup). The first live pass (AC / AI-13) confirms the exact
# dimension + metric names via properties/{p}/metadata (Phase B gate). A lead-gen site with
# no e-commerce simply returns ZERO transaction rows -> the reconciliation degrades to
# "no GA4 transactions" honestly (never a fabricated coverage).
#
# PAGINATION / LIMIT HONESTY: a single day can carry MORE transactions than one GA4
# runReport page (default GA4 page size). We apply a per-request `limit` (top-N bound,
# reusing _get_page_top_n clamp semantics via a dedicated env GA4_TXN_LIMIT) AND follow
# GA4's response-envelope pagination (rowCount vs returned rows) by paging on `offset`
# until the day is exhausted OR a hard page cap is hit. Unlike the acquisition/pages
# profiles, transactions are a JOIN KEY (not a top-N leaderboard) -- dropping the tail
# would SILENTLY understate the coverage denominator, so we page to completeness within a
# documented bound rather than truncate. The bound + any truncation is logged (never a
# silent drop). ordering by transactionId keeps paging deterministic.

# GA4 transactions per-request page size + hard page cap (documented bound). Env override
# GA4_TXN_LIMIT (clamped [50, 100000]); GA4 Data API caps a single runReport `limit` at
# 250000, so the default 10000 is conservative. _MAX_TXN_PAGES bounds the offset loop so a
# broken/non-progressing response can never hang the worker (mirrors Shopify _MAX_PAGES).
_TXN_LIMIT_DEFAULT = 10000
_TXN_LIMIT_MIN = 50
_TXN_LIMIT_MAX = 100000
_MAX_TXN_PAGES = 1000


def _get_txn_limit() -> int:
    """Resolve the GA4 transactions per-request page size (env GA4_TXN_LIMIT, clamped)."""
    try:
        n = int(os.environ.get("GA4_TXN_LIMIT", str(_TXN_LIMIT_DEFAULT)))
    except (ValueError, TypeError):
        n = _TXN_LIMIT_DEFAULT
    return max(_TXN_LIMIT_MIN, min(_TXN_LIMIT_MAX, n))


_RAW_INSERT_SQL_TXN = """
INSERT INTO raw_ga4_transactions_daily
    (date, transaction_id, conversions, purchase_revenue,
     pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?)
"""

_RAW_CREATE_DDL_TXN = """
CREATE TABLE IF NOT EXISTS raw_ga4_transactions_daily (
    date             VARCHAR,
    transaction_id   VARCHAR,
    conversions      INTEGER,
    purchase_revenue DOUBLE,
    pull_id          VARCHAR,
    loaded_at        VARCHAR,
    project_id       VARCHAR
)
"""


def _insert_raw_rows_transactions(
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    project_id: str,
    db_mode: str,
    duckdb_path: str,
) -> int:
    """Insert canonical transaction rows into raw_ga4_transactions_daily (Story 17.4).

    Each row dict carries: date, transaction_id, conversions, purchase_revenue.
    Append-only (AD-7). DuckDB only at P2 (BigQuery path via load_seed for BQ).
    """
    if db_mode == "duckdb":
        from core import warehouse_write  # noqa: PLC0415

        con = warehouse_write.open_raw_writer(duckdb_path, project_id=project_id)
        con.execute(_RAW_CREATE_DDL_TXN)
        con.execute(
            "ALTER TABLE raw_ga4_transactions_daily ADD COLUMN IF NOT EXISTS "
            "project_id VARCHAR DEFAULT 'default'"
        )
        values = [
            (
                r["date"],
                # transaction_id is the JOIN KEY: a GA4 '(not set)' means the transaction
                # carries no id and CANNOT be reconciled -> keep None (never the string
                # '(not set)', which would create a fake match bucket). Honestly measured
                # as a ga4_only-without-id row the mart filters out of the match denominator.
                (
                    None
                    if r.get("transaction_id") in (None, "", "(not set)")
                    else r["transaction_id"]
                ),
                int(r["conversions"]),
                float(r["purchase_revenue"]),
                pull_id,
                loaded_at,
                project_id,
            )
            for r in rows
        ]
        con.executemany(_RAW_INSERT_SQL_TXN, values)
        con.close()
        return len(values)
    else:
        raise ValueError(
            f"_insert_raw_rows_transactions: unsupported db_mode {db_mode!r} at P2 "
            "(BigQuery path not yet implemented; use load_seed for BQ)"
        )


def pull_transactions_daily(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    property_id: str | None = None,
) -> dict:
    """Fetch GA4 e-commerce transactions (conversions + purchaseRevenue by transactionId)
    into the raw table, PAGING to completeness within a documented bound.

    Story 17.4. Parallel to the other pulls -- SAME signature. Hits the GA4 Data API with
    dimensions [date, transactionId], metrics [conversions, purchaseRevenue]. Lands rows in
    raw_ga4_transactions_daily (NOT the other raw tables, and NOT fact_daily_kpi -- the
    transaction grain is a JOIN KEY for the reconciliation mart, never a breakdown
    partition). Existing pulls UNTOUCHED.

    AI-53 / AI-13 HONESTY: the transactionId dimension and conversions / purchaseRevenue
    metric names are DECLARED per the GA4 Data API doc (e-commerce) but NOT verified live in
    this story (Phase B gate; e-commerce tracking is a property-level setup a lead-gen site
    may not have -> zero rows, honest degrade). AC confirms the exact names via
    properties/{p}/metadata.

    PAGINATION (point dur): a day can carry MORE transactions than one page. Unlike the
    acquisition/pages profiles, transactions are a JOIN KEY -- dropping the tail would
    silently understate the reconciliation denominator. So we ORDER BY transactionId,
    apply a per-request `limit` (_get_txn_limit), and PAGE on `offset` until the day is
    exhausted (returned rows < limit) OR the hard page cap (_MAX_TXN_PAGES) is reached
    (logged, never silent).

    GA4 mapping before insert:
      * date: YYYYMMDD -> YYYY-MM-DD.
      * transactionId '(not set)'/'' -> None (no id -> cannot reconcile; never a fake
        match bucket) -- honestly landed as a ga4-side row without a join key.

    Returns {"pull_id": str, "row_count": int, "date_from": str, "date_to": str,
             "txn_limit": int, "pages": int, "truncated": bool}.

    # HG-2 (AD-3): token used immediately as a Bearer header, never stored/logged.
    # HG-3 (AD-7): pull_id minted by the caller; never generated here.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    if property_id is None:
        property_id = os.environ.get("GA4_PROPERTY_ID")
    if not property_id:
        raise ValueError(
            "GA4_PROPERTY_ID env var required for pull_transactions_daily "
            "(set it to your GA4 property numeric ID)"
        )

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()
    txn_limit = _get_txn_limit()

    token = nango_client.get_fresh_token(connection_id, provider="google-analytics")

    canonical_rows: list[dict] = []
    offset = 0
    pages = 0
    truncated = False
    while True:
        if pages >= _MAX_TXN_PAGES:
            truncated = True
            logger.warning(
                "ga4_pull_transactions: page cap %d reached, stopping (pull_id=%s) -- "
                "coverage denominator may be understated; raise GA4_TXN_LIMIT or narrow window",
                _MAX_TXN_PAGES,
                pull_id,
            )
            break
        pages += 1

        request_body = {
            "dimensions": [
                {"name": "date"},
                # AI-13: exact dimension name confirmed at first live pass; declared here.
                {"name": "transactionId"},
            ],
            "metrics": [
                {"name": "conversions"},
                {"name": "purchaseRevenue"},
            ],
            "dateRanges": [{"startDate": date_from, "endDate": date_to}],
            # Deterministic paging: order by the join key so offset paging is stable.
            "orderBys": [{"dimension": {"dimensionName": "transactionId"}}],
            "limit": str(txn_limit),
            "offset": str(offset),
        }

        payload = _ga4_runreport(property_id, token, request_body)
        raw_rows = payload.get("rows") or []

        for api_row in raw_rows:
            dim_values = [d["value"] for d in api_row.get("dimensionValues", [])]
            met_values = [m["value"] for m in api_row.get("metricValues", [])]
            date_str = _ga4_date(dim_values[0] if len(dim_values) > 0 else "")
            txn_raw = dim_values[1] if len(dim_values) > 1 else ""
            canonical_rows.append(
                {
                    "date": date_str,
                    "transaction_id": txn_raw,
                    "conversions": met_values[0] if len(met_values) > 0 else "0",
                    "purchase_revenue": met_values[1] if len(met_values) > 1 else "0",
                }
            )

        # Page exhausted when GA4 returns fewer rows than the requested limit.
        if len(raw_rows) < txn_limit:
            break
        offset += txn_limit

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_raw_rows_transactions(
        canonical_rows, pull_id, loaded_at, project_id, db_mode, duckdb_path
    )

    # AD-3: no token in log. Log the paging bound so any truncation is explicit.
    logger.info(
        "ga4_pull_transactions_done: pull_id=%s row_count=%d pages=%d txn_limit=%d truncated=%s",
        pull_id,
        row_count,
        pages,
        txn_limit,
        truncated,
    )

    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
        "txn_limit": txn_limit,
        "pages": pages,
        "truncated": truncated,
    }


def pull_user_type_daily(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    property_id: str | None = None,
) -> dict:
    """Fetch the user_type_daily report (GA4 newVsReturning) into the raw table.

    Story 10.1. Parallel to pull() -- SAME signature shape, but hits the GA4 Data
    API with dimensions [date, newVsReturning] and lands rows in
    raw_ga4_user_type_daily (NOT raw_ga4_standard_daily). The existing pull() and
    raw_ga4_standard_daily are UNTOUCHED (non-interfering partition).

    GA4 mapping applied before insert:
      * date: YYYYMMDD -> YYYY-MM-DD (same slice pattern as pull()).
      * newVsReturning: '(not set)' -> 'unknown' (never silently dropped -- the
        'unknown' bucket must reach the mart so the three user_type values sum to
        the connector daily total, reconciliation AC9c).

    Returns {"pull_id": str, "row_count": int, "date_from": str, "date_to": str}.

    # HG-2 (AD-3): token used immediately as a Bearer header, never stored/logged.
    # HG-3 (AD-7): pull_id minted by the caller; never generated here.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    if property_id is None:
        property_id = os.environ.get("GA4_PROPERTY_ID")
    if not property_id:
        raise ValueError(
            "GA4_PROPERTY_ID env var required for pull_user_type_daily "
            "(set it to your GA4 property numeric ID)"
        )

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    token = nango_client.get_fresh_token(connection_id, provider="google-analytics")

    request_body = {
        "dimensions": [
            {"name": "date"},
            {"name": "newVsReturning"},
        ],
        "metrics": [
            {"name": "sessions"},
            {"name": "activeUsers"},
            {"name": "conversions"},
        ],
        "dateRanges": [{"startDate": date_from, "endDate": date_to}],
    }

    url = f"{GA4_DATA_API_BASE}/properties/{property_id}:runReport"

    resp = httpx.post(
        url,
        json=request_body,
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
        raise RateLimitError("google-analytics", retry_after)

    if resp.status_code != 200:
        # Story 25.2: canonical typed error; provider payload preserved.
        from core.pull_errors import classify_http_error  # noqa: PLC0415

        try:
            _body = resp.json()
        except Exception:
            _body = resp.text
        raise classify_http_error(resp.status_code, _body)

    payload = resp.json()
    raw_rows = payload.get("rows") or []

    canonical_rows: list[dict] = []
    for api_row in raw_rows:
        dim_values = [d["value"] for d in api_row.get("dimensionValues", [])]
        met_values = [m["value"] for m in api_row.get("metricValues", [])]
        date_raw = dim_values[0] if len(dim_values) > 0 else ""
        date_str = (
            date_raw[:4] + "-" + date_raw[4:6] + "-" + date_raw[6:]
            if len(date_raw) == 8
            else date_raw
        )
        user_type_raw = dim_values[1] if len(dim_values) > 1 else ""
        # (not set) -> unknown mapping (never suppress; the unknown bucket must land).
        user_type = "unknown" if user_type_raw == "(not set)" else user_type_raw
        canonical_rows.append(
            {
                "date": date_str,
                "user_type": user_type,
                "sessions": met_values[0] if len(met_values) > 0 else "0",
                "active_users": met_values[1] if len(met_values) > 1 else "0",
                "conversions": met_values[2] if len(met_values) > 2 else "0",
            }
        )

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_raw_rows_user_type(
        canonical_rows, pull_id, loaded_at, project_id, db_mode, duckdb_path
    )

    # AD-3: no token in log -- only pull_id and row_count (safe metadata).
    logger.info(
        "ga4_pull_user_type_done: pull_id=%s row_count=%d", pull_id, row_count
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
    property_id: str | None = None,
) -> dict:
    """Fetch the standard_daily report and land rows in the raw table.

    # AD-12 NOTE: synchronous API call from HTTP endpoint is allowed at P2
    # (queue not deployed). Story 3.2 moves this behind Cloud Tasks.

    # HG-2 (AD-3): the token is used immediately as a Bearer header and then
    # discarded — it is NEVER stored, logged, or passed further.
    # HG-3 (AD-7): pull_id is minted by the caller (trigger endpoint); this
    # function receives it as a parameter and never mints its own pull_id.

    Parameters
    ----------
    connection_id:
        Nango connection identifier passed to get_fresh_token.
    date_from, date_to:
        ISO-8601 date strings (YYYY-MM-DD) for the GA4 date range.
    project_id:
        Project binding for the raw rows (from connection_ref in Postgres).
    pull_id:
        Core-minted pull ULID (AD-7). Passed in; not generated here.
    property_id:
        GA4 numeric property ID string. If None, reads GA4_PROPERTY_ID env var.

    Returns
    -------
    dict
        {"pull_id": str, "row_count": int, "date_from": str, "date_to": str}

    Raises
    ------
    ValueError
        If GA4_PROPERTY_ID env var is missing and property_id is None.
    RuntimeError
        If the GA4 Data API returns a non-200 response.
    NangoTokenError
        If Nango cannot provide a valid access token.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    if property_id is None:
        property_id = os.environ.get("GA4_PROPERTY_ID")
    if not property_id:
        raise ValueError(
            "GA4_PROPERTY_ID env var required for pull (set it to your GA4 property numeric ID)"
        )

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    # HG-2 (AD-3): token obtained immediately before use; falls out of scope after httpx call.
    token = nango_client.get_fresh_token(connection_id, provider="google-analytics")

    request_body = {
        "dimensions": [
            {"name": "date"},
            {"name": "deviceCategory"},
            {"name": "country"},
        ],
        "metrics": [
            {"name": "sessions"},
            {"name": "activeUsers"},
            {"name": "conversions"},
        ],
        "dateRanges": [{"startDate": date_from, "endDate": date_to}],
    }

    url = f"{GA4_DATA_API_BASE}/properties/{property_id}:runReport"

    # AD-12 NOTE: synchronous GA4 API call from HTTP endpoint is allowed at P2
    # (queue not deployed). Story 3.2 moves this behind Cloud Tasks.
    resp = httpx.post(
        url,
        json=request_body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )

    if resp.status_code == 429:
        # Story 3.3 (AC7, T5.1): raise RateLimitError so the worker trips the breaker.
        # "google-analytics" lives in the MODULE (not in core) -- AD-2 compliant.
        from core.quota import RateLimitError  # noqa: PLC0415

        retry_after_raw = resp.headers.get("Retry-After", "0")
        try:
            retry_after = int(retry_after_raw) or None
        except (ValueError, TypeError):
            retry_after = None
        raise RateLimitError("google-analytics", retry_after)

    if resp.status_code != 200:
        # Story 25.2: canonical typed error; provider payload preserved.
        from core.pull_errors import classify_http_error  # noqa: PLC0415

        try:
            _body = resp.json()
        except Exception:
            _body = resp.text
        raise classify_http_error(resp.status_code, _body)

    payload = resp.json()
    raw_rows = payload.get("rows") or []

    # Parse GA4 API response rows into canonical dicts
    # GA4 date dimension returns YYYYMMDD — convert to YYYY-MM-DD
    canonical_rows: list[dict] = []
    for api_row in raw_rows:
        dim_values = [d["value"] for d in api_row.get("dimensionValues", [])]
        met_values = [m["value"] for m in api_row.get("metricValues", [])]
        date_raw = dim_values[0] if len(dim_values) > 0 else ""
        date_str = (
            date_raw[:4] + "-" + date_raw[4:6] + "-" + date_raw[6:]
            if len(date_raw) == 8
            else date_raw
        )
        # Use transform() to map GA4 API names to canonical names
        raw_record = {
            "date": date_str,
            "deviceCategory": dim_values[1] if len(dim_values) > 1 else "",
            "country": dim_values[2] if len(dim_values) > 2 else "",
            "sessions": met_values[0] if len(met_values) > 0 else "0",
            "activeUsers": met_values[1] if len(met_values) > 1 else "0",
            "conversions": met_values[2] if len(met_values) > 2 else "0",
        }
        transformed = transform([raw_record])
        canonical_rows.extend(transformed)

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_raw_rows(
        canonical_rows, pull_id, loaded_at, project_id, db_mode, duckdb_path
    )

    # AD-3: no token in log — only pull_id and row_count (safe metadata)
    logger.info("ga4_pull_started: pull_id=%s row_count=%d", pull_id, row_count)

    return {"pull_id": pull_id, "row_count": row_count, "date_from": date_from, "date_to": date_to}


# ---------------------------------------------------------------------------
# Story 25.9 — catalog_driven execution: pull_catalog_daily (GA4).
#
# Principle (Jean, extends the 25.8 meta-ads reference): the catalog IS the
# execution contract. core validates the selection against api_catalog.json and
# passes selection={"metrics","dimensions","source_fields": {...}} into this
# callable; here we translate that generic selection into concrete GA4 runReport
# dimensions[]+metrics[] arrays.
#
# GA4 HARD LIMITS (point dur): a single runReport accepts AT MOST 10 metrics AND
# 9 dimensions. A selection wider than either ceiling CHUNKS into >1 runReport:
#   * date is carried in EVERY chunk (it is the time key of the merge grain);
#   * the breakdown dimensions (everything but date) are chunked to <= 8 so date
#     + a dim-chunk stays within the 9-dimension ceiling;
#   * the dimension-chunks CROSS the metric-chunks (full cartesian) so every
#     (metric-chunk x dim-chunk) pair is issued and no cell is dropped.
# The per-chunk rows MERGE on the (date, sorted-breakdown-value) grain key so the
# SAME grain's metrics from different chunks collapse into one series set. Rows
# land LONG in the dedicated raw_ga4_catalog_daily (one row per grain x metric).
#
# REALTIME-ONLY minute dims (dateHourMinute / minute / nthMinute) are cataloged
# GA4 apiNames but only defined for the Realtime API; pull_catalog_daily builds a
# standard runReport over a date window and REFUSES them BEFORE any HTTP call with
# core.pull_errors.InvalidRequestError (the declared incompatibility the single
# 'core' TIME section map cannot express -- catalog_sources.realtime_incompatible_fields).
# ---------------------------------------------------------------------------

# GA4 runReport hard ceilings (documented API limits). date always occupies one
# of the 9 dimension slots, so breakdown dims are chunked to <= 8.
_CATALOG_MAX_METRICS = 10
_CATALOG_MAX_DIMENSIONS = 9

# The time key: always present as the first dimension of every chunk and never
# treated as a breakdown (it is the grain's date axis, not a partition value).
_CATALOG_DATE_FIELD_ID = "date"


def _load_catalog_sources() -> dict:
    """Load and return catalog_sources/catalog_sources.json (cached per process)."""
    try:
        return _CATALOG_SOURCES  # type: ignore[name-defined]
    except NameError:
        pass
    path = Path(__file__).parent / "catalog_sources" / "catalog_sources.json"
    globals()["_CATALOG_SOURCES"] = json.loads(path.read_text(encoding="utf-8"))
    return globals()["_CATALOG_SOURCES"]


def _realtime_incompatible_field_ids() -> set[str]:
    """Return the declared realtime-only field ids (refused for a daily pull).

    Source of truth is catalog_sources.realtime_incompatible_fields (AD-2 — the
    incompatibility is DECLARED, not coded in core). The leading ``_comment`` key
    is metadata, not a field id, so it is filtered out.
    """
    block = _load_catalog_sources().get("realtime_incompatible_fields", {})
    return {fid for fid in block if not fid.startswith("_")}


def _catalog_default_selection() -> dict:
    """Resolve the tier-core default selection FROM the catalog, pruned of the
    realtime-incompatible dims so a None selection is a valid daily runReport.

    ``catalog_default_selection`` returns EVERY non-excluded core-tier field,
    which for GA4 includes the realtime-only minute dims (they are cataloged,
    exposed, core-tier TIME apiNames). Those cannot go on a standard runReport,
    so we drop them here BEFORE validation -- the default must never trip the
    request-build refusal it exists to keep valid.
    """
    from core.catalog_contract import (  # noqa: PLC0415
        catalog_default_selection,
        validate_selection,
    )

    catalog = json.loads(
        (Path(__file__).parent / "api_catalog.json").read_text(encoding="utf-8")
    )
    default = catalog_default_selection(catalog)
    refused = _realtime_incompatible_field_ids()
    default["dimensions"] = [d for d in default["dimensions"] if d not in refused]
    default["metrics"] = [m for m in default["metrics"] if m not in refused]
    selection, _issues = validate_selection(catalog, default)
    return selection


def _build_catalog_dimensions_metrics(
    selection: dict,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Translate a validated *selection* into GA4 (dimension, metric) request lists.

    Returns ``(dimensions, metrics)`` where each entry is a
    ``(field_id, source_field)`` pair -- ``source_field`` is the GA4 apiName used
    in the runReport, ``field_id`` is the catalog/platform id used as the merge
    grain key + the landed ``breakdown_dimension`` / ``metric_name``.

    ``date`` is guaranteed to be the FIRST dimension (the time key). Realtime-only
    minute dims are refused here, BEFORE any HTTP call, with InvalidRequestError.
    """
    from core.pull_errors import InvalidRequestError  # noqa: PLC0415 -- AD-2

    source_fields: dict[str, str] = selection.get("source_fields", {})
    metric_ids: list[str] = list(selection.get("metrics", []))
    dimension_ids: list[str] = list(selection.get("dimensions", []))

    # Refuse realtime-only dims BEFORE the API call (the declared incompatibility
    # the single 'core' TIME section map cannot express -> invalid_request drift).
    refused = _realtime_incompatible_field_ids()
    offending = [d for d in dimension_ids if d in refused]
    if offending:
        raise InvalidRequestError(
            provider_status=None,
            provider_payload=None,
            message=(
                "selection requests realtime-only GA4 dimension(s) "
                + ", ".join(sorted(offending))
                + " which pull_catalog_daily cannot serve on a standard runReport "
                "(realtime_incompatible_fields) -- only the Realtime API defines them"
            ),
        )

    metrics: list[tuple[str, str]] = [
        (fid, source_fields.get(fid, fid)) for fid in metric_ids
    ]

    # date leads the dimension list; the rest (breakdowns) follow in selection order.
    dims: list[tuple[str, str]] = [(_CATALOG_DATE_FIELD_ID, _CATALOG_DATE_FIELD_ID)]
    for fid in dimension_ids:
        if fid == _CATALOG_DATE_FIELD_ID:
            continue
        dims.append((fid, source_fields.get(fid, fid)))

    return dims, metrics


def _chunk(seq: list, size: int) -> list[list]:
    """Split *seq* into consecutive chunks of at most *size* (>= 1 chunk always)."""
    size = max(1, size)
    if not seq:
        return [[]]
    return [seq[i:i + size] for i in range(0, len(seq), size)]


def _catalog_grain_key(
    dim_pairs: list[tuple[str, str]], dim_values: list[str]
) -> tuple:
    """Merge key for a runReport row: date value + sorted breakdown (id, value).

    dim_pairs is the chunk's (field_id, source_field) order; dim_values are the
    row's dimensionValues in the SAME order. date is the time axis; the remaining
    breakdown pairs are sorted by field_id so the key is stable no matter which
    dimension-chunk produced the row (chunks cross metric-chunks).
    """
    date_value = ""
    breakdowns: list[tuple[str, str]] = []
    for (fid, _src), value in zip(dim_pairs, dim_values):
        if fid == _CATALOG_DATE_FIELD_ID:
            date_value = value
        else:
            breakdowns.append((fid, value))
    breakdowns.sort(key=lambda kv: kv[0])
    return (date_value, tuple(breakdowns))


_RAW_CREATE_DDL_CATALOG = """
CREATE TABLE IF NOT EXISTS raw_ga4_catalog_daily (
    date                VARCHAR,
    metric_name         VARCHAR,
    metric_value        VARCHAR,
    breakdown_dimension VARCHAR,
    breakdown_value     VARCHAR,
    pull_id             VARCHAR,
    loaded_at           VARCHAR,
    project_id          VARCHAR
)
"""

_RAW_INSERT_SQL_CATALOG = """
INSERT INTO raw_ga4_catalog_daily
    (date, metric_name, metric_value, breakdown_dimension, breakdown_value,
     pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""


def _insert_raw_rows_catalog(
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    project_id: str,
    db_mode: str,
    duckdb_path: str,
) -> int:
    """Land LONG catalog rows into raw_ga4_catalog_daily (Story 25.9, DuckDB only).

    Each row dict carries: date, metric_name, metric_value, breakdown_dimension,
    breakdown_value. Append-only (AD-7). Dedicated raw table so the catalog_driven
    path never perturbs raw_ga4_standard_daily (AD-22).
    """
    if db_mode == "duckdb":
        from core import warehouse_write  # noqa: PLC0415

        con = warehouse_write.open_raw_writer(duckdb_path, project_id=project_id)
        con.execute(_RAW_CREATE_DDL_CATALOG)
        values = [
            (
                r.get("date", ""),
                r.get("metric_name", ""),
                None if r.get("metric_value") is None else str(r.get("metric_value")),
                r.get("breakdown_dimension", ""),
                r.get("breakdown_value", ""),
                pull_id,
                loaded_at,
                project_id,
            )
            for r in rows
        ]
        if values:
            con.executemany(_RAW_INSERT_SQL_CATALOG, values)
        con.close()
        return len(values)
    else:
        raise ValueError(
            f"_insert_raw_rows_catalog: unsupported db_mode {db_mode!r} at P-dev "
            "(BigQuery path not yet implemented)"
        )


def pull_catalog_daily(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    selection: dict | None = None,
    property_id: str | None = None,
) -> dict:
    """Story 25.9: catalog_driven daily pull for GA4 (param-style, meta-ads 25.8 kin).

    core resolves+validates ``selection`` against api_catalog.json and passes it
    in as ``{"metrics","dimensions","source_fields"}``. A ``None`` selection (job
    without an explicit selection) falls back to the catalog tier-core default,
    pruned of realtime-incompatible minute dims, resolved here so the callable is
    safe to invoke standalone in tests too.

    The pull:
      1. builds GA4 runReport dimensions[]+metrics[] from the selection's
         source_fields (date is always the first dimension / time key);
      2. refuses realtime-only dims (dateHourMinute/minute/nthMinute) BEFORE any
         API call (InvalidRequestError -- the invalid_request drift signal);
      3. CHUNKS under the GA4 10-metric / 9-dimension ceilings (date in every
         chunk; breakdown dims <= 8 per chunk; dimension-chunks cross metric-chunks)
         and MERGES the per-chunk rows on the (date, sorted-breakdown) grain key;
      4. lands LONG rows (one per grain x metric) in the dedicated
         raw_ga4_catalog_daily -- the legacy standard_daily path is UNTOUCHED (AD-22).

    # HG-2 (AD-3): token used immediately as a Bearer header, never stored/logged.
    # HG-3 (AD-7): pull_id minted by the caller; never generated here.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    if property_id is None:
        property_id = os.environ.get("GA4_PROPERTY_ID")
    if not property_id:
        raise ValueError(
            "GA4_PROPERTY_ID env var required for pull_catalog_daily "
            "(set it to your GA4 property numeric ID)"
        )

    # Default selection resolved FROM the catalog (tier-core, minute dims pruned)
    # when absent, so the callable is honest standalone (core normally passes a
    # validated selection).
    if selection is None:
        selection = _catalog_default_selection()

    # Build + refuse realtime dims BEFORE the token is even fetched (pre-call).
    dim_pairs, metric_pairs = _build_catalog_dimensions_metrics(selection)

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    token = nango_client.get_fresh_token(connection_id, provider="google-analytics")

    # Chunk under the GA4 ceilings. date always leads a dim-chunk (it is the time
    # key of the merge grain), so breakdown dims chunk to <= 8 (date + 8 = 9).
    breakdown_pairs = [p for p in dim_pairs if p[0] != _CATALOG_DATE_FIELD_ID]
    date_pair = (_CATALOG_DATE_FIELD_ID, _CATALOG_DATE_FIELD_ID)
    breakdown_chunks = _chunk(breakdown_pairs, _CATALOG_MAX_DIMENSIONS - 1)
    dim_chunks = [[date_pair] + bc for bc in breakdown_chunks]
    metric_chunks = _chunk(metric_pairs, _CATALOG_MAX_METRICS)

    # Merge grain -> {"date","breakdown_dimension","breakdown_value",
    #                 "metrics": {metric_field_id: metric_value}}.
    merged: dict[tuple, dict] = {}

    for dim_chunk in dim_chunks:
        for metric_chunk in metric_chunks:
            request_body = {
                "dimensions": [{"name": src} for (_fid, src) in dim_chunk],
                "metrics": [{"name": src} for (_fid, src) in metric_chunk],
                "dateRanges": [{"startDate": date_from, "endDate": date_to}],
            }
            payload = _ga4_runreport(property_id, token, request_body)
            for api_row in payload.get("rows") or []:
                dim_values = [d.get("value", "") for d in api_row.get("dimensionValues", [])]
                met_values = [m.get("value", "") for m in api_row.get("metricValues", [])]
                key = _catalog_grain_key(dim_chunk, dim_values)
                date_value, breakdowns = key
                entry = merged.get(key)
                if entry is None:
                    entry = {
                        "date": _ga4_date(date_value),
                        # breakdown_dimension / breakdown_value carry the sorted
                        # breakdown id/value pairs joined by '|' (a single-dim
                        # selection lands the bare id/value). date is NOT a breakdown.
                        "breakdown_dimension": "|".join(fid for fid, _v in breakdowns),
                        "breakdown_value": "|".join(v for _f, v in breakdowns),
                        "metrics": {},
                    }
                    merged[key] = entry
                for (met_fid, _src), value in zip(metric_chunk, met_values):
                    # Merge: same grain from a different metric-chunk fills its slot.
                    entry["metrics"][met_fid] = value

    # Explode to LONG: one row per (grain, selected metric).
    long_rows: list[dict] = []
    for entry in merged.values():
        for met_fid, value in entry["metrics"].items():
            long_rows.append(
                {
                    "date": entry["date"],
                    "metric_name": met_fid,
                    "metric_value": value,
                    "breakdown_dimension": entry["breakdown_dimension"],
                    "breakdown_value": entry["breakdown_value"],
                }
            )

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_raw_rows_catalog(
        long_rows, pull_id, loaded_at, project_id, db_mode, duckdb_path
    )

    logger.info(
        "ga4_pull_catalog_done: pull_id=%s row_count=%d dims=%d metrics=%d "
        "dim_chunks=%d metric_chunks=%d",
        pull_id,
        row_count,
        len(dim_pairs),
        len(metric_pairs),
        len(dim_chunks),
        len(metric_chunks),
    )

    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
    }


# ---------------------------------------------------------------------------
# Story 25.5 (AC5): account topology discovery via the GA Admin API.
#
# The onboarding flow (discovery -> selection -> access check -> trial ->
# backfill) is core-owned; the connector supplies ONLY this discovery call. It
# lists what the token can reach so core can present the account>property
# hierarchy for selection. selection_level='property' (a GA4 runReport targets a
# property). No *_ACCOUNT_ID env var is read here -- the selected property is
# resolved core-side (account_topology.resolve_selected_account) at rollout.
#
# GA Admin API: GET accountSummaries.list returns accountSummaries[], each with
# an `account` name, a `displayName`, and `propertySummaries[]` (property `property`
# name + `displayName`). We map that to the generic hierarchy core expects:
#   [{"id": "<account>", "label": "<name>", "children": [{"id": "<property>", ...}]}]
# HG-2 (AD-3): the token is used immediately as a Bearer header, never stored/logged.
# ---------------------------------------------------------------------------

# GA Admin API v1beta base (distinct from the Data API used by the pulls).
GA4_ADMIN_API_BASE = "https://analyticsadmin.googleapis.com/v1beta"

# accountSummaries.list is paged; cap the loop so a broken/non-progressing
# response can never hang discovery (mirrors the transactions paging discipline).
_MAX_ACCOUNT_SUMMARY_PAGES = 100


def discover_accounts(connection_id: str) -> list[dict]:
    """List the GA4 accounts + properties the connection's token can reach.

    Story 25.5 (AC5). Calls the GA Admin API accountSummaries.list (paged) and
    returns the generic hierarchy core's topology flow consumes:

        [
          {"id": "accounts/123", "label": "Acme",
           "children": [
              {"id": "properties/456", "label": "Acme Web"},
              ...
           ]},
          ...
        ]

    The `id` strings are opaque to core (AD-2); the selected reporting entity is a
    PROPERTY (selection_level='property'). The Data API pulls target
    properties/{numeric_id}, so a property id here is the resource name
    'properties/<id>' -- core stores it verbatim and hands it back at pull time.

    Raises:
        A typed core.pull_errors.ConnectorError on any non-2xx (401 -> auth_expired
        via the pure-HTTP taxonomy) -- mirrors _ga4_runreport's error handling.
        core.quota.RateLimitError on 429 (breaker path, unchanged contract).
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    token = nango_client.get_fresh_token(connection_id, provider="google-analytics")

    accounts_by_id: dict[str, dict] = {}
    ordered_ids: list[str] = []
    page_token: str | None = None
    pages = 0

    while True:
        if pages >= _MAX_ACCOUNT_SUMMARY_PAGES:
            logger.warning(
                "ga4_discover_accounts: page cap %d reached, stopping",
                _MAX_ACCOUNT_SUMMARY_PAGES,
            )
            break
        pages += 1

        params: dict[str, str] = {"pageSize": "200"}
        if page_token:
            params["pageToken"] = page_token

        url = f"{GA4_ADMIN_API_BASE}/accountSummaries"
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
            raise RateLimitError("google-analytics", retry_after)

        if resp.status_code != 200:
            # Story 25.2 taxonomy: 401 -> auth_expired; provider payload preserved.
            from core.pull_errors import classify_http_error  # noqa: PLC0415

            try:
                _body = resp.json()
            except Exception:
                _body = resp.text
            raise classify_http_error(resp.status_code, _body)

        payload = resp.json()
        for summary in payload.get("accountSummaries", []) or []:
            account_id = summary.get("account", "")
            if not account_id:
                continue
            if account_id not in accounts_by_id:
                node = {
                    "id": account_id,
                    "label": summary.get("displayName", "") or account_id,
                    "children": [],
                }
                accounts_by_id[account_id] = node
                ordered_ids.append(account_id)
            children = accounts_by_id[account_id]["children"]
            for prop in summary.get("propertySummaries", []) or []:
                prop_id = prop.get("property", "")
                if not prop_id:
                    continue
                children.append(
                    {
                        "id": prop_id,
                        "label": prop.get("displayName", "") or prop_id,
                    }
                )

        page_token = payload.get("nextPageToken")
        if not page_token:
            break

    # AD-3: no token in log. Only counts (safe metadata).
    n_props = sum(len(a["children"]) for a in accounts_by_id.values())
    logger.info(
        "ga4_discover_accounts: accounts=%d properties=%d pages=%d",
        len(ordered_ids),
        n_props,
        pages,
    )
    return [accounts_by_id[aid] for aid in ordered_ids]


# ---------------------------------------------------------------------------
# transform() — manifest-driven canonical field mapping (Story 1.8, T6.4)
# ---------------------------------------------------------------------------


def _load_country_alias_map() -> dict[str, str]:
    """Load alias -> iso_code mapping from dbt/seeds/dim_country.csv.

    Story 4.1 (AC4): country normalization uses the same vocabulary seed as dbt staging,
    ensuring Python transform() and dbt staging produce identical canonical values.
    Returns an empty dict if the seed file is not found (graceful degradation).
    """
    import csv  # noqa: PLC0415

    # Resolve path: connector.py is in server/modules/google-analytics/
    # dim_country.csv is in dbt/seeds/ (four levels up from connector.py)
    _seed_path = Path(__file__).parents[3] / "dbt" / "seeds" / "dim_country.csv"
    alias_map: dict[str, str] = {}
    if not _seed_path.exists():
        return alias_map
    with _seed_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            iso_code = (row.get("iso_code") or "").strip()
            aliases_raw = (row.get("aliases") or "").strip()
            for alias in aliases_raw.split("|"):
                alias = alias.strip()
                if alias:
                    alias_map[alias] = iso_code
    return alias_map


# Eagerly build the alias map at import time (it's a small CSV; no I/O at call time).
_COUNTRY_ALIAS_MAP: dict[str, str] = _load_country_alias_map()


def transform(raw_rows: list[dict]) -> list[dict]:
    """Map raw source fields to canonical names using manifest mappings.

    # AD-4: canonical field mapping driven by manifest — do not hardcode source field names
    Reads ``canonical_metric_mapping`` and ``canonical_dimension_mapping`` from
    ``manifest.json`` at import time and renames fields accordingly.  Fields not
    present in either mapping are passed through unchanged (e.g. ``pull_id``,
    ``connector``, ``date``).

    Story 4.1 (AC4): after field renaming, the ``country`` field is normalized from
    GA4 full names (e.g. 'France') to ISO-3166 alpha-2 codes (e.g. 'FR') using the
    dim_country.csv alias map — the same vocabulary seed consumed by dbt staging.
    Unknown country values are passed through unchanged (non-blocking at transform layer;
    the dbt not_null test on the staging model is the hard enforcement gate).
    """
    _manifest_path = Path(__file__).parent / "manifest.json"
    _manifest = json.loads(_manifest_path.read_text(encoding="utf-8"))

    # Build a unified rename map: source_name -> canonical_name
    rename_map: dict[str, str] = {}
    rename_map.update(_manifest.get("canonical_metric_mapping", {}))
    rename_map.update(_manifest.get("canonical_dimension_mapping", {}))

    result: list[dict] = []
    for row in raw_rows:
        canonical_row: dict = {}
        for key, value in row.items():
            canonical_row[rename_map.get(key, key)] = value

        # Story 4.1 (AC4): normalize country to ISO-3166 alpha-2 using dim_country aliases.
        # Pass through unchanged if not found (graceful degradation; dbt enforces vocabulary).
        if "country" in canonical_row and isinstance(canonical_row["country"], str):
            raw_country = canonical_row["country"]
            canonical_row["country"] = _COUNTRY_ALIAS_MAP.get(raw_country, raw_country)

        result.append(canonical_row)
    return result


# ---------------------------------------------------------------------------
# Story 3.5: Register this module's raw table name with core.verification.
# This is the module→core direction (allowed by AD-2: only core→modules is
# forbidden). The raw table name never appears in core source code (AD-2).
# ---------------------------------------------------------------------------
try:
    from core.verification import register_raw_table_name as _register_raw  # noqa: PLC0415

    _register_raw("raw_ga4_standard_daily", provider="google-analytics")  # review-3-6: per-provider
    # Epic 10, story 10.1: user_type_daily profile raw table (additive registration).
    _register_raw("raw_ga4_user_type_daily", provider="google-analytics")
    # Epic 10, story 10.3: pages_daily profiles raw tables (additive registration).
    _register_raw("raw_ga4_landing_daily", provider="google-analytics")
    _register_raw("raw_ga4_paths_daily", provider="google-analytics")
    # Epic 16, story 16.1: acquisition profiles raw tables (additive registration).
    _register_raw("raw_ga4_acquisition_session", provider="google-analytics")
    _register_raw("raw_ga4_acquisition_first_user", provider="google-analytics")
    # Epic 17, story 17.4: transactions profile raw table (GA4 x Shopify reconciliation).
    # NB: this raw table feeds the transaction_reconciliation mart ONLY -- the transaction
    # grain never enters fact_daily_kpi (join key, not a breakdown partition).
    _register_raw("raw_ga4_transactions_daily", provider="google-analytics")
    # Story 25.9: catalog_driven profile raw table (LONG grain x metric shape).
    # Dedicated table -> the catalog_driven path never perturbs raw_ga4_standard_daily (AD-22).
    _register_raw("raw_ga4_catalog_daily", provider="google-analytics")
except Exception:
    pass  # best-effort; verification will log a warning if table name is missing
