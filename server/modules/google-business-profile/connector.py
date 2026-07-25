"""Google Business Profile connector -- Performance API (daily location metrics).

Exposes a ``mcp_app: FastMCP`` instance the core loader mounts under the
``google-business-profile`` namespace (AD-2). Built to the epic-25 industrial
standard: generated api_catalog.json (DailyMetric enum transcription),
status-keyed error_map (Google returns the standard error envelope, no numeric
subcodes), two-level account->location topology.

Central design fact: GBP HAS history but it is HARD-CAPPED at ~18 months (daily).
Data older than 18 months from the request date is unreachable; there is no
backfill beyond that window, so the connector must sync early and persist to the
warehouse for anything longer (YoY). Plus a ~3-7 day reporting lag on recent
days, and zero-days OMITTED from datedValues (gap-filled to 0 here). ACCESS GATE:
a newly-enabled project has 0 QPM until Google approves an access request -- every
call 403s until then (a provisioning gate, surfaced honestly at ratification).

# AD-12: MCP server reads the fact_gbp_location_daily mart only -- no raw_* tables.
# AD-3:  OAuth token via the DIRECT Google path (get_fresh_token(..., provider=
#        'google-business-profile')) immediately before use -- never stored/logged.
#        Single scope business.manage. NOT Nango (AD-21 Google-direct exception).
# AD-7:  pull_id minted by the core scheduler and passed into pull().
# AD-2:  metric renames driven by the manifest canonical_metric_mapping (enum ->
#        canonical) -- no hardcoded field list.
# AD-4:  all 11 DailyMetric values are ADDITIVE daily counts.
# AI-03: ASCII-only stdout/log strings.
#
# API facts (VERIFIED 2026-07-21 against developers.google.com/my-business):
#   - GET https://businessprofileperformance.googleapis.com/v1/{location}:
#     fetchMultiDailyMetricsTimeSeries?dailyMetrics=<enum>&dailyMetrics=...&
#     dailyRange.startDate.year=&...&dailyRange.endDate.day= -> multiDailyMetric
#     TimeSeries[].dailyMetricTimeSeries[].{dailyMetric, timeSeries.datedValues[]
#     .{date:{year,month,day}, value:"<int64>"}}.
#   - Topology: accounts.list (mybusinessaccountmanagement) -> accounts.locations
#     .list (mybusinessbusinessinformation). locations/{id} = reporting entity.
#   - Error body = standard Google { error:{code,message,status,errors[]} };
#     error_map keyed on HTTP status. 429 RESOURCE_EXHAUSTED -> RateLimitError.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Module-level FastMCP instance -- the public surface the loader mounts.
mcp_app = FastMCP("google-business-profile")

# Base hosts (federated GBP API model).
GBP_PERFORMANCE_BASE = "https://businessprofileperformance.googleapis.com/v1"
GBP_ACCOUNTS_BASE = "https://mybusinessaccountmanagement.googleapis.com/v1"
GBP_BUSINESSINFO_BASE = "https://mybusinessbusinessinformation.googleapis.com/v1"

# ---------------------------------------------------------------------------
# Database connection helpers -- env-var driven (no hardcoded paths).
# ---------------------------------------------------------------------------

_DEFAULT_DUCKDB_PATH = os.path.join(os.path.dirname(__file__), "seeds", "local.duckdb")


def _get_db_mode() -> str:
    return os.environ.get("TOOROW_DB_MODE", "duckdb")


def _get_duckdb_path() -> str:
    return os.environ.get("TOOROW_DUCKDB_PATH", _DEFAULT_DUCKDB_PATH)


# ---------------------------------------------------------------------------
# Manifest access (cached): error_map + canonical mappings.
# ---------------------------------------------------------------------------

_MANIFEST: dict | None = None


def _load_manifest() -> dict:
    global _MANIFEST
    if _MANIFEST is None:
        _MANIFEST = json.loads(
            (Path(__file__).parent / "manifest.json").read_text(encoding="utf-8")
        )
    return _MANIFEST


def _load_error_map() -> dict:
    return _load_manifest().get("error_map") or {}


def _metric_source_tokens() -> list[str]:
    """The provider DailyMetric enums to request (from the canonical mapping keys)."""
    return list(_load_manifest().get("canonical_metric_mapping", {}).keys())


# ---------------------------------------------------------------------------
# transform() -- manifest-driven canonical field mapping (AD-2)
# ---------------------------------------------------------------------------


def transform(raw_rows: list[dict]) -> list[dict]:
    """Map raw DailyMetric enum keys to canonical names via the manifest mapping.

    AD-2: renames driven by canonical_metric_mapping (BUSINESS_IMPRESSIONS_* ->
    business_impressions_*, CALL_CLICKS -> call_clicks, ...). Fields absent from
    the mapping pass through unchanged (pull_id, connector, date, location_id).
    """
    manifest = _load_manifest()
    rename_map: dict[str, str] = {}
    for src, val in manifest.get("canonical_metric_mapping", {}).items():
        rename_map[src] = val if isinstance(val, str) else val.get("canonical", src)
    rename_map.update(manifest.get("canonical_dimension_mapping", {}))

    result: list[dict] = []
    for row in raw_rows:
        canonical: dict = {}
        for key, value in row.items():
            canonical[rename_map.get(key, key)] = value
        result.append(canonical)
    return result


# ---------------------------------------------------------------------------
# Raw landing (wide, canonical metric column names).
# ---------------------------------------------------------------------------

_RAW_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_gbp_location_daily (
    date                                 VARCHAR,
    location_id                          VARCHAR,
    business_impressions_desktop_maps    BIGINT,
    business_impressions_desktop_search  BIGINT,
    business_impressions_mobile_maps     BIGINT,
    business_impressions_mobile_search   BIGINT,
    business_conversations               BIGINT,
    business_direction_requests          BIGINT,
    call_clicks                          BIGINT,
    website_clicks                       BIGINT,
    business_bookings                    BIGINT,
    business_food_orders                 BIGINT,
    business_food_menu_clicks            BIGINT,
    pull_id                              VARCHAR,
    loaded_at                            VARCHAR,
    project_id                           VARCHAR
)
"""

