"""YouTube Analytics connector -- reports.query (channel/video daily metrics).

Exposes a ``mcp_app: FastMCP`` instance the core loader mounts under the
``youtube-analytics`` namespace (AD-2). Built to the epic-25 industrial standard:
generated api_catalog.json (metrics/dimensions transcription), status-keyed
error_map (Google standard envelope; a 403 quotaExceeded is routed to the
breaker), single-channel topology discovered via the Data API.

Central design fact: YouTube is HISTORY-RICH -- full lifetime data at daily grain
(complete from 2013-01-01), the opposite of Strava/GBP. A full backfill is
available at connect; only a trailing window needs re-pulling for late-settling
data. Ratio metrics (averageViewDuration/Percentage, cpm) are non-additive:
averageViewDuration is DERIVABLE (estimatedMinutesWatched*60/views) and computed
at the semantic layer (AD-4), never stored; the additive core is what this
connector extracts. Revenue/ad metrics require the monetary scope (planned).

# AD-12: MCP server reads the fact_youtube_daily mart only -- no raw_* tables.
# AD-3:  OAuth token via the DIRECT Google path (get_fresh_token(..., provider=
#        'youtube-analytics')) immediately before use -- never stored/logged.
#        Scopes yt-analytics.readonly (+ youtube.readonly for discovery). NOT Nango.
# AD-7:  pull_id minted by the core scheduler and passed into pull().
# AD-2:  metric/dimension renames driven by the manifest mappings.
# AD-4:  additive metrics only are stored; ratios computed at the semantic layer.
# AI-03: ASCII-only stdout/log strings.
#
# API facts (VERIFIED 2026-07-21 against developers.google.com/youtube):
#   - GET https://youtubeanalytics.googleapis.com/v2/reports?ids=channel==MINE&
#     startDate=&endDate=&dimensions=day&metrics=views,... -> {columnHeaders[]:
#     {name,columnType,dataType}, rows[[...]]}. Synchronous. Map by columnHeaders.
#   - GET https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true ->
#     items[].{id, snippet.title}. Reporting-entity discovery (1 quota unit).
#   - Error body = standard Google { error:{code,message,errors[]:{reason,domain}} };
#     error_map keyed on HTTP status; 403 quotaExceeded -> RateLimitError (breaker).
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
mcp_app = FastMCP("youtube-analytics")

YT_ANALYTICS_BASE = "https://youtubeanalytics.googleapis.com/v2"
YT_DATA_BASE = "https://www.googleapis.com/youtube/v3"

# ---------------------------------------------------------------------------
# Database connection helpers -- env-var driven (no hardcoded paths).
# ---------------------------------------------------------------------------

_DEFAULT_DUCKDB_PATH = os.path.join(os.path.dirname(__file__), "seeds", "local.duckdb")


def _get_db_mode() -> str:
    return os.environ.get("TOOROW_DB_MODE", "duckdb")


def _get_duckdb_path() -> str:
    return os.environ.get("TOOROW_DUCKDB_PATH", _DEFAULT_DUCKDB_PATH)


# ---------------------------------------------------------------------------
# Manifest access (cached): error_map + canonical mappings + profile fields.
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


def _source_for_canonical() -> dict[str, str]:
    """canonical_field_id -> provider source token, from the manifest capabilities."""
    caps = _load_manifest().get("source_capabilities", {})
    return {f["field_id"]: f.get("source_field", f["field_id"]) for f in caps.get("fields", [])}


def _profile_request_fields(profile_id: str) -> tuple[list[str], list[str]]:
    """Return (metric_source_tokens, dimension_source_tokens) for a report profile.

    AD-2: derived from the manifest capability report, never hardcoded. channel_id
    is stamped (not requested from the API), so it is dropped from the request
    dimension list.
    """
    manifest = _load_manifest()
    src = _source_for_canonical()
    report = next(
        (r for r in manifest.get("source_capabilities", {}).get("reports", [])
         if r.get("id") == profile_id),
        None,
    )
    if report is None:
        raise ValueError(f"Unknown youtube-analytics report profile: {profile_id!r}")
    metrics = [src.get(m, m) for m in report.get("metrics", [])]
    dims = [src.get(d, d) for d in report.get("dimensions", []) if d != "channel_id"]
    return metrics, dims


# ---------------------------------------------------------------------------
# transform() -- manifest-driven canonical field mapping (AD-2)
# ---------------------------------------------------------------------------


def transform(raw_rows: list[dict]) -> list[dict]:
    """Map raw reports.query column names to canonical names via the manifest.

    AD-2: renames driven by canonical_metric_mapping (estimatedMinutesWatched ->
    estimated_minutes_watched, ...) + canonical_dimension_mapping (day -> date).
    Fields absent from both pass through (pull_id, connector, channel_id, video).
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
# Raw landing (long format: metric/value + optional video breakdown).
# ---------------------------------------------------------------------------

