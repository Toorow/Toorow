"""WooCommerce connector -- epic-25 (self-hosted commerce, sales source of record).

Store-scoped e-commerce sales source, the self-hosted sibling of shopify. Exposes
a module-level ``mcp_app: FastMCP`` the core loader mounts under the ``woocommerce``
namespace (AD-2): same "drop-a-folder" pattern as shopify/meta-ads/gsc, one additive
UNION block in the mart.

# AD-12: the MCP server reads ONLY the fact_daily_kpi mart -- no raw_*, no CSV.
# AD-3: credentials come from Nango just before use -- never stored or logged.
#        WooCommerce uses HTTP Basic auth (consumer key/secret), NOT OAuth, so the
#        connector resolves them via nango_client.get_basic_credentials (epic-25
#        Basic-auth path), NOT get_fresh_token.
# AD-7: pull_id minted by the core scheduler, passed into pull().
# AD-14: identity/project_id parameter.

Story decisions (Jean, 2026-07-21):
  * SELF-HOSTED, STORE-SCOPED: one consumer key/secret = one store, identified by
    its own base URL. Base URL + Basic-auth secrets both come from the connection's
    Nango credential (connection_config.base_url) -- NEVER an env var (doctrine
    epic-25 point 5). HTTPS is mandatory (no OAuth 1.0a signer for HTTP stores).
  * SALES OF RECORD = status in {completed, processing} ONLY. on-hold / pending /
    failed / cancelled / trash are never landed as revenue.
  * REFUNDS: WooCommerce reports refund totals as NEGATIVE amounts. The connector
    takes abs() EXPLICITLY and stores refund_amount in a DEDICATED positive column,
    NEVER subtracted silently from revenue (decision REFERENCE shopify 15.4 / stripe
    15.7). The net (revenue - refund_amount) is computed explicitly downstream.
  * transaction_id = order.transaction_id, OFTEN EMPTY on manual payments -> exposed
    as a nullable DETAIL dimension (GA4 x WooCommerce join key, Epic 17), measured by
    a >=X% presence test rather than assumed present.
  * /reports/sales CROSS-CHECK: /orders is the SOURCE OF TRUTH; /reports/sales is
    fetched best-effort as a consistency alert (never landed), logging a warning when
    the daily revenue delta exceeds a threshold.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Module-level FastMCP instance -- the public surface the loader mounts.
mcp_app = FastMCP("woocommerce")

WC_API_PATH = "/wp-json/wc/v3"

# Sales of record: only these order statuses count as a sale (Jean 2026-07-21).
_SALE_STATUSES = ["completed", "processing"]

# WooCommerce REST caps per_page at 100 (WordPress REST convention).
_PAGE_SIZE = 100

# review-15-4 F-2 (inherited): bounded pagination -- never hang on a broken/looping
# Link cursor. 100 orders/page x 1000 pages = 100k orders/window, far above any real pull.
_MAX_PAGES = 1000

# Cross-check tolerance: warn when |landed_revenue - reports_sales| / reports_sales
# exceeds this fraction. Best-effort only; never fails the pull.
_CROSSCHECK_TOLERANCE = 0.01

_ERROR_MAP: dict[str, str] | None = None


def _load_error_map() -> dict[str, str]:
    """Return the manifest's ``error_map`` (cached)."""
    global _ERROR_MAP
    if _ERROR_MAP is None:
        manifest_path = Path(__file__).parent / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        _ERROR_MAP = manifest.get("error_map") or {}
    return _ERROR_MAP


# ---------------------------------------------------------------------------
# Database helpers -- env-var driven (identical dual-backend pattern to shopify).
# ---------------------------------------------------------------------------

_DEFAULT_DUCKDB_PATH = os.path.join(os.path.dirname(__file__), "seeds", "local.duckdb")


def _get_db_mode() -> str:
    return os.environ.get("TOOROW_DB_MODE", "duckdb")


def _get_duckdb_path() -> str:
    return os.environ.get("TOOROW_DUCKDB_PATH", _DEFAULT_DUCKDB_PATH)


def _query_duckdb(sql: str, params: list, duckdb_path: str) -> list[dict]:
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path, read_only=True)
    try:
        rel = con.execute(sql, params)
        cols = [d[0] for d in rel.description]
        return [dict(zip(cols, row)) for row in rel.fetchall()]
    finally:
        con.close()


