"""X Ads API v12 connector using broker-signed OAuth 1.0a requests."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)
mcp_app = FastMCP("x-ads")

API_VERSION = "12"
PROVIDER_API_VERSION = "ads-api-v12"
API_BASE = "https://ads-api.x.com"
SYNC_MAX_DAYS = 7
ASYNC_MAX_DAYS = 90
ASYNC_SEGMENTED_MAX_DAYS = 45
MAX_PROCESSING_JOBS = 100
REFETCH_DAYS = (3, 14)


class XAdsOnboardingError(RuntimeError):
    """Typed app-approval, role or account-access failure."""


class XAdsCompatibilityError(ValueError):
    """An entity/metric-group/segmentation combination is illegal."""


def _manifest() -> dict:
    return json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))


def _compatibility() -> dict:
    return json.loads(
        (Path(__file__).parent / "catalog_sources" / "compatibility.json").read_text(
            encoding="utf-8"
        )
    )


def _provider_code(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    errors = payload.get("errors") or []
    if errors and isinstance(errors[0], dict):
        return str(errors[0].get("code") or errors[0].get("title") or "")
    return str(payload.get("code") or "") or None


def _raise_response(response) -> None:
    if response.status_code in (429, 503):
        from core.quota import RateLimitError  # noqa: PLC0415

        retry_after = None
        raw = response.headers.get("retry-after")
        if response.status_code == 429:
            reset = response.headers.get("x-rate-limit-reset")
            try:
                retry_after = (
                    max(0, int(reset) - int(datetime.now(UTC).timestamp())) if reset else None
                )
            except (TypeError, ValueError):
                retry_after = None
        if retry_after is None:
            try:
                retry_after = int(raw) if raw not in (None, "") else None
            except (TypeError, ValueError):
                retry_after = None
        raise RateLimitError("x-ads", retry_after)
    try:
        original = response.json()
    except Exception:
        original = response.text
    normalized = dict(original) if isinstance(original, dict) else original
    if isinstance(normalized, dict) and (code := _provider_code(original)):
        normalized["code"] = code
    from core.pull_errors import classify_http_error  # noqa: PLC0415

    raise classify_http_error(response.status_code, normalized, _manifest().get("error_map"))


def _request(
    connection_id: str,
    method: str,
    path: str,
    *,
    provider: str = "x-ads",
    _proxy=None,
    **kwargs,
):
    if _proxy is None:
        from core.nango_client import oauth1_proxy_request  # noqa: PLC0415

        _proxy = oauth1_proxy_request
    response = _proxy(
        connection_id,
        provider,
        method,
        path,
        base_url_override=API_BASE,
        **kwargs,
    )
    if response.status_code < 200 or response.status_code >= 300:
        _raise_response(response)
    return response


def discover_accounts(connection_id: str, *, _proxy=None) -> list[dict]:
    payload = _request(connection_id, "GET", f"/{API_VERSION}/accounts", _proxy=_proxy).json()
    accounts = payload.get("data") or []
    if not accounts:
        raise XAdsOnboardingError(
            "No X Ads account is accessible; verify Ads API approval and account role"
        )
    return [
        {
            "id": f"x_ads_selection_{index}",
            "account_id": str(item["id"]),
            "display_name": item.get("name") or str(item["id"]),
            "timezone": item.get("timezone") or "",
            "currency": item.get("currency") or item.get("currency_code") or "",
        }
        for index, item in enumerate(accounts, start=1)
    ]


def validate_shape(profile_id: str, metrics: list[str], segmentation: str | None) -> None:
    profile = _compatibility()["profiles"].get(profile_id)
    if profile is None:
        raise XAdsCompatibilityError(f"Unknown X Ads profile: {profile_id}")
    invalid = sorted(set(metrics) - set(profile["metrics"]))
    if invalid or segmentation not in profile["segmentations"]:
        raise XAdsCompatibilityError(
            f"Illegal X Ads shape for {profile_id}: metrics={invalid}, segmentation={segmentation}"
        )


def canonical_request_hash(request: dict) -> str:
    return hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def split_date_windows(date_from: str, date_to: str, max_days: int) -> list[tuple[str, str]]:
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    if end < start:
        raise ValueError("date_to must not precede date_from")
    windows = []
    cursor = start
    while cursor <= end:
        window_end = min(end, cursor + timedelta(days=max_days - 1))
        windows.append((cursor.isoformat(), window_end.isoformat()))
        cursor = window_end + timedelta(days=1)
    return windows


def build_stats_request(
    profile_id: str,
    metrics: list[str],
    date_from: str,
    date_to: str,
    *,
    segmentation: str | None = None,
) -> dict:
    validate_shape(profile_id, metrics, segmentation)
    profile = _compatibility()["profiles"][profile_id]
    return {
        "entity": profile["entity"],
        "metric_groups": sorted(metrics),
        "granularity": "DAY",
        "start_time": f"{date_from}T00:00:00Z",
        "end_time": f"{date_to}T23:59:59Z",
        "placement": "ALL_ON_TWITTER",
        "segmentation_type": segmentation,
    }


def sync_stats(connection_id: str, account_id: str, request: dict, *, _proxy=None) -> list[dict]:
    start = request["start_time"][:10]
    end = request["end_time"][:10]
    if (date.fromisoformat(end) - date.fromisoformat(start)).days + 1 > SYNC_MAX_DAYS:
        raise XAdsCompatibilityError("X Ads synchronous analytics is limited to seven days")
    if request.get("segmentation_type"):
        raise XAdsCompatibilityError("X Ads synchronous analytics does not support segmentation")
    payload = _request(
        connection_id,
        "GET",
        f"/{API_VERSION}/stats/accounts/{account_id}",
        _proxy=_proxy,
        params={key: value for key, value in request.items() if value is not None},
    ).json()
    return payload.get("data") or []


class _AsyncStatsFlow:
    def __init__(self, connection_id: str, account_id: str, request: dict, proxy=None):
        self.connection_id = connection_id
        self.account_id = account_id
        self.request = request
        self.proxy = proxy

    def submit(self) -> str:
        active = (
            _request(
                self.connection_id,
                "GET",
                f"/{API_VERSION}/stats/jobs/accounts/{self.account_id}",
                _proxy=self.proxy,
                params={"count": MAX_PROCESSING_JOBS, "status": "PROCESSING"},
            )
            .json()
            .get("data")
            or []
        )
        if len(active) >= MAX_PROCESSING_JOBS:
            from core.pull_errors import ProviderTransientError  # noqa: PLC0415

            raise ProviderTransientError(message="X Ads account already has 100 processing jobs")
        payload = _request(
            self.connection_id,
            "POST",
            f"/{API_VERSION}/stats/jobs/accounts/{self.account_id}",
            _proxy=self.proxy,
            json={key: value for key, value in self.request.items() if value is not None},
        ).json()
        return str((payload.get("data") or payload)["id"])

    def poll(self, job_id: str) -> str:
        payload = _request(
            self.connection_id,
            "GET",
            f"/{API_VERSION}/stats/jobs/accounts/{self.account_id}/{job_id}",
            _proxy=self.proxy,
        ).json()
        status = (payload.get("data") or payload).get("status")
        return {
            "QUEUED": "pending",
            "PROCESSING": "processing",
            "SUCCESS": "completed",
            "FAILED": "failed",
            "EXPIRED": "expired",
        }.get(status, "pending")

    def download(self, job_id: str):
        payload = _request(
            self.connection_id,
            "GET",
            f"/{API_VERSION}/stats/jobs/accounts/{self.account_id}/{job_id}",
            _proxy=self.proxy,
        ).json()
        data = payload.get("data") or payload
        if isinstance(data.get("result"), list):
            return data["result"]
        url = data.get("url") or data.get("download_url")
        if not url:
            return []
        response = httpx.get(url, timeout=60)
        response.raise_for_status()
        body = response.json()
        return body.get("data") or body


def run_async_stats(
    connection_id: str,
    account_id: str,
    request: dict,
    *,
    _proxy=None,
    **driver_kwargs,
) -> dict:
    from core.async_reports import AsyncReportFlow, run_async_report  # noqa: PLC0415

    implementation = _AsyncStatsFlow(connection_id, account_id, request, _proxy)
    flow = AsyncReportFlow(
        submit=implementation.submit,
        poll=implementation.poll,
        download=implementation.download,
        request_hash=canonical_request_hash(request),
    )
    return run_async_report(flow, ledger_ref=connection_id, **driver_kwargs)


_RAW_DDL = """
CREATE TABLE IF NOT EXISTS raw_x_ads_daily (
    report_profile VARCHAR, account_id VARCHAR, entity_type VARCHAR, entity_id VARCHAR,
    placement VARCHAR, segment_type VARCHAR, segment_value VARCHAR, interval_start VARCHAR,
    interval_end VARCHAR, timezone VARCHAR, currency VARCHAR, metric VARCHAR, value DOUBLE,
    provider_value VARCHAR, non_additive BOOLEAN, request_hash VARCHAR, pull_id VARCHAR,
    loaded_at VARCHAR, project_id VARCHAR
)
"""


def _land(rows: list[dict], context: dict) -> int:
    if os.environ.get("TOOROW_DB_MODE", "duckdb") != "duckdb":
        raise ValueError("x-ads local landing currently requires duckdb")
    import duckdb  # noqa: PLC0415

    path = os.environ.get(
        "TOOROW_DUCKDB_PATH", str(Path(__file__).parent / "seeds" / "local.duckdb")
    )
    loaded_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    non_additive_tokens = ("rate", "ratio", "average", "reach", "frequency")
    values = []
    for row in rows:
        entity_id = str(row.get("id") or row.get("entity_id") or "")
        segments = row.get("segment") or {}
        metrics = row.get("metrics") or row.get("id_data") or {}
        for metric, points in metrics.items():
            point_values = points if isinstance(points, list) else [points]
            for point in point_values:
                provider_value = point.get("value") if isinstance(point, dict) else point
                try:
                    value = float(provider_value)
                    if metric.endswith("_micro") or metric.endswith("_micros"):
                        value /= 1_000_000
                except (TypeError, ValueError):
                    value = None
                values.append(
                    (
                        context["report_profile"],
                        context["account_id"],
                        context["entity_type"],
                        entity_id,
                        row.get("placement") or "ALL_ON_TWITTER",
                        context.get("segmentation") or "",
                        str(segments.get("value") or row.get("segment_value") or ""),
                        row.get("start_time") or context["date_from"],
                        row.get("end_time") or context["date_to"],
                        context.get("timezone", ""),
                        context.get("currency", ""),
                        metric,
                        value,
                        str(provider_value),
                        any(token in metric for token in non_additive_tokens),
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
            "INSERT INTO raw_x_ads_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values
        )
    connection.close()
    return len(values)


def _pull_profile(
    connection_id, date_from, date_to, project_id, pull_id, profile_id, selection, *, _proxy=None
):
    from core.pull_errors import ProviderTransientError  # noqa: PLC0415

    if not selection:
        raise XAdsOnboardingError("X Ads account selection is required")
    profile = next(
        item for item in _manifest()["source_capabilities"]["reports"] if item["id"] == profile_id
    )
    segmentation = selection.get("segmentation")
    max_days = ASYNC_SEGMENTED_MAX_DAYS if segmentation else ASYNC_MAX_DAYS
    windows = split_date_windows(date_from, date_to, max_days)
    total = 0
    for window_from, window_to in windows:
        request = build_stats_request(
            profile_id, profile["metrics"], window_from, window_to, segmentation=segmentation
        )
        days = (date.fromisoformat(window_to) - date.fromisoformat(window_from)).days + 1
        # prefer_sync is a non-public test/dev escape hatch documented in
        # catalog_sources/ROLLOUT_NOTES.md (M-1). It must not appear in
        # production selection payloads or manifest profiles.
        if days <= SYNC_MAX_DAYS and not segmentation and selection.get("prefer_sync", False):
            rows = sync_stats(connection_id, selection["account_id"], request, _proxy=_proxy)
        else:
            outcome = run_async_stats(
                connection_id, selection["account_id"], request, _proxy=_proxy
            )
            if outcome["status"] != "completed":
                raise ProviderTransientError(
                    message=f"X Ads stats job deferred for {outcome.get('report_ref')}"
                )
            rows = outcome["rows"]
        context = {
            **selection,
            "report_profile": profile_id,
            "entity_type": _compatibility()["profiles"][profile_id]["entity"],
            "date_from": window_from,
            "date_to": window_to,
            "request_hash": canonical_request_hash(request),
            "pull_id": pull_id,
            "project_id": project_id,
        }
        total += _land(rows, context)
    logger.info("x_ads_pull_completed: pull_id=%s profile=%s rows=%d", pull_id, profile_id, total)
    return {
        "pull_id": pull_id,
        "row_count": total,
        "date_from": date_from,
        "date_to": date_to,
        "refetch_days": list(REFETCH_DAYS),
    }


def pull(connection_id, date_from, date_to, project_id, pull_id, selection=None):
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "campaign_daily", selection
    )


def pull_campaign_daily(connection_id, date_from, date_to, project_id, pull_id, selection=None):
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "campaign_daily", selection
    )


def pull_line_item_daily(connection_id, date_from, date_to, project_id, pull_id, selection=None):
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "line_item_daily", selection
    )


def pull_promoted_post_daily(
    connection_id, date_from, date_to, project_id, pull_id, selection=None
):
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "promoted_post_daily", selection
    )


def pull_segmented_daily(connection_id, date_from, date_to, project_id, pull_id, selection=None):
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "segmented_daily", selection
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
def get_x_ads_report(
    project_id: str = "default",
    report_profile: str = "campaign_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Read-only governed X Ads report envelope; extraction remains queue-only."""
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
