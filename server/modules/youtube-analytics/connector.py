"""YouTube Analytics connector -- reports.query (channel/video daily metrics).

Exposes a ``mcp_app: FastMCP`` instance as the conformance surface (AD-1
envelope); since AD-42 the core no longer mounts it — execution uses the
Datastream-parameterized core tools. Built to the epic-25 industrial standard:
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
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Module-level FastMCP instance, kept as the conformance surface (AD-1 envelope,
# validated by server/tests/conformance/test_envelope.py). Since AD-42 the core
# no longer mounts it: execution uses the Datastream-parameterized core tools.
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


def _profile_date_axis(profile_id: str) -> str:
    """How this report carries the date: as a dimension, or as the request window.

    WHICH BREAKDOWNS ACCEPT A DAY IS THE API'S ANSWER, NOT A RULE. A blanket rule
    -- "a breakdown never takes `day`" -- was applied to all of them and refused by
    the endpoint on four of the seven (500, "An internal error has occurred"), so
    four reports could not collect at all. Each report declares what the endpoint
    answered, measured one request at a time, and this reads that declaration.
    """
    manifest = _load_manifest()
    report = next(
        (r for r in manifest.get("source_capabilities", {}).get("reports", [])
         if r.get("id") == profile_id),
        None,
    )
    return str((report or {}).get("date_axis") or "request_dimension")


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

# Canonical metric ids this connector stores. Two families:
#   * the additive FLOWS of reports.query (views .. subscribers_lost);
#   * the non-additive STOCKS of channels.list(statistics) -- declared
#     aggregation=latest in the manifest, one row per reading day.
# The stocks were MISSING from this list until 2026-09-01: `transform` renamed
# subscriberCount -> subscriber_count and this loop then skipped every one of
# them, so `pull_channel_snapshot` landed ZERO rows while reporting the call a
# success. The class is AI-310's: the profile's own pull function must land
# what the profile declares.
_METRIC_IDS = [
    "views",
    "estimated_minutes_watched",
    "likes",
    "comments",
    "shares",
    "subscribers_gained",
    "subscribers_lost",
    "subscriber_count",
    "lifetime_view_count",
    "video_count",
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
    if db_mode not in ("duckdb", "bigquery"):
        raise ValueError(f"_insert_raw_rows: unsupported db_mode {db_mode!r}")
    from core import warehouse_write  # noqa: PLC0415

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    con = warehouse_write.open_raw_writer(_get_duckdb_path(), project_id=project_id)
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
    if db_mode == "duckdb":
        if values:
            con.executemany(_RAW_INSERT_SQL, values)
        con.close()
        return len(values)
    elif db_mode == "bigquery":
        from core.raw_landing import land_raw_rows  # noqa: PLC0415

        raw_rows = [
            {
                "date": v[0],
                "channel_id": v[1],
                "video": v[2],
                "metric": v[3],
                "value": v[4],
                "pull_id": v[5],
                "loaded_at": v[6],
                "project_id": v[7],
            }
            for v in values
        ]
        columns = [
            ("date", "STRING"),
            ("channel_id", "STRING"),
            ("video", "STRING"),
            ("metric", "STRING"),
            ("value", "FLOAT"),
            ("pull_id", "STRING"),
            ("loaded_at", "STRING"),
            ("project_id", "STRING"),
        ]
        land_raw_rows(
            "raw_youtube_daily",
            raw_rows,
            columns=columns,
            project_id=project_id,
            backend="bigquery",
        )
        return len(values)
    else:
        raise ValueError(f"_insert_raw_rows: unsupported db_mode {db_mode!r}")


# ---------------------------------------------------------------------------
# Breakdown landing: one long relation for every non-daily combination.
# ---------------------------------------------------------------------------

_BREAKDOWN_TABLE = "raw_youtube_breakdown"

# THE RAW TABLE, DECLARED ONCE. `core.raw_landing` renders the DuckDB DDL and
# INSERT from this list, and the BigQuery landing is handed the same one -- the
# BigQuery branch used to spell its own copy out at the call site, which is how a
# second, drifting definition is born.
_BREAKDOWN_COLUMNS: list[tuple[str, str]] = [
    ("date", "STRING"),
    ("channel_id", "STRING"),
    ("breakdown_dimension", "STRING"),
    ("breakdown_value", "STRING"),
    ("metric", "STRING"),
    ("value", "FLOAT"),
    ("pull_id", "STRING"),
    ("loaded_at", "STRING"),
    ("project_id", "STRING"),
]


def _insert_breakdown_rows(
    rows: list[dict], pull_id: str, project_id: str, profile: str
) -> int:
    """Land a breakdown in the canonical long shape the platform already reads.

    A demographic split has `age_group` and `gender`; a geographic one has
    `country`. Widening `raw_youtube_daily` per combination would add a column
    per dimension anyone ever asks for. The platform's own answer is the
    dimension/value pair, so one relation serves every combination and the
    mapping names which pair it carries.
    """
    manifest = _load_manifest()
    report = next(
        (r for r in manifest["source_capabilities"]["reports"] if r.get("id") == profile), {}
    )
    dimensions = [d for d in (report.get("dimensions") or []) if d not in ("channel_id", "date")]
    metrics = list(report.get("metrics") or [])

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    values = []
    for row in rows:
        for dimension in dimensions:
            for metric in metrics:
                if metric not in row:
                    continue
                values.append((
                    row.get("date", ""), row.get("channel_id", ""),
                    dimension, str(row.get(dimension, "")),
                    metric, _to_float(row.get(metric)),
                    pull_id, loaded_at, project_id,
                ))
    if not values:
        return 0

    from core import raw_landing  # noqa: PLC0415 -- AD-2

    if _get_db_mode() == "duckdb":
        from core import warehouse_write  # noqa: PLC0415

        con = warehouse_write.open_raw_writer(_get_duckdb_path(), project_id=project_id)
        try:
            con.execute(raw_landing.duckdb_ddl(_BREAKDOWN_TABLE, _BREAKDOWN_COLUMNS))
            con.executemany(
                raw_landing.duckdb_insert(_BREAKDOWN_TABLE, _BREAKDOWN_COLUMNS), values
            )
        finally:
            con.close()
        return len(values)

    raw_landing.land_raw_rows(
        _BREAKDOWN_TABLE,
        [raw_landing.row_from_values(_BREAKDOWN_COLUMNS, v) for v in values],
        columns=_BREAKDOWN_COLUMNS,
        project_id=project_id,
        backend="bigquery",
    )
    return len(values)


# ---------------------------------------------------------------------------
# Video directory landing (Story: Competitors on YouTube, 2026-09-01).
#
# The uploads of a channel -- own or tracked competitor -- as a dated snapshot:
# one row per (reading day, channel, video) carrying the PUBLIC facts the Data
# API serves without any channel permission: title, publication date, duration,
# lifetime view count. This is the relation that names videos (the analytics
# relations only ever carry ids) and the honest ground for recent-window
# comparisons: views-per-day-since-publication needs `published_at`, which no
# analytics report returns.
# ---------------------------------------------------------------------------

_DIRECTORY_TABLE = "raw_youtube_video_directory"

_DIRECTORY_COLUMNS: list[tuple[str, str]] = [
    ("date", "STRING"),
    ("channel_id", "STRING"),
    ("channel_title", "STRING"),
    ("video", "STRING"),
    ("video_title", "STRING"),
    ("published_at", "STRING"),
    ("duration_seconds", "FLOAT"),
    ("lifetime_views", "FLOAT"),
    ("is_own_channel", "BOOLEAN"),
    ("pull_id", "STRING"),
    ("loaded_at", "STRING"),
    ("project_id", "STRING"),
]


def _insert_directory_rows(rows: list[dict], pull_id: str, project_id: str) -> int:
    """Land directory rows in both backends from the ONE column list above."""
    if not rows:
        return 0
    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    values = [
        (
            r.get("date", ""), r.get("channel_id", ""), r.get("channel_title", ""),
            r.get("video", ""), r.get("video_title", ""), r.get("published_at", ""),
            _to_float(r.get("duration_seconds")), _to_float(r.get("lifetime_views")),
            bool(r.get("is_own_channel")), pull_id, loaded_at, project_id,
        )
        for r in rows
    ]
    from core import raw_landing  # noqa: PLC0415 -- AD-2

    if _get_db_mode() == "duckdb":
        from core import warehouse_write  # noqa: PLC0415

        con = warehouse_write.open_raw_writer(_get_duckdb_path(), project_id=project_id)
        try:
            con.execute(raw_landing.duckdb_ddl(_DIRECTORY_TABLE, _DIRECTORY_COLUMNS))
            con.executemany(
                raw_landing.duckdb_insert(_DIRECTORY_TABLE, _DIRECTORY_COLUMNS), values
            )
        finally:
            con.close()
        return len(values)

    raw_landing.land_raw_rows(
        _DIRECTORY_TABLE,
        [raw_landing.row_from_values(_DIRECTORY_COLUMNS, v) for v in values],
        columns=_DIRECTORY_COLUMNS,
        project_id=project_id,
        backend="bigquery",
    )
    return len(values)


def _iso8601_duration_seconds(value: str | None) -> float | None:
    """PT#H#M#S -> seconds; None on anything unparseable (AD-9: never a fake 0)."""
    if not value:
        return None
    match = re.fullmatch(
        r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value
    )
    if not match or not any(match.groups()):
        return None
    days, hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return float(days * 86400 + hours * 3600 + minutes * 60 + seconds)