def _query_bigquery(sql: str, params: dict) -> list[dict]:
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
    WHERE connector = 'woocommerce'
      AND project_id = {p_project}
      AND date BETWEEN {p_from} AND {p_to}
    GROUP BY metric, breakdown_dimension, breakdown_value
    ORDER BY metric, breakdown_dimension, breakdown_value
"""


def _query_mart(date_from: str, date_to: str, project_id: str = "default") -> list[dict]:
    """Query fact_daily_kpi mart -- DuckDB or BigQuery (AD-12: marts only)."""
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
        data_by_metric.setdefault(r["metric"], []).append(
            {
                "breakdown_dimension": r["breakdown_dimension"],
                "breakdown_value": r["breakdown_value"],
                "value": r["value"],
            }
        )

    provenance = (
        {
            "source_system": "woocommerce",
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
def get_woocommerce_report(
    project_id: str = "default",
    report_profile: str = "orders_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """WooCommerce Orders Report -- sales source of record -- reads fact_daily_kpi.

    Report profiles: orders_daily
    Metrics: revenue, refund_amount, orders_count (additive at day grain, AD-4).
        refund_amount is a DEDICATED positive column -- NEVER subtracted silently
        from revenue; a net figure is computed explicitly downstream.
    Breakdown: day-total only. transaction_id is a raw detail dimension (GA4 x
        WooCommerce join key, Epic 17), NOT a mart partition.

    Returns the canonical AD-1 envelope via structuredContent.

    Parameters:
        project_id: Project identifier (default: 'default', AD-14 placeholder).
        report_profile: orders_daily.
        date_from: Start date ISO-8601. Defaults to 90 days ago.
        date_to: End date ISO-8601. Defaults to yesterday.
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
# WooCommerce REST API pull
# ---------------------------------------------------------------------------


def _require_https(base_url: str) -> None:
    """Reject non-HTTPS store URLs (Jean 2026-07-21: no OAuth 1.0a signer at v1)."""
    if not base_url.lower().startswith("https://"):
        from core.pull_errors import InvalidRequestError  # noqa: PLC0415

        raise InvalidRequestError(
            "woocommerce requires an HTTPS store URL "
            "(HTTP / OAuth 1.0a stores are not supported); got a non-https base_url"
        )


def _resolve_store(connection_id: str, store_url: str | None):
    """Resolve (base_url, auth) from the connection's Nango Basic credential.

    AD-3: the consumer key/secret come from Nango just before use and fall out of
    scope after the request. The base URL lives in connection_config -- NEVER an
    env var (doctrine epic-25 point 5). ``store_url`` is a test/standalone override.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    creds = nango_client.get_basic_credentials(connection_id, provider="woocommerce")
    base_url = store_url or creds.connection_config.get("base_url")
    if not base_url:
        from core.pull_errors import InvalidRequestError  # noqa: PLC0415

        raise InvalidRequestError(
            "woocommerce connection has no base_url in connection_config "
            "(the store URL must be stored on the Nango connection, not an env var)"
        )
    base_url = base_url.rstrip("/")
    _require_https(base_url)
    return base_url, (creds.username, creds.password)


def _orders_endpoint(base_url: str) -> str:
    return f"{base_url}{WC_API_PATH}/orders"


# WordPress REST Link-header pagination: rel="next" carries the full next-page URL
# (all filters preserved). Follow it until absent -- same mechanism as shopify.
_LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


def _next_page_url(link_header: str | None) -> str | None:
    if not link_header:
        return None
    m = _LINK_NEXT_RE.search(link_header)
    return m.group(1) if m else None


_RAW_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_woocommerce_orders (
    date              VARCHAR,
    order_id          VARCHAR,
    transaction_id    VARCHAR,
    revenue           DOUBLE,
    refund_amount     DOUBLE,
    orders_count      INTEGER,
    revenue_source_currency VARCHAR DEFAULT 'EUR',
    pull_id           VARCHAR,
    loaded_at         VARCHAR,
    project_id        VARCHAR
)
"""

