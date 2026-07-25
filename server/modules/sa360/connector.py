"""Search Ads 360 Reporting API v0 governed connector."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)
mcp_app = FastMCP("sa360")

API_VERSION = "v0"
PROVIDER_API_VERSION = "search-ads-360-reporting-v0"
API_BASE = "https://searchads360.googleapis.com/v0"
MAX_PAGE_SIZE = 10_000
MAX_IN_ITEMS = 20_000
STREAM_THRESHOLD_ROWS = 10_000
QUERIES_PER_MINUTE_PROJECT_USER = 3_000
QUERIES_PER_MINUTE_PROJECT = 3_000
QUERIES_PER_DAY_PROJECT = 150_000
REQUEST_TIMEOUT_SECONDS = 60

_quota_lock = threading.Lock()
_minute_usage: dict[tuple[str, str], int] = {}
_daily_usage: dict[tuple[str, str], int] = {}


class Sa360OnboardingError(RuntimeError):
    """Typed customer discovery or routing failure."""


class Sa360CompatibilityError(ValueError):
    """An explicit field selection is incompatible with its SA360 resource."""


class Sa360MessageTooLargeError(RuntimeError):
    """The provider rejected a response at its transport message boundary."""


def _manifest() -> dict:
    return json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))


def _compatibility() -> dict:
    return json.loads(
        (Path(__file__).parent / "catalog_sources" / "compatibility.json").read_text(
            encoding="utf-8"
        )
    )


def _reason(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    error = payload.get("error") or {}
    details = error.get("details") or []
    for detail in details:
        errors = detail.get("errors") if isinstance(detail, dict) else None
        if errors and isinstance(errors[0], dict):
            return errors[0].get("errorCode") or errors[0].get("message")
    return error.get("status") or payload.get("code")


def _raise_response(response) -> None:
    try:
        original = response.json()
    except Exception:
        original = response.text
    reason = _reason(original)
    message = json.dumps(original).lower() if isinstance(original, dict) else str(original).lower()
    # H-2 NOTE (validation_required): The substrings below are a heuristic for the gRPC 4 MB
    # transport boundary 429.  The exact provider error shape has NOT been confirmed against a live
    # SA360 response (live gate is blocked — see manifest verification.validation_required).
    # Assumed shape: HTTP 429, JSON body with an "error.message" field containing text like
    # "Received message larger than 4 MB" or equivalent.  If the provider instead encodes this in
    # a grpc-message/grpc-status header, or uses different wording (e.g. "message too large"), the
    # 429 will silently fall through to RateLimitError below rather than Sa360MessageTooLargeError.
    # Secondary signal to consider when unblocked: check response.headers.get("grpc-message").
    if response.status_code == 429 and any(
        marker in message for marker in ("message larger", "4 mb", "received message larger")
    ):
        raise Sa360MessageTooLargeError(
            "SA360 response exceeded the transport boundary; reduce fields/page size "
            "or use SearchStream"
        )
    if response.status_code == 429 or reason == "RESOURCE_EXHAUSTED":
        from core.quota import RateLimitError  # noqa: PLC0415

        raw = response.headers.get("Retry-After")
        try:
            retry_after = int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            retry_after = None
        raise RateLimitError("sa360", retry_after)
    normalized = dict(original) if isinstance(original, dict) else original
    if isinstance(normalized, dict) and reason:
        normalized["code"] = reason
        request_id = response.headers.get("request-id")
        if request_id:
            normalized["request_id"] = request_id[:64]
    from core.pull_errors import classify_http_error  # noqa: PLC0415

    raise classify_http_error(response.status_code, normalized, _manifest().get("error_map"))


def _request(client, method: str, path: str, token: str, login_customer_id=None, **kwargs):
    headers = {"Authorization": f"Bearer {token}"}
    if login_customer_id:
        headers["login-customer-id"] = normalize_customer_id(login_customer_id)
    response = client.request(
        method,
        f"{API_BASE}{path}",
        headers=headers,
        timeout=REQUEST_TIMEOUT_SECONDS,
        **kwargs,
    )
    if response.status_code < 200 or response.status_code >= 300:
        _raise_response(response)
    return response


def normalize_customer_id(value: str) -> str:
    normalized = re.sub(r"\D", "", str(value))
    if not normalized:
        raise Sa360OnboardingError("SA360 customer id must contain digits")
    return normalized


def discover_accounts(connection_id: str, *, _client=None, _token: str | None = None) -> list[dict]:
    from core import nango_client  # noqa: PLC0415

    token = _token or nango_client.get_fresh_token(connection_id, provider="sa360")
    client = _client or httpx.Client()
    payload = _request(client, "GET", "/customers:listAccessibleCustomers", token).json()
    names = payload.get("resourceNames") or []
    if not names:
        raise Sa360OnboardingError(
            "No Search Ads 360 customer is accessible; verify direct or manager permissions"
        )
    return [
        {
            "id": f"sa360_selection_{index}",
            "customer_id": normalize_customer_id(name),
            "login_customer_id": None,
            "routing_mode": "direct",
            "display_name": name,
        }
        for index, name in enumerate(names, start=1)
    ]


def with_manager_route(selection: dict, login_customer_id: str) -> dict:
    return {
        **selection,
        "login_customer_id": normalize_customer_id(login_customer_id),
        "routing_mode": "manager_client",
    }


def validate_fields(profile_id: str, fields: list[str]) -> None:
    legal = _compatibility()["profiles"].get(profile_id)
    if legal is None:
        raise Sa360CompatibilityError(f"Unknown SA360 profile: {profile_id}")
    invalid = sorted(set(fields) - set(legal["fields"]))
    if invalid:
        raise Sa360CompatibilityError(f"Fields incompatible with {legal['resource']}: {invalid}")


def validate_in_values(values: list[str]) -> None:
    if len(values) > MAX_IN_ITEMS:
        raise Sa360CompatibilityError(
            f"SA360 IN list contains {len(values)} values; maximum is {MAX_IN_ITEMS}"
        )


def build_saql(
    profile_id: str,
    fields: list[str],
    date_from: str,
    date_to: str,
    *,
    in_filter: tuple[str, list[str]] | None = None,
) -> str:
    validate_fields(profile_id, fields)
    legal = _compatibility()["profiles"][profile_id]
    predicates = []
    if "segments.date" in fields:
        predicates.append(f"segments.date BETWEEN '{date_from}' AND '{date_to}'")
    if in_filter:
        field, values = in_filter
        validate_in_values(values)
        if field not in legal["filterable"]:
            raise Sa360CompatibilityError(f"Field is not filterable for {profile_id}: {field}")
        escaped = ", ".join("'" + value.replace("'", "\\'") + "'" for value in values)
        predicates.append(f"{field} IN ({escaped})")
    where = f" WHERE {' AND '.join(predicates)}" if predicates else ""
    return f"SELECT {', '.join(fields)} FROM {legal['resource']}{where} ORDER BY segments.date"


def query_hash(query: str) -> str:
    return hashlib.sha256(query.encode()).hexdigest()


def _charge_query(project_id: str, *, now: datetime | None = None) -> None:
    from core import quota  # noqa: PLC0415
    from core.pull_errors import ProviderTransientError  # noqa: PLC0415

    allowed, reason = quota.pre_check("sa360", 1)
    if not allowed:
        raise ProviderTransientError(message=f"SA360 minute quota unavailable: {reason}")
    instant = now or datetime.now(UTC)
    minute_key = (project_id, instant.strftime("%Y-%m-%dT%H:%M"))
    day_key = (project_id, instant.date().isoformat())
    with _quota_lock:
        minute_spent = _minute_usage.get(minute_key, 0)
        day_spent = _daily_usage.get(day_key, 0)
        if minute_spent >= QUERIES_PER_MINUTE_PROJECT:
            raise ProviderTransientError(message=f"SA360 minute budget exhausted for {project_id}")
        if day_spent >= QUERIES_PER_DAY_PROJECT:
            raise ProviderTransientError(message=f"SA360 daily budget exhausted for {project_id}")
        _minute_usage[minute_key] = minute_spent + 1
        _daily_usage[day_key] = day_spent + 1
    quota.record_spend("sa360", 1)


def search(
    client,
    token: str,
    customer_id: str,
    query: str,
    project_id: str,
    *,
    page_size: int = MAX_PAGE_SIZE,
    login_customer_id: str | None = None,
) -> tuple[list[dict], str | None]:
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise Sa360CompatibilityError(f"SA360 page size must be between 1 and {MAX_PAGE_SIZE}")
    _charge_query(project_id)
    rows: list[dict] = []
    page_token = None
    request_id = None
    while True:
        body = {"query": query, "pageSize": page_size}
        if page_token:
            body["pageToken"] = page_token
        response = _request(
            client,
            "POST",
            f"/customers/{normalize_customer_id(customer_id)}/searchAds360:search",
            token,
            login_customer_id,
            json=body,
        )
        request_id = response.headers.get("request-id") or request_id
        payload = response.json()
        rows.extend(payload.get("results") or [])
        page_token = payload.get("nextPageToken")
        if not page_token:
            return rows, request_id[:64] if request_id else None


def search_stream(
    client,
    token: str,
    customer_id: str,
    query: str,
    project_id: str,
    *,
    login_customer_id: str | None = None,
) -> tuple[list[dict], str | None]:
    _charge_query(project_id)
    response = _request(
        client,
        "POST",
        f"/customers/{normalize_customer_id(customer_id)}/searchAds360:searchStream",
        token,
        login_customer_id,
        json={"query": query},
    )
    batches = response.json()
    rows = [row for batch in batches for row in batch.get("results") or []]
    request_id = response.headers.get("request-id")
    return rows, request_id[:64] if request_id else None


def check_account_access(connection_id: str, selection: dict, *, _client=None, _token=None):
    from core import nango_client  # noqa: PLC0415

    token = _token or nango_client.get_fresh_token(connection_id, provider="sa360")
    client = _client or httpx.Client()
    query = "SELECT customer.id FROM customer LIMIT 1"
    rows, request_id = search(
        client,
        token,
        selection["customer_id"],
        query,
        "onboarding",
        page_size=1,
        login_customer_id=selection.get("login_customer_id"),
    )
    return {"accessible": True, "rows": len(rows), "request_id": request_id}


_RAW_DDL = """
CREATE TABLE IF NOT EXISTS raw_sa360_daily (
    report_profile VARCHAR, manager_customer_id VARCHAR, customer_id VARCHAR,
    resource_id VARCHAR, date VARCHAR, campaign_id VARCHAR, ad_group_id VARCHAR,
    criterion_id VARCHAR, device VARCHAR, currency_code VARCHAR, dimensions_json VARCHAR,
    metric VARCHAR, value DOUBLE, provider_value VARCHAR, non_additive BOOLEAN,
    query_hash VARCHAR, request_id VARCHAR, pull_id VARCHAR, loaded_at VARCHAR, project_id VARCHAR
)
"""


def _extract_path(row: dict, path: str):
    source_paths = {
        item["field_id"]: item["source_field"]
        for item in _manifest()["source_capabilities"]["fields"]
    }
    path = source_paths.get(path, path)
    current: Any = row
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _land(rows: list[dict], fields: list[str], context: dict) -> int:
    if os.environ.get("TOOROW_DB_MODE", "duckdb") != "duckdb":
        raise ValueError("sa360 local landing currently requires duckdb")
    import duckdb  # noqa: PLC0415

    path = os.environ.get(
        "TOOROW_DUCKDB_PATH", str(Path(__file__).parent / "seeds" / "local.duckdb")
    )
    loaded_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    metric_fields = {
        item["field_id"]
        for item in _manifest()["source_capabilities"]["fields"]
        if item["kind"] == "metric"
    }
    values = []
    non_additive_tokens = ("share", "rate", "average", "ratio")
    for row in rows:
        dims = {field: _extract_path(row, field) for field in fields if field not in metric_fields}
        resource_id = next(
            (
                str(value)
                for key, value in dims.items()
                if key.endswith(".id") and value is not None
            ),
            "",
        )
        for metric in fields:
            if metric not in metric_fields:
                continue
            provider_value = _extract_path(row, metric)
            try:
                value = float(provider_value)
                if metric.endswith("_micros"):
                    value /= 1_000_000
            except (TypeError, ValueError):
                value = None
            values.append(
                (
                    context["report_profile"],
                    context.get("login_customer_id") or "",
                    context["customer_id"],
                    resource_id,
                    dims.get("segments.date"),
                    dims.get("campaign.id"),
                    dims.get("adGroup.id"),
                    dims.get("adGroupCriterion.criterionId"),
                    dims.get("segments.device"),
                    context.get("currency_code", ""),
                    json.dumps(dims, sort_keys=True),
                    metric,
                    value,
                    str(provider_value),
                    any(token in metric for token in non_additive_tokens),
                    context["query_hash"],
                    context.get("request_id"),
                    context["pull_id"],
                    loaded_at,
                    context["project_id"],
                )
            )
    connection = duckdb.connect(path)
    connection.execute(_RAW_DDL)
    if values:
        connection.executemany(
            "INSERT INTO raw_sa360_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values
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
    _token=None,
):
    from core import nango_client  # noqa: PLC0415

    if not selection:
        raise Sa360OnboardingError("SA360 customer selection is required")
    profile = next(
        item for item in _manifest()["source_capabilities"]["reports"] if item["id"] == profile_id
    )
    fields = profile["dimensions"] + profile["metrics"]
    query = build_saql(profile_id, fields, date_from, date_to)
    token = _token or nango_client.get_fresh_token(connection_id, provider="sa360")
    client = _client or httpx.Client()
    extractor = (
        search_stream if int(selection.get("estimated_rows", 0)) > STREAM_THRESHOLD_ROWS else search
    )
    rows, request_id = extractor(
        client,
        token,
        selection["customer_id"],
        query,
        project_id,
        login_customer_id=selection.get("login_customer_id"),
    )
    context = {
        **selection,
        "report_profile": profile_id,
        "query_hash": query_hash(query),
        "request_id": request_id,
        "pull_id": pull_id,
        "project_id": project_id,
    }
    row_count = _land(rows, fields, context)
    logger.info(
        "sa360_pull_completed: pull_id=%s profile=%s rows=%d request_id=%s",
        pull_id,
        profile_id,
        row_count,
        request_id,
    )
    return {"pull_id": pull_id, "row_count": row_count, "date_from": date_from, "date_to": date_to}


def pull(connection_id, date_from, date_to, project_id, pull_id, selection=None):
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "campaign_daily", selection
    )


def pull_campaign_daily(connection_id, date_from, date_to, project_id, pull_id, selection=None):
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "campaign_daily", selection
    )


def pull_ad_group_daily(connection_id, date_from, date_to, project_id, pull_id, selection=None):
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "ad_group_daily", selection
    )


def pull_keyword_daily(connection_id, date_from, date_to, project_id, pull_id, selection=None):
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "keyword_daily", selection
    )


def pull_catalog_daily(connection_id, date_from, date_to, project_id, pull_id, selection=None):
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "catalog_daily", selection
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


@mcp_app.tool()
def get_sa360_report(
    project_id: str = "default",
    report_profile: str = "campaign_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Read-only governed SA360 report envelope; extraction remains queue-only."""
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