# ---------------------------------------------------------------------------
# HTTP error handling -- 403 quotaExceeded routed to the breaker (google shim).
# ---------------------------------------------------------------------------

#: Les combinaisons qui atterrissent dans la relation de repartition.
_BREAKDOWN_PROFILES = {
    "audience_demographics", "audience_geography", "audience_device",
    "traffic_sources", "playback_locations", "audience_subscription",
}

_QUOTA_REASONS = {
    "quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded",
}


def _error_reasons(body) -> set[str]:
    if isinstance(body, dict):
        err = body.get("error") or {}
        return {e.get("reason") for e in err.get("errors") or [] if e.get("reason")}
    return set()


#: AI-114 : jugements portes par le STATUT SEUL -- ils ne peuvent pas vivre dans
#: `error_map` (grammaire `<status>:<provider_code>`), donc ils vivent ici.
_STATUS_OVERRIDES: dict[int, str] = {404: "permission_denied"}


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
    except Exception:
        body = resp.text

    # WHAT THE PROVIDER ACTUALLY SAID. `classify_http_error` renders a typed
    # class ("invalid_request: provider_status=400") which tells an operator
    # nothing about WHICH parameter Google refused, and the body is the only
    # place that says. It carries no credential -- the token travels in the
    # request header, never in the response.
    message = body
    if isinstance(body, dict):
        message = (body.get("error") or {}).get("message") or body
    logger.warning("youtube_api_refused: status=%s message=%s", resp.status_code, message)

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

    from core import pull_errors  # noqa: PLC0415

    # AI-114 : un jugement porte par le STATUT SEUL n'est pas un raffinement de
    # fournisseur et n'a aucune forme exprimable dans `error_map`, dont la
    # grammaire est `<status>:<provider_code>` avec un lecteur unique
    # (server/modules/README.md, etape 4). La cle nue "404" du manifeste n'etait
    # donc JAMAIS consultee : un 404 sortait `unclassified, retryable=True` et le
    # worker rejouait en boucle une requete deja refusee. Le jugement vit ici,
    # explicite, la ou il s'applique reellement.
    override = pull_errors.error_for_class(
        _STATUS_OVERRIDES.get(resp.status_code), resp.status_code, body
    )
    if override is not None:
        raise override

    raise pull_errors.classify_http_error(resp.status_code, body, _load_error_map())


