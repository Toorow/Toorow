"""Shopify connector — Story 15.4 (Epic 15, connecteurs vague 1).

Source de verite VENTES e-commerce. Expose une instance ``mcp_app: FastMCP`` que
le loader du core monte sous le namespace ``shopify`` (AD-2). Meme patron
"drop-a-folder" que meta-ads / gsc : zero edit sous server/core, un bloc UNION
dans le mart.

# AD-12: le MCP server lit UNIQUEMENT le mart fact_daily_kpi -- pas de raw_*, pas de CSV.
# AD-3: le token vient de Nango juste avant usage -- jamais stocke ni logge.
# AD-7: pull_id frappe par le scheduler du core, passe dans pull().
# AD-14: parametre identity/project_id.

Decisions de story (Dev Agent Record 15-4-shopify.md) :
  * REFUNDS EN COLONNE DEDIEE (refund_amount, additif positif) -- JAMAIS soustrait
    silencieusement de revenue. Cette decision est la REFERENCE pour Stripe 15.7.
  * transaction_id = dimension de DETAIL du raw (cle de jointure GA4 x Shopify,
    Epic 17) -- PAS une partition de breakdown du mart (le mart agrege au jour).
  * revenue est un metrique NOUVEAU (aucun autre connecteur ne l'expose aujourd'hui)
    -> pas de regle de priorite 3.7 requise a ce stade ; elle devient OBLIGATOIRE
    des que Stripe (15.7) exposera revenue (documente dans le bloc mart).
  * orders_count N'est PAS mappe sur conversions (pas de collision avec la regle
    3.7 GA4/Meta) -- choix documente.

# AI-53: tous les champs/endpoints API Shopify sont declares d'apres la doc et
# annotes "a verifier en story live" (AI-53/AI-13). Rien de non verifiable n'est affirme.
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

# Module-level FastMCP instance — the public surface the loader mounts.
mcp_app = FastMCP("shopify")

# Story 25.7 (AC3): Shopify uses pure HTTP status semantics — no provider-level
# numeric codes refine beyond the HTTP status. error_map is intentionally empty
# (see manifest._error_map_note). Cached here so classify_http_error receives a
# consistent map on every call (AD-2: core never hardcodes provider codes).
_ERROR_MAP: dict[str, str] | None = None


def _load_error_map() -> dict[str, str]:
    """Return the manifest's ``error_map`` (empty for Shopify), cached."""
    global _ERROR_MAP
    if _ERROR_MAP is None:
        manifest_path = Path(__file__).parent / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        _ERROR_MAP = manifest.get("error_map", {})
    return _ERROR_MAP


