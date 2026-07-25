"""Google Ad Manager connector — REST-only (Interactive Reports API).

AD-2: module name never hardcoded in core/.
AD-3: token obtained immediately before use, never stored or logged.
AD-7: pull_id minted by the caller (core scheduler), passed in here.
AD-4: non-additive metrics (CTR, eCPM, ...) stored raw per row, never aggregated.
AI-03: ASCII-only stdout.

GAM has NO synchronous report endpoint: a report is an ASYNC JOB. The pull is
    submit report  ->  poll the returned operation until done  ->  fetch result rows.
MONEY metrics come back in MICROS (1e-6 currency unit) and are divided by 1e6 here.
The REST client + field catalogue + report-type compatibility are ported from
`C:/Users/littl/Programmation/gam-native/backend/src` (see the
gam-connector-from-gam-native memory).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Module-level FastMCP instance — core loader mounts this under the 'google-ad-manager' namespace.
mcp_app = FastMCP("google-ad-manager")

_GAM_REST_BASE = "https://admanager.googleapis.com/v1"

# ---------------------------------------------------------------------------
# Database helpers (dual-backend pattern, same as gsc/ga4 connectors)
# ---------------------------------------------------------------------------

_DEFAULT_DUCKDB_PATH = os.path.join(os.path.dirname(__file__), "seeds", "local.duckdb")


def _get_db_mode() -> str:
    return os.environ.get("TOOROW_DB_MODE", "duckdb")


def _get_duckdb_path() -> str:
    return os.environ.get("TOOROW_DUCKDB_PATH", _DEFAULT_DUCKDB_PATH)


# ---------------------------------------------------------------------------
# Manifest-backed caches (error_map + MONEY-metric set for micros division)
# ---------------------------------------------------------------------------

_MANIFEST: dict | None = None
_ERROR_MAP: dict | None = None


def _manifest() -> dict:
    global _MANIFEST
    if _MANIFEST is None:
        _MANIFEST = json.loads(
            (Path(__file__).parent / "manifest.json").read_text(encoding="utf-8")
        )
    return _MANIFEST


def _load_error_map() -> dict:
    global _ERROR_MAP
    if _ERROR_MAP is None:
        _ERROR_MAP = _manifest().get("error_map") or {}
    return _ERROR_MAP


# ── Money contract (decision 2026-07-22) ────────────────────────────────────
# MICROS TO THE END: GAM MONEY metrics arrive in micros and are STORED AS MICROS
# all the way through raw -> staging -> mart. The ÷1e6 happens ONCE, at read,
# together with the currency — never per-row (that accumulates float drift over
# aggregation). transform() therefore does NOT divide.
# CURRENCY + REPORT TIMEZONE: captured at pull() from the network (currencyCode +
# timeZone) and stored per row so a) revenue is never a naked number and b) we
# never sum across currencies ("choux != carottes"). The report timezone also
# explains day-boundary offsets when reconciling with GA4/Shopify.
# FX conversion + cross-datastream money reconciliation = a SEPARATE shared money
# module (helper: fixed/as-of-day rate via API), NOT built in this connector.
#
# The catalogue carries each metric's Data format in its description
# ("... Data format: `MONEY`"), exactly as gam-native derives MONEY_METRICS.
_DATA_FORMAT_RE = re.compile(r"Data format: `([A-Z_]+)`")


def _catalog_fields() -> list[dict]:
    catalog_path = Path(__file__).parent / "api_catalog.json"
    if not catalog_path.exists():
        return []
    return json.loads(catalog_path.read_text(encoding="utf-8")).get("fields", [])


def _money_metrics() -> set[str]:
    """Source metric names whose value is MONEY-in-micros (÷1e6 ONCE at read)."""
    money: set[str] = set()
    for f in _catalog_fields():
        if f.get("kind") != "metric":
            continue
        match = _DATA_FORMAT_RE.search(f.get("description", "") or "")
        if match and match.group(1) == "MONEY":
            money.add(f["source_field"])
    return money


def _canonical_money_metrics() -> set[str]:
    """Canonical metric names stored as MONEY-in-micros in the mart.

    The read layer (MCP tool) divides these by 1e6 ONCE and attaches currency.
    NEVER sum GAM micros with a currency-unit metric from another connector.
    """
    man = _manifest()
    rename: dict[str, str] = {}
    for src, val in man.get("canonical_metric_mapping", {}).items():
        rename[src] = val if isinstance(val, str) else val.get("canonical", src)
    return {rename.get(s, s) for s in _money_metrics()}


def _ratio_source_fields() -> set[str]:
    """Non-additive (ratio/rate) source metrics — DROPPED in transform, NOT stored.

    AD-4: ratios (CTR, eCPM, CPM-rate, CPC-rate, fill rate, viewability %) are
    NEVER imported — summing them is meaningless. They are RECONSTRUCTED in the
    mart from additive components (e.g. ctr = clicks/impressions,
    ecpm = revenue/impressions*1000, cpc = revenue/clicks). The additive REVENUE
    metric (e.g. AD_SERVER_CPM_AND_CPC_REVENUE) is NOT a ratio despite its name —
    it is kept. Droppable = manifest metric field flagged non_additive.
    """
    dropped: set[str] = set()
    for f in _manifest().get("source_capabilities", {}).get("fields", []):
        if f.get("kind") == "metric" and f.get("non_additive") is True:
            dropped.add(f["source_field"])
    return dropped


# ---------------------------------------------------------------------------
# Raw table DDL (long format)
# ---------------------------------------------------------------------------

# value holds MONEY metrics IN MICROS (exact in DOUBLE up to 2^53); currency and
# report_timezone are network-level context captured at pull() so revenue is
# never naked and cross-currency sums are refusable.
_RAW_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_google_ad_manager_daily (
    date                VARCHAR,
    metric              VARCHAR,
    value               DOUBLE,
    currency            VARCHAR,
    report_timezone     VARCHAR,
    breakdown_dimension VARCHAR,
    breakdown_value     VARCHAR,
    pull_id             VARCHAR,
    loaded_at           VARCHAR,
    project_id          VARCHAR
)
"""