_METRIC_COLUMNS = [
    "business_impressions_desktop_maps",
    "business_impressions_desktop_search",
    "business_impressions_mobile_maps",
    "business_impressions_mobile_search",
    "business_conversations",
    "business_direction_requests",
    "call_clicks",
    "website_clicks",
    "business_bookings",
    "business_food_orders",
    "business_food_menu_clicks",
]

_RAW_INSERT_SQL = f"""
INSERT INTO raw_gbp_location_daily
    (date, location_id, {", ".join(_METRIC_COLUMNS)}, pull_id, loaded_at, project_id)
VALUES ({", ".join(["?"] * (2 + len(_METRIC_COLUMNS) + 3))})
"""


def _to_int(value) -> int | None:
    """Coerce a metric value (stringified int64 or number) to int; None stays None."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _insert_raw_rows(rows: list[dict], pull_id: str, project_id: str) -> int:
    """Insert canonical (post-transform) daily rows into raw_gbp_location_daily."""
    db_mode = _get_db_mode()
    if db_mode != "duckdb":
        raise ValueError(f"_insert_raw_rows: unsupported db_mode {db_mode!r} at P-dev")
    import duckdb  # noqa: PLC0415

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    con = duckdb.connect(_get_duckdb_path())
    con.execute(_RAW_CREATE_DDL)
    values = [
        (
            r.get("date", ""),
            r.get("location_id", ""),
            *[_to_int(r.get(col)) for col in _METRIC_COLUMNS],
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
# HTTP error handling -- standard Google envelope, HTTP-status error_map.
# ---------------------------------------------------------------------------


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    if resp.status_code == 429:
        from core.quota import RateLimitError  # noqa: PLC0415

        retry_after_raw = resp.headers.get("Retry-After", "0")
        try:
            retry_after = int(retry_after_raw) or None
        except (ValueError, TypeError):
            retry_after = None
        raise RateLimitError("google-business-profile", retry_after)

    from core.pull_errors import classify_http_error  # noqa: PLC0415

    try:
        body = resp.json()
    except Exception:
        body = resp.text
    raise classify_http_error(resp.status_code, body, _load_error_map())


def _date_str(date_obj: dict) -> str:
    """Format a Google {year,month,day} date object as YYYY-MM-DD."""
    y = date_obj.get("year", 0)
    m = date_obj.get("month", 0)
    d = date_obj.get("day", 0)
    return f"{y:04d}-{m:02d}-{d:02d}"


# ---------------------------------------------------------------------------
# pull() -- Performance API (called by the queue worker only, AD-12)
# ---------------------------------------------------------------------------


def _pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    location_id: str | None,
) -> dict:
    """Fetch fetchMultiDailyMetricsTimeSeries for a location and land daily rows.

    # AD-3: token via the direct Google path, used immediately, then discarded.
    Pivots the per-metric time series into one wide row per (location, date).
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    if not location_id:
        raise ValueError(
            "google-business-profile pull requires a selected location_id "
            "(topology selection_level 'location'); no env-var fallback (25.5+)."
        )

    token = nango_client.get_fresh_token(
        connection_id, provider="google-business-profile"
    )

    df = [int(p) for p in date_from.split("-")]
    dt = [int(p) for p in date_to.split("-")]
    params: list[tuple[str, str]] = [("dailyMetrics", m) for m in _metric_source_tokens()]
    params += [
        ("dailyRange.startDate.year", str(df[0])),
        ("dailyRange.startDate.month", str(df[1])),
        ("dailyRange.startDate.day", str(df[2])),
        ("dailyRange.endDate.year", str(dt[0])),
        ("dailyRange.endDate.month", str(dt[1])),
        ("dailyRange.endDate.day", str(dt[2])),
    ]

    loc = location_id if location_id.startswith("locations/") else f"locations/{location_id}"
    resp = httpx.get(
        f"{GBP_PERFORMANCE_BASE}/{loc}:fetchMultiDailyMetricsTimeSeries",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60.0,
    )
    _raise_for_status(resp)

    # Pivot: {date -> {metric_canonical: value}}. Zero-days are omitted by the API;
    # rows that appear carry their real value (gap-fill to 0 is a mart concern).
    rename = _load_manifest().get("canonical_metric_mapping", {})
    by_date: dict[str, dict] = {}
    payload = resp.json().get("multiDailyMetricTimeSeries") or []
    for block in payload:
        for series in block.get("dailyMetricTimeSeries") or []:
            enum = series.get("dailyMetric")
            canonical = rename.get(enum, enum)
            for dv in (series.get("timeSeries") or {}).get("datedValues") or []:
                d = _date_str(dv.get("date") or {})
                by_date.setdefault(d, {"date": d, "location_id": loc})[canonical] = dv.get("value")

    canonical_rows = list(by_date.values())
    row_count = _insert_raw_rows(canonical_rows, pull_id, project_id)

    logger.info(
        "gbp_pull_completed: pull_id=%s location=%s row_count=%d",
        pull_id, loc, row_count,
    )
    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
    }


