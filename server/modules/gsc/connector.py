"""Google Search Console connector — Story 6.2.

Exposes a ``mcp_app: FastMCP`` instance that the core loader mounts under the
``gsc`` namespace (AD-2). This is the third module on the shared base —
the FR2 "drop-a-folder" proof: zero edits to server/core, one mart UNION.

# AD-12: MCP server reads the fact_daily_kpi mart only — no raw_* tables, no CSV.
# AD-3: token from Nango immediately before use — never stored or logged.
# AD-7: pull_id minted by the core scheduler and passed into pull().
# AD-14: identity/project_id parameter scaffold.
# AD-4: average_position is NON-ADDITIVE. Stored raw per row with impressions as
#        weight. The semantic_avg_position view applies impression-weighted aggregation.
#        NEVER SUM average_position directly — use semantic_avg_position.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Module-level FastMCP instance — the public surface the loader mounts.
# The core does: mcp.mount(loaded.connector_module.mcp_app, namespace=loaded.name)
mcp_app = FastMCP("gsc")

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


def _get_semantic_view(db_mode: str, metric: str) -> str:
    """Fully-qualified semantic view reference per engine.

    # AD-4: average_position is non-additive; it MUST be read from the semantic
    # view (semantic_avg_position), never summed from fact_daily_kpi directly.
    # GUARD: non-additive — use semantic view, never SUM from fact table.
    """
    view_name = f"semantic_{metric.lower()}"
    if db_mode == "duckdb":
        from core import warehouse_tenancy  # noqa: PLC0415

        return f"{warehouse_tenancy.mart_prefix(None)}{view_name}"
    dataset = os.environ.get("BQ_MARTS_DATASET", "marts")
    gcp_project = os.environ.get("GCP_PROJECT", "")
    prefix = f"{gcp_project}.{dataset}" if gcp_project else dataset
    return f"{prefix}.{view_name}"


# SQL for additive metrics: SUM from fact_daily_kpi
# User-supplied values NEVER enter the string (F-01).
_MART_QUERY = """
    SELECT
        metric,
        breakdown_dimension,
        breakdown_value,
        SUM(value) AS value,
        MAX(pull_id) AS pull_id,
        MAX(loaded_at) AS freshness
    FROM {table}
    WHERE connector = 'gsc'
      AND metric NOT IN ('average_position')
      AND project_id = {p_project}
      AND date BETWEEN {p_from} AND {p_to}
    GROUP BY metric, breakdown_dimension, breakdown_value
    ORDER BY metric, breakdown_dimension, breakdown_value
"""

# SQL for average_position from the semantic view.
# AD-4: GUARD: non-additive — use semantic view, never SUM from fact table.
_SEMANTIC_AVG_POSITION_QUERY = """
    SELECT
        'average_position' AS metric,
        breakdown_dimension,
        breakdown_value,
        average_position AS value,
        pull_id,
        loaded_at AS freshness
    FROM {semantic_view}
    WHERE project_id = {p_project}
      AND connector = 'gsc'
      AND date BETWEEN {p_from} AND {p_to}
    ORDER BY breakdown_dimension, breakdown_value
"""


def _query_mart(date_from: str, date_to: str, project_id: str = "default") -> list[dict]:
    """Query fact_daily_kpi mart — DuckDB or BigQuery depending on env.

    # AD-12: MCP server reads marts only — never raw_* tables or CSV.
    # AD-4: average_position is non-additive; routed to semantic_avg_position view.
    """
    db_mode = _get_db_mode()
    table = _get_mart_table(db_mode)
    semantic_view = _get_semantic_view(db_mode, "avg_position")

    if db_mode == "duckdb":
        # Additive metrics: clicks, impressions
        sql_additive = _MART_QUERY.format(table=table, p_project="?", p_from="?", p_to="?")
        additive_rows = _query_duckdb(
            sql_additive, [project_id, date_from, date_to], _get_duckdb_path()
        )
        # Non-additive: average_position via semantic view
        # GUARD: non-additive — use semantic view, never SUM average_position.
        sql_semantic = _SEMANTIC_AVG_POSITION_QUERY.format(
            semantic_view=semantic_view, p_project="?", p_from="?", p_to="?"
        )
        try:
            semantic_rows = _query_duckdb(
                sql_semantic, [project_id, date_from, date_to], _get_duckdb_path()
            )
        except Exception:
            # semantic view may not exist yet (before dbt run); return additive only
            semantic_rows = []
        return additive_rows + semantic_rows

    elif db_mode == "bigquery":
        sql_additive = _MART_QUERY.format(
            table=table, p_project="@project_id", p_from="@date_from", p_to="@date_to"
        )
        additive_rows = _query_bigquery(
            sql_additive, {"project_id": project_id, "date_from": date_from, "date_to": date_to}
        )
        sql_semantic = _SEMANTIC_AVG_POSITION_QUERY.format(
            semantic_view=semantic_view,
            p_project="@project_id",
            p_from="@date_from",
            p_to="@date_to",
        )
        try:
            semantic_rows = _query_bigquery(
                sql_semantic,
                {"project_id": project_id, "date_from": date_from, "date_to": date_to},
            )
        except Exception:
            semantic_rows = []
        return additive_rows + semantic_rows
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
            "source_system": "gsc",
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
def get_gsc_report(
    project_id: str = "default",  # AD-14: identity resolved from OAuth 2.1 + PKCE
    report_profile: str = "page_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """GSC Search Analytics Report — reads from fact_daily_kpi mart.

    Report profiles: page_daily, country_daily, device_daily, query_page_daily,
                     discover_daily, google_news_daily, news_daily, image_daily,
                     video_daily, search_appearance_daily
    Metrics: clicks, impressions (additive, AD-4);
             average_position (NON-ADDITIVE — read from semantic_avg_position view)
    Breakdown dimensions: page, country, device, country>device, query>page,
                          search_type, search_type>page, search_appearance

    Returns the canonical AD-1 envelope via structuredContent.
    Text channel (this docstring) is the lean LLM summary (<=30 lines).

    AD-4 non-additive guard: average_position is NEVER summed from fact_daily_kpi.
    It is read from the semantic_avg_position view (impression-weighted aggregate).
    GUARD: must call semantic view, not SUM, for average_position.

    Parameters:
        project_id: Project identifier (default: 'default', AD-14 placeholder)
        report_profile: One of page_daily / country_daily / device_daily
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
# GSC Search Analytics API pull — Story 6.2 (AC3, AC4, AC5)
# ---------------------------------------------------------------------------