_RAW_INSERT_SQL = """
INSERT INTO raw_google_ad_manager_daily
    (date, metric, value, currency, report_timezone, breakdown_dimension,
     breakdown_value, pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _insert_raw_rows(
    rows: list[dict],
    pull_id: str,
    project_id: str,
    currency: str | None = None,
    report_timezone: str | None = None,
) -> int:
    """Insert canonical long-format rows into raw_google_ad_manager_daily (DuckDB).

    ``currency`` + ``report_timezone`` are the network-level context (from
    networks.get: currencyCode + timeZone). MONEY values stay in MICROS.
    """
    db_mode = _get_db_mode()
    if db_mode != "duckdb":
        raise ValueError(f"_insert_raw_rows: unsupported db_mode {db_mode!r}")

    import duckdb  # noqa: PLC0415

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    con = duckdb.connect(_get_duckdb_path())
    con.execute(_RAW_CREATE_DDL)
    values = [
        (
            r.get("date", ""),
            r.get("metric", ""),
            float(r.get("value", 0.0) or 0.0),
            currency,
            report_timezone,
            r.get("breakdown_dimension", ""),
            r.get("breakdown_value", ""),
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


# ---------------------------------------------------------------------------
# REST client — Interactive Reports API, ported from gam-native
# (backend/src/adapters/gam_rest_client.py). A report is an ASYNC JOB:
#   submit (POST /reports) -> run (POST /reports/{id}:run, LRO)
#   -> poll (GET /{op}) -> fetch (GET /{result}:fetchRows).
# httpx + toorow error taxonomy (core.pull_errors / core.quota).
# AI-13: mocked-only until a live GAM network ratifies the contract
# (no test account, per doctrine) — verification stays 'blocked'.
# ---------------------------------------------------------------------------

_POLL_INTERVAL_SECS = 3
_MAX_POLL_ATTEMPTS = 40  # ~2 min ceiling
_FETCH_PAGE_SIZE = 10000
_MAX_FETCH_PAGES = 100
_HTTP_TIMEOUT = 60.0


def _iso_to_date_dict(iso_date: str) -> dict:
    """'YYYY-MM-DD' -> GAM Date {year, month, day}."""
    parts = iso_date.strip().split("-")
    if len(parts) != 3:
        raise ValueError(f"invalid date {iso_date!r}, expected YYYY-MM-DD")
    return {"year": int(parts[0]), "month": int(parts[1]), "day": int(parts[2])}


def _gam_request(
    method: str,
    url: str,
    token: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
) -> dict:
    """One GAM REST call with the toorow error taxonomy (Story 25.2).

    429 -> RateLimitError (breaker). Other non-2xx -> classify_http_error with the
    GAM body preserved (GAM 400s carry the actionable validation message, e.g.
    "MONTH_AND_YEAR is not a valid dimension for REST reports").
    AD-3: token used as a Bearer header, never logged.
    """
    resp = httpx.request(
        method,
        url,
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        json=json_body,
        timeout=_HTTP_TIMEOUT,
    )
    if resp.status_code == 429:
        from core.quota import RateLimitError  # noqa: PLC0415

        retry_after_raw = resp.headers.get("Retry-After", "0")
        try:
            retry_after = int(retry_after_raw) or None
        except (ValueError, TypeError):
            retry_after = None
        raise RateLimitError("google-ad-manager", retry_after)
    if resp.status_code // 100 != 2:
        from core.pull_errors import classify_http_error  # noqa: PLC0415

        try:
            body = resp.json()
        except Exception:
            body = resp.text
        raise classify_http_error(resp.status_code, body, _load_error_map())
    return resp.json() if resp.text else {}


def _list_networks(token: str) -> list[dict]:
    """GET /networks -> [{name: 'networks/{code}', displayName, networkCode?}]."""
    return _gam_request("GET", f"{_GAM_REST_BASE}/networks", token).get("networks", []) or []


def _network_context(network_code: str, token: str) -> tuple[str | None, str | None]:
    """(currency_code, report_timezone) from GET /networks/{code} — the money/date context."""
    data = _gam_request("GET", f"{_GAM_REST_BASE}/networks/{network_code}", token)
    return (data.get("currencyCode") or None, data.get("timeZone") or None)


def _submit_report(
    network_code: str,
    token: str,
    dimensions: list[str],
    metrics: list[str],
    date_from: str,
    date_to: str,
    report_type: str,
) -> str:
    """POST /networks/{nc}/reports -> reportId (a HIDDEN fixed-range report)."""
    url = f"{_GAM_REST_BASE}/networks/{network_code}/reports"
    payload = {
        "visibility": "HIDDEN",
        "reportDefinition": {
            "dimensions": dimensions,
            "metrics": metrics,
            "reportType": report_type,
            "dateRange": {
                "fixed": {
                    "startDate": _iso_to_date_dict(date_from),
                    "endDate": _iso_to_date_dict(date_to),
                }
            },
        },
    }
    name = (_gam_request("POST", url, token, json_body=payload).get("name", "") or "")
    report_id = name.rsplit("/", 1)[-1]
    if not report_id:
        raise ValueError("GAM submit_report: report name missing from response")
    return report_id


def _run_report(network_code: str, token: str, report_id: str) -> str:
    """POST /reports/{id}:run -> LRO operation name."""
    url = f"{_GAM_REST_BASE}/networks/{network_code}/reports/{report_id}:run"
    op_name = _gam_request("POST", url, token, json_body={}).get("name", "")
    if not op_name:
        raise ValueError("GAM run_report: LRO operation name missing from response")
    return op_name


def _poll_operation(token: str, operation_name: str) -> str:
    """Poll the run LRO (GET /{op}) until done; return the reportResult resource name."""
    for _ in range(_MAX_POLL_ATTEMPTS):
        data = _gam_request("GET", f"{_GAM_REST_BASE}/{operation_name}", token)
        if data.get("done"):
            if data.get("error"):
                raise RuntimeError(f"GAM report run failed: {str(data['error'])[:200]}")
            result_name = (data.get("response") or {}).get("reportResult") or ""
            if not result_name:
                raise ValueError("GAM poll: reportResult missing from a done operation")
            return result_name
        time.sleep(_POLL_INTERVAL_SECS)
    raise TimeoutError(
        f"GAM report run timed out after {_MAX_POLL_ATTEMPTS * _POLL_INTERVAL_SECS}s"
    )


def _coerce_metric(value: dict):
    """GAM MetricValue oneof -> scalar. MONEY is KEPT IN MICROS (÷1e6 once at read)."""
    if not value:
        return 0
    if "intValue" in value:
        try:
            return int(value["intValue"])
        except (TypeError, ValueError):
            return 0
    if "doubleValue" in value:
        try:
            return float(value["doubleValue"])
        except (TypeError, ValueError):
            return 0.0
    if "microsValue" in value:
        try:
            return float(value["microsValue"])  # micros kept as-is
        except (TypeError, ValueError):
            return 0.0
    return value.get("value", 0)


def _coerce_dimension(value: dict):
    """GAM DimensionValue oneof -> scalar (dateValue -> 'YYYY-MM-DD')."""
    if not value:
        return ""
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        return value["intValue"]
    if "boolValue" in value:
        return value["boolValue"]
    if "dateValue" in value:
        d = value["dateValue"] or {}
        return (
            f"{int(d.get('year', 0)):04d}-"
            f"{int(d.get('month', 0)):02d}-"
            f"{int(d.get('day', 0)):02d}"
        )
    if "dateTimeValue" in value:
        return value["dateTimeValue"]
    if "unknownValueLabel" in value:
        return value["unknownValueLabel"]
    return ""


def _fetch_rows(
    token: str,
    result_name: str,
    dimensions: list[str],
    metrics: list[str],
) -> list[dict]:
    """GET /{result}:fetchRows (paginated) -> WIDE rows keyed by GAM enum name.

    Handles both metricValueGroups shapes: one-group-per-metric (fixtures) and
    one-group-with-N-primaryValues (live multi-metric reports). MONEY stays in micros.
    """
    url = f"{_GAM_REST_BASE}/{result_name}:fetchRows"
    rows: list[dict] = []
    page_token: str | None = None
    pages = 0
    while pages < _MAX_FETCH_PAGES:
        pages += 1
        params: dict = {"pageSize": _FETCH_PAGE_SIZE}
        if page_token:
            params["pageToken"] = page_token
        data = _gam_request("GET", url, token, params=params)

        for raw in data.get("rows", []) or []:
            row: dict = {}
            dim_vals = raw.get("dimensionValues", []) or []
            for name, dv in zip(dimensions, dim_vals):
                row[name] = _coerce_dimension(dv or {})
            for i in range(len(dim_vals), len(dimensions)):
                row[dimensions[i]] = ""

            groups = raw.get("metricValueGroups", []) or []
            flat: list[dict] = []
            if len(groups) == 1 and groups[0]:
                flat = list(groups[0].get("primaryValues", []) or [])
            if len(flat) != len(metrics):
                flat = []
                for g in groups:
                    pv = (g or {}).get("primaryValues", []) or []
                    flat.append(pv[0] if pv else {})
            for name, prim in zip(metrics, flat):
                row[name] = _coerce_metric(prim or {})
            for j in range(len(flat), len(metrics)):
                row[metrics[j]] = 0
            rows.append(row)

        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return rows


def _profile_enum_lists(report_profile: str) -> tuple[list[str], list[str]]:
    """Resolve a report profile's field_ids -> GAM enum source_fields (dims, metrics)."""
    sc = _manifest().get("source_capabilities", {})
    fid_to_source = {f["field_id"]: f["source_field"] for f in sc.get("fields", [])}
    report = next((r for r in sc.get("reports", []) if r["id"] == report_profile), None)
    if report is None:
        raise ValueError(f"unknown report_profile {report_profile!r}")
    dim_enums = [fid_to_source[d] for d in report.get("dimensions", []) if d in fid_to_source]
    metric_enums = [fid_to_source[m] for m in report.get("metrics", []) if m in fid_to_source]
    return dim_enums, metric_enums


