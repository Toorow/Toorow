"""Adobe Analytics 2.0 report-suite-aware connector."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from datetime import date as _date
from pathlib import Path
from typing import Any

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)
mcp_app = FastMCP("adobe-analytics")

API_BASE = "https://analytics.adobe.io"
PROVIDER_API_VERSION = "analytics-2.0"
TIMEOUT_SECONDS = 60
MIN_FRESHNESS_POLL_MINUTES = 30


class AdobeAnalyticsOnboardingError(RuntimeError):
    """The Adobe product profile or report-suite selection is unusable."""


class AdobeAnalyticsCompatibilityError(ValueError):
    """A component does not belong to the selected report-suite snapshot."""


class AdobeAnalyticsPartialResponseError(RuntimeError):
    """A strict report received HTTP 206 and cannot be landed completely."""


def _manifest() -> dict:
    return json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))


def _api_key(value: str | None = None) -> str:
    key = value or os.environ.get("ADOBE_ANALYTICS_API_KEY", "")
    if not key:
        raise AdobeAnalyticsOnboardingError(
            "Adobe Analytics OAuth is present but x-api-key is not configured"
        )
    return key


def _provider_code(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    error = payload.get("error") or payload
    return str(error.get("error_code") or error.get("code") or "") or None


def _raise_response(response) -> None:
    if response.status_code == 429:
        from core.quota import RateLimitError  # noqa: PLC0415

        raw = response.headers.get("Retry-After")
        try:
            retry_after = int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            retry_after = None
        raise RateLimitError("adobe-analytics", retry_after)
    try:
        original = response.json()
    except Exception:
        original = response.text
    normalized = dict(original) if isinstance(original, dict) else original
    if isinstance(normalized, dict) and (code := _provider_code(original)):
        normalized["code"] = code
    from core.pull_errors import classify_http_error  # noqa: PLC0415

    raise classify_http_error(response.status_code, normalized, _manifest().get("error_map"))


def _request(client, method: str, path: str, token: str, api_key: str, **kwargs):
    response = client.request(
        method,
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}", "x-api-key": api_key},
        timeout=TIMEOUT_SECONDS,
        **kwargs,
    )
    if response.status_code not in (200, 201, 206):
        _raise_response(response)
    return response


def _credentials(connection_id: str, api_key: str | None) -> tuple[str, str]:
    from core import nango_client  # noqa: PLC0415

    return (
        nango_client.get_fresh_token(connection_id, provider="adobe-analytics"),
        _api_key(api_key),
    )


def discover_accounts(
    connection_id: str,
    *,
    _client=None,
    _token: str | None = None,
    _api_key_value: str | None = None,
) -> list[dict]:
    """Discover organization -> global company -> report suite selections."""
    token, api_key = (
        (_token, _api_key(_api_key_value))
        if _token is not None
        else _credentials(connection_id, _api_key_value)
    )
    client = _client or httpx.Client()
    discovery = _request(client, "GET", "/discovery/me", token, api_key).json()
    selections: list[dict] = []
    for organization in discovery.get("imsOrgs") or discovery.get("organizations") or []:
        org_id = str(organization.get("imsOrgId") or organization.get("id") or "")
        companies = organization.get("companies") or organization.get("globalCompanies") or []
        for company in companies:
            company_id = str(company.get("globalCompanyId") or company.get("id") or "")
            payload = _request(
                client,
                "GET",
                f"/api/{company_id}/collections/suites",
                token,
                api_key,
                params={"limit": 1000},
            ).json()
            for suite in payload.get("content") or payload.get("reportSuites") or []:
                rsid = str(suite.get("rsid") or suite.get("id") or "")
                selections.append(
                    {
                        "id": f"adobe_analytics_selection_{len(selections) + 1}",
                        "organization_id": org_id,
                        "global_company_id": company_id,
                        "rsid": rsid,
                        "display_name": suite.get("name") or rsid,
                        "timezone": suite.get("timezoneZoneinfo") or suite.get("timezone") or "",
                        "currency": suite.get("currency") or "",
                    }
                )
    if not selections:
        raise AdobeAnalyticsOnboardingError(
            "No Adobe Analytics report suite is accessible; verify the product profile"
        )
    return selections


def _normalize_component(item: dict, kind: str) -> dict:
    component_id = str(item.get("id") or "")
    return {
        "id": component_id,
        "kind": kind,
        "name": item.get("name") or component_id,
        "type": item.get("type") or item.get("dataType") or "string",
        "reportable": bool(item.get("reportable", True)),
    }


def snapshot_catalog(
    connection_id: str,
    global_company_id: str,
    rsid: str,
    *,
    _client=None,
    _token: str | None = None,
    _api_key_value: str | None = None,
) -> dict:
    """Return a deterministic zero-planned snapshot scoped to exactly one rsid."""
    token, api_key = (
        (_token, _api_key(_api_key_value))
        if _token is not None
        else _credentials(connection_id, _api_key_value)
    )
    client = _client or httpx.Client()
    endpoints = {
        "dimension": "dimensions",
        "metric": "metrics",
        "calculated_metric": "calculatedmetrics",
        "segment": "segments",
    }
    components: list[dict] = []
    for kind, endpoint in endpoints.items():
        payload = _request(
            client,
            "GET",
            f"/api/{global_company_id}/{endpoint}",
            token,
            api_key,
            params={"rsid": rsid, "reportable": "true"},
        ).json()
        items = payload.get("content") if isinstance(payload, dict) else payload
        components.extend(_normalize_component(item, kind) for item in (items or []))
    components.sort(key=lambda item: (item["kind"], item["id"]))
    snapshot = {
        "global_company_id": global_company_id,
        "rsid": rsid,
        "components": components,
        "planned": [],
    }
    snapshot["snapshot_hash"] = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return snapshot


def validate_components(snapshot: dict, metrics: list[str], dimensions: list[str]) -> None:
    """Fail before reporting when a component is absent or belongs to another rsid."""
    if not metrics:
        raise AdobeAnalyticsCompatibilityError("At least one reportable metric is required")
    allowed = {
        (item["kind"], item["id"])
        for item in snapshot.get("components", [])
        if item.get("reportable")
    }
    invalid = sorted(
        [
            metric
            for metric in metrics
            if ("metric", metric) not in allowed and ("calculated_metric", metric) not in allowed
        ]
        + [dimension for dimension in dimensions if ("dimension", dimension) not in allowed]
    )
    if invalid:
        raise AdobeAnalyticsCompatibilityError(
            f"Components are not reportable for rsid={snapshot.get('rsid')}: {', '.join(invalid)}"
        )


def build_report_request(
    rsid: str,
    metrics: list[str],
    date_from: str,
    date_to: str,
    *,
    dimension: str | None = None,
    segments: list[str] | None = None,
    parent_item_id: str | None = None,
    page: int = 0,
) -> dict:
    body: dict[str, Any] = {
        "rsid": rsid,
        "globalFilters": [
            {"type": "dateRange", "dateRange": f"{date_from}T00:00:00.000/{date_to}T23:59:59.999"}
        ],
        "metricContainer": {
            "metrics": [
                {"id": value, "columnId": str(index)} for index, value in enumerate(metrics)
            ]
        },
        "settings": {"countRepeatInstances": True, "limit": 1000, "page": page},
    }
    if dimension:
        body["dimension"] = dimension
    if segments:
        body["globalFilters"].extend({"type": "segment", "segmentId": value} for value in segments)
    if parent_item_id:
        body["rowContainer"] = {"rowId": parent_item_id}
    return body


def run_report(
    client,
    token: str,
    api_key: str,
    global_company_id: str,
    request: dict,
    *,
    completeness: str = "strict",
) -> dict:
    """Page from zero and preserve 206 errors without fabricating metric zeroes."""
    rows: list[dict] = []
    partial_errors: list[dict] = []
    page = 0
    while True:
        body = json.loads(json.dumps(request))
        body.setdefault("settings", {})["page"] = page
        response = _request(
            client,
            "POST",
            f"/api/{global_company_id}/reports",
            token,
            api_key,
            json=body,
        )
        payload = response.json()
        if response.status_code == 206:
            errors = payload.get("columnErrors") or payload.get("errors") or []
            if completeness == "strict":
                raise AdobeAnalyticsPartialResponseError(
                    f"Adobe Analytics returned partial columns: {errors}"
                )
            partial_errors.extend(errors)
        for row in payload.get("rows") or []:
            landed = dict(row)
            landed["page"] = page
            landed["partial"] = response.status_code == 206
            rows.append(landed)
        last_page = bool(payload.get("lastPage"))
        total_pages = int(payload.get("totalPages") or 1)
        if last_page or page + 1 >= total_pages:
            return {"rows": rows, "partial_errors": partial_errors, "complete": not partial_errors}
        page += 1


def canonical_request_hash(request: dict) -> str:
    return hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


_RAW_DDL = """
CREATE TABLE IF NOT EXISTS raw_adobe_analytics_daily (
    report_profile VARCHAR, global_company_id VARCHAR, rsid VARCHAR, report_suite_timezone VARCHAR,
    date VARCHAR, dimension VARCHAR, item_id VARCHAR, item_value VARCHAR, parent_item_id VARCHAR,
    segment_ids VARCHAR, metric VARCHAR, value DOUBLE, unit VARCHAR, non_additive BOOLEAN,
    partial BOOLEAN, partial_errors VARCHAR, request_hash VARCHAR, pull_id VARCHAR,
    loaded_at VARCHAR, project_id VARCHAR
)
"""


def _land(result: dict, context: dict, metrics: list[str], dimension: str | None) -> int:
    if os.environ.get("TOOROW_DB_MODE", "duckdb") != "duckdb":
        raise ValueError("adobe-analytics local landing currently requires duckdb")
    import duckdb  # noqa: PLC0415

    path = os.environ.get("TOOROW_DUCKDB_PATH", str(Path(__file__).parent / "local.duckdb"))
    loaded_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    non_additive_tokens = ("visit", "visitor", "unique", "rate", "average", "ratio")
    # When the dimension is variables/daterangeday the Adobe API returns each
    # calendar day as a distinct row whose itemId carries the date in
    # YYYYMMDD format (e.g. "20260715").  Using that value as the row's date
    # preserves the correct grain for multi-day pulls and keeps the staging
    # QUALIFY PARTITION (…, date, …) from collapsing all days into one.
    # For all other dimensions the date is taken from the pull window
    # (date_from .. date_to).  Non-daterangeday multi-day pulls therefore
    # require the caller to issue one request per day and pass "date" in
    # context — see _pull_profile.
    _DATE_DIMENSION = "variables/daterangeday"

    def _row_date(row_item_id: str) -> str:
        if dimension == _DATE_DIMENSION and len(row_item_id) == 8 and row_item_id.isdigit():
            return f"{row_item_id[:4]}-{row_item_id[4:6]}-{row_item_id[6:]}"
        return context.get("date", context["date_from"])

    values = []
    for row in result["rows"]:
        item_id = str(row.get("itemId") or row.get("item_id") or "")
        item_value = str(row.get("value") or row.get("item_value") or "")
        row_date = _row_date(item_id)
        data = row.get("data") or []
        for index, metric in enumerate(metrics):
            provider_value = data[index] if index < len(data) else None
            if provider_value is None:
                continue
            values.append(
                (
                    context["report_profile"],
                    context["global_company_id"],
                    context["rsid"],
                    context.get("timezone", ""),
                    row_date,
                    dimension or "",
                    item_id,
                    item_value,
                    context.get("parent_item_id", ""),
                    json.dumps(context.get("segments") or [], separators=(",", ":")),
                    metric,
                    float(provider_value),
                    context.get("currency", ""),
                    any(token in metric.lower() for token in non_additive_tokens),
                    bool(row.get("partial")),
                    json.dumps(result["partial_errors"], separators=(",", ":")),
                    context["request_hash"],
                    context["pull_id"],
                    loaded_at,
                    context["project_id"],
                )
            )
    connection = duckdb.connect(path)
    connection.execute(_RAW_DDL)
    if values:
        connection.executemany(
            "INSERT INTO raw_adobe_analytics_daily VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
    connection.close()
    return len(values)


def _pull_profile(connection_id, date_from, date_to, project_id, pull_id, profile_id, selection):
    if not selection:
        raise AdobeAnalyticsOnboardingError("A report-suite selection is required")
    token, api_key = _credentials(connection_id, selection.get("api_key"))
    profile = next(
        item for item in _manifest()["source_capabilities"]["reports"] if item["id"] == profile_id
    )
    metrics = selection.get("metrics") or profile["metrics"]
    dimension = selection.get("dimension")
    dimensions = [dimension] if dimension else []
    snapshot = selection.get("catalog_snapshot") or snapshot_catalog(
        connection_id,
        selection["global_company_id"],
        selection["rsid"],
        _token=token,
        _api_key_value=api_key,
    )
    validate_components(snapshot, metrics, dimensions)
    request = build_report_request(
        selection["rsid"],
        metrics,
        date_from,
        date_to,
        dimension=dimension,
        segments=selection.get("segments"),
        parent_item_id=selection.get("parent_item_id"),
    )
    result = run_report(
        httpx.Client(),
        token,
        api_key,
        selection["global_company_id"],
        request,
        completeness=selection.get("completeness", "strict"),
    )
    context = {
        **selection,
        "report_profile": profile_id,
        "date_from": date_from,
        "date_to": date_to,
        "request_hash": canonical_request_hash(request),
        "pull_id": pull_id,
        "project_id": project_id,
    }
    count = _land(result, context, metrics, dimension)
    return {
        "pull_id": pull_id,
        "row_count": count,
        "date_from": date_from,
        "date_to": date_to,
        "complete": result["complete"],
    }


def pull(connection_id, date_from, date_to, project_id, pull_id, selection=None):
    """AI-58 dispatch: default profile = daily_kpi (grain date × rsid × dimension × item)."""
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "daily_kpi", selection
    )


def pull_daily_kpi(connection_id, date_from, date_to, project_id, pull_id, selection=None):
    """AI-58 dispatch: daily KPI report (grain date × rsid × dimension × item)."""
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "daily_kpi", selection
    )


def pull_totals(connection_id, date_from, date_to, project_id, pull_id, selection=None):
    """AI-58 dispatch: report-suite totals (grain rsid × segment_ids, no date breakdown)."""
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "totals", selection
    )


def pull_bounded_breakdown(connection_id, date_from, date_to, project_id, pull_id, selection=None):
    """AI-58 dispatch: bounded breakdown (grain date × rsid × dimension × item × parent_item)."""
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "bounded_breakdown", selection
    )


def transform(raw_rows: list[dict]) -> list[dict]:
    mappings = _manifest()["canonical_metric_mapping"] | _manifest()["canonical_dimension_mapping"]
    return [
        {
            (
                mappings.get(key, key)
                if isinstance(mappings.get(key, key), str)
                else mappings[key]["canonical"]
            ): value
            for key, value in row.items()
        }
        for row in raw_rows
    ]


# ---------------------------------------------------------------------------
# MCP tool helpers — AI-45/56 seam reads staged data (AD-12: staging only).
# ---------------------------------------------------------------------------

_DEFAULT_DUCKDB_PATH = os.path.join(os.path.dirname(__file__), "local.duckdb")

_STAGING_QUERY = """
    SELECT
        metric,
        dimension,
        item_id,
        item_value,
        rsid,
        SUM(value) AS value,
        MAX(pull_id) AS pull_id,
        MAX(loaded_at) AS freshness
    FROM {table}
    WHERE project_id = {p_project}
      AND report_profile = {p_profile}
      AND date BETWEEN {p_from} AND {p_to}
    GROUP BY metric, dimension, item_id, item_value, rsid
    ORDER BY metric, dimension, item_id