def pull_location_daily(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    location_id: str | None = None,
) -> dict:
    """AI-58 dispatch: daily location performance (grain date x location_id)."""
    return _pull(connection_id, date_from, date_to, project_id, pull_id, location_id)


def pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    location_id: str | None = None,
) -> dict:
    """Default pull() = location_daily (AI-58 profile-less default dispatch)."""
    return pull_location_daily(
        connection_id, date_from, date_to, project_id, pull_id, location_id
    )


# ---------------------------------------------------------------------------
# Account topology discovery (playbook section 5) -- account -> location.
# ---------------------------------------------------------------------------


def discover_accounts(connection_id: str) -> list[dict]:
    """List the reachable Business Profile LOCATIONS (the reporting entity).

    Enumerates accounts (Account Management accounts.list) then locations per
    account (Business Information accounts.locations.list) and returns the generic
    hierarchy core's topology flow consumes:

        [{"id": "locations/<id>", "label": "<location title>",
          "parent": "accounts/<id>"}, ...]

    Raises core.quota.RateLimitError on 429; a typed ConnectorError otherwise.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2

    token = nango_client.get_fresh_token(
        connection_id, provider="google-business-profile"
    )
    headers = {"Authorization": f"Bearer {token}"}

    accounts: list[str] = []
    with httpx.Client() as client:
        page_token = None
        while True:
            params = {"pageSize": 100}
            if page_token:
                params["pageToken"] = page_token
            resp = client.get(
                f"{GBP_ACCOUNTS_BASE}/accounts", params=params, headers=headers, timeout=30.0
            )
            _raise_for_status(resp)
            body = resp.json()
            for acc in body.get("accounts") or []:
                name = acc.get("name")
                if name:
                    accounts.append(name)
            page_token = body.get("nextPageToken")
            if not page_token:
                break

        results: list[dict] = []
        for account in accounts:
            page_token = None
            while True:
                params = {"pageSize": 100, "readMask": "name,title"}
                if page_token:
                    params["pageToken"] = page_token
                resp = client.get(
                    f"{GBP_BUSINESSINFO_BASE}/{account}/locations",
                    params=params, headers=headers, timeout=30.0,
                )
                _raise_for_status(resp)
                body = resp.json()
                for loc in body.get("locations") or []:
                    name = loc.get("name")
                    if not name:
                        continue
                    results.append(
                        {"id": name, "label": loc.get("title") or name, "parent": account}
                    )
                page_token = body.get("nextPageToken")
                if not page_token:
                    break
    return results


# ---------------------------------------------------------------------------
# Epic 31.6 -- social_post event profile (landing: context_events).
#
# Generalises the YouTube 31.3 video_upload pattern to the local family: each GBP
# local post becomes a canonical marker `gbp > social_post > <summary>` that
# annotates the impressions/clicks curves AND feeds the MMM exogenous-regressor
# feature builder (epic-31 §10). This profile writes ONLY context_events -- never
# raw_gbp nor fact_daily_kpi (HG-2 inverted per profile).
#
# LIVE-DEFERRED: local posts live on the LEGACY v4 host, which is allowlist-gated,
# and GBP is 0-QPM by default (manifest verification.status blocked). The
# transform_events mapping + fixtures prove the pattern; the live pull is ratified
# once the v4 host is granted (no test account 2026-07-21).
# ---------------------------------------------------------------------------

# Legacy v4 host (localPosts / reviews). The Performance/BusinessInfo APIs are v1.
GBP_V4_BASE = "https://mybusiness.googleapis.com/v4"

# Canonical event mapping mirrors canonical_event_mapping in the manifest. Pure
# lookup (no I/O) -- unit-testable via golden_events/expected_events.
_CANONICAL_EVENT_MAPPING = {"social_post": "social_post"}


def transform_events(
    raw_rows: list[dict],
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict]:
    """Map raw v4 localPost dicts to canonical event dicts (AD-2, pure function).

    Input: a list of localPost resource dicts, each with ``createTime`` and
    ``summary``. Output: canonical event dicts with keys event_type, event_date,
    label, platform, source -- ready for persist_context_event() (value = None).

    event_date = createTime truncated to YYYY-MM-DD (UTC date).
    label      = summary.
    event_type = "social_post" (via _CANONICAL_EVENT_MAPPING).
    platform   = "gbp".
    source     = "google-business-profile".

    Validity (M1): a post with no usable createTime (missing, or shorter than a
    10-char ISO date prefix) is SKIPPED rather than persisted with an empty
    event_date (validate_event_input would reject it downstream anyway).

    Date window (H1): the localPosts edge returns every post, so when
    ``date_from``/``date_to`` are supplied, posts whose createTime date falls
    OUTSIDE [date_from, date_to] are dropped. Both None (the golden-replay
    contract) means "no window" -> every post passes.
    """
    event_type = _CANONICAL_EVENT_MAPPING.get("social_post", "social_post")
    result: list[dict] = []
    for post in raw_rows:
        create_time = post.get("createTime") or ""
        # M1: reject a post without a valid ISO date prefix (>=10 chars).
        if len(create_time) < 10:
            logger.debug(
                "transform_events: skipping localPost with createTime=%r", create_time
            )
            continue
        event_date = create_time[:10]
        # H1: bounded window filter (no-op when both bounds are None).
        if date_from is not None and event_date < date_from:
            continue
        if date_to is not None and event_date > date_to:
            continue
        result.append(
            {
                "event_type": event_type,
                "event_date": event_date,
                "label": post.get("summary") or "",
                "platform": "gbp",
                "source": "google-business-profile",
            }
        )
    return result


def _resolve_location_parent(connection_id: str, location_id: str) -> str:
    """Return the v4 parent path ``accounts/<a>/locations/<l>`` for *location_id*.

    The v4 localPosts edge needs the account+location pair, but the topology
    selection is a bare ``locations/<id>``. If *location_id* already carries an
    ``accounts/`` prefix it is used verbatim; otherwise the owning account is
    resolved from discover_accounts (never guessed). Raises ValueError when the
    location cannot be matched to a reachable account (honest, not a silent skip).
    """
    if "accounts/" in location_id:
        return location_id
    loc = location_id if location_id.startswith("locations/") else f"locations/{location_id}"
    for entry in discover_accounts(connection_id):
        if entry.get("id") == loc and entry.get("parent"):
            return f"{entry['parent']}/{loc}"
    raise ValueError(
        f"google-business-profile pull_social_post: could not resolve the owning "
        f"account for {loc!r} (v4 localPosts needs accounts/*/locations/*)."
    )


def pull_social_post(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    location_id: str | None = None,
) -> dict:
    """Fetch a location's local posts and persist each as a context_event.

    Legacy v4 path (reuses the existing business.manage scope -- no new scope):
    GET {v4}/{account}/{location}/localPosts, paginated via pageToken. The account
    is resolved from the selected location's topology parent.

    Idempotence (Epic 31.3/H1): two guards make a daily re-pull idempotent:
      1. transform_events() filters posts to [date_from, date_to] client-side.
      2. delete_connector_events_in_window() clears this project's prior
         google-business-profile social_post events in the SAME window BEFORE the
         re-insert. No new migration is required.

    LIVE-DEFERRED: the v4 host is allowlist-gated and GBP is 0-QPM by default, so a
    live call 403s until the host is granted (surfaced honestly via the typed
    error map). The transform + fixtures prove the mapping regardless.

    AD-3:  token fetched immediately before use, then discarded.
    AI-03: ASCII-only in log strings.
    AD-2:  canonical event mapping via transform_events().
    AD-8:  project-scoped (delete + insert both bound to project_id).
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2
    from core.context_events import (  # noqa: PLC0415
        delete_connector_events_in_window,
        persist_context_event,
    )

    if not location_id:
        raise ValueError(
            "google-business-profile pull_social_post requires a selected location_id "
            "(topology selection_level 'location'); no env-var fallback (25.5+)."
        )

    parent = _resolve_location_parent(connection_id, location_id)
    token = nango_client.get_fresh_token(connection_id, provider="google-business-profile")

    raw_rows: list[dict] = []
    page_token: str | None = None
    while True:
        params: dict = {"pageSize": 100}
        if page_token:
            params["pageToken"] = page_token
        resp = httpx.get(
            f"{GBP_V4_BASE}/{parent}/localPosts",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60.0,
        )
        _raise_for_status(resp)
        body = resp.json()
        raw_rows.extend(body.get("localPosts") or [])
        page_token = body.get("nextPageToken")
        if not page_token:
            break

    logger.info(
        "pull_social_post: fetched %d local post(s) for parent=%s", len(raw_rows), parent
    )

    canonical_events = transform_events(raw_rows, date_from=date_from, date_to=date_to)

    # H1: delete-by-source-window BEFORE re-insert -> idempotent daily re-pull.
    event_type = _CANONICAL_EVENT_MAPPING.get("social_post", "social_post")
    delete_connector_events_in_window(
        project_id=project_id,
        source="google-business-profile",
        event_type=event_type,
        date_from=date_from,
        date_to=date_to,
    )

    event_count = 0
    for ev in canonical_events:
        label = ev["label"][:120]  # validate_event_input enforces max 120.
        persist_context_event(
            project_id=project_id,
            event_date=ev["event_date"],
            type=ev["event_type"],
            label=label,
            description=label,
            created_by=f"google-business-profile_pull:{pull_id}",
            platform=ev["platform"],
            value=None,
            source=ev["source"],
        )
        event_count += 1

    logger.info(
        "pull_social_post: persisted %d context_events: pull_id=%s date_from=%s date_to=%s",
        event_count, pull_id, date_from, date_to,
    )
    return {
        "pull_id": pull_id,
        "event_count": event_count,
        "date_from": date_from,
        "date_to": date_to,
    }


