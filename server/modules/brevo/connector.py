"""Brevo API v3 KPI and event connector."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)
mcp_app = FastMCP("brevo")

API_BASE = "https://api.brevo.com/v3"
PROVIDER_API_VERSION = "v3"
EVENT_MAX_DAYS = 90
EVENT_MAX_PAGE = 5000
DEFAULT_PAGE = 2500
PROFILE_SCOPES = {
    "email_campaign_daily": {"account:read", "campaigns.email:read"},
    "sms_campaign_daily": {"account:read", "campaigns.sms:read"},
    "transactional_events": {
        "account:read",
        "transactional.email:read",
        "transactional.sms:read",
        "events:read",
    },
    "contact_list_growth": {"account:read", "contacts:read"},
}
_quota_lock = threading.Lock()
_quota_state: dict[tuple[str, str], dict[str, int]] = {}


class BrevoOnboardingError(RuntimeError):
    """The OAuth account/scopes are insufficient for the enabled profiles."""


class BrevoScopeError(PermissionError):
    """A required least-privilege read scope is absent."""


def _manifest() -> dict:
    return json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))


def required_scopes(profiles: list[str]) -> list[str]:
    unknown = sorted(set(profiles) - set(PROFILE_SCOPES))
    if unknown:
        raise BrevoScopeError(f"Unknown Brevo profile(s): {', '.join(unknown)}")
    return sorted(set().union(*(PROFILE_SCOPES[profile] for profile in profiles)))


def validate_scopes(profiles: list[str], granted_scopes: list[str]) -> None:
    missing = sorted(set(required_scopes(profiles)) - set(granted_scopes))
    if missing:
        raise BrevoScopeError(f"Missing Brevo read scope(s): {', '.join(missing)}")


def _provider_code(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    return str(payload.get("code") or payload.get("error") or "") or None


def _raise_response(response) -> None:
    if response.status_code == 429:
        from core.quota import RateLimitError  # noqa: PLC0415

        raw = response.headers.get("x-sib-ratelimit-reset") or response.headers.get("Retry-After")
        try:
            retry_after = int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            retry_after = None
        raise RateLimitError("brevo", retry_after)
    try:
        original = response.json()
    except Exception:
        original = response.text
    normalized = dict(original) if isinstance(original, dict) else original
    if isinstance(normalized, dict) and (code := _provider_code(original)):
        normalized["code"] = code
    from core.pull_errors import classify_http_error  # noqa: PLC0415

    raise classify_http_error(response.status_code, normalized, _manifest().get("error_map"))


def _cache_quota(account_id: str, endpoint: str, headers: dict) -> None:
    values = {}
    for name in ("limit", "remaining", "reset"):
        raw = headers.get(f"x-sib-ratelimit-{name}")
        if raw not in (None, ""):
            try:
                values[name] = int(raw)
            except (TypeError, ValueError):
                continue
    if values:
        with _quota_lock:
            _quota_state[(account_id, endpoint)] = values


def quota_snapshot(account_id: str, endpoint: str) -> dict[str, int]:
    with _quota_lock:
        return dict(_quota_state.get((account_id, endpoint), {}))


def _request(client, method: str, path: str, token: str, *, account_id="unknown", **kwargs):
    response = client.request(
        method,
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}", "accept": "application/json"},
        timeout=60,
        **kwargs,
    )
    _cache_quota(account_id, path.split("?", 1)[0], response.headers)
    if response.status_code < 200 or response.status_code >= 300:
        _raise_response(response)
    return response


def _token(connection_id: str) -> str:
    from core import nango_client  # noqa: PLC0415

    return nango_client.get_fresh_token(connection_id, provider="brevo")


def discover_accounts(connection_id: str, *, _client=None, _token_value: str | None = None):
    token = _token_value or _token(connection_id)
    client = _client or httpx.Client()
    account = _request(client, "GET", "/account", token).json()
    account_id = str(account.get("id") or account.get("email") or "")
    if not account_id:
        raise BrevoOnboardingError(
            "Brevo /account returned no accessible OAuth account; verify account:read"
        )
    return [
        {
            "id": "brevo_account_selection_1",
            "account_id": account_id,
            "display_name": account.get("companyName") or "Brevo account",
            "plan": account.get("plan") or [],
        }
    ]


def _validate_event_window(date_from: str, date_to: str, limit: int) -> None:
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    if end < start or (end - start).days + 1 > EVENT_MAX_DAYS:
        raise ValueError("Brevo transactional event window must be between 1 and 90 days")
    if limit < 1 or limit > EVENT_MAX_PAGE:
        raise ValueError("Brevo transactional event limit must be between 1 and 5000")


def paginate_offset(
    client,
    token: str,
    path: str,
    *,
    account_id: str,
    item_key: str,
    limit: int = DEFAULT_PAGE,
    params: dict | None = None,
) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        query = dict(params or {})
        query.update({"limit": limit, "offset": offset})
        payload = _request(client, "GET", path, token, account_id=account_id, params=query).json()
        page = payload.get(item_key) or []
        rows.extend(page)
        if len(page) < limit:
            return rows
        offset += limit


def fetch_transactional_events(
    client,
    token: str,
    account_id: str,
    date_from: str,
    date_to: str,
    *,
    limit: int = DEFAULT_PAGE,
) -> list[dict]:
    _validate_event_window(date_from, date_to, limit)
    return paginate_offset(
        client,
        token,
        "/smtp/statistics/events",
        account_id=account_id,
        item_key="events",
        limit=limit,
        params={"startDate": date_from, "endDate": date_to},
    )


def verify_webhook_signature(body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.removeprefix("sha256="))


def deduplicate_events(events: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique = []
    for event in events:
        event_id = str(
            event.get("id")
            or event.get("messageId")
            or hashlib.sha256(
                json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
        if event_id not in seen:
            seen.add(event_id)
            unique.append({**event, "event_id": event_id})
    return unique


def protect_identifier(value: str, project_id: str) -> str:
    return hashlib.sha256(f"{project_id}:{value}".encode()).hexdigest()


def safe_rate(numerator: float | int, denominator: float | int) -> float | None:
    return float(numerator) / float(denominator) if denominator else None


_RAW_DDL = """
CREATE TABLE IF NOT EXISTS raw_brevo_daily (
  profile VARCHAR, account_id VARCHAR, source_id VARCHAR, date VARCHAR, channel VARCHAR,
  event_type VARCHAR, protected_identifier VARCHAR, metric VARCHAR, value DOUBLE,
  non_additive BOOLEAN, payload_json VARCHAR, pull_id VARCHAR, loaded_at VARCHAR,
  project_id VARCHAR
)
"""

# Catalog-declared metric field_ids (api_catalog.json exposure=exposed, kind=metric).
# Only these are landed in raw_brevo_daily; API-returned rate fields (openRate,
# clickRate, unsubscriptionRate, etc.) are intentionally excluded (H-2: AD-4).
_CATALOG_METRIC_IDS: frozenset[str] = frozenset(
    {
        "sent",
        "delivered",
        "opens",
        "clicks",
        "hard_bounces",
        "unsubscribed",
        "event_count",
        "contact_count",
    }
)


def _land(rows: list[dict], context: dict) -> int:
    if os.environ.get("TOOROW_DB_MODE", "duckdb") != "duckdb":
        raise ValueError("brevo local landing currently requires duckdb")
    import duckdb  # noqa: PLC0415

    path = os.environ.get("TOOROW_DUCKDB_PATH", str(Path(__file__).parent / "local.duckdb"))
    loaded_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    values = []
    for row in rows:
        source_id = str(row.get("id") or row.get("event_id") or row.get("messageId") or "")
        protected = protect_identifier(
            str(row.get("email") or row.get("contact") or ""), context["project_id"]
        )
        row_date = str(row.get("date") or row.get("createdAt") or context["date_from"])[:10]
        # H-2: gate against catalog-declared field ids only.  API-returned computed
        # rate fields (openRate, clickRate, unsubscriptionRate, softBounceRate,
        # hardBounceRate) are NOT in _CATALOG_METRIC_IDS and are therefore excluded
        # from the raw landing.  This prevents non-additive ratios from appearing
        # alongside additive counters in raw_brevo_daily / stg_brevo_daily.
        metrics = {
            key: value
            for key, value in row.items()
            if isinstance(value, (int, float))
            and key not in {"id"}
            and key in _CATALOG_METRIC_IDS
        }
        for metric, value in metrics.items():
            values.append(
                (
                    context["profile"],
                    context["account_id"],
                    source_id,
                    row_date,
                    context["channel"],
                    str(row.get("event") or ""),
                    protected,
                    metric,
                    float(value),
                    any(token in metric.lower() for token in ("rate", "unique", "average")),
                    json.dumps(
                        {
                            key: value
                            for key, value in row.items()
                            if key not in {"email", "contact"}
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    context["pull_id"],
                    loaded_at,
                    context["project_id"],
                )
            )
    connection = duckdb.connect(path)
    connection.execute(_RAW_DDL)
    if values:
        connection.executemany(
            "INSERT INTO raw_brevo_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values
        )
    connection.close()
    return len(values)


def _pull_profile(connection_id, date_from, date_to, project_id, pull_id, profile, selection):
    if not selection or not selection.get("account_id"):
        raise BrevoOnboardingError("A Brevo OAuth account selection is required")
    validate_scopes([profile], selection.get("granted_scopes") or [])
    token = _token(connection_id)
    client = httpx.Client()
    account_id = selection["account_id"]
    if profile == "email_campaign_daily":
        rows = paginate_offset(
            client,
            token,
            "/emailCampaigns",
            account_id=account_id,
            item_key="campaigns",
            limit=50,
            params={"startDate": date_from, "endDate": date_to},
        )
        channel = "email"
    elif profile == "sms_campaign_daily":
        rows = paginate_offset(
            client,
            token,
            "/smsCampaigns",
            account_id=account_id,
            item_key="campaigns",
            limit=50,
            params={"startDate": date_from, "endDate": date_to},
        )
        channel = "sms"
    elif profile == "transactional_events":
        # Epic 31: transactional_events profile declares landing="context_events".
        # Route through persist_context_event(), not _land() / raw_brevo_daily.
        return _pull_transactional_events_to_context(
            client, token, account_id, date_from, date_to, project_id, pull_id, selection
        )
    else:
        rows = paginate_offset(
            client, token, "/contacts/lists", account_id=account_id, item_key="lists", limit=50
        )
        channel = "contacts_snapshot"
    context = {
        "profile": profile,
        "account_id": account_id,
        "channel": channel,
        "date_from": date_from,
        "pull_id": pull_id,
        "project_id": project_id,
    }
    count = _land(rows, context)
    return {"pull_id": pull_id, "row_count": count, "date_from": date_from, "date_to": date_to}


def _pull_transactional_events_to_context(
    client,
    token: str,
    account_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    selection: dict,
) -> dict:
    """Fetch Brevo transactional events and persist them in app.context_events (Epic 31).

    Mirrors the youtube-analytics pull_video_upload / github _pull pattern:
    1. Fetch raw event rows from /smtp/statistics/events (paginated, deduplicated).
    2. transform_events() maps rows to canonical event dicts (windowed).
    3. delete_connector_events_in_window() clears prior rows for this source + window
       (idempotent re-pull: delete-by-source-window before re-insert).
    4. persist_context_event() inserts each canonical event.

    Landing: app.context_events (Postgres) via core.context_events, NOT raw_brevo_daily.
    Verification: blocked (no live account); correct per manifest verification.status.
    """
    from core.context_events import (  # noqa: PLC0415
        delete_connector_events_in_window,
        persist_context_event,
    )

    raw_rows = deduplicate_events(
        fetch_transactional_events(
            client,
            token,
            account_id,
            date_from,
            date_to,
            limit=selection.get("limit", DEFAULT_PAGE),
        )
    )
    # channel = "transactional_email" for /smtp/statistics/events (email path).
    # SMS transactional events would use a separate endpoint; for now this callable
    # handles the combined transactional profile, and event_type is row-level.
    canonical_events = transform_events(
        raw_rows, date_from=date_from, date_to=date_to, channel="transactional_email"
    )
    # Idempotent window: clear all brevo transactional events in this window before
    # re-inserting.  Both email and sms event types are cleared by source.
    for event_type in _CANONICAL_EVENT_MAPPING.values():
        delete_connector_events_in_window(
            project_id=project_id,
            source="brevo",
            event_type=event_type,
            date_from=date_from,
            date_to=date_to,
        )
    event_count = 0
    for ev in canonical_events:
        persist_context_event(
            project_id=project_id,
            event_date=ev["event_date"],
            type=ev["event_type"],
            label=ev["label"],
            description=ev["description"],
            created_by=pull_id,
            platform=ev["platform"],
            value=None,
            source=ev["source"],
        )
        event_count += 1
    logger.info(
        "brevo pull_transactional_events: persisted %d context_events pull_id=%s "
        "date_from=%s date_to=%s",
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


def pull(connection_id, date_from, date_to, project_id, pull_id, selection=None):
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "email_campaign_daily", selection
    )


def pull_email_campaign_daily(
    connection_id, date_from, date_to, project_id, pull_id, selection=None
):
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "email_campaign_daily", selection
    )


def pull_sms_campaign_daily(connection_id, date_from, date_to, project_id, pull_id, selection=None):
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "sms_campaign_daily", selection
    )


def pull_transactional_events(
    connection_id, date_from, date_to, project_id, pull_id, selection=None
):
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "transactional_events", selection
    )


def pull_contact_list_growth(
    connection_id, date_from, date_to, project_id, pull_id, selection=None
):
    return _pull_profile(
        connection_id, date_from, date_to, project_id, pull_id, "contact_list_growth", selection
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
# transform_events() -- canonical event mapping for the transactional_events
# profile (Epic 31).  Pure function (no I/O), unit-testable.
# ---------------------------------------------------------------------------

# Canonical event mapping mirrors canonical_event_mapping from the manifest.
_CANONICAL_EVENT_MAPPING: dict[str, str] = {
    "transactional_email": "transactional_email",
    "transactional_sms": "transactional_sms",
}

# Brevo /smtp/statistics/events returns an "event" field with values like
# "delivered", "opened", "clicked", "hardBounced", etc.  We normalise the
# channel from context (transactional_email / transactional_sms) rather than
# from the individual event row, since the SMS endpoint is separate.
_BREVO_EVENT_TYPE_MAP: dict[str, str] = {
    "delivered": "transactional_email",
    "opened": "transactional_email",
    "clicked": "transactional_email",
    "hardBounced": "transactional_email",
    "softBounced": "transactional_email",
    "unsubscribed": "transactional_email",
    "spamReported": "transactional_email",
}


def transform_events(
    raw_rows: list[dict],
    date_from: str | None = None,
    date_to: str | None = None,
    *,
    channel: str = "transactional",
) -> list[dict]:
    """Map raw /smtp/statistics/events rows to canonical event dicts (AD-2, pure function).

    Input: deduplicated event rows from fetch_transactional_events().
    Output: canonical event dicts with keys event_type, event_date, label,
    description, platform, source -- ready for persist_context_event().

    event_date  = date/createdAt field truncated to YYYY-MM-DD.
    event_type  = canonical from _CANONICAL_EVENT_MAPPING keyed by channel;
                  defaults to "transactional_email".
    label       = brevo event name (e.g. "delivered", "opened") -- max 120 chars.
    description = messageId for traceability.
    platform    = "brevo".
    source      = "brevo".

    Date window: items whose event_date falls outside [date_from, date_to] are
    dropped -- symmetric to the youtube-analytics/github guards.  Both None means
    no window (golden-replay contract).

    Rows with no parseable date (< 10 chars) are skipped.
    """
    event_type = _CANONICAL_EVENT_MAPPING.get(channel, "transactional_email")
    result: list[dict] = []
    for row in raw_rows:
        raw_date = str(row.get("date") or row.get("createdAt") or "")
        if len(raw_date) < 10:
            logger.debug("transform_events: skipping row with missing date: %r", row.get("event"))
            continue
        event_date = raw_date[:10]
        if date_from is not None and event_date < date_from:
            continue
        if date_to is not None and event_date > date_to:
            continue
        label = str(row.get("event") or "transactional_event")[:120]
        description = str(row.get("messageId") or row.get("event_id") or "")
        result.append(
            {
                "event_type": event_type,
                "event_date": event_date,
                "label": label,
                "description": description,
                "platform": "brevo",
                "source": "brevo",
            }
        )
    return result


@mcp_app.tool()
def get_brevo_report(
    project_id: str = "default",
    report_profile: str = "email_campaign_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Read-only governed Brevo KPI/event envelope."""
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