# ---------------------------------------------------------------------------
# pull() -- reports.query (called by the queue worker only, AD-12)
# ---------------------------------------------------------------------------


def _windows(date_from: str, date_to: str, per_day: bool):
    """The (startDate, endDate, stamped_day) tuples one pull has to ask for.

    One tuple for a time-based report -- the whole range in a single request.
    One tuple PER DAY for an entity-level report that declares a daily grain,
    because the API serves a top-N over a range and has no `day` breakdown for
    it; the day is stamped from the window rather than read from the answer.
    """
    if not per_day:
        return [(date_from, date_to, None)]
    from datetime import date, timedelta  # noqa: PLC0415

    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    if end < start:
        return []
    days = []
    cursor = start
    while cursor <= end:
        stamped = cursor.isoformat()
        days.append((stamped, stamped, stamped))
        cursor += timedelta(days=1)
    return days


def _pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    profile: str,
    channel_id: str | None,
    dry_run: bool = False,
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
    stamped_channel = channel_id or "MINE"

    # A BREAKDOWN WITHOUT A DATE CANNOT BE ANALYSED: two days are indistinguishable
    # and a daily run overwrites the previous one instead of extending it. Some
    # breakdowns take `day` in the request and some are refused it, and only the
    # endpoint knows which -- so the report declares what it answered and this
    # honours it. Where `day` is refused, the daily grain is still reachable and
    # only one way: one request PER DAY, the day stamped from the window, at one
    # quota unit per day rather than one per pull.
    per_day = "day" in dims and _profile_date_axis(profile) == "per_day_request"
    request_dims = [d for d in dims if d != "day"] if per_day else dims

    raw_rows: list[dict] = []
    for window_from, window_to, stamped_day in _windows(date_from, date_to, per_day):
        params = {
            "ids": ids,
            "startDate": window_from,
            "endDate": window_to,
            "metrics": ",".join(metrics),
            "dimensions": ",".join(request_dims),
        }
        # An entity-level report is always a TOP-N list and the API will not
        # guess which N: with `video`, `sort` and `maxResults` are required.
        if "video" in request_dims:
            params["sort"] = "-views"
            params["maxResults"] = 200
        resp = httpx.get(
            f"{YT_ANALYTICS_BASE}/reports",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60.0,
        )
        _raise_for_status(resp)

        payload = resp.json()
        headers = [h.get("name") for h in payload.get("columnHeaders") or []]
        for row in payload.get("rows") or []:
            record = dict(zip(headers, row))
            record["channel_id"] = stamped_channel
            if stamped_day is not None:
                record["day"] = stamped_day
            raw_rows.append(record)

    canonical_rows = transform(raw_rows)

    # dry_run=True fetches and transforms exactly as a real pull does, then
    # RETURNS the rows instead of landing them: no `_insert_raw_rows`, so nothing
    # reaches raw, staging or the marts. Same contract as gsc and
    # google-analytics -- the setup preview of step 4 calls this and refuses a
    # module that ignores the flag.
    if dry_run:
        logger.info(
            "youtube_pull_dry_run: profile=%s rows=%d (nothing landed)",
            profile, len(canonical_rows),
        )
        return {
            "pull_id": None,
            "dry_run": True,
            "row_count": len(canonical_rows),
            "rows": canonical_rows,
            "schema": sorted({key for row in canonical_rows for key in row}),
            "date_from": date_from,
            "date_to": date_to,
        }

    row_count = (
        _insert_breakdown_rows(canonical_rows, pull_id, project_id, profile)
        if profile in _BREAKDOWN_PROFILES
        else _insert_raw_rows(canonical_rows, pull_id, project_id)
    )

    logger.info(
        "youtube_pull_completed: pull_id=%s profile=%s row_count=%d",
        pull_id, profile, row_count,
    )
    return {"pull_id": pull_id, "row_count": row_count, "date_from": date_from, "date_to": date_to}