# ---------------------------------------------------------------------------
# Database connection helpers — env-var driven (no hardcoded paths).
# Same dual-backend pattern as meta-ads / gsc / google-analytics connectors.
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
            bigquery.ScalarQueryParameter(name, "STRING", value) for name, value in params.items()
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
    WHERE connector = 'shopify'
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
            "source_system": "shopify",
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
def get_shopify_report(
    project_id: str = "default",  # AD-14: identity resolved from OAuth 2.1 + PKCE
    report_profile: str = "orders_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Shopify Orders Report — source de verite ventes — reads from fact_daily_kpi mart.

    Report profiles: orders_daily
    Metrics: revenue, refund_amount, orders_count (all additive at day grain, AD-4).
        refund_amount is a DEDICATED positive column -- it is NEVER subtracted
        silently from revenue; a net figure is computed explicitly downstream
        (revenue - refund_amount).
    Breakdown: day-total only (breakdown_dimension = 'day_total'). transaction_id is
        a raw detail dimension (GA4 x Shopify join key, Epic 17), NOT a mart partition.

    Returns the canonical AD-1 envelope via structuredContent.
    Text channel (this docstring) is the lean LLM summary (<=30 lines).

    Parameters:
        project_id: Project identifier (default: 'default', AD-14 placeholder)
        report_profile: orders_daily
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
# Shopify Admin REST API pull — Story 15.4
# ---------------------------------------------------------------------------

# AI-53 A CONFIRMER EN STORY (live, Phase B): version d'API et endpoint.
# Shopify Admin REST orders endpoint. La version d'API est datee (YYYY-MM) et
# fait partie du chemin. Declaree ici d'apres la doc ; a confirmer en passe live.
SHOPIFY_API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2024-10")


def _orders_endpoint(shop_domain: str) -> str:
    """Build the Admin REST orders endpoint for *shop_domain*.

    AI-53 A CONFIRMER EN STORY: format suppose d'apres la doc Shopify Admin REST:
    https://{shop}.myshopify.com/admin/api/{version}/orders.json
    A confirmer (endpoint/params exacts, GraphQL Admin API en alternative) en live.
    """
    return f"https://{shop_domain}/admin/api/{SHOPIFY_API_VERSION}/orders.json"


_RAW_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_shopify_orders (
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
INSERT INTO raw_shopify_orders
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
    """Insert canonical rows into raw_shopify_orders (DuckDB only at P-dev).

    Same self-contained pattern as meta-ads/gsc _insert_raw_rows: the connector owns
    its raw table DDL and never imports from a non-package seeds/ folder.

    # refund_amount is stored in its OWN column (dedicated, positive). It is NEVER
    # subtracted from revenue here (decision de story 15.4, reference Stripe 15.7).
    """
    if db_mode == "duckdb":
        from core import warehouse_write  # noqa: PLC0415

        con = warehouse_write.open_raw_writer(duckdb_path, project_id=project_id)
        con.execute(_RAW_CREATE_DDL)
        # Additive/idempotent guard for tables created before revenue_source_currency existed.
        con.execute(
            "ALTER TABLE raw_shopify_orders ADD COLUMN IF NOT EXISTS "
            "revenue_source_currency VARCHAR DEFAULT 'EUR'"
        )
        values = [
            (
                r.get("date", ""),
                r.get("order_id", ""),
                # transaction_id may be absent on some orders (AI-53): keep None so
                # the >=95% presence quality test measures the real coverage.
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
    else:
        raise ValueError(
            f"_insert_raw_rows: unsupported db_mode {db_mode!r} at P-dev "
            "(BigQuery path not yet implemented)"
        )


def _extract_refund_amount(api_order: dict) -> float:
    """Derive a refund amount for one Shopify order.

    AI-53 A CONFIRMER EN STORY: the Admin REST order payload may expose refunds as a
    ``refunds[]`` array (each with ``transactions[].amount``) rather than a scalar
    ``total_refunded``. We support BOTH: prefer the scalar ``total_refunded`` when
    present, otherwise sum the refund transaction amounts. Field names to confirm live.
    """
    scalar = api_order.get("total_refunded")
    if scalar not in (None, ""):
        try:
            return float(scalar)
        except (ValueError, TypeError):
            pass

    total = 0.0
    for refund in api_order.get("refunds", []) or []:
        for txn in refund.get("transactions", []) or []:
            try:
                total += float(txn.get("amount", 0) or 0)
            except (ValueError, TypeError):
                continue
    return total


def _extract_transaction_id(api_order: dict) -> str | None:
    """Derive the payment transaction id used as the GA4 x Shopify join key.

    AI-53 A CONFIRMER EN STORY: Shopify orders carry a ``transactions[]`` array (Admin
    REST). The first/kind='sale' transaction id is the payment reference we expose. Some
    orders (e.g. draft/free) may carry none -> None (measured by the >=95% quality test).
    Field name/shape to confirm live.
    """
    txns = api_order.get("transactions") or []
    for txn in txns:
        if txn.get("kind") in ("sale", "capture") and txn.get("id") not in (None, ""):
            return str(txn["id"])
    # review-15-4 F-7: NO any-kind fallback -- a refund/void/authorization id would
    # silently corrupt the GA4 x Shopify join key (Epic 17). Absent sale/capture ->
    # None, honestly measured by the >=95% presence gate. AI-53: tighten on live data
    # (kinds catalog to confirm).
    return None


def _parse_order(api_order: dict) -> dict:
    """Map one Shopify Admin REST order to the canonical raw record shape.

    AI-53 A CONFIRMER EN STORY: field names supposed d'apres la doc:
      - created_at -> date (day grain, ISO date extracted from the ISO-8601 timestamp)
      - id -> order_id
      - transactions[] -> transaction_id (detail dimension, GA4 join key)
      - total_price -> revenue (additive)
      - total_refunded / refunds[] -> refund_amount (DEDICATED column, positive)
      - currency -> revenue_source_currency (normalized to project currency in staging)
      - orders_count = 1 per order (aggregated to a daily count in the mart)
    """
    created_at = api_order.get("created_at", "") or ""
    # created_at is an ISO-8601 timestamp (e.g. '2026-07-01T14:22:05-04:00');
    # day grain = the date portion. AI-53: timezone policy mirrors meta/ga4 (day grain,
    # source tz boundary accepted -- no intraday shift; documented in staging).
    date_str = created_at[:10] if created_at else ""
    # review-15-4 F-1: parse is STRUCTURAL extraction only -- metric fields keep their
    # SOURCE API names (total_price, total_refunded). The manifest-driven transform()
    # renames them to canonical (revenue, refund_amount), so the rename map is genuinely
    # exercised by both the pull path and the golden fixture (AI-54).
    return {
        "date": date_str,
        "order_id": str(api_order.get("id", "")),
        "transaction_id": _extract_transaction_id(api_order),
        "total_price": api_order.get("total_price", 0),
        "total_refunded": _extract_refund_amount(api_order),
        "orders_count": 1,
        "revenue_source_currency": api_order.get("currency", "EUR"),
    }


# Link header pagination: Shopify returns a `Link` header with rel="next" carrying
# the full URL of the next page (cursor-based). We follow it until absent.
_LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')

# review-15-4 F-2: hard bound on pagination (250 orders/page x 1000 pages = 250k orders
# per window -- far above any realistic daily pull). The loop also breaks on a repeated
# URL (non-progressing cursor). No silent unbounded loop.
_MAX_PAGES = 1000


def _next_page_url(link_header: str | None) -> str | None:
    """Extract the rel="next" URL from a Shopify Link header (cursor pagination)."""
    if not link_header:
        return None
    m = _LINK_NEXT_RE.search(link_header)
    return m.group(1) if m else None


def pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    shop_domain: str | None = None,
) -> dict:
    """Fetch Shopify orders and land rows in raw_shopify_orders.

    # AD-12: called by the queue worker only — no synchronous API call during an LLM tool call.
    # AD-3: the token is used immediately as an X-Shopify-Access-Token header then discarded —
    #        it is NEVER stored, logged, or passed further.
    # AD-7: pull_id is minted by the caller; this function receives it and never mints its own.

    Parameters
    ----------
    connection_id:
        Nango connection identifier passed to get_fresh_token (provider='shopify').
    date_from, date_to:
        ISO-8601 date strings (YYYY-MM-DD) for the created_at_min / created_at_max window.
    project_id:
        Project binding for the raw rows.
    pull_id:
        Core-minted pull ULID (AD-7). Passed in; not generated here.
    shop_domain:
        Merchant shop domain (e.g. 'my-store.myshopify.com'). If None, reads the
        SHOPIFY_SHOP_DOMAIN env var (same env-fallback pattern as gsc site_url / meta
        ad_account_id -- the queue dispatch does not pass shop_domain).

    Returns
    -------
    dict
        {"pull_id": str, "row_count": int, "date_from": str, "date_to": str}

    Raises
    ------
    ValueError
        If SHOPIFY_SHOP_DOMAIN env var is missing and shop_domain is None.
    RuntimeError
        If the Shopify Admin API returns a non-200 (non-429) response.
    RateLimitError
        If the Shopify Admin API returns HTTP 429 (leaky-bucket overflow).
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    if shop_domain is None:
        shop_domain = os.environ.get("SHOPIFY_SHOP_DOMAIN")
    if not shop_domain:
        raise ValueError(
            "SHOPIFY_SHOP_DOMAIN env var required for pull "
            "(set it to your merchant shop domain, e.g. 'my-store.myshopify.com')"
        )

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    # AD-3: token obtained immediately before use; falls out of scope after the call.
    token = nango_client.get_fresh_token(connection_id, provider="shopify")

    # AI-53 A CONFIRMER EN STORY: created_at_min/max + status=any + limit=250 are the
    # supposed Admin REST params for a date-windowed order pull. To confirm live.
    url: str | None = _orders_endpoint(shop_domain)
    params: dict | None = {
        "status": "any",
        "created_at_min": f"{date_from}T00:00:00Z",
        "created_at_max": f"{date_to}T23:59:59Z",
        "limit": 250,
    }
    headers = {"X-Shopify-Access-Token": token}

    all_orders: list[dict] = []
    # Follow Link-header cursor pagination until rel="next" is absent.
    # review-15-4 F-2: the loop is BOUNDED -- a server (or broken proxy/mock) returning a
    # repeating or non-progressing rel="next" cursor must never hang the worker. Cap at
    # _MAX_PAGES (250 orders/page x 1000 = 250k orders/window, far above any real pull)
    # and break on an already-seen URL, warning either way (no silent unbounded loop).
    seen_urls: set[str] = set()
    pages = 0
    while url:
        if url in seen_urls:
            logger.warning(
                "shopify_pull: non-progressing pagination cursor detected, stopping "
                "(pull_id=%s pages=%d)",
                pull_id,
                pages,
            )
            break
        seen_urls.add(url)
        if pages >= _MAX_PAGES:
            logger.warning(
                "shopify_pull: page cap %d reached, stopping (pull_id=%s)",
                _MAX_PAGES,
                pull_id,
            )
            break
        pages += 1
        resp = httpx.get(url, params=params, headers=headers, timeout=30.0)

        if resp.status_code == 429:
            # Leaky-bucket overflow: raise RateLimitError so the worker trips the breaker.
            # "shopify" lives in the MODULE (not in core) -- AD-2 compliant.
            from core.quota import RateLimitError  # noqa: PLC0415

            retry_after_raw = resp.headers.get("Retry-After", "0")
            try:
                retry_after = int(float(retry_after_raw)) or None
            except (ValueError, TypeError):
                retry_after = None
            raise RateLimitError("shopify", retry_after)

        if resp.status_code != 200:
            # Story 25.2/25.7: canonical typed error; provider payload preserved.
            # error_map is empty for Shopify (pure HTTP semantics, no provider codes).
            from core.pull_errors import classify_http_error  # noqa: PLC0415

            try:
                _body = resp.json()
            except Exception:
                _body = resp.text
            raise classify_http_error(resp.status_code, _body, _load_error_map())

        payload = resp.json()
        all_orders.extend(payload.get("orders") or [])

        # Next page: Shopify's rel="next" URL already carries the page_info cursor;
        # subsequent requests MUST NOT resend the filter params (Shopify rejects them).
        url = _next_page_url(resp.headers.get("Link"))
        params = None

    # review-15-4 F-1: parse (source keys) -> transform (manifest rename) -> insert.
    # The canonical_metric_mapping is exercised on EVERY pull, not just in conformance.
    raw_rows: list[dict] = [_parse_order(o) for o in all_orders]
    canonical_rows: list[dict] = transform(raw_rows)

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_raw_rows(
        canonical_rows, pull_id, loaded_at, project_id, db_mode, duckdb_path
    )

    # AD-3: no token in log — only pull_id and row_count (safe metadata).
    logger.info("shopify_pull_completed: pull_id=%s row_count=%d", pull_id, row_count)

    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
    }


# ---------------------------------------------------------------------------
# Epic 31.6 — product_launch event profile (landing: context_events).
#
# Generalises the YouTube 31.3 video_upload pattern to the commerce family: each
# Shopify product publication becomes a canonical marker
# `shopify > product_launch > <title>` that annotates the revenue/orders curves
# AND feeds the MMM exogenous-regressor feature builder (epic-31 §10). This
# profile writes ONLY context_events -- never raw_shopify_orders nor
# fact_daily_kpi (HG-2 inverted per profile).
# ---------------------------------------------------------------------------

# Canonical event mapping mirrors canonical_event_mapping in the manifest. Pure
# lookup (no I/O) -- unit-testable via golden_events/expected_events.
_CANONICAL_EVENT_MAPPING = {"product_launch": "product_launch"}


# Products endpoint (Admin REST). AI-53: version-dated path, to confirm live.
def _products_endpoint(shop_domain: str) -> str:
    """Build the Admin REST products endpoint for *shop_domain* (Epic 31.6)."""
    return f"https://{shop_domain}/admin/api/{SHOPIFY_API_VERSION}/products.json"


def transform_events(
    raw_rows: list[dict],
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict]:
    """Map raw Admin REST product dicts to canonical event dicts (AD-2, pure).

    Input: a list of product resource dicts from /products.json, each with
    ``title`` and ``published_at`` (nullable). Output: canonical event dicts with
    keys event_type, event_date, label, platform, source -- ready for
    persist_context_event() (value = None, pulse).

    event_date = published_at truncated to YYYY-MM-DD.
    label      = title.
    event_type = "product_launch" (via _CANONICAL_EVENT_MAPPING).
    platform   = "shopify".
    source     = "shopify".

    Semantics (M1): a product with a null/empty ``published_at`` was never
    published to the Online Store -> it has NO launch date, so it is SKIPPED
    (not emitted with an empty event_date). A published_at shorter than a 10-char
    ISO date prefix is likewise skipped (validate_event_input would reject it).

    Date window (H1): the products edge returns the whole catalogue, so an
    unfiltered transform would emit every product on every pull. When
    ``date_from``/``date_to`` are supplied, products whose publish date falls
    OUTSIDE [date_from, date_to] are dropped. Both None (the golden-replay
    contract) means "no window" -> every published product passes.
    """
    event_type = _CANONICAL_EVENT_MAPPING.get("product_launch", "product_launch")
    result: list[dict] = []
    for product in raw_rows:
        published_at = product.get("published_at") or ""
        # M1: unpublished product (null published_at) or invalid date -> no event.
        if len(published_at) < 10:
            logger.debug("transform_events: skipping product with published_at=%r", published_at)
            continue
        event_date = published_at[:10]
        # H1: bounded window filter (no-op when both bounds are None).
        if date_from is not None and event_date < date_from:
            continue
        if date_to is not None and event_date > date_to:
            continue
        result.append(
            {
                "event_type": event_type,
                "event_date": event_date,
                "label": product.get("title") or "",
                "platform": "shopify",
                "source": "shopify",
            }
        )
    return result


def pull_product_launch(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    shop_domain: str | None = None,
) -> dict:
    """Fetch the shop's products and persist each publication as a context_event.

    Admin REST path (reuses the existing shopify token/scope -- no new scope):
    GET /admin/api/{version}/products.json?published_at_min=&published_at_max=&
    limit=250, following the ``Link`` rel="next" cursor (same bounded pagination
    as the orders pull).

    Idempotence (Epic 31.3/H1): two guards make a daily re-pull idempotent (no
    double-counted markers / doubled MMM regressor):
      1. transform_events() filters products to [date_from, date_to] client-side.
      2. delete_connector_events_in_window() clears this project's prior shopify
         product_launch events in the SAME window BEFORE the re-insert.
    No new migration is required (context_events.type/source already exist).

    AD-3:  token used immediately as an X-Shopify-Access-Token header, then discarded.
    AI-03: ASCII-only in log strings.
    AD-2:  canonical event mapping via transform_events().
    AD-8:  project-scoped (delete + insert both bound to project_id).
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time
    from core.context_events import (  # noqa: PLC0415
        delete_connector_events_in_window,
        persist_context_event,
    )

    if shop_domain is None:
        shop_domain = os.environ.get("SHOPIFY_SHOP_DOMAIN")
    if not shop_domain:
        raise ValueError(
            "SHOPIFY_SHOP_DOMAIN env var required for pull "
            "(set it to your merchant shop domain, e.g. 'my-store.myshopify.com')"
        )

    token = nango_client.get_fresh_token(connection_id, provider="shopify")

    url: str | None = _products_endpoint(shop_domain)
    params: dict | None = {
        "published_at_min": f"{date_from}T00:00:00Z",
        "published_at_max": f"{date_to}T23:59:59Z",
        "limit": 250,
    }
    headers = {"X-Shopify-Access-Token": token}

    all_products: list[dict] = []
    seen_urls: set[str] = set()
    pages = 0
    while url:
        if url in seen_urls:
            logger.warning(
                "pull_product_launch: non-progressing pagination cursor, stopping "
                "(pull_id=%s pages=%d)",
                pull_id,
                pages,
            )
            break
        seen_urls.add(url)
        if pages >= _MAX_PAGES:
            logger.warning(
                "pull_product_launch: page cap %d reached, stopping (pull_id=%s)",
                _MAX_PAGES,
                pull_id,
            )
            break
        pages += 1
        resp = httpx.get(url, params=params, headers=headers, timeout=30.0)

        if resp.status_code == 429:
            from core.quota import RateLimitError  # noqa: PLC0415

            retry_after_raw = resp.headers.get("Retry-After", "0")
            try:
                retry_after = int(float(retry_after_raw)) or None
            except (ValueError, TypeError):
                retry_after = None
            raise RateLimitError("shopify", retry_after)

        if resp.status_code != 200:
            from core.pull_errors import classify_http_error  # noqa: PLC0415

            try:
                _body = resp.json()
            except Exception:
                _body = resp.text
            raise classify_http_error(resp.status_code, _body, _load_error_map())

        payload = resp.json()
        all_products.extend(payload.get("products") or [])
        url = _next_page_url(resp.headers.get("Link"))
        params = None  # the next URL already carries the page_info cursor

    logger.info(
        "pull_product_launch: fetched %d product(s) in %d page(s) for pull_id=%s",
        len(all_products),
        pages,
        pull_id,
    )

    canonical_events = transform_events(all_products, date_from=date_from, date_to=date_to)

    # H1: delete-by-source-window BEFORE re-insert -> idempotent daily re-pull.
    event_type = _CANONICAL_EVENT_MAPPING.get("product_launch", "product_launch")
    delete_connector_events_in_window(
        project_id=project_id,
        source="shopify",
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
            created_by=f"shopify_pull:{pull_id}",
            platform=ev["platform"],
            value=None,
            source=ev["source"],
        )
        event_count += 1

    logger.info(
        "pull_product_launch: persisted %d context_events: pull_id=%s date_from=%s date_to=%s",
        event_count,
        pull_id,
        date_from,
        date_to,
    )
    return {
        "pull_id": pull_id,
        "event_count": event_count,
        "date_from": date_from,
        "date_to": date_to,
    }


# ---------------------------------------------------------------------------
# transform() — manifest-driven canonical field mapping
# ---------------------------------------------------------------------------


def transform(raw_rows: list[dict]) -> list[dict]:
    """Map raw source fields to canonical names using manifest mappings.

    # AD-4: canonical field mapping driven by manifest — do not hardcode source names.
    review-15-4 F-1: _parse_order() keeps the SOURCE metric names (total_price,
    total_refunded); THIS function performs the canonical rename (-> revenue,
    refund_amount) from the manifest's canonical_metric_mapping. It runs on every
    pull (parse -> transform -> insert) AND on the golden fixture (conformance
    Layer 4), so the rename map is genuinely exercised in both paths. Structural
    fields (date, order_id, transaction_id, orders_count, revenue_source_currency)
    pass through unchanged.
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

    result: list[dict] = []
    for row in raw_rows:
        canonical_row: dict = {}
        for key, value in row.items():
            canonical_row[rename_map.get(key, key)] = value
        result.append(canonical_row)
    return result


# ---------------------------------------------------------------------------
# Story 15.4: register this module's raw table name with core.verification.
# module->core direction is allowed by AD-2 (only core->modules is forbidden).
# The raw table name never appears in core source code (AD-2).
# ---------------------------------------------------------------------------
try:
    from core.verification import register_raw_table_name as _register_raw  # noqa: PLC0415

    _register_raw("raw_shopify_orders", provider="shopify")
except Exception:
    pass  # best-effort; verification will log a warning if table name is missing


# ---------------------------------------------------------------------------
# Story 25.9: catalog_driven execution — pull_catalog_daily (PROJECTION style).
# Fetches the same /orders.json payload as pull() (the single orders endpoint
# carries the full order object: order root, line_items[], customer, billing/
# shipping address, refunds[]). Selection controls which catalog fields are
# projected into landed rows at parse time.
# No excluded sections: the single orders payload serves all cataloged fields.
# ---------------------------------------------------------------------------

# Page size for catalog pull — identical to pull() (Admin REST limit).
_CATALOG_PAGE_LIMIT = 250


def _get_dotted(obj: dict, path: str):
    """Extract from *obj* via a dotted *path* (e.g. 'billing_address.city').

    Handles bracket-notation source_fields (e.g. 'line_items[].price') by
    stripping '[]' markers before traversal; callers that need array semantics
    (SUM / comma-join) handle the list themselves.
    Returns None if any intermediate key is absent or None.
    """
    # Normalise: strip array markers so 'line_items[].price' → 'line_items.price'
    clean_path = path.replace("[]", "")
    parts = clean_path.split(".")
    current = obj
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _sum_list_field(items: list, field: str) -> float:
    """Sum a scalar numeric field across a list of dicts. Returns 0.0 if empty."""
    total = 0.0
    for item in items or []:
        try:
            total += float(item.get(field, 0) or 0)
        except (ValueError, TypeError):
            continue
    return round(total, 10)


def _join_list_field(items: list, field: str) -> str | None:
    """Comma-join distinct non-empty string values of *field* across a list of dicts.

    Returns None when no non-empty value is present.
    """
    values = []
    seen: set = set()
    for item in items or []:
        v = item.get(field)
        if v not in (None, "") and str(v) not in seen:
            seen.add(str(v))
            values.append(str(v))
    return ", ".join(values) if values else None


def _project_order(api_order: dict, source_fields: dict) -> dict:
    """Project one Shopify order into a catalog-driven raw row (order grain).

    Only the fields listed in *source_fields* (plus structural anchors) are set.
    Structural anchors (order_id, date, revenue_source_currency) are always
    present via setdefault so _insert_catalog_rows can embed them in every
    catalog row without re-deriving them.

    Section routing:
      PLATFORM CONTRACT  — special computed fields via _parse_order helpers:
                           order_id, date, revenue_source_currency, revenue,
                           refund_amount, orders_count, transaction_id, created_at,
                           total_price, total_refunded.
      LINE ITEM          — source_field 'line_items[].X': SUM for numeric fields /
                           comma-join for string dimensions.
      REFUND             — source_field 'refunds[].transactions[].X' → SUM;
                           'refunds[].X' → SUM for numeric / comma-join for strings.
      CUSTOMER / CUSTOMER GEO / BILLING ADDRESS / SHIPPING ADDRESS /
      ORDER / TRAFFIC SOURCE / PRODUCT  — dotted-path extraction via _get_dotted.

    The row contains ONLY source_fields keys + structural anchors. Unselected
    fields are absent — projection is strict, not a pass-through.
    """
    parsed = _parse_order(api_order)
    order_id = parsed["order_id"]
    date_str = parsed["date"]
    currency = parsed.get("revenue_source_currency", "EUR") or "EUR"

    # Structural anchors — always present; excluded from catalog rows by _insert_catalog_rows.
    row: dict = {
        "order_id": order_id,
        "date": date_str,
        "revenue_source_currency": currency,
    }

    line_items: list = api_order.get("line_items") or []
    refunds: list = api_order.get("refunds") or []

    # Map each selected field_id using its catalog source_field.
    # source_fields = {field_id: source_field} from validate_selection.
    for field_id, src in source_fields.items():
        # Structural anchors already set — update them if explicitly selected
        # (e.g. field_id='date') but don't add them twice.
        if field_id in ("order_id", "date", "revenue_source_currency"):
            # They're already correctly set; just skip re-derivation.
            continue

        # ------ PLATFORM CONTRACT: fields derived via _parse_order helpers ------
        if field_id == "revenue" or src == "total_price":
            row[field_id] = float(parsed.get("total_price") or 0)
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
        if field_id == "created_at" or src == "created_at":
            row[field_id] = api_order.get("created_at", "")
            continue
        if field_id == "order_id_field" or src in ("id",):
            row[field_id] = order_id
            continue

        # ------ LINE ITEM arrays (source_field starts with 'line_items[].') ------
        if src.startswith("line_items[]."):
            sub_field = src[len("line_items[].") :]
            # Heuristic: try float-parse on first non-None value to detect numeric.
            # Numeric → SUM; string → comma-join of distinct values.
            sample = next(
                (item.get(sub_field) for item in line_items if item.get(sub_field) is not None),
                None,
            )
            numeric = False
            if sample is not None:
                try:
                    float(sample)
                    numeric = True
                except (ValueError, TypeError):
                    pass
            row[field_id] = (
                _sum_list_field(line_items, sub_field)
                if numeric
                else _join_list_field(line_items, sub_field)
            )
            continue

        # ------ REFUND nested arrays ------
        if src.startswith("refunds[].transactions[]."):
            txn_field = src[len("refunds[].transactions[].") :]
            total = 0.0
            for refund in refunds:
                for txn in refund.get("transactions") or []:
                    try:
                        total += float(txn.get(txn_field, 0) or 0)
                    except (ValueError, TypeError):
                        continue
            row[field_id] = round(total, 10)
            continue

        if src.startswith("refunds[]."):
            sub_field = src[len("refunds[].") :]
            sample = next(
                (r.get(sub_field) for r in refunds if r.get(sub_field) is not None),
                None,
            )
            numeric = False
            if sample is not None:
                try:
                    float(sample)
                    numeric = True
                except (ValueError, TypeError):
                    pass
            row[field_id] = (
                _sum_list_field(refunds, sub_field)
                if numeric
                else _join_list_field(refunds, sub_field)
            )
            continue

        # ------ Default: dotted-path extraction on api_order ------
        row[field_id] = _get_dotted(api_order, src)

    return row


_CATALOG_RAW_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_shopify_catalog_daily (
    date                     VARCHAR,
    order_id                 VARCHAR,
    field_id                 VARCHAR,
    row_type                 VARCHAR,
    value                    VARCHAR,
    pull_id                  VARCHAR,
    loaded_at                VARCHAR,
    project_id               VARCHAR
)
"""

_CATALOG_RAW_INSERT_SQL = """
INSERT INTO raw_shopify_catalog_daily
    (date, order_id, field_id, row_type, value, pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""


def _insert_catalog_rows(
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    project_id: str,
    db_mode: str,
    duckdb_path: str,
) -> int:
    """Land projected order rows into raw_shopify_catalog_daily (long format).

    Shape: one row per (order_id, field_id) — the catalog_daily analog of
    raw_stripe_catalog_daily. Structural anchors (order_id, date,
    revenue_source_currency) are NOT re-emitted as catalog rows; they are already
    encoded as order grain identifiers. Every selected field_id becomes its own row.

    # AD-22: raw_shopify_orders (the legacy pull() table) is never touched here.
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
                # structural anchors — not emitted as catalog field rows
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
    shop_domain: str | None = None,
    selection: dict | None = None,
) -> dict:
    """Story 25.9: catalog_driven daily pull for Shopify (PROJECTION style).

    Fetches /orders.json (same objects as pull()). The single orders payload
    carries all cataloged fields (order root, line_items[], customer,
    billing/shipping address, refunds[]) — no excluded sections.
    Selection controls which catalog fields are projected into
    raw_shopify_catalog_daily at parse time.

    # AD-22: pull() and raw_shopify_orders are untouched.
    # AD-3: token used immediately as X-Shopify-Access-Token then discarded.
    # AD-7: pull_id minted by the caller.

    Parameters
    ----------
    connection_id:
        Nango connection identifier (provider='shopify').
    date_from, date_to:
        ISO-8601 date strings for the created_at window.
    project_id:
        Project binding for the raw rows.
    pull_id:
        Core-minted pull ULID (AD-7).
    shop_domain:
        Merchant shop domain. Falls back to SHOPIFY_SHOP_DOMAIN env var.
    selection:
        ``{"metrics": [...], "dimensions": [...]}`` — which catalog fields
        to project. None → tier-core default from api_catalog.json.

    Returns
    -------
    dict
        {"pull_id": str, "row_count": int, "date_from": str, "date_to": str}
    """
    from core import nango_client  # noqa: PLC0415

    if shop_domain is None:
        shop_domain = os.environ.get("SHOPIFY_SHOP_DOMAIN")
    if not shop_domain:
        raise ValueError(
            "SHOPIFY_SHOP_DOMAIN env var required for pull_catalog_daily "
            "(set it to your merchant shop domain, e.g. 'my-store.myshopify.com')"
        )

    if selection is None:
        from core.catalog_contract import (  # noqa: PLC0415
            catalog_default_selection,
            validate_selection,
        )

        _cat = json.loads((Path(__file__).parent / "api_catalog.json").read_text(encoding="utf-8"))
        selection, _ = validate_selection(_cat, catalog_default_selection(_cat))

    if "source_fields" not in selection:
        _cat_raw = json.loads(
            (Path(__file__).parent / "api_catalog.json").read_text(encoding="utf-8")
        )
        from core.catalog_contract import validate_selection  # noqa: PLC0415

        selection, _ = validate_selection(_cat_raw, selection)

    # Merge metrics + dimensions into a single source_fields map for projection.
    source_fields: dict[str, str] = selection.get("source_fields", {})

    db_mode = _get_db_mode()
    duckdb_path = _get_duckdb_path()

    # AD-3: token obtained immediately before use; falls out of scope after the call.
    token = nango_client.get_fresh_token(connection_id, provider="shopify")

    url: str | None = _orders_endpoint(shop_domain)
    params: dict | None = {
        "status": "any",
        "created_at_min": f"{date_from}T00:00:00Z",
        "created_at_max": f"{date_to}T23:59:59Z",
        "limit": _CATALOG_PAGE_LIMIT,
    }
    headers = {"X-Shopify-Access-Token": token}

    all_orders: list[dict] = []
    seen_urls: set[str] = set()
    pages = 0
    while url:
        if url in seen_urls:
            logger.warning(
                "shopify_catalog_pull: non-progressing pagination cursor detected, stopping "
                "(pull_id=%s pages=%d)",
                pull_id,
                pages,
            )
            break
        seen_urls.add(url)
        if pages >= _MAX_PAGES:
            logger.warning(
                "shopify_catalog_pull: page cap %d reached, stopping (pull_id=%s)",
                _MAX_PAGES,
                pull_id,
            )
            break
        pages += 1
        resp = httpx.get(url, params=params, headers=headers, timeout=30.0)

        if resp.status_code == 429:
            from core.quota import RateLimitError  # noqa: PLC0415

            retry_after_raw = resp.headers.get("Retry-After", "0")
            try:
                retry_after = int(float(retry_after_raw)) or None
            except (ValueError, TypeError):
                retry_after = None
            raise RateLimitError("shopify", retry_after)

        if resp.status_code != 200:
            from core.pull_errors import classify_http_error  # noqa: PLC0415

            try:
                _body = resp.json()
            except Exception:
                _body = resp.text
            raise classify_http_error(resp.status_code, _body, _load_error_map())

        payload = resp.json()
        all_orders.extend(payload.get("orders") or [])

        url = _next_page_url(resp.headers.get("Link"))
        params = None

    projected_rows: list[dict] = [_project_order(o, source_fields) for o in all_orders]

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    row_count = _insert_catalog_rows(
        projected_rows, pull_id, loaded_at, project_id, db_mode, duckdb_path
    )

    logger.info(
        "shopify_catalog_pull_completed: pull_id=%s row_count=%d orders=%d fields=%d",
        pull_id,
        row_count,
        len(all_orders),
        len(selection.get("metrics", [])) + len(selection.get("dimensions", [])),
    )
    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
    }