# ---------------------------------------------------------------------------
# MCP tool -- reads from fact_gbp_location_daily mart (AD-12).
# ---------------------------------------------------------------------------


def _get_mart_table(db_mode: str) -> str:
    if db_mode == "duckdb":
        from core import warehouse_tenancy  # noqa: PLC0415

        return f"{warehouse_tenancy.mart_prefix(None)}fact_gbp_location_daily"
    dataset = os.environ.get("BQ_MARTS_DATASET", "marts")
    gcp_project = os.environ.get("GCP_PROJECT", "")
    prefix = f"{gcp_project}.{dataset}" if gcp_project else dataset
    return f"{prefix}.fact_gbp_location_daily"


def _query_mart(date_from: str, date_to: str, project_id: str) -> list[dict]:
    db_mode = _get_db_mode()
    if db_mode != "duckdb":
        raise ValueError(f"gbp mart query: unsupported db_mode {db_mode!r} at P-dev")
    import duckdb  # noqa: PLC0415

    table = _get_mart_table(db_mode)
    sql = (
        f"SELECT * FROM {table} WHERE project_id = ? "
        "AND date BETWEEN ? AND ? ORDER BY date, location_id"
    )
    con = duckdb.connect(_get_duckdb_path(), read_only=True)
    try:
        rel = con.execute(sql, [project_id, date_from, date_to])
        cols = [d[0] for d in rel.description]
        return [dict(zip(cols, row)) for row in rel.fetchall()]
    finally:
        con.close()