# ---------------------------------------------------------------------------
# Breakdown reports: one SUPPORTED combination each (AI-... 2026-08-11).
#
# `_pull` reads the metrics and dimensions from the manifest, so these add no
# provider vocabulary -- they name WHICH declared combination to ask for. The
# API refuses arbitrary pairings ("The query is not supported"), which is why a
# combination is a REPORT and not a free choice of dimensions.
# ---------------------------------------------------------------------------


def _breakdown(profile: str):
    def _fn(
        connection_id: str, date_from: str, date_to: str, project_id: str, pull_id: str = "",
        channel_id: str | None = None, dry_run: bool = False,
    ) -> dict:
        return _pull(
            connection_id, date_from, date_to, project_id, pull_id, profile, channel_id, dry_run
        )
    _fn.__name__ = f"pull_{profile}"
    _fn.__qualname__ = _fn.__name__
    _fn.__doc__ = f"Pull the `{profile}` supported combination (manifest-driven)."
    return _fn


pull_audience_demographics = _breakdown("audience_demographics")
pull_audience_geography = _breakdown("audience_geography")
pull_audience_device = _breakdown("audience_device")
pull_traffic_sources = _breakdown("traffic_sources")
pull_playback_locations = _breakdown("playback_locations")
pull_audience_subscription = _breakdown("audience_subscription")

