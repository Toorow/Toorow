"""Organic Instagram Insights connector for professional accounts.

The connector uses Instagram Login (``graph.instagram.com``), not the paid
Meta Ads API. Account insights are daily series. Media insights are current
lifetime snapshots attached to the media publication date and are therefore
landed as non-additive facts.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from core import pull_errors
from fastmcp import FastMCP

logger = logging.getLogger(__name__)
mcp_app = FastMCP("instagram-insights")

API_VERSION = os.environ.get("INSTAGRAM_INSIGHTS_API_VERSION", "v24.0")
API_BASE = f"https://graph.instagram.com/{API_VERSION}"
ACCOUNT_METRICS = ("reach", "profile_views")
MEDIA_METRICS = ("shares", "comments")
MEDIA_FIELDS = "id,media_type,media_product_type,timestamp,permalink,caption"


class InstagramAccountNotSelectedError(pull_errors.InvalidRequestError):
    """The connection is valid, but no discovered Instagram account was selected."""

    user_action = pull_errors.SELECT_SOURCE_ACCOUNT


class InstagramWindowError(pull_errors.InvalidRequestError, ValueError):
    """The requested reporting window is invalid for the selected profile."""

    user_action = pull_errors.REVIEW_MAPPING


def _manifest() -> dict[str, Any]:
    return json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))


def _token(connection_id: str) -> str:
    from core import nango_client  # noqa: PLC0415

    return nango_client.get_fresh_token(connection_id, provider="instagram-insights")


def _retry_after(response: httpx.Response) -> int | None:
    raw = response.headers.get("Retry-After")
    try:
        return int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _raise_response(response: httpx.Response) -> None:
    if response.status_code == 429:
        from core.quota import RateLimitError  # noqa: PLC0415

        raise RateLimitError("instagram-insights", _retry_after(response))
    try:
        payload: Any = response.json()
    except Exception:
        payload = response.text
    raise pull_errors.classify_http_error(
        response.status_code, payload, _manifest().get("error_map")
    )


def _request(client: httpx.Client, path_or_url: str, token: str, **kwargs) -> httpx.Response:
    url = path_or_url if path_or_url.startswith("https://") else f"{API_BASE}{path_or_url}"
    response = client.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
        **kwargs,
    )
    if not 200 <= response.status_code < 300:
        _raise_response(response)
    return response


def _require_account(instagram_account_id: str | None) -> str:
    if not instagram_account_id:
        raise InstagramAccountNotSelectedError(
            "No Instagram professional account is selected for this connection. "
            "Choose the account returned by discover_accounts in the Datastream wizard."
        )
    return str(instagram_account_id)


def _validate_window(date_from: str, date_to: str, *, account_history: bool) -> tuple[date, date]:
    try:
        start = date.fromisoformat(date_from)
        end = date.fromisoformat(date_to)
    except (TypeError, ValueError) as exc:
        raise InstagramWindowError("Instagram Insights requires ISO dates (YYYY-MM-DD)") from exc
    if end < start:
        raise InstagramWindowError("date_to must be on or after date_from")
    if account_history and (end - start).days >= 90:
        raise InstagramWindowError("Instagram account insight windows cannot exceed 90 days")
    if account_history and start < datetime.now(UTC).date() - timedelta(days=90):
        raise InstagramWindowError("Instagram stores account insight history for at most 90 days")
    return start, end


def discover_accounts(
    connection_id: str, *, _client: httpx.Client | None = None, _token_value: str | None = None
) -> list[dict[str, Any]]:
    """Return the professional account represented by the Instagram user token."""

    token = _token_value or _token(connection_id)
    owns_client = _client is None
    client = _client or httpx.Client()
    try:
        account = _request(
            client,
            "/me",
            token,
            params={"fields": "id,username,account_type,media_count,followers_count"},
        ).json()
    finally:
        if owns_client:
            client.close()
    account_id = str(account.get("id") or "")
    if not account_id:
        raise pull_errors.PermissionDeniedError(
            message=(
                "Instagram Login did not return a professional account. Business or Creator "
                "account access with instagram_business_basic is required."
            )
        )
    username = str(account.get("username") or account_id)
    return [
        {
            "id": account_id,
            "label": f"@{username}",
            "username": username,
            "account_type": account.get("account_type"),
            "media_count": account.get("media_count"),
            "followers_count": account.get("followers_count"),
        }
    ]


def _day_bounds(start: date, end: date) -> tuple[int, int]:
    since = int(datetime.combine(start, datetime.min.time(), tzinfo=UTC).timestamp())
    until = int(
        datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=UTC).timestamp()
    )
    return since, until


def fetch_account_daily(
    client: httpx.Client,
    token: str,
    instagram_account_id: str,
    date_from: str,
    date_to: str,
) -> list[dict[str, Any]]:
    start, end = _validate_window(date_from, date_to, account_history=True)
    since, until = _day_bounds(start, end)
    payload = _request(
        client,
        f"/{instagram_account_id}/insights",
        token,
        params={
            "metric": ",".join(ACCOUNT_METRICS),
            "period": "day",
            "since": since,
            "until": until,
        },
    ).json()
    by_date: dict[str, dict[str, Any]] = {}
    for series in payload.get("data") or []:
        source_metric = str(series.get("name") or "")
        if source_metric not in ACCOUNT_METRICS:
            continue
        field_id = f"account_{source_metric}"
        for point in series.get("values") or []:
            value = point.get("value")
            end_time = str(point.get("end_time") or "")
            if not end_time or not isinstance(value, (int, float)):
                continue
            report_date = end_time[:10]
            row = by_date.setdefault(
                report_date,
                {
                    "report_profile": "account_daily",
                    "date": report_date,
                    "account_id": instagram_account_id,
                },
            )
            row[field_id] = value
            row.setdefault("_provider_payloads", {})[source_metric] = point
    return [by_date[key] for key in sorted(by_date)]


def _paginate_media(
    client: httpx.Client, token: str, instagram_account_id: str
) -> list[dict[str, Any]]:
    url = f"/{instagram_account_id}/media"
    params: dict[str, Any] | None = {"fields": MEDIA_FIELDS, "limit": 100}
    rows: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    while url:
        response = _request(client, url, token, params=params)
        payload = response.json()
        rows.extend(item for item in (payload.get("data") or []) if isinstance(item, dict))
        next_url = str((payload.get("paging") or {}).get("next") or "")
        if not next_url:
            break
        if next_url in seen_urls:
            raise pull_errors.ProviderTransientError(
                message="Instagram media pagination repeated a cursor; refusing a partial result"
            )
        seen_urls.add(next_url)
        url = next_url
        params = None
    return rows


def fetch_media_performance(
    client: httpx.Client,
    token: str,
    instagram_account_id: str,
    date_from: str,
    date_to: str,
) -> list[dict[str, Any]]:
    """Return current lifetime snapshots for non-Story media published in the window."""

    start, end = _validate_window(date_from, date_to, account_history=False)
    output: list[dict[str, Any]] = []
    for media in _paginate_media(client, token, instagram_account_id):
        timestamp = str(media.get("timestamp") or "")
        try:
            published_date = date.fromisoformat(timestamp[:10])
        except ValueError:
            continue
        if published_date < start or published_date > end:
            continue
        if str(media.get("media_product_type") or "").upper() == "STORY":
            continue
        media_id = str(media.get("id") or "")
        if not media_id:
            continue
        insights = _request(
            client,
            f"/{media_id}/insights",
            token,
            params={"metric": ",".join(MEDIA_METRICS)},
        ).json()
        row: dict[str, Any] = {
            "report_profile": "media_performance",
            "date": published_date.isoformat(),
            "account_id": instagram_account_id,
            "media_id": media_id,
            "media_type": media.get("media_type"),
            "media_product_type": media.get("media_product_type"),
            "permalink": media.get("permalink"),
            "caption": media.get("caption"),
            "published_at": timestamp,
            "_provider_payloads": {},
        }
        for metric in insights.get("data") or []:
            source_metric = str(metric.get("name") or "")
            if source_metric not in MEDIA_METRICS:
                continue
            values = metric.get("values") or []
            value = values[-1].get("value") if values else metric.get("value")
            if isinstance(value, (int, float)):
                row[f"media_{source_metric}"] = value
                row["_provider_payloads"][source_metric] = metric
        output.append(row)
    return output


_RAW_DDL = """
CREATE TABLE IF NOT EXISTS raw_instagram_insights_daily (
 report_profile VARCHAR, date VARCHAR, account_id VARCHAR, media_id VARCHAR,
 media_type VARCHAR, media_product_type VARCHAR, permalink VARCHAR, caption VARCHAR,
 published_at VARCHAR, metric VARCHAR, value DOUBLE, non_additive BOOLEAN,
 payload_json VARCHAR, report_timezone VARCHAR, pull_id VARCHAR, loaded_at VARCHAR,
 project_id VARCHAR
)
"""

_RAW_INSERT_SQL = """
INSERT INTO raw_instagram_insights_daily
 (report_profile, date, account_id, media_id, media_type, media_product_type,
  permalink, caption, published_at, metric, value, non_additive, payload_json,
  report_timezone, pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _metric_contract() -> dict[str, tuple[str, bool]]:
    contract: dict[str, tuple[str, bool]] = {}
    for source_field, target in _manifest()["canonical_metric_mapping"].items():
        if isinstance(target, str):
            contract[source_field] = (target, False)
        else:
            contract[source_field] = (target["canonical"], bool(target.get("non_additive")))
    return contract


def _land(rows: list[dict[str, Any]], context: dict[str, Any]) -> int:
    from core import warehouse_write  # noqa: PLC0415

    path = os.environ.get("TOOROW_DUCKDB_PATH", str(Path(__file__).parent / "local.duckdb"))
    loaded_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    values: list[tuple[Any, ...]] = []
    metrics = _metric_contract()
    for row in rows:
        payload_json = json.dumps(
            row.get("_provider_payloads") or row, sort_keys=True, separators=(",", ":")
        )
        for source_field, (canonical_metric, non_additive) in metrics.items():
            value = row.get(source_field)
            if not isinstance(value, (int, float)):
                continue
            values.append(
                (
                    row.get("report_profile"),
                    row.get("date"),
                    row.get("account_id"),
                    row.get("media_id"),
                    row.get("media_type"),
                    row.get("media_product_type"),
                    row.get("permalink"),
                    row.get("caption"),
                    row.get("published_at"),
                    canonical_metric,
                    float(value),
                    non_additive,
                    payload_json,
                    None,
                    context["pull_id"],
                    loaded_at,
                    context["project_id"],
                )
            )
    connection = warehouse_write.open_raw_writer(path, project_id=context["project_id"])
    connection.execute(_RAW_DDL)
    if values:
        connection.executemany(_RAW_INSERT_SQL, values)
    connection.close()
    return len(values)


def _pull_profile(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    profile: str,
    instagram_account_id: str | None,
) -> dict[str, Any]:
    account_id = _require_account(instagram_account_id)
    token = _token(connection_id)
    with httpx.Client() as client:
        if profile == "account_daily":
            rows = fetch_account_daily(client, token, account_id, date_from, date_to)
        elif profile == "media_performance":
            rows = fetch_media_performance(client, token, account_id, date_from, date_to)
        else:  # defensive: dispatch only exposes the two branches above
            raise InstagramWindowError(f"Unsupported Instagram report profile: {profile}")
    row_count = _land(rows, {"pull_id": pull_id, "project_id": project_id})
    logger.info(
        "instagram_insights_pull_completed pull_id=%s profile=%s row_count=%d",
        pull_id,
        profile,
        row_count,
    )
    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
    }


def pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    instagram_account_id: str | None = None,
    selection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del selection
    return _pull_profile(
        connection_id,
        date_from,
        date_to,
        project_id,
        pull_id,
        "account_daily",
        instagram_account_id,
    )


def pull_account_daily(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    instagram_account_id: str | None = None,
    selection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del selection
    return _pull_profile(
        connection_id,
        date_from,
        date_to,
        project_id,
        pull_id,
        "account_daily",
        instagram_account_id,
    )


def pull_media_performance(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    instagram_account_id: str | None = None,
    selection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del selection
    return _pull_profile(
        connection_id,
        date_from,
        date_to,
        project_id,
        pull_id,
        "media_performance",
        instagram_account_id,
    )


def transform(raw_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    manifest = _manifest()
    mappings = manifest["canonical_metric_mapping"] | manifest["canonical_dimension_mapping"]
    transformed: list[dict[str, Any]] = []
    for row in raw_rows:
        output: dict[str, Any] = {}
        for key, value in row.items():
            target = mappings.get(key, key)
            if isinstance(target, dict):
                target = target["canonical"]
            output[target] = value
        transformed.append(output)
    return transformed


@mcp_app.tool()
def get_instagram_insights_report(
    project_id: str = "default",
    report_profile: str = "account_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict[str, Any]:
    """Read-only governed Instagram Insights envelope."""

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