_RAW_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_youtube_daily (
    date         VARCHAR,
    channel_id   VARCHAR,
    video        VARCHAR,
    metric       VARCHAR,
    value        DOUBLE,
    pull_id      VARCHAR,
    loaded_at    VARCHAR,
    project_id   VARCHAR
)
"""

_RAW_INSERT_SQL = """
INSERT INTO raw_youtube_daily
    (date, channel_id, video, metric, value, pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

# Canonical metric ids (the additive set this connector stores).
_METRIC_IDS = [
    "views",
    "estimated_minutes_watched",
    "likes",
    "comments",
    "shares",
    "subscribers_gained",
    "subscribers_lost",
]


def _to_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _insert_raw_rows(rows: list[dict], pull_id: str, project_id: str) -> int:
    """Unpivot canonical wide rows into long raw_youtube_daily rows (metric/value)."""
    db_mode = _get_db_mode()
    if db_mode != "duckdb":
        raise ValueError(f"_insert_raw_rows: unsupported db_mode {db_mode!r} at P-dev")
    import duckdb  # noqa: PLC0415

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    con = duckdb.connect(_get_duckdb_path())
    con.execute(_RAW_CREATE_DDL)
    values = []
    for r in rows:
        for metric in _METRIC_IDS:
            if metric not in r:
                continue
            values.append(
                (
                    r.get("date", ""),
                    r.get("channel_id", ""),
                    r.get("video", ""),
                    metric,
                    _to_float(r.get(metric)),
                    pull_id,
                    loaded_at,
                    project_id,
                )
            )
    if values:
        con.executemany(_RAW_INSERT_SQL, values)
    con.close()
    return len(values)


# ---------------------------------------------------------------------------
# HTTP error handling -- 403 quotaExceeded routed to the breaker (google shim).
# ---------------------------------------------------------------------------

_QUOTA_REASONS = {
    "quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded",
}


def _error_reasons(body) -> set[str]:
    if isinstance(body, dict):
        err = body.get("error") or {}
        return {e.get("reason") for e in err.get("errors") or [] if e.get("reason")}
    return set()


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
    except Exception:
        body = resp.text

    rate_limited = resp.status_code == 429 or (
        resp.status_code == 403 and (_error_reasons(body) & _QUOTA_REASONS)
    )
    if rate_limited:
        from core.quota import RateLimitError  # noqa: PLC0415

        retry_after_raw = resp.headers.get("Retry-After", "0")
        try:
            retry_after = int(retry_after_raw) or None
        except (ValueError, TypeError):
            retry_after = None
        raise RateLimitError("youtube-analytics", retry_after)

    from core.pull_errors import classify_http_error  # noqa: PLC0415

    raise classify_http_error(resp.status_code, body, _load_error_map())


# ---------------------------------------------------------------------------
# pull() -- reports.query (called by the queue worker only, AD-12)
# ---------------------------------------------------------------------------


def _pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    profile: str,
    channel_id: str | None,
) -> dict:
    """Fetch reports.query for a channel and land long-format daily rows.

    # AD-3: token via the direct Google path, used immediately, then discarded.
    Rows are mapped by columnHeaders order (never positional guessing).
    channel_id is stamped on every row (the reporting entity).
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    token = nango_client.get_fresh_token(connection_id, provider="youtube-analytics")
    metrics, dims = _profile_request_fields(profile)

    ids = f"channel=={channel_id}" if channel_id else "channel==MINE"
    params = {
        "ids": ids,
        "startDate": date_from,
        "endDate": date_to,
        "metrics": ",".join(metrics),
        "dimensions": ",".join(dims),
    }
    resp = httpx.get(
        f"{YT_ANALYTICS_BASE}/reports",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60.0,
    )
    _raise_for_status(resp)

    payload = resp.json()
    headers = [h.get("name") for h in payload.get("columnHeaders") or []]
    stamped_channel = channel_id or "MINE"

    raw_rows: list[dict] = []
    for row in payload.get("rows") or []:
        record = dict(zip(headers, row))
        record["channel_id"] = stamped_channel
        raw_rows.append(record)

    canonical_rows = transform(raw_rows)
    row_count = _insert_raw_rows(canonical_rows, pull_id, project_id)

    logger.info(
        "youtube_pull_completed: pull_id=%s profile=%s row_count=%d",
        pull_id, profile, row_count,
    )
    return {"pull_id": pull_id, "row_count": row_count, "date_from": date_from, "date_to": date_to}


def pull_channel_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str, pull_id: str,
    channel_id: str | None = None,
) -> dict:
    """AI-58 dispatch: channel-level daily metrics (grain date x channel_id)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id, "channel_daily", channel_id
    )


