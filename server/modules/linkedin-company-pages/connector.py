"""Organic LinkedIn Company Pages Community Management connector."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)
mcp_app = FastMCP("linkedin-company-pages")
API_BASE = "https://api.linkedin.com/rest"
LINKEDIN_VERSION = os.environ.get("LINKEDIN_COMPANY_PAGES_API_VERSION", "202604")
RESTLI_VERSION = "2.0.0"
MAX_VERSION_AGE_MONTHS = 12


class LinkedInPagesOnboardingError(RuntimeError):
    """Community Management approval/scope/admin access is unavailable."""


class LinkedInPagesCompatibilityError(ValueError):
    """The requested profile/window/facet combination is illegal."""


def _manifest() -> dict:
    return json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))


def check_version_support(today: date | None = None) -> int:
    current = today or datetime.now(UTC).date()
    year, month = int(LINKEDIN_VERSION[:4]), int(LINKEDIN_VERSION[4:])
    age = (current.year - year) * 12 + current.month - month
    if age < 0 or age >= MAX_VERSION_AGE_MONTHS:
        raise RuntimeError(
            f"Pinned Linkedin-Version {LINKEDIN_VERSION} is outside the supported rollout window"
        )
    return age


def _provider_code(payload):
    if not isinstance(payload, dict):
        return None
    return str(payload.get("code") or payload.get("serviceErrorCode") or "") or None


def _raise_response(response):
    if response.status_code == 429:
        from core.quota import RateLimitError  # noqa: PLC0415

        raw = response.headers.get("Retry-After")
        try:
            retry_after = int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            retry_after = None
        raise RateLimitError("linkedin-company-pages", retry_after)
    try:
        original = response.json()
    except Exception:
        original = response.text
    normalized = dict(original) if isinstance(original, dict) else original
    if isinstance(normalized, dict) and (code := _provider_code(original)):
        normalized["code"] = code
    from core.pull_errors import classify_http_error  # noqa: PLC0415

    raise classify_http_error(response.status_code, normalized, _manifest().get("error_map"))


def _headers(token: str) -> dict[str, str]:
    check_version_support()
    return {
        "Authorization": f"Bearer {token}",
        "Linkedin-Version": LINKEDIN_VERSION,
        "X-Restli-Protocol-Version": RESTLI_VERSION,
    }


def _request(client, method, path, token, **kwargs):
    response = client.request(
        method, f"{API_BASE}{path}", headers=_headers(token), timeout=60, **kwargs
    )
    if response.status_code < 200 or response.status_code >= 300:
        _raise_response(response)
    return response


def _token(connection_id: str) -> str:
    from core import nango_client  # noqa: PLC0415

    return nango_client.get_fresh_token(connection_id, provider="linkedin-company-pages")


def discover_accounts(connection_id: str, *, _client=None, _token_value=None) -> list[dict]:
    token = _token_value or _token(connection_id)
    client = _client or httpx.Client()
    payload = _request(
        client,
        "GET",
        "/organizationAcls",
        token,
        params={"q": "roleAssignee", "role": "ADMINISTRATOR", "state": "APPROVED"},
    ).json()
    selections = []
    for item in payload.get("elements") or []:
        urn = str(item.get("organization") or item.get("organizationalTarget") or "")
        if not urn:
            continue
        selections.append(
            {
                "id": f"linkedin_pages_selection_{len(selections) + 1}",
                "organization_urn": urn,
                "organization_path": quote(urn, safe=""),
                "display_name": item.get("organizationName") or urn,
            }
        )
    if not selections:
        raise LinkedInPagesOnboardingError(
            "No approved administered organization; verify rw_organization_admin "
            "and Community Management access"
        )
    return selections


def validate_window(
    date_from: str | None,
    date_to: str | None,
    *,
    lifetime: bool,
    facet: str | None,
    today: date | None = None,
) -> None:
    if lifetime:
        return
    if facet:
        raise LinkedInPagesCompatibilityError(
            "Time-bound follower/page demographic facets are unsupported"
        )
    current = today or datetime.now(UTC).date()
    start, end = date.fromisoformat(date_from or ""), date.fromisoformat(date_to or "")
    latest = current - timedelta(days=2)
    earliest = latest - timedelta(days=365)
    if start < earliest or end > latest or end < start:
        raise LinkedInPagesCompatibilityError(
            "Time-bound LinkedIn Page statistics require the rolling 12 months ending two days ago"
        )


def paginate_restli(client, token, path, *, params, count=100) -> list[dict]:
    start = 0
    rows = []
    while True:
        query = {**params, "start": start, "count": count}
        payload = _request(client, "GET", path, token, params=query).json()
        page = payload.get("elements") or []
        rows.extend(page)
        paging = payload.get("paging") or {}
        total = int(paging.get("total") or len(rows))
        if not page or start + len(page) >= total:
            return rows
        start += len(page)


PROFILE_PATHS = {
    "follower_statistics": "/organizationalEntityFollowerStatistics",
    "page_statistics": "/organizationPageStatistics",
    "organic_share_statistics": "/organizationalEntityShareStatistics",
}


def fetch_statistics(client, token, profile, selection, date_from, date_to):
    if profile not in PROFILE_PATHS:
        raise LinkedInPagesCompatibilityError(f"Unsupported organic profile: {profile}")
    lifetime = bool(selection.get("lifetime"))
    facet = selection.get("facet")
    validate_window(date_from, date_to, lifetime=lifetime, facet=facet)
    params = {"q": "organizationalEntity", "organizationalEntity": selection["organization_urn"]}
    if not lifetime:
        params["timeIntervals.timeRange.start"] = int(
            datetime.fromisoformat(date_from).replace(tzinfo=UTC).timestamp() * 1000
        )
        params["timeIntervals.timeRange.end"] = int(
            (datetime.fromisoformat(date_to).replace(tzinfo=UTC) + timedelta(days=1)).timestamp()
            * 1000
        )
        params["timeIntervals.timeGranularityType"] = selection.get("grain", "DAY")
    if facet:
        params["facet"] = facet
    return paginate_restli(client, token, PROFILE_PATHS[profile], params=params)


_RAW_DDL = """
CREATE TABLE IF NOT EXISTS raw_linkedin_company_pages_daily (
 profile VARCHAR, organization_urn VARCHAR, entity_id VARCHAR, interval_start VARCHAR,
 interval_end VARCHAR, facet VARCHAR, facet_value VARCHAR, metric VARCHAR, value DOUBLE,
 non_additive BOOLEAN, lifetime BOOLEAN, payload_json VARCHAR, pull_id VARCHAR,
 loaded_at VARCHAR, project_id VARCHAR
)
"""


def _extract_page_statistics_metrics(row: dict) -> list[tuple[str, float]]:
    """Flatten the nested totalPageStatistics structure into (metric_name, value) pairs.

    LinkedIn organizationPageStatistics nests page view counts at:
      totalPageStatistics.views.allPageViews.pageViews
      totalPageStatistics.views.allDesktopPageViews.pageViews
      totalPageStatistics.views.allMobilePageViews.pageViews
    Clicks are nested inside totalPageStatistics.clicks (a dict keyed by click type);
    the total is the sum across all click-type keys whose values are numeric.
    """
    tps = row.get("totalPageStatistics") or {}
    views = tps.get("views") or {}
    pairs: list[tuple[str, float]] = []

    def _view(key: str) -> float | None:
        section = views.get(key) or {}
        v = section.get("pageViews")
        return float(v) if isinstance(v, (int, float)) else None

    for metric_name, view_key in (
        ("page_views", "allPageViews"),
        ("desktop_page_views", "allDesktopPageViews"),
        ("mobile_page_views", "allMobilePageViews"),
    ):
        val = _view(view_key)
        if val is not None:
            pairs.append((metric_name, val))

    # clicks: totalPageStatistics.clicks is a dict of click-type → count
    clicks_section = tps.get("clicks") or {}
    if isinstance(clicks_section, dict):
        total_clicks = sum(v for v in clicks_section.values() if isinstance(v, (int, float)))
        if clicks_section:  # only emit if the dict was non-empty
            pairs.append(("clicks", float(total_clicks)))
    elif isinstance(clicks_section, (int, float)):
        pairs.append(("clicks", float(clicks_section)))

    return pairs


def _land(rows, context):
    if os.environ.get("TOOROW_DB_MODE", "duckdb") != "duckdb":
        raise ValueError("linkedin-company-pages local landing currently requires duckdb")
    import duckdb  # noqa: PLC0415

    path = os.environ.get("TOOROW_DUCKDB_PATH", str(Path(__file__).parent / "local.duckdb"))
    loaded_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    profile = context["profile"]
    values = []
    for row in rows:
        entity_id = str(row.get("share") or row.get("organizationalEntity") or "")
        interval_start = str(row.get("timeRange", {}).get("start") or context["date_from"])
        interval_end = str(row.get("timeRange", {}).get("end") or context["date_to"])
        facet_value = str(row.get("facetValue") or "")
        payload_json = json.dumps(row, sort_keys=True, separators=(",", ":"))

        if profile == "page_statistics":
            metric_pairs = _extract_page_statistics_metrics(row)
        else:
            stats = (
                row.get("followerGains")
                or row.get("totalShareStatistics")
                or row.get("statistics")
                or {}
            )
            metric_pairs = [
                (metric, float(value))
                for metric, value in stats.items()
                if isinstance(value, (int, float))
            ]

        for metric, value in metric_pairs:
            values.append(
                (
                    profile,
                    context["organization_urn"],
                    entity_id,
                    interval_start,
                    interval_end,
                    context.get("facet", ""),
                    facet_value,
                    metric,
                    value,
                    context["lifetime"]
                    or any(
                        token in metric.lower()
                        for token in ("engagement", "unique", "rate", "distribution")
                    ),
                    context["lifetime"],
                    payload_json,
                    context["pull_id"],
                    loaded_at,
                    context["project_id"],
                )
            )
    connection = duckdb.connect(path)
    connection.execute(_RAW_DDL)
    if values:
        connection.executemany(
            "INSERT INTO raw_linkedin_company_pages_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
    connection.close()
    return len(values)


def _pull_profile(connection_id, date_from, date_to, project_id, pull_id, profile, selection):
    if not selection:
        raise LinkedInPagesOnboardingError("An administered organization selection is required")
    token = _token(connection_id)
    rows = fetch_statistics(httpx.Client(), token, profile, selection, date_from, date_to)
    context = {
        **selection,
        "profile": profile,
        "date_from": date_from,
        "date_to": date_to,
        "lifetime": bool(selection.get("lifetime")),
        "pull_id": pull_id,
        "project_id": project_id,
    }
    count = _land(rows, context)
    return {"pull_id": pull_id, "row_count": count, "date_from": date_from, "date_to": date_to}


def pull(connection_id, date_from, date_to, project_id, pull_id, selection=None):
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "follower_statistics", selection
    )


def pull_follower_statistics(
    connection_id, date_from, date_to, project_id, pull_id, selection=None
):
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "follower_statistics", selection
    )


def pull_page_statistics(connection_id, date_from, date_to, project_id, pull_id, selection=None):
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "page_statistics", selection
    )


def pull_organic_share_statistics(
    connection_id, date_from, date_to, project_id, pull_id, selection=None
):
    return _pull_profile(
        connection_id,
        date_from,
        date_to,
        project_id,
        pull_id,
        "organic_share_statistics",
        selection,
    )


def transform(raw_rows):
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
def get_linkedin_company_pages_report(
    project_id: str = "default",
    report_profile: str = "follower_statistics",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Read-only governed organic LinkedIn Company Pages envelope."""
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