_RAW_INSERT_SQL = """
INSERT INTO raw_woocommerce_orders
    (date, order_id, transaction_id, revenue, refund_amount, orders_count,
     revenue_source_currency, pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _insert_raw_rows(
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    project_id: str,
    db_mode: str,
    duckdb_path: str,
) -> int:
    """Insert canonical rows into raw_woocommerce_orders (DuckDB at P-dev).

    # refund_amount is stored in its OWN positive column. It is NEVER subtracted
    # from revenue here (decision REFERENCE shopify 15.4 / stripe 15.7).
    """
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
            r.get("order_id", ""),
            (r.get("transaction_id") or None),
            float(r.get("revenue", 0) or 0),
            float(r.get("refund_amount", 0) or 0),
            int(r.get("orders_count", 1) or 0),
            r.get("revenue_source_currency", "EUR"),
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


def _extract_refund_amount(api_order: dict) -> float:
    """Positive refund amount for one WooCommerce order.

    WooCommerce embeds refunds as ``refunds[]`` where each entry's ``total`` is a
    NEGATIVE string (e.g. "-42.18"). We take abs() of each and sum. The result is a
    DEDICATED positive figure -- never netted against revenue.
    """
    total = 0.0
    for refund in api_order.get("refunds") or []:
        raw = refund.get("total")
        if raw in (None, ""):
            continue
        try:
            total += abs(float(raw))
        except (ValueError, TypeError):
            continue
    return round(total, 10)


def _parse_order(api_order: dict) -> dict:
    """Map one WooCommerce order to the canonical raw record shape.

    Source field names (WooCommerce REST v3 Order):
      - date_created -> date (day grain, site-tz date portion)
      - id -> order_id
      - transaction_id -> transaction_id (nullable detail dim, GA4 join key)
      - total -> revenue source (WooCommerce returns amounts as STRINGS)
      - refunds[].total -> refund_amount (abs sum, DEDICATED positive column)
      - currency -> revenue_source_currency
      - orders_count = 1 per order

    review F-1 (inherited): parse is STRUCTURAL extraction only -- the metric field
    keeps its SOURCE name ('total'); the manifest-driven transform() renames it to
    canonical ('revenue'), so the rename map is exercised on every pull.
    """
    created = api_order.get("date_created") or api_order.get("date_created_gmt") or ""
    date_str = created[:10] if created else ""
    txn = api_order.get("transaction_id")
    return {
        "date": date_str,
        "order_id": str(api_order.get("id", "")),
        "transaction_id": (txn or None),
        "total": api_order.get("total", 0),
        "total_refunded": _extract_refund_amount(api_order),
        "orders_count": 1,
        "revenue_source_currency": api_order.get("currency", "EUR") or "EUR",
    }


def _handle_non_200(resp: "httpx.Response", pull_id: str) -> None:
    """Raise the canonical typed error for a non-200 WooCommerce response.

    429 (only if a WAF/security plugin fronts the store) -> RateLimitError (breaker).
    Everything else -> classify_http_error refined by the manifest error_map. The
    provider payload ({code, message, data}) is preserved as evidence.
    """
    if resp.status_code == 429:
        from core.quota import RateLimitError  # noqa: PLC0415

        retry_after_raw = resp.headers.get("Retry-After", "0")
        try:
            retry_after = int(float(retry_after_raw)) or None
        except (ValueError, TypeError):
            retry_after = None
        raise RateLimitError("woocommerce", retry_after)

    from core.pull_errors import classify_http_error  # noqa: PLC0415

    try:
        body = resp.json()
    except Exception:
        body = resp.text
    raise classify_http_error(resp.status_code, body, _load_error_map())


def _fetch_orders(base_url: str, auth: tuple, date_from: str, date_to: str,
                  pull_id: str) -> list[dict]:
    """Fetch all sale-of-record orders in the window (bounded Link pagination)."""
    url: str | None = _orders_endpoint(base_url)
    # WooCommerce `status` is an array param; `after`/`before` filter date_created.
    params: list | None = [
        ("after", f"{date_from}T00:00:00"),
        ("before", f"{date_to}T23:59:59"),
        ("per_page", _PAGE_SIZE),
        ("orderby", "date"),
        ("order", "asc"),
    ] + [("status[]", s) for s in _SALE_STATUSES]

    all_orders: list[dict] = []
    seen_urls: set[str] = set()
    pages = 0
    while url:
        if url in seen_urls:
            logger.warning(
                "woocommerce_pull: non-progressing pagination cursor, stopping "
                "(pull_id=%s pages=%d)", pull_id, pages,
            )
            break
        seen_urls.add(url)
        if pages >= _MAX_PAGES:
            logger.warning(
                "woocommerce_pull: page cap %d reached, stopping (pull_id=%s)",
                _MAX_PAGES, pull_id,
            )
            break
        pages += 1

        resp = httpx.get(url, params=params, auth=auth, timeout=30.0)
        if resp.status_code != 200:
            _handle_non_200(resp, pull_id)

        payload = resp.json()
        all_orders.extend(payload if isinstance(payload, list) else [])

        # rel="next" carries all filters -> do not resend params on the next hop.
        url = _next_page_url(resp.headers.get("Link"))
        params = None
    return all_orders


def _reports_sales_crosscheck(base_url: str, auth: tuple, date_from: str,
                              date_to: str, landed_revenue: float,
                              pull_id: str) -> None:
    """Best-effort consistency alert against /reports/sales (Jean: cross-check).

    /orders is the SOURCE OF TRUTH; this only logs a warning when the pre-aggregated
    report diverges beyond tolerance. NEVER fails the pull -- any error is swallowed.
    """
    try:
        resp = httpx.get(
            f"{base_url}{WC_API_PATH}/reports/sales",
            params={"date_min": date_from, "date_max": date_to},
            auth=auth,
            timeout=30.0,
        )
        if resp.status_code != 200:
            logger.info(
                "woocommerce_crosscheck_skipped: /reports/sales HTTP %d (pull_id=%s)",
                resp.status_code, pull_id,
            )
            return
        payload = resp.json()
        entry = payload[0] if isinstance(payload, list) and payload else payload
        reported = float((entry or {}).get("total_sales", 0) or 0)
        if reported <= 0:
            return
        delta = abs(landed_revenue - reported) / reported
        if delta > _CROSSCHECK_TOLERANCE:
            logger.warning(
                "woocommerce_crosscheck_divergence: landed_revenue=%.2f "
                "reports_sales=%.2f delta=%.1f%% (pull_id=%s) -- /orders is the "
                "source of truth; investigate report window/status semantics",
                landed_revenue, reported, delta * 100, pull_id,
            )
    except Exception as exc:  # noqa: BLE001 -- cross-check must never break the pull
        logger.info(
            "woocommerce_crosscheck_error: %s (pull_id=%s)", exc, pull_id
        )


def pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    store_url: str | None = None,
) -> dict:
    """Fetch WooCommerce orders and land rows in raw_woocommerce_orders.

    # AD-12: called by the queue worker only.
    # AD-3: Basic-auth key/secret used immediately then discarded.
    # AD-7: pull_id minted by the caller.

    Parameters
    ----------
    connection_id: Nango connection id (Basic auth, provider='woocommerce').
    date_from, date_to: ISO-8601 date strings (YYYY-MM-DD) for the date_created window.
    project_id: Project binding for the raw rows.
    pull_id: Core-minted pull ULID (AD-7).
    store_url: Test/standalone override for the store base URL. In production the
        base URL is resolved from the connection's Nango connection_config.

    Returns {"pull_id", "row_count", "date_from", "date_to"}.
    """
    base_url, auth = _resolve_store(connection_id, store_url)

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    all_orders = _fetch_orders(base_url, auth, date_from, date_to, pull_id)

    # parse (source keys) -> transform (manifest rename) -> insert.
    raw_rows = [_parse_order(o) for o in all_orders]
    canonical_rows = transform(raw_rows)

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_raw_rows(
        canonical_rows, pull_id, loaded_at, project_id, db_mode, duckdb_path
    )

    # Cross-check (best-effort): compare landed revenue to /reports/sales.
    landed_revenue = sum(float(r.get("revenue", 0) or 0) for r in canonical_rows)
    _reports_sales_crosscheck(base_url, auth, date_from, date_to, landed_revenue, pull_id)

    logger.info("woocommerce_pull_completed: pull_id=%s row_count=%d", pull_id, row_count)
    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
    }


# ---------------------------------------------------------------------------
# transform() -- manifest-driven canonical field mapping
# ---------------------------------------------------------------------------


def transform(raw_rows: list[dict]) -> list[dict]:
    """Rename raw source fields to canonical names using manifest mappings.

    # AD-4: renames driven by canonical_metric_mapping (total->revenue,
    # total_refunded->refund_amount) + canonical_dimension_mapping. Structural
    # fields (date, order_id, transaction_id, orders_count, revenue_source_currency)
    # pass through unchanged.
    """
    _manifest = json.loads(
        (Path(__file__).parent / "manifest.json").read_text(encoding="utf-8")
    )
    rename_map: dict[str, str] = {}
    for src, val in _manifest.get("canonical_metric_mapping", {}).items():
        rename_map[src] = val if isinstance(val, str) else val.get("canonical", src)
    rename_map.update(_manifest.get("canonical_dimension_mapping", {}))

    result: list[dict] = []
    for row in raw_rows:
        result.append({rename_map.get(k, k): v for k, v in row.items()})
    return result


# ---------------------------------------------------------------------------
# Story 25.9 pattern -- catalog_driven projection-style pull (order grain).
# The pull fetches the SAME /orders payload; the selection controls which cataloged
# fields are projected into long-format raw rows at parse time (no extra API calls).
# ---------------------------------------------------------------------------

_CATALOG_RAW_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_woocommerce_catalog_daily (
    date        VARCHAR,
    order_id    VARCHAR,
    field_id    VARCHAR,
    row_type    VARCHAR,
    value       VARCHAR,
    pull_id     VARCHAR,
    loaded_at   VARCHAR,
    project_id  VARCHAR
)
"""