# Base URL for GSC Search Analytics API
GSC_API_BASE = "https://searchconsole.googleapis.com/webmasters/v3/sites"

_RAW_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_gsc_daily (
    date                VARCHAR,
    page                VARCHAR,
    country             VARCHAR,
    device              VARCHAR,
    query               VARCHAR,
    search_type         VARCHAR,
    search_appearance   VARCHAR,
    hour                VARCHAR,
    clicks              INTEGER,
    impressions         INTEGER,
    average_position    DOUBLE,
    pull_id             VARCHAR,
    loaded_at           VARCHAR,
    project_id          VARCHAR
)
"""

# Story 10.5 + GSC full coverage: additive column guards for pre-existing
# raw_gsc_daily tables (mirror migrations 027 and 043). All nullable/additive:
#   query             — only query_page_daily lands it (stg filters IS NOT NULL)
#   search_type       — API 'type' (web/image/video/news/discover/googleNews);
#                       NULL on legacy rows == 'web' (staging treats them alike)
#   search_appearance — only search_appearance_daily lands it
#   hour              — only ad-hoc hourly pulls (dataState=hourly_all) land it
_RAW_ADD_COLUMN_DDLS = (
    "ALTER TABLE raw_gsc_daily ADD COLUMN IF NOT EXISTS query VARCHAR",
    "ALTER TABLE raw_gsc_daily ADD COLUMN IF NOT EXISTS search_type VARCHAR",
    "ALTER TABLE raw_gsc_daily ADD COLUMN IF NOT EXISTS search_appearance VARCHAR",
    "ALTER TABLE raw_gsc_daily ADD COLUMN IF NOT EXISTS hour VARCHAR",
)

# GSC Search Analytics 'type' enum — every reporting surface the API exposes.
GSC_SEARCH_TYPES = ("web", "image", "video", "news", "discover", "googleNews")

# Pagination safety cap: 40 pages x 25k rows = 1M rows per pull. The loop
# normally terminates on a short page (len(rows) < rowLimit); the cap only
# guards against a pathological API that keeps returning full pages.
_MAX_PAGES = 40

# AD-4: average_position is stored RAW per row — NOT pre-aggregated.
# Device mapping: GSC returns MOBILE/DESKTOP/TABLET — normalize to lowercase.
_DEVICE_CANONICAL_MAP = {
    "MOBILE": "mobile",
    "DESKTOP": "desktop",
    "TABLET": "tablet",
}

_RAW_INSERT_SQL = """
INSERT INTO raw_gsc_daily
    (date, page, country, device, query, search_type, search_appearance, hour,
     clicks, impressions, average_position, pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _insert_raw_rows(
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    project_id: str,
    db_mode: str,
    duckdb_path: str,
) -> int:
    """Insert canonical rows into raw_gsc_daily (DuckDB only at P3-dev).

    AD-4: average_position stored RAW per row with impressions as weight.
    NEVER aggregate average_position here — the semantic_avg_position view handles it.
    """
    if db_mode == "duckdb":
        from core import warehouse_write  # noqa: PLC0415

        con = warehouse_write.open_raw_writer(duckdb_path, project_id=project_id)
        con.execute(_RAW_CREATE_DDL)
        # Additive column guards for tables created before migrations 027/043
        # (nullable; no-op when the column is already present).
        for ddl in _RAW_ADD_COLUMN_DDLS:
            con.execute(ddl)
        values = [
            (
                r.get("date", ""),
                r.get("page", ""),
                r.get("country", ""),
                r.get("device", ""),
                # query is nullable: only the query_page_daily profile carries it. Blank-safe:
                # None when absent so page/country/device rows land with a NULL query and are
                # excluded by stg_gsc_query_page_daily (query IS NOT NULL).
                (r.get("query") or None),
                # search_type is always stamped by pull() ('web' by default); the
                # staging models route web vs non-web surfaces on this column.
                (r.get("search_type") or None),
                (r.get("search_appearance") or None),
                (r.get("hour") or None),
                int(r.get("clicks", 0) or 0),
                int(r.get("impressions", 0) or 0),
                float(r.get("average_position", 0.0) or 0.0),
                pull_id,
                loaded_at,
                project_id,
            )
            for r in rows
        ]
        if values:
            con.executemany(_RAW_INSERT_SQL, values)
        con.close()
        return len(values)
    else:
        raise ValueError(
            f"_insert_raw_rows: unsupported db_mode {db_mode!r} at P3-dev "
            "(BigQuery path not yet implemented)"
        )


def _parse_gsc_row(api_row: dict, dimensions: list[str]) -> dict:
    """Map one GSC Search Analytics API row to the canonical raw record shape.

    AC4: GSC returns 'position' (not 'average_position') and 'ctr' (computed ratio).
    - position → average_position (transform maps this)
    - ctr is DISCARDED — it is a ratio (clicks/impressions); computed at view time.
    - keys[] contains the dimension values in the same order as the dimensions[] param.

    AD-4: device values from GSC are MOBILE/DESKTOP/TABLET — normalize to lowercase.
    """
    keys = api_row.get("keys", [])
    row: dict = {
        "clicks": api_row.get("clicks", 0),
        "impressions": api_row.get("impressions", 0),
        # GSC field name is 'position', not 'average_position' — transform() renames it.
        "position": api_row.get("position", 0.0),
        # ctr is intentionally NOT stored — AD-4 ratio rule; computed at semantic layer.
    }
    # Unpack dimension keys in the declared order
    for i, dim in enumerate(dimensions):
        if i < len(keys):
            val = keys[i]
            col = dim
            # Device canonical vocabulary: lowercase per canonical dimension dict.
            if dim == "device":
                val = _DEVICE_CANONICAL_MAP.get(str(val).upper(), str(val).lower())
            # Story 10.5: GSC returns the literal string '(not set)' (and occasionally
            # a blank) for missing query/page dimension values. Normalize these to None
            # so they land as NULL and are excluded by stg_gsc_query_page_daily
            # (query IS NOT NULL) rather than forming a fake '(not set)' query bucket.
            elif dim in ("query", "page"):
                sval = str(val).strip()
                val = None if sval in ("", "(not set)") else sval
            # API dimension name -> canonical snake_case raw column.
            elif dim == "searchAppearance":
                col = "search_appearance"
            # 'hour' keys arrive as ISO-8601 timestamps (dataState=hourly_all);
            # stored verbatim in the nullable hour column.
            row[col] = val
    return row


def pull(
    connection_id: str,
    site_url: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    dimensions: list[str] | None = None,
    row_limit: int = 25000,
    dimension_filter: dict | list[dict] | None = None,
    search_type: str = "web",
    data_state: str | None = None,
    aggregation_type: str | None = None,
    static_fields: dict | None = None,
) -> dict:
    """Fetch GSC Search Analytics data and land rows in raw_gsc_daily.

    Full searchanalytics.query coverage: every request-body parameter of the API
    is either exposed here (type, dataState, aggregationType, dimensionFilterGroups)
    or handled internally (startRow pagination until a short page — never a single
    silently-truncated 25k request).

    # AD-12: called by the queue worker only — no synchronous API call during
    # an LLM tool invocation.
    # AD-3: the token is used immediately as a Bearer header then discarded —
    # it is NEVER stored, logged, or passed further.
    # AD-7: pull_id is minted by the caller; this function receives it and never
    # mints its own.
    # AD-4: average_position is stored RAW per row (NOT pre-aggregated).
    #        The semantic_avg_position view applies impression-weighted aggregation.

    Parameters
    ----------
    connection_id:
        Nango connection identifier passed to get_fresh_token.
    site_url:
        GSC property URL (e.g. 'https://example.com/' or 'sc-domain:example.com').
    date_from, date_to:
        ISO-8601 date strings (YYYY-MM-DD) for the GSC search analytics date range.
    project_id:
        Project binding for the raw rows.
    pull_id:
        Core-minted pull ULID (AD-7). Passed in; not generated here.
    dimensions:
        List of GSC dimension strings among: date, query, page, country, device,
        searchAppearance, hour. Defaults to ['page']. Profile shims pin these —
        ALWAYS lead with 'date' for daily profiles (keys map positionally; a
        missing date collapses the window into one degenerate bucket).
    row_limit:
        Rows per API page. Default 25,000 (GSC per-request maximum). Pagination
        continues past it via startRow until the API returns a short page.
    dimension_filter:
        One filter dict or a list of filter dicts. All filters land in a single
        dimensionFilterGroups group (AND semantics — the only groupType the API
        supports). Operators: equals, notEquals, contains, notContains,
        includingRegex, excludingRegex (RE2).
    search_type:
        GSC 'type' parameter — one of GSC_SEARCH_TYPES (web, image, video, news,
        discover, googleNews). Sent explicitly and stamped on every raw row
        (search_type column) so surfaces never collide in staging.
    data_state:
        Optional GSC 'dataState': 'final' (API default), 'all' (include fresh
        partial data), or 'hourly_all' (required when grouping by hour).
    aggregation_type:
        Optional GSC 'aggregationType': auto (API default), byProperty, byPage,
        byNewsShowcasePanel. byProperty is not supported for discover/googleNews.
    static_fields:
        Optional constant fields merged into every landed row. Used by the
        search_appearance_daily shim to stamp the date on single-day pulls
        (the API forbids grouping searchAppearance with other dimensions).

    Returns
    -------
    dict
        {"pull_id", "row_count", "date_from", "date_to", "pages", "truncated",
         "metadata"} — metadata carries first_incomplete_date/first_incomplete_hour
        when dataState surfaces fresh data.

    Raises
    ------
    RuntimeError
        If the GSC Search Analytics API returns a non-200 (non-429) response.
    RateLimitError
        If the GSC API returns HTTP 429 (AC5).
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    if dimensions is None:
        dimensions = ["page"]

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    # AD-3: token obtained immediately before use; falls out of scope after the call.
    token = nango_client.get_fresh_token(connection_id, provider="gsc")

    # URL-encode the site_url for the API path
    site_url_encoded = quote(site_url, safe="")
    url = f"{GSC_API_BASE}/{site_url_encoded}/searchAnalytics/query"

    body: dict = {
        "startDate": date_from,
        "endDate": date_to,
        "dimensions": dimensions,
        "type": search_type,
        "rowLimit": row_limit,
        "startRow": 0,
    }
    if data_state:
        body["dataState"] = data_state
    if aggregation_type:
        body["aggregationType"] = aggregation_type
    if dimension_filter:
        filters = (
            dimension_filter if isinstance(dimension_filter, list) else [dimension_filter]
        )
        body["dimensionFilterGroups"] = [{"filters": filters}]

    # Pagination: the API caps each response at rowLimit (max 25k) and signals
    # nothing on truncation — completeness requires looping startRow until the
    # API returns fewer rows than requested (short page).
    api_rows: list[dict] = []
    metadata: dict = {}
    pages = 0
    truncated = False
    while True:
        resp = httpx.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )

        if resp.status_code == 429:
            # AC5: raise RateLimitError so the worker trips the breaker.
            # No partial batch lands: rows are inserted only after all pages succeed,
            # so the retried pull re-reads from page 0 without duplicates.
            from core.quota import RateLimitError  # noqa: PLC0415

            retry_after_raw = resp.headers.get("Retry-After", "0")
            try:
                retry_after = int(retry_after_raw) or None
            except (ValueError, TypeError):
                retry_after = None
            raise RateLimitError("gsc", retry_after)

        if resp.status_code != 200:
            # Story 25.2: canonical typed error; provider payload preserved.
            from core.pull_errors import classify_http_error  # noqa: PLC0415

            try:
                _body = resp.json()
            except Exception:
                _body = resp.text
            raise classify_http_error(resp.status_code, _body)

        payload = resp.json()
        page_rows = payload.get("rows") or []
        if payload.get("metadata"):
            metadata = payload["metadata"]
        api_rows.extend(page_rows)
        pages += 1

        if len(page_rows) < row_limit:
            break
        if pages >= _MAX_PAGES:
            truncated = True
            logger.warning(
                "gsc_pull_truncated: pull_id=%s hit _MAX_PAGES=%d (%d rows)",
                pull_id, _MAX_PAGES, len(api_rows),
            )
            break
        body["startRow"] += len(page_rows)

    # Parse API rows into canonical form (position → to be renamed by transform)
    raw_rows: list[dict] = [_parse_gsc_row(r, dimensions) for r in api_rows]

    # Every raw row is stamped with its search surface so web/image/video/news/
    # discover/googleNews rows never collide in the staging grain.
    for row in raw_rows:
        row["search_type"] = search_type
        if static_fields:
            row.update(static_fields)

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")

    # Apply transform() to rename position → average_position and drop ctr
    canonical_rows = transform(raw_rows)

    row_count = _insert_raw_rows(
        canonical_rows, pull_id, loaded_at, project_id, db_mode, duckdb_path
    )

    # AD-3: no token in log — only pull_id and row_count (safe metadata).
    logger.info(
        "gsc_pull_completed: pull_id=%s row_count=%d pages=%d type=%s",
        pull_id, row_count, pages, search_type,
    )

    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
        "pages": pages,
        "truncated": truncated,
        "metadata": metadata,
    }


def _resolve_site_url(site_url: str | None, profile: str) -> str:
    """Resolve the GSC property URL: explicit arg wins, else GSC_SITE_URL env.

    The queue dispatch passes only (connection_id, date_from, date_to, project_id,
    pull_id) by keyword — it never passes site_url — so every GSC profile shim
    relies on this env fallback (mirrors GA4's property_id <- GA4_PROPERTY_ID).
    """
    if site_url is None:
        site_url = os.environ.get("GSC_SITE_URL")
    if not site_url:
        raise ValueError(
            f"GSC_SITE_URL env var required for {profile} "
            "(set it to your GSC property URL, e.g. 'sc-domain:example.com')"
        )
    return site_url


def _make_profile_pull(profile_id: str, dimensions: list[str], search_type: str = "web"):
    """Build a ``pull_<profile_id>`` shim pinning dimensions + search type.

    review-epic-10 CRITICAL-B generalized: the profile-aware dispatch
    (main.get_module_pull_fn -> ``pull_<profile_id>``) falls back to the default
    ``pull()`` with ``dimensions=['page']`` when no shim exists — which drops the
    date key and collapses the whole window into one degenerate date bucket
    (review-10-6 F-1). Every declared profile therefore gets an explicit shim.
    'date' MUST lead the dimensions: GSC only returns keys that are requested and
    ``_parse_gsc_row`` maps them positionally.

    Signature mirrors the queue dispatch contract (queue.py passes
    ``connection_id, date_from, date_to, project_id, pull_id`` by keyword).
    """

    def _profile_pull(
        connection_id: str,
        date_from: str,
        date_to: str,
        project_id: str,
        pull_id: str,
        site_url: str | None = None,
        row_limit: int = 25000,
    ) -> dict:
        return pull(
            connection_id=connection_id,
            site_url=_resolve_site_url(site_url, f"pull_{profile_id}"),
            date_from=date_from,
            date_to=date_to,
            project_id=project_id,
            pull_id=pull_id,
            dimensions=dimensions,
            row_limit=row_limit,
            search_type=search_type,
        )

    _profile_pull.__name__ = f"pull_{profile_id}"
    _profile_pull.__qualname__ = f"pull_{profile_id}"
    _profile_pull.__doc__ = (
        f"Profile pull for the ``{profile_id}`` GSC report profile.\n\n"
        f"Pins dimensions={dimensions} and type='{search_type}' at the 25k page size\n"
        f"(full startRow pagination handled by pull()). Returns the same dict shape\n"
        f"as ``pull``."
    )
    return _profile_pull


# Web search — core daily profiles (grain led by 'date', review-10-6 F-1).
pull_page_daily = _make_profile_pull("page_daily", ["date", "page"])
pull_country_daily = _make_profile_pull("country_daily", ["date", "country"])
pull_device_daily = _make_profile_pull("device_daily", ["date", "device"])
# Story 10.5: joint (query, page) grain for cannibalisation detection.
pull_query_page_daily = _make_profile_pull("query_page_daily", ["date", "query", "page"])
# Non-web search surfaces — the GSC 'type' parameter is the ONLY way to reach
# Discover / Google News / image / video / news reporting data.
# dims are (date, page): query is not populated for discover/news surfaces and
# device support varies — page is the one universally-supported breakdown.
pull_discover_daily = _make_profile_pull(
    "discover_daily", ["date", "page"], search_type="discover"
)
pull_google_news_daily = _make_profile_pull(
    "google_news_daily", ["date", "page"], search_type="googleNews"
)
pull_news_daily = _make_profile_pull("news_daily", ["date", "page"], search_type="news")
pull_image_daily = _make_profile_pull("image_daily", ["date", "page"], search_type="image")
pull_video_daily = _make_profile_pull("video_daily", ["date", "page"], search_type="video")


def pull_search_appearance_daily(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    site_url: str | None = None,
    row_limit: int = 25000,
) -> dict:
    """Profile pull for the ``search_appearance_daily`` GSC report profile.

    The API forbids grouping ``searchAppearance`` with any other dimension, so the
    daily grain is reconstructed by looping one single-day query per date in the
    window (dimensions=['searchAppearance'], startDate=endDate=day) and stamping
    the date on every landed row via ``static_fields``. One API request per day
    (plus pagination) — bounded by the daily cadence window.

    Returns the same dict shape as ``pull`` (row_count/pages summed over days).
    """
    from datetime import date as date_cls, timedelta  # noqa: PLC0415

    site_url = _resolve_site_url(site_url, "pull_search_appearance_daily")
    total_rows = 0
    total_pages = 0
    truncated = False
    day = date_cls.fromisoformat(date_from)
    end = date_cls.fromisoformat(date_to)
    while day <= end:
        day_str = day.isoformat()
        result = pull(
            connection_id=connection_id,
            site_url=site_url,
            date_from=day_str,
            date_to=day_str,
            project_id=project_id,
            pull_id=pull_id,
            dimensions=["searchAppearance"],
            row_limit=row_limit,
            static_fields={"date": day_str},
        )
        total_rows += result["row_count"]
        total_pages += result["pages"]
        truncated = truncated or result["truncated"]
        day += timedelta(days=1)

    return {
        "pull_id": pull_id,
        "row_count": total_rows,
        "date_from": date_from,
        "date_to": date_to,
        "pages": total_pages,
        "truncated": truncated,
        "metadata": {},
    }


# ---------------------------------------------------------------------------
# transform() — manifest-driven canonical field mapping (AC3, AC4)
# ---------------------------------------------------------------------------


def transform(raw_rows: list[dict]) -> list[dict]:
    """Map raw GSC source fields to canonical names using manifest mappings.

    # AD-4: canonical field mapping driven by manifest — do not hardcode source names.
    Key mappings:
      - position → average_position (GSC API field name → canonical name)
      - clicks → clicks (identity)
      - impressions → impressions (identity)
      - ctr is DISCARDED (ratio metric — computed at semantic layer; never stored)

    The manifest's canonical_metric_mapping for 'average_position' is a nested
    object ({"canonical": "average_position", "aggregation_rule": "impression_weighted",
    "non_additive": true}). The GSC API returns the field as 'position' — we build
    a source-to-canonical rename map that handles both string and object values.

    AD-4: average_position is stored raw (per-row) — NOT aggregated here.
    The semantic_avg_position view applies impression-weighted aggregation.
    """
    _manifest_path = Path(__file__).parent / "manifest.json"
    _manifest = json.loads(_manifest_path.read_text(encoding="utf-8"))

    # Build unified rename map: source_name -> canonical_name
    # Handle both string values and nested aggregation-rule objects.
    rename_map: dict[str, str] = {}
    for src, val in _manifest.get("canonical_metric_mapping", {}).items():
        if isinstance(val, str):
            rename_map[src] = val
        elif isinstance(val, dict):
            # Nested form: {"canonical": "average_position", "aggregation_rule": ...}
            rename_map[src] = val.get("canonical", src)

    rename_map.update(_manifest.get("canonical_dimension_mapping", {}))

    # GSC API-specific: 'position' is the API field name for 'average_position'.
    # The manifest maps 'average_position' (canonical) but GSC returns 'position'.
    # Explicitly add the API-field → canonical mapping.
    rename_map["position"] = "average_position"

    # Fields to drop: ctr is a ratio — NEVER stored (AD-4).
    _DROP_FIELDS = {"ctr"}

    result: list[dict] = []
    for row in raw_rows:
        canonical_row: dict = {}
        for key, value in row.items():
            if key in _DROP_FIELDS:
                continue  # Drop ctr and other ratio metrics
            canonical_row[rename_map.get(key, key)] = value
        result.append(canonical_row)
    return result


# ---------------------------------------------------------------------------
# Story 25.7: account topology discovery (playbook section 5).
#
# GSC topology is a single flat level: the token's reachable sites (GSC
# properties). The Sites list endpoint enumerates what the token can reach;
# core owns selection, access-check, trial extraction, and backfill.
#
# DEPRECATION NOTE: The existing GSC_SITE_URL env-var pattern (used by
# _resolve_site_url) is superseded by the core topology flow once the core
# account_topology.resolve_selected_account is wired at rollout. Until then
# the env-var fallback remains functional so existing pull schedules keep
# working unchanged. Do not remove _resolve_site_url until core topology is live.
#
# AD-3: token obtained immediately before use via nango_client.get_fresh_token;
# falls out of scope after the HTTP call. Never stored or logged.
# ---------------------------------------------------------------------------

# GSC Sites list endpoint
_GSC_SITES_URL = "https://www.googleapis.com/webmasters/v3/sites"


def discover_accounts(connection_id: str) -> list[dict]:
    """List the GSC sites (properties) the connection's token can reach.

    Story 25.7 (AC4). Calls the Webmasters API sites.list endpoint and returns
    the generic flat-level list core's topology flow consumes:

        [{"id": "https://example.com/", "label": "https://example.com/"}, ...]

    For domain properties the siteUrl value is 'sc-domain:<domain>' — the id
    and label are both set to the raw siteUrl so core stores and hands it back
    verbatim at pull time.

    The selection_level is 'site' (single level, no parent hierarchy). Core
    uses the returned list to present a site-selection step during onboarding.

    Raises:
        core.quota.RateLimitError on 429 (breaker path, unchanged contract).
        A typed core.pull_errors.ConnectorError on any other non-2xx.
        401 -> AuthExpiredError (pure-HTTP taxonomy, no error_map needed).

    AD-3: the token is used immediately as a Bearer header, never stored or logged.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    # AD-3: token obtained immediately before use; falls out of scope after call.
    token = nango_client.get_fresh_token(connection_id, provider="gsc")

    resp = httpx.get(
        _GSC_SITES_URL,
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
        raise RateLimitError("gsc", retry_after)

    if resp.status_code != 200:
        from core.pull_errors import classify_http_error  # noqa: PLC0415

        try:
            _body = resp.json()
        except Exception:
            _body = resp.text
        raise classify_http_error(resp.status_code, _body)

    payload = resp.json()
    site_entries = payload.get("siteEntry") or []

    # AD-3: no token in log — only safe counts.
    logger.info("gsc_discover_accounts: sites=%d", len(site_entries))

    return [
        {"id": entry["siteUrl"], "label": entry["siteUrl"]}
        for entry in site_entries
        if entry.get("siteUrl")
    ]


# ---------------------------------------------------------------------------
# Story 25.9 — catalog_driven execution: pull_catalog_daily.
#
# GSC strategy: PROJECTION.
# The GSC searchanalytics.query API always returns all 4 response metrics
# (clicks, impressions, ctr, position) regardless of the dimensions list.
# There is no "fields" selector on the metric side. The selection therefore
# controls:
#   - DIMENSIONS: built from selection.dimensions (date always leads; 'hour'
#     is excluded from catalog_daily; 'search_type' maps to the API 'type'
#     param and is NOT added to the dimensions list).
#   - METRIC PROJECTION: after the pull, non-selected metrics are zeroed in
#     the landed rows via a DuckDB UPDATE on pull_id. ctr is never stored (AD-4).
#   - searchAppearance ALONE → per-day loop (reuses pull() path + static_fields
#     date stamp); searchAppearance + any other grouping dim → InvalidRequestError
#     BEFORE any API call.
#
# Default selection (selection=None): tier-core default resolved from api_catalog.json
# via catalog_contract.catalog_default_selection.
# ---------------------------------------------------------------------------

# All 4 GSC response-metric field_ids (ctr is excluded from storage but returned by API).
_GSC_ALL_RESPONSE_METRICS = ("clicks", "impressions", "average_position")

# The dimension that maps to the API 'type' param (not in the dimensions list).
_GSC_SEARCH_TYPE_FIELD = "search_type"

# Dimensions excluded from catalog_daily (honor dimension_compatibility.excluded_from_catalog_daily).
_GSC_EXCLUDED_FROM_CATALOG_DAILY = {"hour"}


def _load_catalog_sources_gsc() -> dict:
    """Load and return catalog_sources/catalog_sources.json (cached per process)."""
    try:
        return _GSC_CATALOG_SOURCES  # type: ignore[name-defined]
    except NameError:
        pass
    path = Path(__file__).parent / "catalog_sources" / "catalog_sources.json"
    globals()["_GSC_CATALOG_SOURCES"] = json.loads(path.read_text(encoding="utf-8"))
    return globals()["_GSC_CATALOG_SOURCES"]


def _load_api_catalog_gsc() -> dict:
    """Load and return api_catalog.json (cached per process)."""
    try:
        return _GSC_API_CATALOG  # type: ignore[name-defined]
    except NameError:
        pass
    path = Path(__file__).parent / "api_catalog.json"
    globals()["_GSC_API_CATALOG"] = json.loads(path.read_text(encoding="utf-8"))
    return globals()["_GSC_API_CATALOG"]


def _build_gsc_catalog_request(
    selection: dict,
) -> tuple[list[str], str, bool]:
    """Translate a validated *selection* into the GSC request shape.

    Returns:
        (api_dimensions, search_type, is_search_appearance_only)

    - api_dimensions: list of GSC API dimension strings to send ('date' always leads).
      'search_type' is excluded (maps to 'type' param). 'search_appearance' maps
      to 'searchAppearance' in the API.
    - search_type: the value for the API 'type' param (always 'web' for catalog_daily).
    - is_search_appearance_only: True when the only grouping dim is searchAppearance
      (triggers the per-day loop path).

    Raises:
        core.pull_errors.InvalidRequestError:
            - If 'hour' is in the selected dimensions (excluded from catalog_daily).
            - If 'search_appearance' is combined with any other grouping dimension
              (the API forbids it; standalone_only constraint).
    """
    from core.pull_errors import InvalidRequestError  # noqa: PLC0415

    selected_dims = list(selection.get("dimensions", []) or [])

    # Check for excluded dimensions (hour)
    excluded = _GSC_EXCLUDED_FROM_CATALOG_DAILY & set(selected_dims)
    if excluded:
        raise InvalidRequestError(
            f"Dimension(s) {sorted(excluded)} are excluded from catalog_daily "
            "(requires special dataState; ad-hoc only). Remove from selection."
        )

    # Load dimension_compatibility block to enforce standalone_only.
    compat = _load_catalog_sources_gsc().get("dimension_compatibility", {})
    standalone_only = set(compat.get("standalone_only", []))

    # Grouping dims = selected dims minus search_type (which is a request param, not a dim).
    grouping_dims = [d for d in selected_dims if d != _GSC_SEARCH_TYPE_FIELD]

    # Check searchAppearance standalone constraint.
    sa_dims = [d for d in grouping_dims if d in standalone_only]
    if sa_dims:
        other_dims = [d for d in grouping_dims if d not in standalone_only and d != "date"]
        if other_dims:
            raise InvalidRequestError(
                f"dimension 'search_appearance' (standalone_only) cannot be combined "
                f"with other grouping dimensions {other_dims}. Use standalone or a "
                f"different profile."
            )

    # Build the API dimensions list: 'date' always first, 'searchAppearance' maps to API name.
    _DIM_API_MAP = {
        "search_appearance": "searchAppearance",
    }
    api_dims_ordered: list[str] = []
    if "date" not in grouping_dims:
        api_dims_ordered.append("date")
    for d in grouping_dims:
        api_dims_ordered.append(_DIM_API_MAP.get(d, d))

    # Ensure 'date' leads.
    if api_dims_ordered and api_dims_ordered[0] != "date":
        if "date" in api_dims_ordered:
            api_dims_ordered.remove("date")
        api_dims_ordered.insert(0, "date")

    # searchAppearance alone (no other grouping dim beside possibly 'date').
    non_date_grouping = [d for d in grouping_dims if d != "date"]
    is_search_appearance_only = (
        len(non_date_grouping) == 1 and non_date_grouping[0] == "search_appearance"
    )

    # For searchAppearance-alone path: the API dims sent per day = ["searchAppearance"].
    if is_search_appearance_only:
        api_dims_ordered = ["searchAppearance"]

    return api_dims_ordered, "web", is_search_appearance_only


def _project_gsc_metrics(
    pull_id: str,
    selected_metrics: list[str],
    duckdb_path: str,
) -> None:
    """Zero out non-selected metrics in raw_gsc_daily for this pull_id (DuckDB only).

    The GSC API always returns all metrics (clicks, impressions, position/average_position).
    When the selection requests only a subset, we land all values then zero the rest.
    ctr is never stored (AD-4), so it is excluded from this projection.

    Zeroed fields: average_position (0.0), impressions (0), clicks (0) when not selected.
    """
    import duckdb  # noqa: PLC0415

    selected_set = set(selected_metrics)
    updates: list[str] = []
    if "clicks" not in selected_set:
        updates.append("clicks = 0")
    if "impressions" not in selected_set:
        updates.append("impressions = 0")
    if "average_position" not in selected_set:
        updates.append("average_position = 0.0")

    if not updates:
        return  # all metrics selected — no zeroing needed

    sql = (
        f"UPDATE raw_gsc_daily SET {', '.join(updates)} WHERE pull_id = ?"
    )
    con = duckdb.connect(duckdb_path)
    try:
        con.execute(sql, [pull_id])
    finally:
        con.close()


def pull_catalog_daily(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    selection: dict | None = None,
    site_url: str | None = None,
    row_limit: int = 25000,
) -> dict:
    """Catalog-driven daily pull for GSC Search Analytics (Story 25.9).

    The catalog IS the execution contract: any non-excluded GSC field selected in
    a datastream is extracted. Dispatched via manifest profile 'catalog_daily'
    (selection_mode=catalog_driven).

    DIMENSION SELECTION builds the API dimensions list from selection.dimensions:
      - 'date' always leads (prepended if absent).
      - 'search_type' maps to the API 'type' param (always 'web'); not in dims list.
      - 'hour' raises InvalidRequestError (excluded from catalog_daily).
      - 'search_appearance' alone → per-day loop (same path as pull_search_appearance_daily).
      - 'search_appearance' + other grouping dim → InvalidRequestError BEFORE API call.

    METRIC PROJECTION: GSC always returns all 4 metrics. Non-selected metrics are
    zeroed post-pull via DuckDB UPDATE on pull_id. ctr is never stored (AD-4).

    DEFAULT SELECTION (selection=None): tier-core default resolved from api_catalog.json
    via catalog_contract.catalog_default_selection.

    Parameters match the queue dispatch contract:
        connection_id, date_from, date_to, project_id, pull_id (required),
        selection (optional; None = tier-core default),
        site_url (optional; falls back to GSC_SITE_URL env var),
        row_limit (optional; default 25000).
    """
    from core import catalog_contract  # noqa: PLC0415

    # Resolve tier-core default when no explicit selection is provided.
    if selection is None:
        catalog = _load_api_catalog_gsc()
        default_sel = catalog_contract.catalog_default_selection(catalog)
        # Build source_fields map for the default selection.
        catalog_fields = {
            f["field_id"]: f.get("source_field", f["field_id"])
            for f in catalog.get("fields", [])
            if f.get("field_id") and f.get("exposure") != "excluded"
        }
        source_fields = {
            fid: catalog_fields[fid]
            for fid in (default_sel["metrics"] + default_sel["dimensions"])
            if fid in catalog_fields
        }
        selection = {
            "metrics": default_sel["metrics"],
            "dimensions": default_sel["dimensions"],
            "source_fields": source_fields,
        }

    site_url = _resolve_site_url(site_url, "pull_catalog_daily")
    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    selected_metrics = list(selection.get("metrics", []) or [])
    api_dims, search_type, is_sa_only = _build_gsc_catalog_request(selection)

    if is_sa_only:
        # searchAppearance-alone path: per-day loop reusing pull().
        from datetime import date as date_cls, timedelta  # noqa: PLC0415

        total_rows = 0
        total_pages = 0
        truncated = False
        day = date_cls.fromisoformat(date_from)
        end = date_cls.fromisoformat(date_to)
        while day <= end:
            day_str = day.isoformat()
            result = pull(
                connection_id=connection_id,
                site_url=site_url,
                date_from=day_str,
                date_to=day_str,
                project_id=project_id,
                pull_id=pull_id,
                dimensions=["searchAppearance"],
                row_limit=row_limit,
                search_type=search_type,
                static_fields={"date": day_str},
            )
            total_rows += result["row_count"]
            total_pages += result["pages"]
            truncated = truncated or result["truncated"]
            day += timedelta(days=1)

        # Apply metric projection post-loop.
        if db_mode == "duckdb":
            _project_gsc_metrics(pull_id, selected_metrics, duckdb_path)

        return {
            "pull_id": pull_id,
            "row_count": total_rows,
            "date_from": date_from,
            "date_to": date_to,
            "pages": total_pages,
            "truncated": truncated,
            "metadata": {},
        }

    # Standard path: single pull() call with the built api_dims.
    result = pull(
        connection_id=connection_id,
        site_url=site_url,
        date_from=date_from,
        date_to=date_to,
        project_id=project_id,
        pull_id=pull_id,
        dimensions=api_dims,
        row_limit=row_limit,
        search_type=search_type,
    )

    # Apply metric projection (zero non-selected metrics in landed rows).
    if db_mode == "duckdb":
        _project_gsc_metrics(pull_id, selected_metrics, duckdb_path)

    return result


# ---------------------------------------------------------------------------
# Story 6.2: register this module's raw table name with core.verification.
# module->core direction is allowed by AD-2 (only core->modules is forbidden).
# The raw table name never appears in core source code (AD-2).
# ---------------------------------------------------------------------------
try:
    from core.verification import register_raw_table_name as _register_raw  # noqa: PLC0415

    _register_raw("raw_gsc_daily", provider="gsc")
except Exception:
    pass  # best-effort; verification will log a warning if table name is missing