def pull_video_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str, pull_id: str,
    channel_id: str | None = None,
) -> dict:
    """AI-58 dispatch: per-video daily metrics (grain date x video x channel_id)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id, "video_daily", channel_id
    )


def pull(
    connection_id: str, date_from: str, date_to: str, project_id: str, pull_id: str,
    channel_id: str | None = None,
) -> dict:
    """Default pull() = channel_daily (AI-58 profile-less default dispatch)."""
    return pull_channel_daily(connection_id, date_from, date_to, project_id, pull_id, channel_id)


# ---------------------------------------------------------------------------
# transform_events() -- canonical event mapping for the video_upload profile.
# ---------------------------------------------------------------------------

# Canonical event mapping mirrors canonical_event_mapping from the manifest.
# Pure function (no I/O) -- unit-testable via golden_events/expected_events.
_CANONICAL_EVENT_MAPPING = {"video_upload": "video_upload"}


def transform_events(
    raw_rows: list[dict],
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict]:
    """Map raw playlistItems dicts to canonical event dicts (AD-2, pure function).

    Input: a list of playlistItems resource dicts (each with a 'snippet' key).
    Output: canonical event dicts with keys event_type, event_date, label,
    platform, source -- ready for persist_context_event() (value = None, pulse).

    event_date = snippet.publishedAt truncated to YYYY-MM-DD (UTC date).
    label      = snippet.title.
    event_type = "video_upload" (via _CANONICAL_EVENT_MAPPING["video_upload"]).
    platform   = "youtube".
    source     = "youtube-analytics".

    Date window (Epic 31.3, H1): the uploads playlist is UNBOUNDED (it returns
    every video the channel ever published), so an unfiltered transform would
    emit the whole channel history on every pull. When ``date_from``/``date_to``
    are supplied, items whose publishedAt date falls OUTSIDE [date_from, date_to]
    are dropped -- symmetric to the github connector's ``_in_date_range`` guard.
    Both None (the golden-replay contract) means "no window" -> every item passes,
    so golden_events.json -> expected_events.json stays a pure 1:1 mapping.

    Validity (M1): an item with no usable snippet.publishedAt (missing, or shorter
    than a 10-char ISO date prefix) is SKIPPED rather than persisted with an empty
    event_date -- validate_event_input would reject it downstream anyway, and an
    empty date is not a real marker.
    """
    event_type = _CANONICAL_EVENT_MAPPING.get("video_upload", "video_upload")
    result: list[dict] = []
    for item in raw_rows:
        snippet = item.get("snippet") or {}
        published_at = snippet.get("publishedAt") or ""
        # M1: reject an item without a valid ISO date prefix (>=10 chars).
        if len(published_at) < 10:
            logger.debug(
                "transform_events: skipping item with invalid publishedAt=%r", published_at
            )
            continue
        # Take the YYYY-MM-DD prefix from the ISO-8601 datetime (UTC).
        event_date = published_at[:10]
        # H1: bounded window filter (no-op when both bounds are None).
        if date_from is not None and event_date < date_from:
            continue
        if date_to is not None and event_date > date_to:
            continue
        title = snippet.get("title") or ""
        result.append(
            {
                "event_type": event_type,
                "event_date": event_date,
                "label": title,
                "platform": "youtube",
                "source": "youtube-analytics",
            }
        )
    return result


# ---------------------------------------------------------------------------
# pull_video_upload() -- Data API v3 uploads playlist -> context_events.
# ---------------------------------------------------------------------------


def pull_video_upload(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    channel_id: str | None = None,
) -> dict:
    """Fetch the channel uploads playlist and persist each upload as a context_event.

    Data API v3 path (youtube.readonly scope already present -- no new scope):
      1. channels.list(part=contentDetails, mine=true) ->
             items[0].contentDetails.relatedPlaylists.uploads  (playlist id)
      2. playlistItems.list(part=snippet, playlistId=<uploads>, maxResults=50)
             paginated via nextPageToken.
      Each item -> snippet.publishedAt (event_date), snippet.title (label),
      snippet.resourceId.videoId (description / video id for traceability).

    Idempotence (Epic 31.3, H1): the uploads playlist has NO server-side date
    filter, so it returns every video the channel ever published. Two guards make
    a daily re-pull idempotent (no double-counted markers / doubled MMM regressor):
      1. transform_events() filters items to [date_from, date_to] client-side.
      2. delete_connector_events_in_window() clears this project's prior
         youtube-analytics video_upload events in the SAME window BEFORE the
         re-insert (delete-by-source-window). A re-pull of an unchanged window
         is a no-op net of rows; a changed window re-lands the current truth.
    No new migration is required (context_events.type/source already exist).

    AD-3:  token fetched immediately before use via the direct Google path.
    AI-03: ASCII-only in log strings.
    AD-2:  canonical event mapping via transform_events().
    AD-8:  project-scoped (delete + insert both bound to project_id).
    """
    from core import nango_client  # noqa: PLC0415
    from core.context_events import (  # noqa: PLC0415
        delete_connector_events_in_window,
        persist_context_event,
    )

    token = nango_client.get_fresh_token(connection_id, provider="youtube-analytics")

    # Step 1: resolve the uploads playlist id for the connected channel.
    channels_resp = httpx.get(
        f"{YT_DATA_BASE}/channels",
        params={"part": "contentDetails", "mine": "true"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    _raise_for_status(channels_resp)
    items = channels_resp.json().get("items") or []
    if not items:
        logger.warning(
            "pull_video_upload: no channel found for connection_id=%s", connection_id
        )
        return {"pull_id": pull_id, "event_count": 0, "date_from": date_from, "date_to": date_to}

    uploads_playlist_id = (
        (items[0].get("contentDetails") or {})
        .get("relatedPlaylists", {})
        .get("uploads", "")
    )
    if not uploads_playlist_id:
        logger.warning(
            "pull_video_upload: uploads playlist not found for connection_id=%s", connection_id
        )
        return {"pull_id": pull_id, "event_count": 0, "date_from": date_from, "date_to": date_to}

    # Step 2: paginate playlistItems to collect all uploaded videos.
    raw_rows: list[dict] = []
    next_page_token: str | None = None
    page_num = 0
    while True:
        params: dict = {
            "part": "snippet",
            "playlistId": uploads_playlist_id,
            "maxResults": 50,
        }
        if next_page_token:
            params["pageToken"] = next_page_token

        playlist_resp = httpx.get(
            f"{YT_DATA_BASE}/playlistItems",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
        _raise_for_status(playlist_resp)
        payload = playlist_resp.json()
        raw_rows.extend(payload.get("items") or [])
        page_num += 1
        next_page_token = payload.get("nextPageToken")
        if not next_page_token:
            break

    logger.info(
        "pull_video_upload: fetched %d items in %d page(s) for connection_id=%s",
        len(raw_rows), page_num, connection_id,
    )

    # Step 3: map to canonical events (windowed), then idempotently persist.
    # A per-item description (the watch URL) is derived from the RAW item, keyed
    # by (event_date, title) so the pure windowed transform stays 1:1 with the
    # golden fixture while the pull still carries traceability metadata.
    description_by_key: dict[tuple[str, str], str] = {}
    for item in raw_rows:
        snippet = item.get("snippet") or {}
        published_at = snippet.get("publishedAt") or ""
        if len(published_at) < 10:
            continue
        key = (published_at[:10], snippet.get("title") or "")
        video_id = (snippet.get("resourceId") or {}).get("videoId") or ""
        if video_id and key not in description_by_key:
            description_by_key[key] = f"https://www.youtube.com/watch?v={video_id}"

    canonical_events = transform_events(raw_rows, date_from=date_from, date_to=date_to)

    # H1: delete-by-source-window BEFORE re-insert -> idempotent daily re-pull.
    event_type = _CANONICAL_EVENT_MAPPING.get("video_upload", "video_upload")
    delete_connector_events_in_window(
        project_id=project_id,
        source="youtube-analytics",
        event_type=event_type,
        date_from=date_from,
        date_to=date_to,
    )

    event_count = 0
    for ev in canonical_events:
        key = (ev["event_date"], ev["label"])
        description = description_by_key.get(key, ev.get("label", ""))
        # Truncate label to 120 chars (validate_event_input enforces max 120).
        label = ev["label"][:120]
        persist_context_event(
            project_id=project_id,
            event_date=ev["event_date"],
            type=ev["event_type"],
            label=label,
            description=description,
            created_by=pull_id,
            platform=ev["platform"],
            value=None,
            source=ev["source"],
        )
        event_count += 1

    logger.info(
        "pull_video_upload: persisted %d context_events: pull_id=%s date_from=%s date_to=%s",
        event_count, pull_id, date_from, date_to,
    )
    return {
        "pull_id": pull_id,
        "event_count": event_count,
        "date_from": date_from,
        "date_to": date_to,
    }


# ---------------------------------------------------------------------------
# Account topology discovery (playbook section 5) -- the connected channel.
# ---------------------------------------------------------------------------


def discover_accounts(connection_id: str) -> list[dict]:
    """Resolve the connected YouTube CHANNEL via Data API channels.list(mine=true).

    Returns the generic hierarchy core's topology flow consumes:

        [{"id": "<channel_id>", "label": "<channel title>"}]

    A standard token maps to one channel; content-owner (MCN) mode covering many
    channels is a later extension. Raises RateLimitError on 429/quota, a typed
    ConnectorError otherwise.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2

    token = nango_client.get_fresh_token(connection_id, provider="youtube-analytics")
    resp = httpx.get(
        f"{YT_DATA_BASE}/channels",
        params={"part": "snippet", "mine": "true"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    _raise_for_status(resp)

    accounts: list[dict] = []
    for item in resp.json().get("items") or []:
        cid = item.get("id")
        if not cid:
            continue
        title = (item.get("snippet") or {}).get("title") or cid
        accounts.append({"id": cid, "label": title})
    return accounts


# ---------------------------------------------------------------------------
# MCP tool -- reads from fact_youtube_daily mart (AD-12).
# ---------------------------------------------------------------------------


def _get_mart_table(db_mode: str) -> str:
    if db_mode == "duckdb":
        from core import warehouse_tenancy  # noqa: PLC0415

        return f"{warehouse_tenancy.mart_prefix(None)}fact_youtube_daily"
    dataset = os.environ.get("BQ_MARTS_DATASET", "marts")
    gcp_project = os.environ.get("GCP_PROJECT", "")
    prefix = f"{gcp_project}.{dataset}" if gcp_project else dataset
    return f"{prefix}.fact_youtube_daily"


def _query_mart(date_from: str, date_to: str, project_id: str) -> list[dict]:
    db_mode = _get_db_mode()
    if db_mode != "duckdb":
        raise ValueError(f"youtube mart query: unsupported db_mode {db_mode!r} at P-dev")
    import duckdb  # noqa: PLC0415

    table = _get_mart_table(db_mode)
    sql = (
        f"SELECT date, channel_id, video, metric, SUM(value) AS value "
        f"FROM {table} WHERE project_id = ? AND date BETWEEN ? AND ? "
        "GROUP BY date, channel_id, video, metric ORDER BY date, metric"
    )
    con = duckdb.connect(_get_duckdb_path(), read_only=True)
    try:
        rel = con.execute(sql, [project_id, date_from, date_to])
        cols = [d[0] for d in rel.description]
        return [dict(zip(cols, row)) for row in rel.fetchall()]
    finally:
        con.close()


@mcp_app.tool()
def get_youtube_analytics_report(
    project_id: str = "default",  # AD-14: identity resolved from OAuth 2.1 + PKCE
    report_profile: str = "channel_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """YouTube channel/video daily performance -- reads the fact_youtube_daily mart.

    Report profiles: channel_daily, video_daily. Metrics: views,
    estimated_minutes_watched, likes, comments, shares, subscribers_gained,
    subscribers_lost -- all ADDITIVE. Ratio metrics (average view duration/%) are
    computed at the semantic layer from these, never stored. Full lifetime history.

    Returns the canonical AD-1 envelope via structuredContent.

    Parameters:
        project_id: Project identifier (default: 'default', AD-14 placeholder).
        report_profile: 'channel_daily' or 'video_daily'.
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
        "meta": {"freshness": None, "provenance": {"source_system": "youtube-analytics",
                 "source_field": "fact_youtube_daily", "pull_id": None}, "alerts": []},
        "data": {"project_id": project_id, "report_profile": report_profile,
                 "date_from": date_from, "date_to": date_to, "rows": rows},
    }


# ---------------------------------------------------------------------------
# Register this module's raw table name with core.verification.
# ---------------------------------------------------------------------------
try:
    from core.verification import register_raw_table_name as _register_raw  # noqa: PLC0415

    _register_raw("raw_youtube_daily", provider="youtube-analytics")
except Exception:
    pass  # best-effort
