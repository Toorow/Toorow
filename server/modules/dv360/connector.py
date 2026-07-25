"""Display & Video 360 v4 discovery and Bid Manager v2 reporting connector."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)
mcp_app = FastMCP("dv360")

DISCOVERY_API_VERSION = "v4"
REPORTING_API_VERSION = "v2"
DISCOVERY_BASE = "https://displayvideo.googleapis.com/v4"
REPORTING_BASE = "https://doubleclickbidmanager.googleapis.com/v2"
REQUEST_TIMEOUT_SECONDS = 60
V4_PROJECT_PER_MINUTE = 1_500
V4_PROJECT_WRITES_PER_MINUTE = 700
V4_ADVERTISER_PER_MINUTE = 300
V4_ADVERTISER_WRITES_PER_MINUTE = 150
BID_MANAGER_PER_DAY_PROJECT = 2_000
BID_MANAGER_QPS_PROJECT = 4
BID_MANAGER_PER_MINUTE_USER = 240

_quota_lock = threading.Lock()
_v4_minute_usage: dict[tuple[str, str], int] = {}
_v4_advertiser_minute_usage: dict[tuple[str, str, str], int] = {}
_bid_daily_usage: dict[tuple[str, str], int] = {}
_bid_second_usage: dict[tuple[str, str], int] = {}


class Dv360OnboardingError(RuntimeError):
    """Typed failure for partner/advertiser discovery or selection."""


class Dv360CompatibilityError(ValueError):
    """A requested report definition is not legal for its report family."""


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
    details = error.get("details") or []
    for detail in details:
        if isinstance(detail, dict):
            reason = detail.get("reason") or detail.get("metadata", {}).get("reason")
            if reason:
                return reason
    return error.get("status") or payload.get("code")


def _raise_response(response, quota_domain: str) -> None:
    try:
        original = response.json()
    except Exception:
        original = response.text
    reason = _provider_reason(original)
    if response.status_code == 429 or reason == "RESOURCE_EXHAUSTED":
        from core.quota import RateLimitError  # noqa: PLC0415

        raw = response.headers.get("Retry-After")
        try:
            retry_after = int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            retry_after = None
        raise RateLimitError(quota_domain, retry_after)
    normalized = dict(original) if isinstance(original, dict) else original
    if isinstance(normalized, dict) and reason:
        normalized["code"] = reason
    from core.pull_errors import classify_http_error  # noqa: PLC0415

    raise classify_http_error(response.status_code, normalized, _manifest().get("error_map"))


def _request(client, method: str, base: str, path: str, token: str, quota_domain: str, **kwargs):
    response = client.request(
        method,
        f"{base}{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=REQUEST_TIMEOUT_SECONDS,
        **kwargs,
    )
    if response.status_code < 200 or response.status_code >= 300:
        _raise_response(response, quota_domain)
    return response


def _charge_v4(
    quota_key: str,
    advertiser_id: str | None = None,
    *,
    now: datetime | None = None,
) -> None:
    """Charge read-only v4 discovery against project and advertiser minute domains."""
    from core.pull_errors import ProviderTransientError  # noqa: PLC0415

    minute = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M")
    project_key = (quota_key, minute)
    with _quota_lock:
        project_spent = _v4_minute_usage.get(project_key, 0)
        if project_spent >= V4_PROJECT_PER_MINUTE:
            raise ProviderTransientError(
                message=f"DV360 v4 project minute budget exhausted for {quota_key}"
            )
        if advertiser_id:
            advertiser_key = (quota_key, advertiser_id, minute)
            advertiser_spent = _v4_advertiser_minute_usage.get(advertiser_key, 0)
            if advertiser_spent >= V4_ADVERTISER_PER_MINUTE:
                raise ProviderTransientError(
                    message=f"DV360 v4 advertiser minute budget exhausted for {advertiser_id}"
                )
            _v4_advertiser_minute_usage[advertiser_key] = advertiser_spent + 1
        _v4_minute_usage[project_key] = project_spent + 1


def _paged_items(
    client,
    token: str,
    path: str,
    item_key: str,
    quota_key: str,
    params: dict | None = None,
):
    items: list[dict] = []
    page_token = None
    while True:
        _charge_v4(quota_key)
        page_params = dict(params or {})
        if page_token:
            page_params["pageToken"] = page_token
        payload = _request(
            client, "GET", DISCOVERY_BASE, path, token, "dv360-v4", params=page_params
        ).json()
        items.extend(payload.get(item_key) or [])
        page_token = payload.get("nextPageToken")
        if not page_token:
            return items


def discover_accounts(connection_id: str, *, _client=None, _token: str | None = None) -> list[dict]:
    """Discover visible partners and advertisers while preserving permission context."""
    from core import nango_client  # noqa: PLC0415

    token = _token or nango_client.get_fresh_token(connection_id, provider=None)
    client = _client or httpx.Client()
    partners = _paged_items(client, token, "/partners", "partners", connection_id)
    selections = []
    for partner in partners:
        partner_id = str(partner["partnerId"])
        advertisers = _paged_items(
            client,
            token,
            "/advertisers",
            "advertisers",
            connection_id,
            params={"filter": f"partnerId={partner_id}"},
        )
        for advertiser in advertisers:
            selections.append(
                {
                    "id": f"dv360_selection_{len(selections) + 1}",
                    "partner_id": partner_id,
                    "advertiser_id": str(advertiser["advertiserId"]),
                    "display_name": advertiser.get("displayName")
                    or str(advertiser["advertiserId"]),
                    "partner_role": partner.get("entityStatus", "unknown"),
                    "advertiser_status": advertiser.get("entityStatus", "unknown"),
                    "currency": advertiser.get("generalConfig", {}).get("currencyCode", ""),
                    "timezone": advertiser.get("generalConfig", {}).get("timeZone", ""),
                }
            )
    if not selections:
        raise Dv360OnboardingError(
            "No DV360 advertiser is visible; verify the Read only role and partner permissions"
        )
    return selections


def _profile(profile_id: str) -> dict:
    return next(
        item for item in _manifest()["source_capabilities"]["reports"] if item["id"] == profile_id
    )


def validate_report_shape(profile_id: str, metrics: list[str], dimensions: list[str]) -> None:
    legal = _compatibility()["profiles"].get(profile_id)
    if legal is None:
        raise Dv360CompatibilityError(f"Unknown DV360 report profile: {profile_id}")
    invalid_metrics = sorted(set(metrics) - set(legal["metrics"]))
    invalid_dimensions = sorted(set(dimensions) - set(legal["dimensions"]))
    if invalid_metrics or invalid_dimensions:
        raise Dv360CompatibilityError(
            f"Incompatible DV360 selection for {profile_id}: "
            f"metrics={invalid_metrics}, dimensions={invalid_dimensions}"
        )


def _google_date(value: str) -> dict:
    parsed = datetime.strptime(value, "%Y-%m-%d").date()
    return {"year": parsed.year, "month": parsed.month, "day": parsed.day}


def _source_field_map() -> dict[str, str]:
    return {
        item["field_id"]: item["source_field"]
        for item in _manifest()["source_capabilities"]["fields"]
    }


def build_query_definition(
    profile_id: str,
    metrics: list[str],
    dimensions: list[str],
    date_from: str,
    date_to: str,
    advertiser_id: str,
) -> dict:
    validate_report_shape(profile_id, metrics, dimensions)
    source_fields = _source_field_map()
    return {
        "metadata": {
            "title": "",
            "dataRange": {
                "range": "CUSTOM_DATES",
                "customStartDate": _google_date(date_from),
                "customEndDate": _google_date(date_to),
            },
            "format": "CSV",
        },
        "params": {
            "type": _compatibility()["profiles"][profile_id]["report_type"],
            "groupBys": [source_fields[value] for value in dimensions],
            "filters": [{"type": "FILTER_ADVERTISER", "value": advertiser_id}],
            "metrics": [source_fields[value] for value in metrics],
        },
        "schedule": {"frequency": "ONE_TIME"},
    }


def canonical_query_hash(definition: dict) -> str:
    value = json.loads(json.dumps(definition))
    value.get("metadata", {}).pop("title", None)
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _charge_bid_manager(project_id: str, *, now: datetime | None = None) -> None:
    from core import quota  # noqa: PLC0415
    from core.pull_errors import ProviderTransientError  # noqa: PLC0415

    allowed, reason = quota.pre_check("dv360-bid-manager", 1)
    if not allowed:
        raise ProviderTransientError(message=f"DV360 Bid Manager quota unavailable: {reason}")
    instant = now or datetime.now(UTC)
    day_key = (project_id, instant.date().isoformat())
    second_key = (project_id, instant.strftime("%Y-%m-%dT%H:%M:%S"))
    with _quota_lock:
        daily_spent = _bid_daily_usage.get(day_key, 0)
        if daily_spent >= BID_MANAGER_PER_DAY_PROJECT:
            raise ProviderTransientError(
                message=f"DV360 Bid Manager daily budget exhausted for {project_id}"
            )
        second_spent = _bid_second_usage.get(second_key, 0)
        if second_spent >= BID_MANAGER_QPS_PROJECT:
            raise ProviderTransientError(
                message=f"DV360 Bid Manager QPS budget exhausted for {project_id}"
            )
        _bid_daily_usage[day_key] = daily_spent + 1
        _bid_second_usage[second_key] = second_spent + 1
    quota.record_spend("dv360-bid-manager", 1)


def ensure_saved_query(client, token: str, definition: dict, project_id: str) -> str:
    """Reuse a canonical saved query; create once when no reusable definition exists."""
    request_hash = canonical_query_hash(definition)
    title = f"toorow-{request_hash[:32]}"
    page_token = None
    while True:
        _charge_bid_manager(project_id)
        params = {"pageToken": page_token} if page_token else None
        payload = _request(
            client,
            "GET",
            REPORTING_BASE,
            "/queries",
            token,
            "dv360-bid-manager",
            params=params,
        ).json()
        for query in payload.get("queries") or []:
            if query.get("metadata", {}).get("title") == title:
                return str(query["queryId"])
        page_token = payload.get("nextPageToken")
        if not page_token:
            break
    body = json.loads(json.dumps(definition))
    body["metadata"]["title"] = title
    _charge_bid_manager(project_id)
    created = _request(
        client, "POST", REPORTING_BASE, "/queries", token, "dv360-bid-manager", json=body
    ).json()
    return str(created["queryId"])


class _BidManagerReportFlow:
    def __init__(self, client, token: str, query_id: str, project_id: str):
        self.client = client
        self.token = token
        self.query_id = query_id
        self.project_id = project_id
        self.artifact_path: str | None = None

    def submit(self) -> str:
        _charge_bid_manager(self.project_id)
        payload = _request(
            self.client,
            "POST",
            REPORTING_BASE,
            f"/queries/{self.query_id}:run",
            self.token,
            "dv360-bid-manager",
            json={},
        ).json()
        report_id = (payload.get("key") or {}).get("reportId") or payload.get("reportId")
        if not report_id:
            raise ValueError(f"queries.run response missing reportId: {payload!r}")
        return str(report_id)

    def poll(self, report_id: str) -> str:
        _charge_bid_manager(self.project_id)
        payload = _request(
            self.client,
            "GET",
            REPORTING_BASE,
            f"/queries/{self.query_id}/reports/{report_id}",
            self.token,
            "dv360-bid-manager",
        ).json()
        metadata = payload.get("metadata") or {}
        state = metadata.get("status", {}).get("state") or metadata.get("status")
        if state == "DONE":
            self.artifact_path = metadata.get("googleCloudStoragePath")
        return {
            "QUEUED": "pending",
            "RUNNING": "processing",
            "DONE": "completed",
            "FAILED": "failed",
        }.get(state, "pending")

    def download(self, report_id: str):  # noqa: ARG002
        if not self.artifact_path:
            raise ValueError("DV360 completed report did not include googleCloudStoragePath")
        response = self.client.request(
            "GET",
            self.artifact_path,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code < 200 or response.status_code >= 300:
            _raise_response(response, "dv360-bid-manager")
        return response.text


def run_saved_query(
    client,
    token: str,
    query_id: str,
    definition: dict,
    connection_id: str,
    project_id: str,
    **driver_kwargs,
) -> dict:
    from core.async_reports import AsyncReportFlow, run_async_report  # noqa: PLC0415

    implementation = _BidManagerReportFlow(client, token, query_id, project_id)
    flow = AsyncReportFlow(
        submit=implementation.submit,
        poll=implementation.poll,
        download=implementation.download,
        request_hash=canonical_query_hash(definition),
    )
    return run_async_report(flow, ledger_ref=connection_id, **driver_kwargs)


def _parse_artifact(payload: str, metrics: list[str], dimensions: list[str]) -> list[dict]:
    source_fields = _source_field_map()
    rows = []
    for row in csv.DictReader(io.StringIO(payload)):
        rows.append(
            {
                "dimensions": {
                    name: row.get(name, row.get(source_fields[name])) for name in dimensions
                },
                "metrics": {name: row.get(name, row.get(source_fields[name])) for name in metrics},
            }
        )
    return rows


def check_account_access(connection_id: str, selection: dict, *, _client=None, _token=None) -> dict:
    from core import nango_client  # noqa: PLC0415

    token = _token or nango_client.get_fresh_token(connection_id, provider=None)
    client = _client or httpx.Client()
    advertiser_id = str(selection["advertiser_id"])

    # Probe DV360 v4 — confirms display-video scope and entity-level access.
    _charge_v4(connection_id, advertiser_id)
    advertiser = _request(
        client,
        "GET",
        DISCOVERY_BASE,
        f"/advertisers/{advertiser_id}",
        token,
        "dv360-v4",
    ).json()

    # Build the smallest-safe query definition for the hash; use a relative
    # date so the hash is stable and the probe has a realistic date range.
    today = datetime.now(UTC).date().isoformat()
    definition = build_query_definition(
        "standard_daily",
        ["impressions"],
        ["date", "advertiser_id"],
        today,
        today,
        advertiser_id,
    )

    # Probe Bid Manager v2 — confirms doubleclickbidmanager.googleapis.com is
    # reachable with the granted token and that query listing is permitted.
    # A failure here surfaces during onboarding rather than on the first pull.
    _charge_bid_manager(connection_id)
    _request(
        client,
        "GET",
        REPORTING_BASE,
        "/queries",
        token,
        "dv360-bid-manager",
        params={"pageSize": 1},
    )

    return {
        "accessible": True,
        "advertiser_id": str(advertiser["advertiserId"]),
        "query_hash": canonical_query_hash(definition),
    }


_RAW_DDL = """
CREATE TABLE IF NOT EXISTS raw_dv360_daily (
    report_profile VARCHAR, partner_id VARCHAR, advertiser_id VARCHAR,
    date VARCHAR, campaign_id VARCHAR, insertion_order_id VARCHAR,
    line_item_id VARCHAR, creative_id VARCHAR, conversion_type VARCHAR,
    country VARCHAR, youtube_ad_group_id VARCHAR,
    timezone VARCHAR, currency VARCHAR, dimensions_json VARCHAR,
    metric VARCHAR, value DOUBLE, provider_value VARCHAR, non_additive BOOLEAN,
    pull_id VARCHAR, loaded_at VARCHAR, project_id VARCHAR
)
"""


def _convert_metric(metric: str, provider_value: Any) -> float | None:
    try:
        value = float(str(provider_value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return value / 1_000_000 if metric.endswith("_micros") else value


def _land(rows: list[dict], context: dict) -> int:
    if os.environ.get("TOOROW_DB_MODE", "duckdb") != "duckdb":
        from core.pull_errors import InvalidRequestError  # noqa: PLC0415

        raise InvalidRequestError("dv360 local landing currently requires duckdb")
    import duckdb  # noqa: PLC0415

    path = os.environ.get(
        "TOOROW_DUCKDB_PATH", str(Path(__file__).parent / "seeds" / "local.duckdb")
    )
    loaded_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    non_additive = {"unique_reach", "average_frequency", "viewability_rate"}
    values = []
    for row in rows:
        dimensions = row.get("dimensions") or {}
        for metric, provider_value in (row.get("metrics") or {}).items():
            values.append(
                (
                    context["report_profile"],
                    context["partner_id"],
                    context["advertiser_id"],
                    dimensions.get("date"),
                    dimensions.get("campaign_id"),
                    dimensions.get("insertion_order_id"),
                    dimensions.get("line_item_id"),
                    dimensions.get("creative_id"),
                    dimensions.get("conversion_type"),
                    dimensions.get("country"),
                    dimensions.get("youtube_ad_group_id"),
                    context.get("timezone", ""),
                    context.get("currency", ""),
                    json.dumps(dimensions, sort_keys=True),
                    metric,
                    _convert_metric(metric, provider_value),
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
            "INSERT INTO raw_dv360_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
    connection.close()
    return len(values)


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
    from core.pull_errors import ProviderTransientError  # noqa: PLC0415

    if not selection:
        raise Dv360OnboardingError("DV360 partner and advertiser selection is required")
    profile = _profile(profile_id)
    definition = build_query_definition(
        profile_id,
        profile["metrics"],
        profile["dimensions"],
        date_from,
        date_to,
        str(selection["advertiser_id"]),
    )
    token = _token or nango_client.get_fresh_token(connection_id, provider=None)
    client = _client or httpx.Client()
    query_id = str(
        selection.get("saved_query_id") or ensure_saved_query(client, token, definition, project_id)
    )
    outcome = run_saved_query(client, token, query_id, definition, connection_id, project_id)
    if outcome["status"] != "completed":
        raise ProviderTransientError(
            message=f"DV360 report deferred for resumable reference {outcome.get('report_ref')}"
        )
    rows = _parse_artifact(outcome["rows"], profile["metrics"], profile["dimensions"])
    row_count = _land(
        rows,
        {**selection, "report_profile": profile_id, "pull_id": pull_id, "project_id": project_id},
    )
    logger.info(
        "dv360_pull_completed: pull_id=%s profile=%s rows=%d", pull_id, profile_id, row_count
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


def pull_conversion_daily(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    selection: dict | None = None,
) -> dict:
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "conversion_daily", selection
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


def pull_youtube_compatible(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    selection: dict | None = None,
) -> dict:
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "youtube_compatible", selection
    )


def transform(raw_rows: list[dict]) -> list[dict]:
    """Rename provider fields to canonical names and convert micros-suffixed metrics to units.

    Both renaming and micros→unit conversion happen here so that any consumer
    calling transform() receives semantically correct values.  The _land() path
    applies the same _convert_metric() conversion before writing to DuckDB, so
    the conversion occurs exactly once per path — never double-converted.
    """
    mappings = _manifest()["canonical_metric_mapping"] | _manifest()["canonical_dimension_mapping"]
    result = []
    for row in raw_rows:
        out = {}
        for key, value in row.items():
            mapping = mappings.get(key, key)
            canonical = mapping if isinstance(mapping, str) else mapping["canonical"]
            # Apply micros→unit conversion for any field whose source key ends with _micros
            if key.endswith("_micros"):
                try:
                    value = float(str(value).replace(",", "")) / 1_000_000
                except (TypeError, ValueError):
                    pass
            out[canonical] = value
        result.append(out)
    return result


@mcp_app.tool()
def get_dv360_report(
    project_id: str = "default",
    report_profile: str = "standard_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Read-only governed DV360 report envelope; extraction remains queue-only."""
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