def pull_channel_snapshot(
    connection_id: str, date_from: str, date_to: str, project_id: str, pull_id: str = "",
    channel_id: str | None = None, dry_run: bool = False,
) -> dict:
    """How many subscribers, right now -- the question this connector could not answer.

    `reports.query` serves FLOWS only: `subscribersGained` and `subscribersLost`,
    what changed during a window. The STOCK -- the number a person reads on their
    dashboard -- lives on the Data API, `channels.list(part=statistics)`. Nothing
    here fetched it, so "how many subscribers do I have" had no field to land in.

    A stock is RELEVE, not accumulated: one row per observation day, and the
    manifest declares `aggregation=latest`/`non_additive=true` so summing two
    days is refused rather than silently wrong. The date stamped is the day the
    reading was taken (`date_to`), because that is when the number was true.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2

    token = nango_client.get_fresh_token(connection_id, provider="youtube-analytics")
    params = {"part": "statistics,snippet"}
    if channel_id:
        params["id"] = channel_id
    else:
        params["mine"] = "true"
    resp = httpx.get(
        f"{YT_DATA_BASE}/channels",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    _raise_for_status(resp)

    raw_rows: list[dict] = []
    for item in resp.json().get("items") or []:
        stats = item.get("statistics") or {}
        raw_rows.append({
            "day": date_to,
            "channel_id": item.get("id") or channel_id or "MINE",
            "subscriberCount": stats.get("subscriberCount"),
            "viewCount": stats.get("viewCount"),
            "videoCount": stats.get("videoCount"),
        })
    canonical_rows = transform(raw_rows)

    if dry_run:
        logger.info("youtube_snapshot_dry_run: rows=%d (nothing landed)", len(canonical_rows))
        return {
            "pull_id": None,
            "dry_run": True,
            "row_count": len(canonical_rows),
            "rows": canonical_rows,
            "schema": sorted({key for row in canonical_rows for key in row}),
            "date_from": date_from,
            "date_to": date_to,
        }

    row_count = _insert_raw_rows(canonical_rows, pull_id, project_id)
    logger.info("youtube_snapshot_completed: pull_id=%s row_count=%d", pull_id, row_count)
    return {"pull_id": pull_id, "row_count": row_count,
            "date_from": date_from, "date_to": date_to}


def pull_competitor_channel_snapshot(
    connection_id: str, date_from: str, date_to: str, project_id: str, pull_id: str = "",
    channel_ids: list[str] | None = None, own_channel_ids: list[str] | None = None,
    channel_id: str | None = None, dry_run: bool = False,
) -> dict:
    """Subscriber/view/video stocks for the TRACKED channels (Competitors outbound).

    Same Data API read as `pull_channel_snapshot`, pointed at the channels the
    tracked-entity bindings supply (`channels.list` serves PUBLIC statistics for
    any channel id -- no permission on the channel is needed, only a live Google
    authorization). `channel_ids` is the query-driver parameter declared in the
    manifest's `tracked_entity` block; without a bound list the pull lands zero
    rows (25.5+ standard: no fallback, an unbound registry is an empty answer).
    Batched 50 ids per request, the Data API's own ceiling. `own_channel_ids` is
    accepted for driver symmetry; stocks land identically either way, and the
    own/competitor role lives in Governance, never in the row.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2

    ids = [str(c) for c in (channel_ids or []) if str(c).strip()]
    # `channel_id` is the worker's selected-account parameter (pull-contract
    # conformance): the connected channel. It joins the request and marks own.
    own_set = {str(c) for c in (own_channel_ids or [])}
    if channel_id:
        own_set.add(str(channel_id))
        if str(channel_id) not in ids:
            ids.append(str(channel_id))
    if not ids:
        logger.info("youtube_competitor_snapshot: no bound channel ids, landing 0 rows")
        return {"pull_id": pull_id, "row_count": 0,
                "date_from": date_from, "date_to": date_to}

    token = nango_client.get_fresh_token(connection_id, provider="youtube-analytics")
    raw_rows: list[dict] = []
    for start in range(0, len(ids), 50):
        batch = ids[start:start + 50]
        resp = httpx.get(
            f"{YT_DATA_BASE}/channels",
            params={"part": "statistics,snippet", "id": ",".join(batch), "maxResults": 50},
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
        _raise_for_status(resp)
        for item in resp.json().get("items") or []:
            stats = item.get("statistics") or {}
            raw_rows.append({
                "day": date_to,
                "channel_id": item.get("id") or "",
                "subscriberCount": stats.get("subscriberCount"),
                "viewCount": stats.get("viewCount"),
                "videoCount": stats.get("videoCount"),
            })
    canonical_rows = transform(raw_rows)

    if dry_run:
        return {"pull_id": None, "dry_run": True, "row_count": len(canonical_rows),
                "rows": canonical_rows,
                "schema": sorted({key for row in canonical_rows for key in row}),
                "date_from": date_from, "date_to": date_to}

    row_count = _insert_raw_rows(canonical_rows, pull_id, project_id)
    logger.info(
        "youtube_competitor_snapshot_completed: pull_id=%s channels=%d row_count=%d",
        pull_id, len(ids), row_count,
    )
    return {"pull_id": pull_id, "row_count": row_count,
            "date_from": date_from, "date_to": date_to}


def pull_channel_video_directory(
    connection_id: str, date_from: str, date_to: str, project_id: str, pull_id: str = "",
    channel_ids: list[str] | None = None, own_channel_ids: list[str] | None = None,
    channel_id: str | None = None, dry_run: bool = False,
) -> dict:
    """The named uploads of each tracked channel -- titles, dates, durations, views.

    One dated snapshot row per (channel, video) from the PUBLIC Data API:
    `channels.list(contentDetails)` -> uploads playlist, `playlistItems.list`
    (one page of 50 -- the recent-window contract, not a full history), then
    `videos.list(snippet,statistics,contentDetails)` for the facts. This is the
    relation that puts NAMES on video ids -- the analytics relations never carry
    a title -- and `published_at` is what makes recent-window comparisons honest
    (views per day since publication, never lifetime counts across eras).

    Driven by the same `channel_ids` binding parameter as the competitor
    snapshot; ids in `own_channel_ids` are stamped `is_own_channel` so the own
    catalogue and the tracked ones can land through one profile. No bound list,
    zero rows.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2

    ids = [str(c) for c in (channel_ids or []) if str(c).strip()]
    own = {str(c) for c in (own_channel_ids or [])}
    # Selected-account parameter (pull-contract conformance): the connected
    # channel joins the directory and is stamped own.
    if channel_id:
        own.add(str(channel_id))
        if str(channel_id) not in ids:
            ids.append(str(channel_id))
    if not ids:
        logger.info("youtube_video_directory: no bound channel ids, landing 0 rows")
        return {"pull_id": pull_id, "row_count": 0,
                "date_from": date_from, "date_to": date_to}

    token = nango_client.get_fresh_token(connection_id, provider="youtube-analytics")
    headers = {"Authorization": f"Bearer {token}"}
    snapshot_day = date_to or datetime.now(tz=timezone.utc).date().isoformat()
    rows: list[dict] = []
    unreachable: list[str] = []

    for start in range(0, len(ids), 50):
        batch = ids[start:start + 50]
        resp = httpx.get(
            f"{YT_DATA_BASE}/channels",
            params={"part": "contentDetails,snippet", "id": ",".join(batch), "maxResults": 50},
            headers=headers, timeout=30.0,
        )
        _raise_for_status(resp)
        uploads: dict[str, tuple[str, str]] = {}
        for item in resp.json().get("items") or []:
            playlist = (((item.get("contentDetails") or {}).get("relatedPlaylists") or {})
                        .get("uploads"))
            if playlist:
                uploads[item["id"]] = (playlist, (item.get("snippet") or {}).get("title") or "")
        unreachable.extend(c for c in batch if c not in uploads)

        for channel_id, (playlist, channel_title) in uploads.items():
            page = httpx.get(
                f"{YT_DATA_BASE}/playlistItems",
                params={"part": "contentDetails", "playlistId": playlist, "maxResults": 50},
                headers=headers, timeout=30.0,
            )
            if page.status_code == 404:
                # A channel with zero uploads answers 404 on its uploads
                # playlist -- an empty catalogue, not a fault.
                continue
            _raise_for_status(page)
            video_ids = [
                (it.get("contentDetails") or {}).get("videoId")
                for it in page.json().get("items") or []
            ]
            video_ids = [v for v in video_ids if v]
            if not video_ids:
                continue
            details = httpx.get(
                f"{YT_DATA_BASE}/videos",
                params={"part": "snippet,statistics,contentDetails",
                        "id": ",".join(video_ids), "maxResults": 50},
                headers=headers, timeout=30.0,
            )
            _raise_for_status(details)
            for v in details.json().get("items") or []:
                snip = v.get("snippet") or {}
                rows.append({
                    "date": snapshot_day,
                    "channel_id": channel_id,
                    "channel_title": channel_title,
                    "video": v.get("id") or "",
                    "video_title": snip.get("title") or "",
                    "published_at": (snip.get("publishedAt") or "")[:10],
                    "duration_seconds": _iso8601_duration_seconds(
                        (v.get("contentDetails") or {}).get("duration")
                    ),
                    "lifetime_views": (v.get("statistics") or {}).get("viewCount"),
                    "is_own_channel": channel_id in own,
                })

    if dry_run:
        return {"pull_id": None, "dry_run": True, "row_count": len(rows), "rows": rows,
                "schema": sorted({key for row in rows for key in row}),
                "date_from": date_from, "date_to": date_to,
                "unreachable_channel_ids": unreachable}

    row_count = _insert_directory_rows(rows, pull_id, project_id)
    logger.info(
        "youtube_video_directory_completed: pull_id=%s channels=%d row_count=%d unreachable=%d",
        pull_id, len(ids), row_count, len(unreachable),
    )
    return {"pull_id": pull_id, "row_count": row_count,
            "date_from": date_from, "date_to": date_to,
            "unreachable_channel_ids": unreachable}


def pull_channel_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str, pull_id: str = "",
    channel_id: str | None = None, dry_run: bool = False,
) -> dict:
    """AI-58 dispatch: channel-level daily metrics (grain date x channel_id)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id, "channel_daily", channel_id,
        dry_run,
    )