def _expand_to_marginals(
    canonical_wide: list[dict],
    metric_targets: set[str],
    dim_targets: set[str],
) -> list[dict]:
    """Wide canonical rows -> long MARGINAL fact rows (one per metric x breakdown dim).

    Each non-date dimension becomes an INDEPENDENT marginal breakdown (like GA4's
    device_category / country): the mart queries one breakdown_dimension at a time and
    never sums across marginals, so there is no double-count. MONEY values stay in micros.
    """
    breakdown_dims = [d for d in dim_targets if d != "date"]
    long_rows: list[dict] = []
    for row in canonical_wide:
        date = row.get("date", "")
        for metric in metric_targets:
            if metric not in row:
                continue
            value = row[metric]
            for dim in breakdown_dims:
                long_rows.append(
                    {
                        "date": date,
                        "metric": metric,
                        "value": value,
                        "breakdown_dimension": dim,
                        "breakdown_value": row.get(dim, ""),
                    }
                )
    return long_rows


# ---------------------------------------------------------------------------
# pull() — called by the queue worker only (AD-12: no sync API call in an LLM turn).
# ---------------------------------------------------------------------------


def pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    network_code: str | None = None,
    report_profile: str = "historical_daily",
) -> dict:
    """Run a GAM report and land marginal fact rows into raw_google_ad_manager_daily.

    Flow (async job): network context -> submit -> run -> poll -> fetch -> transform
    -> expand to marginals -> insert. MONEY stays in MICROS (÷1e6 once at read);
    currency + report timezone come from networks.get and are stored per row.

    AD-3: fresh access token used immediately as Bearer, never stored/logged.
    AD-7: pull_id passed in by the caller; never minted here.
    AI-13: unverified against a live GAM network (no test account) — verification
    stays 'blocked' until a live pass ratifies the metricValueGroups/date shapes.

    Returns {"pull_id": str, "row_count": int, "date_from": str, "date_to": str}.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2

    if not network_code:
        network_code = os.environ.get("GAM_NETWORK_CODE")
    if not network_code:
        raise ValueError(
            "network_code is required (core passes the selected GAM network from "
            "account_topology; GAM_NETWORK_CODE is a local dev fallback)."
        )

    token = nango_client.get_fresh_token(connection_id, provider="google-ad-manager")

    currency, report_timezone = _network_context(network_code, token)
    dim_enums, metric_enums = _profile_enum_lists(report_profile)

    report_id = _submit_report(
        network_code, token, dim_enums, metric_enums, date_from, date_to, "HISTORICAL"
    )
    operation_name = _run_report(network_code, token, report_id)
    result_name = _poll_operation(token, operation_name)
    wide_rows = _fetch_rows(token, result_name, dim_enums, metric_enums)

    canonical_wide = transform(wide_rows)
    man = _manifest()
    metric_targets = {
        (v if isinstance(v, str) else v.get("canonical"))
        for v in man.get("canonical_metric_mapping", {}).values()
    }
    dim_targets = set(man.get("canonical_dimension_mapping", {}).values())
    long_rows = _expand_to_marginals(canonical_wide, metric_targets, dim_targets)

    row_count = _insert_raw_rows(long_rows, pull_id, project_id, currency, report_timezone)

    # AD-3: no token in log.
    logger.info(
        "gam_pull_done: pull_id=%s network=%s rows=%d currency=%s tz=%s",
        pull_id,
        network_code,
        row_count,
        currency,
        report_timezone,
    )
    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
    }


def discover_accounts(connection_id: str) -> list[dict]:
    """List GAM networks the token can reach (account_topology selection_level=network).

    GET /networks -> [{"id": network_code, "label": display_name, "level": "network"}].
    Core owns selection / access-check / trial / backfill (AD-2).
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2

    token = nango_client.get_fresh_token(connection_id, provider="google-ad-manager")
    accounts: list[dict] = []
    for net in _list_networks(token):
        name = net.get("name", "") or ""
        code = net.get("networkCode") or (name.rsplit("/", 1)[-1] if name else "")
        if not code:
            continue
        accounts.append(
            {"id": str(code), "label": net.get("displayName") or str(code), "level": "network"}
        )
    return accounts


