"""Campaign Manager 360 v5 governed reporting connector."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)
mcp_app = FastMCP("cm360")

API_VERSION = "v5"
PROVIDER_API_VERSION = "dfareporting-v5"
API_BASE = "https://dfareporting.googleapis.com/dfareporting/v5"
QUERY_TIMEOUT_SECONDS = 60
PAGE_SIZE = 1000
QUERY_PER_MINUTE_USER_LIMIT = 120
QUERY_DAILY_PROJECT_LIMIT = 10_000
_daily_query_lock = threading.Lock()
_daily_query_usage: dict[tuple[str, str], int] = {}

_REPORT_TYPES = {
    "standard_daily": "STANDARD",
    "floodlight_daily": "FLOODLIGHT",
    "reach": "REACH",
}


class Cm360OnboardingError(RuntimeError):
    """Typed, actionable failure during profile/advertiser selection."""


class Cm360CompatibilityError(ValueError):
    """An explicit field selection is invalid for the selected report type."""


def _manifest() -> dict:
    return json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))


def _compatibility() -> dict:
    return json.loads(
        (Path(__file__).parent / "catalog_sources" / "compatibility.json").read_text(
            encoding="utf-8"
        )
    )


def _provider_reason(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    error = payload.get("error") or {}
    errors = error.get("errors") or []
    if errors and isinstance(errors[0], dict):
        return errors[0].get("reason")
    return error.get("status") or payload.get("code")


def _raise_response(response) -> None:
    if response.status_code == 429:
        from core.quota import RateLimitError  # noqa: PLC0415

        raw = response.headers.get("Retry-After")
        try:
            retry_after = int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            retry_after = None
        raise RateLimitError("cm360", retry_after)
    try:
        original = response.json()
    except Exception:
        original = response.text
    normalized = dict(original) if isinstance(original, dict) else original
    if isinstance(normalized, dict) and (reason := _provider_reason(original)):
        normalized["code"] = reason
    from core.pull_errors import classify_http_error  # noqa: PLC0415

    raise classify_http_error(response.status_code, normalized, _manifest().get("error_map"))


def _request(client, method: str, path: str, token: str, **kwargs):
    response = client.request(
        method,
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=QUERY_TIMEOUT_SECONDS,
        **kwargs,
    )
    if response.status_code < 200 or response.status_code >= 300:
        _raise_response(response)
    return response


def discover_accounts(connection_id: str, *, _client=None, _token: str | None = None) -> list[dict]:
    """Discover every profile and advertiser reachable by the Google token."""
    from core import nango_client  # noqa: PLC0415

    token = _token or nango_client.get_fresh_token(connection_id, provider="cm360")
    client = _client or httpx.Client()
    profiles = _request(client, "GET", "/userprofiles", token).json().get("items") or []
    if not profiles:
        raise Cm360OnboardingError("No CM360 user profile is reachable by this Google consent")
    selections: list[dict] = []
    for profile in profiles:
        profile_id = str(profile["profileId"])
        advertisers = (
            _request(client, "GET", f"/userprofiles/{profile_id}/advertisers", token)
            .json()
            .get("advertisers")
            or []
        )
        for advertiser in advertisers:
            selections.append(
                {
                    "id": f"cm360_selection_{len(selections) + 1}",
                    "profile_id": profile_id,
                    "account_id": str(profile.get("accountId") or ""),
                    "subaccount_id": str(profile.get("subAccountId") or ""),
                    "advertiser_id": str(advertiser["id"]),
                    "display_name": advertiser.get("name") or str(advertiser["id"]),
                }
            )
    if not selections:
        raise Cm360OnboardingError("CM360 profiles are reachable but contain no advertiser")
    return selections


def validate_fields(report_type: str, metrics: list[str], dimensions: list[str]) -> None:
    """Fail before any provider call; explicit fields are never silently removed."""
    rule = _compatibility().get(report_type)
    if not rule:
        raise Cm360CompatibilityError(f"Unsupported CM360 report type: {report_type}")
    if not metrics:
        raise Cm360CompatibilityError("CM360 reportData.query requires at least one metric")
    invalid = sorted(
        (set(metrics) - set(rule["metrics"])) | (set(dimensions) - set(rule["dimensions"]))
    )
    if invalid:
        raise Cm360CompatibilityError(
            f"Fields are not compatible with {report_type}: {', '.join(invalid)}"
        )


def build_query_request(
    report_type: str,
    metrics: list[str],
    dimensions: list[str],
    date_from: str,
    date_to: str,
    advertiser_id: str,
) -> dict:
    validate_fields(report_type, metrics, dimensions)
    return {
        "kind": f"dfareporting#{report_type.lower()}ReportDataQuery",
        "dateRange": {"startDate": date_from, "endDate": date_to},
        "dimensions": [{"name": value} for value in dimensions],
        "metricNames": metrics,
        "dimensionFilters": [{"dimensionName": "dfa:advertiser", "value": advertiser_id}],
        "pageSize": PAGE_SIZE,
    }


def _consume_query_quota(project_id: str, *, now: datetime | None = None) -> None:
    """Reserve one query page against minute/user and UTC-day/project budgets."""
    from core import quota  # noqa: PLC0415
    from core.pull_errors import ProviderTransientError  # noqa: PLC0415

    allowed, reason = quota.pre_check("cm360", 1)
    if not allowed:
        raise ProviderTransientError(message=f"CM360 query quota unavailable: {reason}")
    day = (now or datetime.now(UTC)).date().isoformat()
    key = (project_id, day)
    with _daily_query_lock:
        spent = _daily_query_usage.get(key, 0)
        if spent >= QUERY_DAILY_PROJECT_LIMIT:
            raise ProviderTransientError(
                message=f"CM360 daily project query budget exhausted for {project_id} on {day}"
            )
        _daily_query_usage[key] = spent + 1
    quota.record_spend("cm360", 1)


def query_report_data(
    client, token: str, profile_id: str, request: dict, *, project_id: str = "onboarding"
) -> list[dict]:
    """Run the bounded JSON path and charge every nextPageToken page."""
    rows: list[dict] = []
    page_token: str | None = None
    while True:
        _consume_query_quota(project_id)
        body = json.loads(json.dumps(request))
        if page_token:
            body["pageToken"] = page_token
        payload = _request(
            client,
            "POST",
            f"/userprofiles/{profile_id}/reportData/query",
            token,
            json=body,
        ).json()
        rows.extend(payload.get("rows") or [])
        page_token = payload.get("nextPageToken")
        if not page_token:
            return rows


def canonical_request_hash(request: dict) -> str:
    encoded = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class _SavedReportFlow:
    def __init__(self, client, token: str, profile_id: str, report_id: str):
        self.client = client
        self.token = token
        self.profile_id = profile_id
        self.report_id = report_id

    def submit(self) -> str:
        payload = _request(
            self.client,
            "POST",
            f"/userprofiles/{self.profile_id}/reports/{self.report_id}/run",
            self.token,
            params={"synchronous": "false"},
        ).json()
        return str(payload["id"])

    def poll(self, file_id: str) -> str:
        payload = _request(
            self.client,
            "GET",
            f"/userprofiles/{self.profile_id}/files/{file_id}",
            self.token,
        ).json()
        status = payload.get("status")
        return {
            "PROCESSING": "processing",
            "REPORT_AVAILABLE": "completed",
            "FAILED": "failed",
            "CANCELLED": "failed",
            "EXPIRED": "expired",
        }.get(status, "pending")

    def download(self, file_id: str):
        response = _request(
            self.client,
            "GET",
            f"/userprofiles/{self.profile_id}/files/{file_id}",
            self.token,
            params={"alt": "media"},
        )
        content_type = response.headers.get("Content-Type", "")
        return response.json().get("rows", []) if "json" in content_type else response.text


def run_saved_report(
    client,
    token: str,
    profile_id: str,
    report_id: str,
    request: dict,
    connection_id: str,
    **driver_kwargs,
) -> dict:
    from core.async_reports import AsyncReportFlow, run_async_report  # noqa: PLC0415

    implementation = _SavedReportFlow(client, token, profile_id, report_id)
    flow = AsyncReportFlow(
        submit=implementation.submit,
        poll=implementation.poll,
        download=implementation.download,
        request_hash=canonical_request_hash(request),
    )
    return run_async_report(flow, ledger_ref=connection_id, **driver_kwargs)


def _normalize_saved_report_rows(payload: Any, request: dict) -> list[dict]:
    """Normalize JSON or CSV report artifacts to the bounded-query row contract."""
    if isinstance(payload, str):
        source_rows = list(csv.DictReader(io.StringIO(payload)))
    elif isinstance(payload, list):
        source_rows = payload
    else:
        raise ValueError("CM360 saved report artifact must be a JSON row list or CSV text")
    metric_names = list(request.get("metricNames") or [])
    dimension_names = [
        item.get("name") if isinstance(item, dict) else item
        for item in request.get("dimensions") or []
    ]
    normalized = []
    for row in source_rows:
        if "metrics" in row or "dimensions" in row:
            normalized.append(row)
            continue
        normalized.append(
            {
                "dimensions": {name: row.get(name) for name in dimension_names},
                "metrics": {name: row.get(name) for name in metric_names},
            }
        )
    return normalized


def check_account_access(
    connection_id: str, selection: dict, *, _client=None, _token: str | None = None
) -> dict:
    from core import nango_client  # noqa: PLC0415

    token = _token or nango_client.get_fresh_token(connection_id, provider="cm360")
    client = _client or httpx.Client()
    profile_id = str(selection["profile_id"])
    advertiser_id = str(selection["advertiser_id"])
    advertiser = _request(
        client,
        "GET",
        f"/userprofiles/{profile_id}/advertisers/{advertiser_id}",
        token,
    ).json()
    # F-TOPO-2 fix: use a recent relative window instead of a hardcoded date so that
    # accounts with no data on a specific past date are not falsely marked accessible.
    # A 30-day window ending yesterday covers typical CM360 delivery lag (T+1 data).
    _today = date.today()
    _access_end = (_today - timedelta(days=1)).isoformat()
    _access_start = (_today - timedelta(days=30)).isoformat()
    minimal = build_query_request(
        "STANDARD", ["impressions"], ["date"], _access_start, _access_end, advertiser_id
    )
    query_report_data(client, token, profile_id, minimal)
    return {"accessible": True, "advertiser_id": str(advertiser["id"]), "profile_id": profile_id}


_RAW_DDL = """
CREATE TABLE IF NOT EXISTS raw_cm360_daily (
    report_profile VARCHAR, profile_id VARCHAR, account_id VARCHAR,
    subaccount_id VARCHAR, advertiser_id VARCHAR, date VARCHAR,
    campaign_id VARCHAR, placement_id VARCHAR, creative_id VARCHAR,
    floodlight_activity_id VARCHAR, dimensions_json VARCHAR,
    metric VARCHAR, value DOUBLE, provider_value VARCHAR, non_additive BOOLEAN,
    pull_id VARCHAR, loaded_at VARCHAR, project_id VARCHAR
)
"""


def _land(rows: list[dict], context: dict) -> int:
    if os.environ.get("TOOROW_DB_MODE", "duckdb") != "duckdb":
        raise ValueError("cm360 local landing currently requires duckdb")
    import duckdb  # noqa: PLC0415

    path = os.environ.get(
        "TOOROW_DUCKDB_PATH", str(Path(__file__).parent / "seeds" / "local.duckdb")
    )
    loaded_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    non_additive = {"unique_reach", "average_frequency"}
    values = []
    for row in rows:
        dimensions = row.get("dimensions") or row
        metrics = row.get("metrics") or {}
        for metric, provider_value in metrics.items():
            try:
                value = float(str(provider_value).replace(",", ""))
            except (TypeError, ValueError):
                value = None
            values.append(
                (
                    context["report_profile"],
                    context["profile_id"],
                    context.get("account_id", ""),
                    context.get("subaccount_id", ""),
                    context["advertiser_id"],
                    dimensions.get("date"),
                    dimensions.get("campaign_id"),
                    dimensions.get("placement_id"),
                    dimensions.get("creative_id"),
                    dimensions.get("floodlight_activity_id"),
                    json.dumps(dimensions, sort_keys=True),
                    metric,
                    value,
                    str(provider_value),
                    metric in non_additive,
                    context["pull_id"],
                    loaded_at,
                    context["project_id"],
                )
            )
    connection = duckdb.connect(path)
    connection.execute(_RAW_DDL)
    if values:
        connection.executemany(
            "INSERT INTO raw_cm360_daily VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
    connection.close()
    return len(values)


def _profile_contract(profile_id: str) -> tuple[str, list[str], list[str]]:
    report = next(
        item for item in _manifest()["source_capabilities"]["reports"] if item["id"] == profile_id
    )
    return _REPORT_TYPES[profile_id], report["metrics"], report["dimensions"]


def _pull_profile(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    profile_id: str,
    selection: dict,
    *,
    _client=None,
    _token: str | None = None,
) -> dict:
    from core import nango_client  # noqa: PLC0415

    if not selection:
        raise Cm360OnboardingError("CM360 profile and advertiser selection is required")
    report_type, metrics, dimensions = _profile_contract(profile_id)
    request = build_query_request(
        report_type, metrics, dimensions, date_from, date_to, str(selection["advertiser_id"])
    )
    token = _token or nango_client.get_fresh_token(connection_id, provider="cm360")
    client = _client or httpx.Client()
    saved_report_id = selection.get("saved_report_id")
    if saved_report_id:
        outcome = run_saved_report(
            client,
            token,
            str(selection["profile_id"]),
            str(saved_report_id),
            request,
            connection_id,
        )
        if outcome["status"] != "completed":
            from core.pull_errors import ProviderTransientError  # noqa: PLC0415

            raise ProviderTransientError(
                message=(
                    "CM360 saved report is deferred and will resume without duplicate submission "
                    f"(report_ref={outcome.get('report_ref')})"
                )
            )
        rows = _normalize_saved_report_rows(outcome["rows"], request)
    else:
        rows = query_report_data(
            client, token, str(selection["profile_id"]), request, project_id=project_id
        )
    context = {
        **selection,
        "report_profile": profile_id,
        "pull_id": pull_id,
        "project_id": project_id,
    }
    row_count = _land(rows, context)
    logger.info(
        "cm360_pull_completed: pull_id=%s profile=%s rows=%d", pull_id, profile_id, row_count
    )
    return {"pull_id": pull_id, "row_count": row_count, "date_from": date_from, "date_to": date_to}


def pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    selection: dict | None = None,
) -> dict:
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "standard_daily", selection
    )


def pull_standard_daily(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    selection: dict | None = None,
) -> dict:
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "standard_daily", selection
    )


def pull_floodlight_daily(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    selection: dict | None = None,
) -> dict:
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "floodlight_daily", selection
    )


def pull_reach(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    selection: dict | None = None,
) -> dict:
    return _pull_profile(connection_id, date_from, date_to, project_id, pull_id, "reach", selection)


def transform(raw_rows: list[dict]) -> list[dict]:
    mappings = _manifest()["canonical_metric_mapping"] | _manifest()["canonical_dimension_mapping"]
    result = []
    for row in raw_rows:
        result.append(
            {
                (
                    mappings.get(key, key)
                    if isinstance(mappings.get(key, key), str)
                    else mappings[key]["canonical"]
                ): value
                for key, value in row.items()
            }
        )
    return result


@mcp_app.tool()
def get_cm360_report(
    project_id: str = "default",
    report_profile: str = "standard_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Read-only governed CM360 report envelope; extraction remains queue-only."""
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
