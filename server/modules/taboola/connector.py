"""Taboola Backstage API 1.0 KPI and campaign-history connector."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)
mcp_app = FastMCP("taboola")
API_BASE = "https://backstage.taboola.com/backstage/api/1.0"
PROVIDER_API_VERSION = "backstage-1.0"
TOP_CONTENT_LIMIT = 1000
REFETCH_DAYS = (3, 14)
PRIOR_MONTH_REFRESH_DAY = 5

REPORT_DIMENSIONS = {
    "campaign_summary": {
        "campaign_breakdown",
        "day",
        "site_breakdown",
        "country_breakdown",
        "platform_breakdown",
    },
    "top_campaign_content": {"item_breakdown"},
    "campaign_history": {"by_campaign", "by_account", "by_day_count"},
}
STATIC_FIELDS = {
    "date",
    "account_id",
    "campaign_id",
    "campaign_name",
    "item_id",
    "item_name",
    "site_id",
    "site_name",
    "country",
    "platform",
    "impressions",
    "clicks",
    "spent",
    "conversions",
    "conversion_value",
    "record_id",
    "change_time",
    "change_type",
    "entity_id",
    "update_time",
}


class TaboolaOnboardingError(RuntimeError):
    """Backstage credentials cannot access a selectable advertiser account."""


class TaboolaCompatibilityError(ValueError):
    """The report/dimension/filter/metadata shape is not supported."""


def _manifest() -> dict:
    return json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))


def _provider_code(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    return str(payload.get("error_code") or payload.get("code") or "") or None


def _raise_response(response) -> None:
    if response.status_code == 429:
        from core.quota import RateLimitError  # noqa: PLC0415

        raw = response.headers.get("Retry-After")
        try:
            retry_after = int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            retry_after = None
        raise RateLimitError("taboola", retry_after)
    try:
        original = response.json()
    except Exception:
        original = response.text
    normalized = dict(original) if isinstance(original, dict) else original
    if isinstance(normalized, dict) and (code := _provider_code(original)):
        normalized["code"] = code
    from core.pull_errors import classify_http_error  # noqa: PLC0415

    raise classify_http_error(response.status_code, normalized, _manifest().get("error_map"))


def _request(client, method, path, token, **kwargs):
    response = client.request(
        method,
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
        **kwargs,
    )
    if response.status_code < 200 or response.status_code >= 300:
        _raise_response(response)
    return response


def _token(connection_id: str) -> str:
    """Nango owns the 12-hour client-credential token cache and renewal."""
    from core import nango_client  # noqa: PLC0415

    return nango_client.get_fresh_token(connection_id, provider="taboola")


def discover_accounts(connection_id: str, *, _client=None, _token_value=None) -> list[dict]:
    token = _token_value or _token(connection_id)
    client = _client or httpx.Client()
    payload = _request(client, "GET", "/users/current/allowed-accounts/", token).json()
    accounts = payload.get("results") or payload.get("accounts") or []
    selections = [
        {
            "id": f"taboola_selection_{index}",
            "account_id": str(item.get("account_id") or item.get("id") or ""),
            "display_name": item.get("name") or str(item.get("account_id") or ""),
            "timezone": item.get("timezone") or "",
            "currency": item.get("currency") or "",
        }
        for index, item in enumerate(accounts, start=1)
        if item.get("account_id") or item.get("id")
    ]
    if not selections:
        raise TaboolaOnboardingError(
            "No allowed Taboola advertiser account; verify Backstage reporting permission"
        )
    return selections


def validate_shape(report: str, dimension: str, filters: dict | None = None) -> None:
    if report not in REPORT_DIMENSIONS or dimension not in REPORT_DIMENSIONS[report]:
        raise TaboolaCompatibilityError(f"Illegal Taboola report/dimension: {report}/{dimension}")
    allowed_filters = {"start_date", "end_date", "campaign", "site", "country", "platform"}
    invalid = sorted(set(filters or {}) - allowed_filters)
    if invalid:
        raise TaboolaCompatibilityError(f"Unsupported Taboola filters: {', '.join(invalid)}")


def validate_response_metadata(rows: list[dict], metadata: dict) -> list[str]:
    definitions = metadata.get("fields") or metadata.get("field_definitions") or []
    declared = {
        str(item.get("id") or item.get("name"))
        for item in definitions
        if item.get("id") or item.get("name")
    }
    returned = set().union(*(row.keys() for row in rows)) if rows else set()
    unknown = sorted(returned - STATIC_FIELDS - declared)
    if unknown:
        raise TaboolaCompatibilityError(
            f"Response contains undeclared dynamic column(s): {', '.join(unknown)}"
        )
    return sorted(returned - STATIC_FIELDS)


def fetch_report(
    client,
    token: str,
    account_id: str,
    report: str,
    dimension: str,
    *,
    filters: dict | None = None,
    page_size: int = 100,
) -> dict:
    validate_shape(report, dimension, filters)
    if report == "top_campaign_content":
        page_size = min(page_size, TOP_CONTENT_LIMIT)
    path_name = report.replace("_", "-")
    page = 1
    rows: list[dict] = []
    metadata: dict = {}
    while True:
        params = {**(filters or {}), "page": page, "page_size": page_size}
        payload = _request(
            client,
            "GET",
            f"/{account_id}/reports/{path_name}/dimensions/{dimension}",
            token,
            params=params,
        ).json()
        batch = payload.get("results") or []
        rows.extend(batch)
        metadata = payload.get("metadata") or metadata
        total = int(payload.get("total") or metadata.get("total") or len(rows))
        if (
            not batch
            or len(rows) >= total
            or (report == "top_campaign_content" and len(rows) >= TOP_CONTENT_LIMIT)
        ):
            rows = rows[:TOP_CONTENT_LIMIT] if report == "top_campaign_content" else rows
            dynamic = validate_response_metadata(rows, metadata)
            return {
                "rows": rows,
                "metadata": metadata,
                "dynamic_columns": dynamic,
                "complete": report != "top_campaign_content" or total <= TOP_CONTENT_LIMIT,
                "row_limit": TOP_CONTENT_LIMIT if report == "top_campaign_content" else None,
            }
        page += 1


def should_refresh_prior_month(run_date: date) -> bool:
    return run_date.day == PRIOR_MONTH_REFRESH_DAY


def prior_month_window(run_date: date) -> tuple[str, str]:
    first_current = run_date.replace(day=1)
    last_prior = first_current - timedelta(days=1)
    return last_prior.replace(day=1).isoformat(), last_prior.isoformat()


_KPI_DDL = """
CREATE TABLE IF NOT EXISTS raw_taboola_daily (
 account_id VARCHAR, report VARCHAR, dimension VARCHAR, date VARCHAR, campaign_id VARCHAR,
 item_id VARCHAR, site_id VARCHAR, breakdown_json VARCHAR, metric VARCHAR, value DOUBLE,
 non_additive BOOLEAN, timezone VARCHAR, currency VARCHAR, update_time VARCHAR,
 pull_id VARCHAR, loaded_at VARCHAR, project_id VARCHAR
)
"""
_HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS raw_taboola_history (
 account_id VARCHAR, dimension VARCHAR, record_id VARCHAR, change_time VARCHAR,
 change_type VARCHAR, entity_id VARCHAR, payload_json VARCHAR, pull_id VARCHAR,
 loaded_at VARCHAR, project_id VARCHAR
)
"""