@mcp_app.tool()
def get_google_business_profile_report(
    project_id: str = "default",  # AD-14: identity resolved from OAuth 2.1 + PKCE
    report_profile: str = "location_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Google Business Profile location performance -- reads the daily mart.

    Report profile: location_daily. Metrics: 4 impression surfaces (desktop/mobile
    x maps/search), conversations, direction requests, call/website clicks,
    bookings, food orders, food menu clicks -- all ADDITIVE daily counts.

    Note: history is hard-capped at ~18 months and recent days lag ~3-7 days.

    Returns the canonical AD-1 envelope via structuredContent.

    Parameters:
        project_id: Project identifier (default: 'default', AD-14 placeholder).
        report_profile: 'location_daily'.
        date_from: Start date ISO-8601. Defaults to 90 days ago.
        date_to: End date ISO-8601. Defaults to yesterday.
    """
    from datetime import date, timedelta  # noqa: PLC0415

    if not date_to:
        date_to = (date.today() - timedelta(days=1)).isoformat()
    if not date_from:
        date_from = (date.today() - timedelta(days=90)).isoformat()

    try:
        rows = _query_mart(date_from, date_to, project_id)
    except Exception as exc:
        return {
            "schema_version": "1",
            "meta": {"freshness": None, "provenance": None,
                     "alerts": [{"level": "error", "message": str(exc)}]},
            "data": {"project_id": project_id, "report_profile": report_profile,
                     "date_from": date_from, "date_to": date_to, "rows": []},
        }

    return {
        "schema_version": "1",
        "meta": {"freshness": None, "provenance": {"source_system": "google-business-profile",
                 "source_field": "fact_gbp_location_daily", "pull_id": None}, "alerts": []},
        "data": {"project_id": project_id, "report_profile": report_profile,
                 "date_from": date_from, "date_to": date_to, "rows": rows},
    }


# ---------------------------------------------------------------------------
# Register this module's raw table name with core.verification.
# ---------------------------------------------------------------------------
try:
    from core.verification import register_raw_table_name as _register_raw  # noqa: PLC0415

    _register_raw("raw_gbp_location_daily", provider="google-business-profile")
except Exception:
    pass  # best-effort