def pull_video_daily(
    connection_id: str, date_from: str, date_to: str, project_id: str, pull_id: str = "",
    channel_id: str | None = None, dry_run: bool = False,
) -> dict:
    """AI-58 dispatch: per-video daily metrics (grain date x video x channel_id)."""
    return _pull(
        connection_id, date_from, date_to, project_id, pull_id, "video_daily", channel_id,
        dry_run,
    )


def pull(
    connection_id: str, date_from: str, date_to: str, project_id: str, pull_id: str = "",
    channel_id: str | None = None, dry_run: bool = False,
) -> dict:
    """Default pull() = channel_daily (AI-58 profile-less default dispatch)."""
    return pull_channel_daily(
        connection_id, date_from, date_to, project_id, pull_id, channel_id, dry_run
    )


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
        # CHANTIER C -- THE EVENT NAMES THE ENTITY IT IS ABOUT. Without this the
        # title landed in a free-text column and nothing could join it to the
        # dimension the same channel is measured by: 519 videos observed, three
        # events landed, and no expressible relation between them (measured
        # 2026-08-14). The key is the one the source itself writes, so it
        # compares to the observed value with no rule in between -- which is why
        # it is extracted HERE, where the row's own shape is known, and never in
        # the server (AD-2).
        #
        # An item with no `resourceId.videoId` still becomes an event: it is a
        # real publication on a real day. It simply names no entity, and the
        # inventory of holes will say so rather than pretend it was matched.
        video_id = str(((snippet.get("resourceId") or {}).get("videoId") or "")).strip()
        event: dict = {
            "event_type": event_type,
            "event_date": event_date,
            "label": title,
            "platform": "youtube",
            "source": "youtube-analytics",
        }
        if video_id:
            event["entity_key"] = video_id
            event["entity_kind"] = "video"
        result.append(event)
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

    # Step 1: resolve the uploads playlist id for the CHOSEN channel. `mine=true`
    # was hard-coded here while `channel_id` sat declared and unused, so this
    # profile read the consenting identity's own uploads whatever the operator
    # picked -- the same break as the one the setup path had, one level down.
    channels_resp = httpx.get(
        f"{YT_DATA_BASE}/channels",
        params=(
            {"part": "contentDetails", "id": channel_id}
            if channel_id
            else {"part": "contentDetails", "mine": "true"}
        ),
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    _raise_for_status(channels_resp)
    items = channels_resp.json().get("items") or []
    if not items:
        logger.warning(
            "pull_video_upload: no channel found for connection_id=%s", connection_id
        )
        return {
            "pull_id": pull_id,
            "event_count": 0,
            "row_count": 0,
            "date_from": date_from,
            "date_to": date_to,
        }

    uploads_playlist_id = (
        (items[0].get("contentDetails") or {})
        .get("relatedPlaylists", {})
        .get("uploads", "")
    )
    if not uploads_playlist_id:
        logger.warning(
            "pull_video_upload: uploads playlist not found for connection_id=%s", connection_id
        )
        return {
            "pull_id": pull_id,
            "event_count": 0,
            "row_count": 0,
            "date_from": date_from,
            "date_to": date_to,
        }

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
            # CHANTIER C -- carried, not re-derived. `transform_events` already
            # read the key from the item it was transforming; picking it out of
            # the URL again here would be a second place that has to know the URL
            # shape, and the two would drift.
            entity_key=ev.get("entity_key"),
            entity_kind=ev.get("entity_kind"),
        )
        event_count += 1

    logger.info(
        "pull_video_upload: persisted %d context_events: pull_id=%s date_from=%s date_to=%s",
        event_count, pull_id, date_from, date_to,
    )
    return {
        "pull_id": pull_id,
        "event_count": event_count,
        "row_count": event_count,
        "date_from": date_from,
        "date_to": date_to,
    }