# ---------------------------------------------------------------------------
# transform() — manifest-driven (AD-2: no source field names hardcoded here)
# ---------------------------------------------------------------------------


def transform(raw_rows: list[dict]) -> list[dict]:
    """Map raw GAM report fields to canonical names using manifest mappings.

    AD-2: renames driven by canonical_metric_mapping + canonical_dimension_mapping.
    AD-4: non-additive RATIO metrics (CTR, eCPM, CPM/CPC rate, fill rate,
    viewability %) are DROPPED — never stored. They are reconstructed in the mart
    from additive components. The additive REVENUE metric is kept.
    MONEY: values are KEPT IN MICROS here (integer-exact). The ÷1e6 happens ONCE
    at read, with the currency — NOT per-row (avoids float drift on aggregation).
    Currency + report timezone are attached in pull() (network context), not here.
    """
    man = _manifest()

    rename_map: dict[str, str] = {}
    for src, val in man.get("canonical_metric_mapping", {}).items():
        rename_map[src] = val if isinstance(val, str) else val.get("canonical", src)
    rename_map.update(man.get("canonical_dimension_mapping", {}))

    drop_fields = _ratio_source_fields()

    result: list[dict] = []
    for row in raw_rows:
        canonical: dict = {}
        for key, value in row.items():
            if key in drop_fields:
                continue
            canonical[rename_map.get(key, key)] = value
        result.append(canonical)
    return result