_CATALOG_RAW_INSERT_SQL = """
INSERT INTO raw_woocommerce_catalog_daily
    (date, order_id, field_id, row_type, value, pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""


def _get_dotted(obj: dict, path: str):
    """Traverse a dotted path (billing.city). Strips [] markers. None if absent."""
    current = obj
    for part in path.replace("[]", "").split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _sum_list_field(items: list, field: str) -> float:
    total = 0.0
    for item in items or []:
        try:
            total += float(item.get(field, 0) or 0)
        except (ValueError, TypeError):
            continue
    return round(total, 10)


def _join_list_field(items: list, field: str) -> str | None:
    values, seen = [], set()
    for item in items or []:
        v = item.get(field)
        if v not in (None, "") and str(v) not in seen:
            seen.add(str(v))
            values.append(str(v))
    return ", ".join(values) if values else None


def _project_order(api_order: dict, source_fields: dict) -> dict:
    """Project one order into a catalog-driven raw row (order grain).

    Structural anchors (order_id, date, revenue_source_currency) are always present;
    only the selected field_ids are added. Section routing:
      PLATFORM CONTRACT -> _parse_order helpers (revenue/refund_amount/orders_count/
                           date/transaction_id).
      line_items[]/refunds[]/coupon_lines[]/fee_lines[]/tax_lines[] -> SUM numeric /
                           comma-join strings (refunds[].total uses abs).
      billing./shipping./scalar -> dotted / top-level access.
    """
    parsed = _parse_order(api_order)
    row: dict = {
        "order_id": parsed["order_id"],
        "date": parsed["date"],
        "revenue_source_currency": parsed.get("revenue_source_currency", "EUR"),
    }

    for field_id, src in source_fields.items():
        if field_id in ("order_id", "date", "revenue_source_currency"):
            continue

        # PLATFORM CONTRACT specials.
        if field_id == "revenue" or src == "total":
            row[field_id] = float(parsed.get("total") or 0)
            continue
        if field_id == "refund_amount" or src == "total_refunded":
            row[field_id] = float(parsed.get("total_refunded") or 0)
            continue
        if field_id in ("orders_count", "order") or src == "order":
            row[field_id] = 1
            continue
        if field_id == "transaction_id" or src == "transaction_id":
            row[field_id] = parsed.get("transaction_id")
            continue
        if src == "date_created":
            row[field_id] = api_order.get("date_created", "")
            continue

        # refunds[].total -> abs-sum; other refund/line/coupon/fee/tax arrays.
        if src == "refunds[].total":
            row[field_id] = _extract_refund_amount(api_order)
            continue
        for prefix, arr_key in (
            ("line_items[].", "line_items"),
            ("refunds[].", "refunds"),
            ("coupon_lines[].", "coupon_lines"),
            ("fee_lines[].", "fee_lines"),
            ("tax_lines[].", "tax_lines"),
        ):
            if src.startswith(prefix):
                sub = src[len(prefix):]
                items = api_order.get(arr_key) or []
                sample = next(
                    (i.get(sub) for i in items if i.get(sub) is not None), None
                )
                numeric = False
                if sample is not None:
                    try:
                        float(sample)
                        numeric = True
                    except (ValueError, TypeError):
                        pass
                row[field_id] = (
                    _sum_list_field(items, sub) if numeric else _join_list_field(items, sub)
                )
                break
        else:
            # dotted (billing.city) or top-level scalar.
            row[field_id] = _get_dotted(api_order, src) if "." in src else api_order.get(src)
    return row


def _insert_catalog_rows(
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    project_id: str,
    db_mode: str,
    duckdb_path: str,
) -> int:
    """Land projected order rows into raw_woocommerce_catalog_daily (long format).

    # AD-22: raw_woocommerce_orders (the legacy pull() table) is never touched here.
    """
    if db_mode != "duckdb":
        raise ValueError(f"_insert_catalog_rows: unsupported db_mode {db_mode!r}")
    from core import warehouse_write  # noqa: PLC0415

    con = warehouse_write.open_raw_writer(duckdb_path, project_id=project_id)
    con.execute(_CATALOG_RAW_CREATE_DDL)

    values = []
    for r in rows:
        order_id = r.get("order_id", "")
        date_str = r.get("date", "")
        for field_id, value in r.items():
            if field_id in ("order_id", "date", "revenue_source_currency"):
                continue
            row_type = "metric" if isinstance(value, (int, float)) else "dimension"
            str_value = str(value) if value is not None else None
            values.append(
                (date_str, order_id, field_id, row_type, str_value, pull_id, loaded_at, project_id)
            )
    if values:
        con.executemany(_CATALOG_RAW_INSERT_SQL, values)
    con.close()
    return len(values)


def pull_catalog_daily(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    selection: dict | None = None,
    store_url: str | None = None,
) -> dict:
    """Catalog-driven projection-style daily pull for WooCommerce (order grain).

    Fetches the same /orders payload as pull(); the selection controls which catalog
    fields are projected into raw_woocommerce_catalog_daily at parse time. A None
    selection falls back to the catalog tier-core default.

    # AD-22: pull() and raw_woocommerce_orders are untouched.
    """
    if selection is None:
        from core.catalog_contract import (  # noqa: PLC0415
            catalog_default_selection,
            validate_selection,
        )

        cat = json.loads(
            (Path(__file__).parent / "api_catalog.json").read_text(encoding="utf-8")
        )
        selection, _ = validate_selection(cat, catalog_default_selection(cat))
    if "source_fields" not in selection:
        from core.catalog_contract import validate_selection  # noqa: PLC0415

        cat = json.loads(
            (Path(__file__).parent / "api_catalog.json").read_text(encoding="utf-8")
        )
        selection, _ = validate_selection(cat, selection)

    source_fields: dict[str, str] = selection.get("source_fields", {})

    base_url, auth = _resolve_store(connection_id, store_url)
    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    all_orders = _fetch_orders(base_url, auth, date_from, date_to, pull_id)
    projected = [_project_order(o, source_fields) for o in all_orders]

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_catalog_rows(
        projected, pull_id, loaded_at, project_id, db_mode, duckdb_path
    )

    logger.info(
        "woocommerce_catalog_pull_completed: pull_id=%s row_count=%d orders=%d fields=%d",
        pull_id, row_count, len(all_orders),
        len(selection.get("metrics", [])) + len(selection.get("dimensions", [])),
    )
    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
    }


# ---------------------------------------------------------------------------
# Register this module's raw table name with core.verification (AD-2: the raw
# table name never appears in core source; module->core direction is allowed).
# ---------------------------------------------------------------------------
try:
    from core.verification import register_raw_table_name as _register_raw  # noqa: PLC0415

    _register_raw("raw_woocommerce_orders", provider="woocommerce")
except Exception:
    pass  # best-effort; verification logs a warning if the table name is missing