# ---------------------------------------------------------------------------
# Account topology discovery (playbook section 5) -- the connected channel.
# ---------------------------------------------------------------------------


def _resolve_channel_id(reference: str, token: str) -> str | None:
    """Turn what a person actually has into the channel id the API needs.

    Accepted, because these are the three things an operator holds:
      * ``UC...``                              -- already the id
      * ``@handle``                            -- what the channel calls itself
      * a URL containing ``/@handle`` or ``/channel/UC...``

    Returns None when nothing resolves, so the caller keeps the original string
    and the access check answers on it -- an unknown reference must fail as
    "not reachable", never as a crash.
    """
    reference = (reference or "").strip()
    if not reference:
        return None
    if "/channel/" in reference:
        reference = reference.split("/channel/", 1)[1].split("/", 1)[0].split("?", 1)[0]
    if reference.startswith("UC") and len(reference) >= 20:
        return reference

    handle = reference
    if "/@" in handle:
        handle = "@" + handle.split("/@", 1)[1].split("/", 1)[0].split("?", 1)[0]
    handle = handle.lstrip("@")
    if not handle:
        return None
    try:
        resp = httpx.get(
            f"{YT_DATA_BASE}/channels",
            params={"part": "id", "forHandle": f"@{handle}"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
        if resp.status_code >= 400:
            return None
        items = resp.json().get("items") or []
        return items[0].get("id") if items else None
    except Exception:  # noqa: BLE001 -- a failed lookup is not an access answer
        return None


def verify_account(connection_id: str, account_id: str) -> dict | None:
    """Can this token read THAT channel? Ask YouTube, do not consult a list.

    `discover_accounts` enumerates one channel -- the consenting identity's --
    because `channels.list(mine=true)` returns no other. But `reports.query`
    answers `ids=channel==<id>` for every channel that identity MANAGES, and an
    agency's whole reason for existing is managing channels it does not own. So
    a channel was unreachable not because the token could not read it, but
    because the provider would not list it.

    This is the access-check this connector's own topology has always described:
    one row, one day, one metric, on the channel actually asked for. It returns
    the channel and its title when YouTube answers, None when YouTube refuses --
    and lets a rate limit or an expired consent propagate as itself, because
    neither is a refusal of access.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2

    token = nango_client.get_fresh_token(connection_id, provider="youtube-analytics")

    # A PERSON KNOWS THE HANDLE, NOT THE `UC...` KEY. Asking for a channel id is
    # asking for a row key -- the same defect as printing one. What an operator
    # has in hand is @sebastienkardinal, or the URL they copied from the browser.
    # `channels.list(forHandle=)` is the public resolution, one quota unit, on
    # the token already in hand.
    account_id = _resolve_channel_id(account_id, token) or account_id

    day = (datetime.now(tz=timezone.utc).date() - timedelta(days=2)).isoformat()
    resp = httpx.get(
        f"{YT_ANALYTICS_BASE}/reports",
        params={
            "ids": f"channel=={account_id}",
            "startDate": day,
            "endDate": day,
            "metrics": "views",
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    if resp.status_code == 403 and not (_error_reasons(resp.json()) & _QUOTA_REASONS):
        # The token cannot read this channel -- a refusal, not a fault.
        return None
    if resp.status_code == 400:
        # An id YouTube does not recognise as a channel.
        return None
    _raise_for_status(resp)

    # The title is a courtesy: the Data API answers it for a channel the token
    # can see, and the id alone is a usable label when it does not.
    label = account_id
    try:
        info = httpx.get(
            f"{YT_DATA_BASE}/channels",
            params={"part": "snippet", "id": account_id},
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
        if info.status_code < 400:
            items = info.json().get("items") or []
            if items:
                label = (items[0].get("snippet") or {}).get("title") or account_id
    except Exception:  # noqa: BLE001 -- a missing title never denies a proven access
        label = account_id
    return {"id": account_id, "label": label}


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


def _get_mart_table(db_mode: str, project_id: str | None) -> str:
    if db_mode == "duckdb":
        from core import warehouse_tenancy  # noqa: PLC0415

        return f"{warehouse_tenancy.mart_prefix(project_id)}fact_youtube_daily"
    dataset = os.environ.get("BQ_MARTS_DATASET", "marts")
    gcp_project = os.environ.get("GCP_PROJECT", "")
    prefix = f"{gcp_project}.{dataset}" if gcp_project else dataset
    return f"{prefix}.fact_youtube_daily"


#: AI-310 -- WHICH GRAIN EACH REPORT PROFILE READS. The two profiles that share
#: `raw_youtube_daily` are the two rows of this map, and `stg_youtube_daily`
#: derives the same two values into `data_level`. The map is the tool's half of
#: the same declaration the manifest makes (`channel_daily` -> [date, channel_id],
#: `video_daily` -> [date, video, channel_id]); a profile absent from here is a
#: profile this tool cannot answer for, and it is refused by name rather than
#: silently widened to everything.
_DATA_LEVEL_BY_PROFILE = {
    "channel_daily": "CHANNEL",
    "video_daily": "VIDEO",
}


class UnknownReportProfile(ValueError):
    """The caller named a report profile this tool does not read."""


def _data_level_for(report_profile: str) -> str:
    level = _DATA_LEVEL_BY_PROFILE.get(str(report_profile or "").strip())
    if level is None:
        offered = ", ".join(sorted(_DATA_LEVEL_BY_PROFILE))
        raise UnknownReportProfile(
            f"This report does not read `{report_profile}`. Ask for one of: {offered}."
        )
    return level


def _query_mart(
    date_from: str, date_to: str, project_id: str, report_profile: str
) -> list[dict]:
    """The rows of ONE report grain, never two.

    AI-310 -- WHAT THIS ARGUMENT REPAIRS. `report_profile` was declared on the
    tool below and reached nothing: the query read the whole mart, so a caller
    asking for `channel_daily` got the channel roll-up AND every video row of the
    same day. Summing the answer gave exactly twice the channel's views --
    measured on production 2026-08-22 (project proj_01KZGCRSV2XACWRP3RSVNWWGBK,
    metric `views`): 2026-08-19 channel 390 / videos 390 / naive sum 780, ratio
    2.000 on every day of the window. An argument a function does not use is not
    a default, it is a promise the answer breaks.

    The restriction is on the NAMED marker `data_level`, not on the `video = ''`
    convention it replaces, so the filter says what it means and reads the same
    way as the four connectors that already carry the column.
    """
    level = _data_level_for(report_profile)
    db_mode = _get_db_mode()
    if db_mode != "duckdb":
        raise ValueError(f"youtube mart query: unsupported db_mode {db_mode!r} at P-dev")
    import duckdb  # noqa: PLC0415

    table = _get_mart_table(db_mode, project_id)
    sql = (
        f"SELECT date, channel_id, video, data_level, metric, SUM(value) AS value "
        f"FROM {table} WHERE project_id = ? AND date BETWEEN ? AND ? "
        "AND data_level = ? "
        "GROUP BY date, channel_id, video, data_level, metric ORDER BY date, metric"
    )
    con = duckdb.connect(_get_duckdb_path(), read_only=True)
    try:
        rel = con.execute(sql, [project_id, date_from, date_to, level])
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

    ONE report grain per answer (AI-310). `channel_daily` returns the channel
    roll-up and `video_daily` the per-video rows; the two are never mixed, so the
    metrics of an answer can be summed without counting the same views twice.
    Every returned row names its own `data_level` (CHANNEL | VIDEO).

    Report profiles: channel_daily, video_daily. Metrics: views,
    estimated_minutes_watched, likes, comments, shares, subscribers_gained,
    subscribers_lost -- all ADDITIVE. Ratio metrics (average view duration/%) are
    computed at the semantic layer from these, never stored. Full lifetime history.

    Returns the canonical AD-1 envelope via structuredContent. A profile this
    report does not read is refused by name, with the ones it does read.

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
        rows = _query_mart(date_from, date_to, project_id, report_profile)
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
    # `raw_youtube_video_directory` is deliberately NOT registered: the registry
    # holds ONE name per module (AI-302), and this module already lands in
    # several relations -- re-registering would re-point the verification
    # counter at the wrong table for every daily profile.
except Exception:
    pass  # best-effort