def _land_kpi(result, context):
    import duckdb  # noqa: PLC0415

    path = os.environ.get("TOOROW_DUCKDB_PATH", str(Path(__file__).parent / "local.duckdb"))
    loaded_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    values = []
    dimensions = {
        "date",
        "campaign_id",
        "campaign_name",
        "item_id",
        "item_name",
        "site_id",
        "site_name",
        "country",
        "platform",
        "update_time",
    }
    non_additive_tokens = ("rate", "ctr", "roas", "cpc", "cpm", "cpa", "reach", "frequency")
    for row in result["rows"]:
        breakdown = {key: value for key, value in row.items() if key in dimensions}
        for metric, value in row.items():
            if metric in dimensions or not isinstance(value, (int, float)):
                continue
            values.append(
                (
                    context["account_id"],
                    context["report"],
                    context["dimension"],
                    str(row.get("date") or context["date_from"]),
                    str(row.get("campaign_id") or ""),
                    str(row.get("item_id") or ""),
                    str(row.get("site_id") or ""),
                    json.dumps(breakdown, sort_keys=True, separators=(",", ":")),
                    metric,
                    float(value),
                    any(token in metric.lower() for token in non_additive_tokens),
                    context.get("timezone", ""),
                    context.get("currency", ""),
                    str(row.get("update_time") or ""),
                    context["pull_id"],
                    loaded_at,
                    context["project_id"],
                )
            )
    connection = duckdb.connect(path)
    connection.execute(_KPI_DDL)
    if values:
        connection.executemany(
            "INSERT INTO raw_taboola_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values
        )
    connection.close()
    return len(values)


def _land_history(result, context):
    import duckdb  # noqa: PLC0415

    path = os.environ.get("TOOROW_DUCKDB_PATH", str(Path(__file__).parent / "local.duckdb"))
    loaded_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    values = []
    for row in result["rows"]:
        values.append(
            (
                context["account_id"],
                context["dimension"],
                str(row.get("record_id") or row.get("id") or ""),
                str(row.get("change_time") or row.get("timestamp") or ""),
                str(row.get("change_type") or row.get("type") or ""),
                str(row.get("entity_id") or row.get("campaign_id") or ""),
                json.dumps(row, sort_keys=True, separators=(",", ":")),
                context["pull_id"],
                loaded_at,
                context["project_id"],
            )
        )
    connection = duckdb.connect(path)
    connection.execute(_HISTORY_DDL)
    if values:
        connection.executemany(
            "INSERT INTO raw_taboola_history VALUES (?,?,?,?,?,?,?,?,?,?)", values
        )
    connection.close()
    return len(values)