# ---------------------------------------------------------------------------
# MCP tool — reads from fact_daily_kpi mart (AD-12: mart only, never raw_*).
# ---------------------------------------------------------------------------


@mcp_app.tool()
def get_google_ad_manager_report(
    project_id: str = "default",
    report_profile: str = "historical_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Google Ad Manager report — reads from fact_daily_kpi mart.

    Returns the canonical AD-1 envelope via structuredContent.
    Text channel is the lean LLM summary (<= 30 lines).

    Parameters:
        project_id: Project identifier (AD-14 placeholder).
        report_profile: Report profile id from manifest (default: 'historical_daily').
        date_from: Start date ISO-8601. Defaults to 90 days ago.
        date_to: End date ISO-8601. Defaults to yesterday.
    """
    # AD-12: reads mart only, never raw_* tables.
    from datetime import date, timedelta  # noqa: PLC0415

    if not date_to:
        date_to = (date.today() - timedelta(days=1)).isoformat()
    if not date_from:
        date_from = (date.today() - timedelta(days=90)).isoformat()

    # TODO(catalogue port): implement mart query (see ga4/gsc _query_mart pattern).
    # MONEY (Story 39.2 canonical adapter): read money via `core.money.read_units` against
    #   `dbt/seeds/money_metric_units.csv` — this connector declares native_unit='micros'
    #   (money sub-object on AD_SERVER_REVENUE), so the adapter normalization is a NO-OP
    #   (micros IS canonical) and read_units divides the canonical micros by 1e6 ONCE here,
    #   surfacing the currency from provenance — never a naked amount, never a cross-currency
    #   sum. Do NOT re-implement the /1e6 locally; the platform adapter is the single source
    #   of the read-time division (AD-2: canonical metric names come from the seed).
    # RATIOS: CTR/eCPM/CPM/CPC are RECONSTRUCTED here from additive components
    #   (clicks/impressions, revenue/impressions*1000, revenue/clicks), never read
    #   from a stored ratio column (none exists — dropped at transform).
    return {
        "schema_version": "1",
        "meta": {
            "freshness": None,
            "provenance": None,
            "alerts": [{"level": "warning", "message": "TODO: implement mart query"}],
        },
        "data": {
            "project_id": project_id,
            "report_profile": report_profile,
            "date_from": date_from,
            "date_to": date_to,
            "metrics": {},
        },
    }