"""


def _get_db_mode() -> str:
    return os.environ.get("TOOROW_DB_MODE", "duckdb")


def _get_duckdb_path() -> str:
    return os.environ.get("TOOROW_DUCKDB_PATH", _DEFAULT_DUCKDB_PATH)


def _query_duckdb(sql: str, params: list, duckdb_path: str) -> list[dict]:
    import duckdb  # noqa: PLC0415

    try:
        con = duckdb.connect(duckdb_path, read_only=True)
    except Exception:
        return []
    try:
        rel = con.execute(sql, params)
        cols = [d[0] for d in rel.description]
        return [dict(zip(cols, row)) for row in rel.fetchall()]
    except Exception:
        return []
    finally:
        con.close()


def _query_staging(
    date_from: str, date_to: str, project_id: str, report_profile: str
) -> list[dict]:
    """Read from stg_adobe_analytics_daily; returns [] gracefully when absent."""
    db_mode = _get_db_mode()
    if db_mode != "duckdb":
        # BigQuery mart reads not yet wired for adobe-analytics; degrade gracefully.
        return []
    table = "stg_adobe_analytics_daily"
    sql = _STAGING_QUERY.format(table=table, p_project="?", p_profile="?", p_from="?", p_to="?")
    return _query_duckdb(sql, [project_id, report_profile, date_from, date_to], _get_duckdb_path())


def _build_envelope(
    rows: list[dict],
    report_profile: str,
    date_from: str,
    date_to: str,
    project_id: str,
) -> dict:
    """Build the canonical AD-1 envelope from staged rows."""
    pull_ids = {r["pull_id"] for r in rows if r.get("pull_id")}
    freshness_values = [r["freshness"] for r in rows if r.get("freshness")]
    latest_pull_id = max(pull_ids) if pull_ids else None
    latest_freshness = max(freshness_values) if freshness_values else None

    data_by_metric: dict[str, list[dict]] = {}
    for r in rows:
        data_by_metric.setdefault(r["metric"], []).append(
            {
                "dimension": r.get("dimension"),
                "item_id": r.get("item_id"),
                "item_value": r.get("item_value"),
                "rsid": r.get("rsid"),
                "value": r["value"],
            }
        )

    provenance = (
        {
            "source_system": "adobe-analytics",
            "source_field": "stg_adobe_analytics_daily",
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
def get_adobe_analytics_report(
    project_id: str = "default",
    report_profile: str = "daily_kpi",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Read-only governed Adobe Analytics envelope (AI-45/56 seam).

    Reads from stg_adobe_analytics_daily (staged DuckDB view).
    Degrades gracefully to empty metrics when the table is absent.

    Report profiles: daily_kpi, totals, bounded_breakdown
    Metrics: occurrences, page_views, orders, revenue, visits, visitors,
             bounce_rate, average_time_spent

    Parameters:
        project_id: Project identifier (default: 'default')
        report_profile: One of the 3 declared profiles
        date_from: Start date ISO-8601 (e.g. '2026-07-01'). Defaults to 30 days ago.
        date_to: End date ISO-8601 (e.g. '2026-07-31'). Defaults to yesterday.
    """
    if not date_to:
        date_to = (_date.today() - timedelta(days=1)).isoformat()
    if not date_from:
        date_from = (_date.today() - timedelta(days=30)).isoformat()

    try:
        rows = _query_staging(date_from, date_to, project_id, report_profile)
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