def _pull_profile(connection_id, date_from, date_to, project_id, pull_id, profile, selection):
    if not selection:
        raise TaboolaOnboardingError("A Taboola advertiser account selection is required")
    token = _token(connection_id)
    report = {
        "campaign_summary": "campaign_summary",
        "top_campaign_content": "top_campaign_content",
        "campaign_history": "campaign_history",
    }[profile]
    dimension = (
        selection.get("dimension")
        or {
            "campaign_summary": "campaign_breakdown",
            "top_campaign_content": "item_breakdown",
            "campaign_history": "by_campaign",
        }[profile]
    )
    filters = {**selection.get("filters", {}), "start_date": date_from, "end_date": date_to}
    result = fetch_report(
        httpx.Client(),
        token,
        selection["account_id"],
        report,
        dimension,
        filters=filters,
        page_size=selection.get("page_size", 100),
    )
    context = {
        **selection,
        "report": report,
        "dimension": dimension,
        "date_from": date_from,
        "pull_id": pull_id,
        "project_id": project_id,
    }
    count = (
        _land_history(result, context)
        if profile == "campaign_history"
        else _land_kpi(result, context)
    )
    return {
        "pull_id": pull_id,
        "row_count": count,
        "date_from": date_from,
        "date_to": date_to,
        "complete": result["complete"],
        "refetch_days": list(REFETCH_DAYS),
        "prior_month_refresh_day": PRIOR_MONTH_REFRESH_DAY,
    }


def pull(connection_id, date_from, date_to, project_id, pull_id, selection=None):
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "campaign_summary", selection
    )


def pull_campaign_summary(connection_id, date_from, date_to, project_id, pull_id, selection=None):
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "campaign_summary", selection
    )


def pull_top_campaign_content(
    connection_id, date_from, date_to, project_id, pull_id, selection=None
):
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "top_campaign_content", selection
    )


def pull_campaign_history(connection_id, date_from, date_to, project_id, pull_id, selection=None):
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "campaign_history", selection
    )


def transform(raw_rows):
    mappings = _manifest()["canonical_metric_mapping"] | _manifest()["canonical_dimension_mapping"]
    return [
        {
            (
                mappings.get(k, k)
                if isinstance(mappings.get(k, k), str)
                else mappings[k]["canonical"]
            ): v
            for k, v in row.items()
        }
        for row in raw_rows
    ]


# ---------------------------------------------------------------------------
# transform_events() -- canonical event mapping for the campaign_history profile.
# ---------------------------------------------------------------------------

# Mirrors canonical_event_mapping: change_type -> event_type (pass-through),
# change_count -> events (Layer 5 landing path).
# Pure function (no I/O) -- unit-tested via golden_events/expected_events fixtures.


def transform_events(
    raw_rows: list[dict],
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict]:
    """Map raw campaign_history rows to canonical event dicts (Layer 5, pure function).

    Input: list of dicts with keys record_id, change_time (ISO datetime), change_type,
    entity_id, and optional payload_json / other fields from _land_history.
    Output: canonical event dicts with keys event_type, event_date, event_id,
    label, platform, source -- ready for persist_context_event().

    event_type  = change_type value (passed through; each history record is one change).
    event_date  = change_time[:10] (YYYY-MM-DD, UTC date of the change).
    event_id    = record_id (immutable Backstage history record id for dedup).
    label       = "{change_type} on {entity_id}" (human-readable marker).
    platform    = "taboola".
    source      = "taboola".

    Date window: change_time-based rows are already window-scoped by the API
    (start_date/end_date filter on change_time).  When date_from/date_to are
    supplied (golden-replay path omits them), rows outside the window are dropped
    client-side -- mirrors youtube-analytics guard for unbounded replays.

    Validity: a row with a change_time shorter than 10 chars (missing / malformed)
    is skipped; persist_context_event would reject an empty event_date downstream.
    """
    events: list[dict] = []
    for row in raw_rows:
        change_time = str(row.get("change_time") or row.get("timestamp") or "")
        if len(change_time) < 10:
            logger.debug(
                "transform_events: skipping row with invalid change_time=%r", change_time
            )
            continue
        event_date = change_time[:10]
        if date_from is not None and event_date < date_from:
            continue
        if date_to is not None and event_date > date_to:
            continue
        change_type = str(row.get("change_type") or row.get("type") or "campaign_change")
        entity_id = str(row.get("entity_id") or row.get("campaign_id") or "")
        record_id = str(row.get("record_id") or row.get("id") or "")
        events.append(
            {
                "event_type": change_type,
                "event_date": event_date,
                "event_id": record_id,
                "label": f"{change_type} on {entity_id}",
                "platform": "taboola",
                "source": "taboola",
            }
        )
    return events


@mcp_app.tool()
def get_taboola_report(
    project_id: str = "default",
    report_profile: str = "campaign_summary",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Read-only governed Taboola report envelope."""
    return {
        "schema_version": "1",
        "meta": {"freshness": None, "provenance": None, "alerts": []},
        "data": {
            "project_id": project_id,
            "report_profile": report_profile,
            "date_from": date_from,
            "date_to": date_to,
            "metrics": {},
        },
    }
