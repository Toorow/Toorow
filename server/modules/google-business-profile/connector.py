"""Google Business Profile connector -- daily performance, reviews, monthly keywords.

Exposes a ``mcp_app: FastMCP`` instance as the conformance surface (AD-1
envelope); since AD-42 the core no longer mounts it — execution uses the
Datastream-parameterized core tools. Built to the epic-25 industrial
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

# THREE GRAINS, THREE FACTS, and they are never merged (research dossier sec. 5):
#   location_daily          -- 11 ADDITIVE daily counts per location  [core]
#   reviews                 -- per-review stream, ratings are NON-ADDITIVE levels;
#                              legacy v4 host behind the _reviews_v4 adapter
#   search_keywords_monthly -- MONTHLY keyword impressions, with a privacy floor
#                              (`threshold`) that must never become a count
#
# AD-12: MCP server reads the fact_gbp_* marts only -- no raw_* tables.
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

# Module-level FastMCP instance, kept as the conformance surface (AD-1 envelope,
# validated by server/tests/conformance/test_envelope.py). Since AD-42 the core
# no longer mounts it: execution uses the Datastream-parameterized core tools.
mcp_app = FastMCP("google-business-profile")

# Base hosts (federated GBP API model).
GBP_PERFORMANCE_BASE = "https://businessprofileperformance.googleapis.com/v1"
GBP_ACCOUNTS_BASE = "https://mybusinessaccountmanagement.googleapis.com/v1"
GBP_BUSINESSINFO_BASE = "https://mybusinessbusinessinformation.googleapis.com/v1"
# Legacy v4 host (reviews / localPosts). DEPRECATED but still the only surface
# serving them in 2026, and allowlisted separately from the quota grant.
GBP_V4_BASE = "https://mybusiness.googleapis.com/v4"

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
    """Return the manifest's ``error_map`` (see _error_map_note).

    Declared EMPTY: GBP publishes no enumerated provider code, and this map's
    only reader is core.pull_errors.classify_http_error, whose key grammar is
    "<status>:<provider_code>". Handed over unchanged; this module never looks
    a key up itself (AI-114).
    """
    return _load_manifest().get("error_map") or {}


# AI-114 (2026-08-01) -- status-level judgment, NOT a provider refinement.
# The former bare key "404" in manifest.error_map was DEAD: the map was handed
# to core, core only ever looked up "<status>:<code>", so a GBP 404 came out
# `unclassified` -- retryable, and mute about what the person should do. Google
# answers 404 for a location the token cannot reach as readily as for one that
# does not exist, so the actionable verdict is permission_denied ("ask for
# access to this location"), not a transient unknown. Now it applies.
_STATUS_OVERRIDES: dict[int, str] = {404: "permission_denied"}


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

_RAW_TABLE = "raw_gbp_location_daily"

# THE RAW TABLE, DECLARED ONCE, built from `_METRIC_COLUMNS` above so the metric
# list is named in exactly one place. `core.raw_landing` renders the DuckDB DDL
# and INSERT from this declaration and the BigQuery landing is handed the same
# one, so the two backends cannot describe this table differently -- the failure
# this connector already paid for once, and which seven other modules were still
# carrying on 2026-08-17.
_RAW_COLUMNS: list[tuple[str, str]] = [
    ("date", "STRING"),
    ("location_id", "STRING"),
    *[(column, "INTEGER") for column in _METRIC_COLUMNS],
    ("pull_id", "STRING"),
    ("loaded_at", "STRING"),
    ("project_id", "STRING"),
]


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
    if db_mode not in ("duckdb", "bigquery"):
        raise ValueError(f"_insert_raw_rows: unsupported db_mode {db_mode!r}")
    from core import warehouse_write  # noqa: PLC0415

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
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
    from core import raw_landing  # noqa: PLC0415 -- AD-2

    if db_mode == "bigquery":
        raw_landing.land_raw_rows(
            _RAW_TABLE,
            [raw_landing.row_from_values(_RAW_COLUMNS, v) for v in values],
            columns=_RAW_COLUMNS,
            project_id=project_id,
            backend="bigquery",
        )
        return len(values)
    else:
        con = warehouse_write.open_raw_writer(_get_duckdb_path(), project_id=project_id)
        try:
            con.execute(raw_landing.duckdb_ddl(_RAW_TABLE, _RAW_COLUMNS))
            if values:
                con.executemany(raw_landing.duckdb_insert(_RAW_TABLE, _RAW_COLUMNS), values)
        finally:
            con.close()
        return len(values)


# --- Generic landing for the two SEPARATE facts (reviews, monthly keywords).
#
# location_daily keeps its own hand-written DDL above (it predates these and its
# shape is pinned by the seed loader). Reviews and keywords share this one so the
# duckdb DDL, the duckdb INSERT and the BigQuery column list cannot drift from
# each other -- the failure this connector already paid for once.

def _create_ddl(table: str, columns: list[tuple[str, str]]) -> str:
    """The CREATE the connector ships for *table*, built once at import."""
    body = ",\n    ".join(f"{name} {sql_type}" for name, sql_type in columns)
    return f"CREATE TABLE IF NOT EXISTS {table} (\n    {body}\n)"


def _insert_sql(table: str, columns: list[tuple[str, str]]) -> str:
    """The positional INSERT the connector ships for *table*, built once at import.

    Built at MODULE level, never inside the landing function: the BigQuery
    translator gate reads the statements a connector actually ships, and a
    template assembled at call time is a statement nothing can check until it
    runs -- which for the warehouse backend means in production.
    """
    from core import raw_landing  # noqa: PLC0415 -- AD-2

    return raw_landing.duckdb_insert(table, columns)


def _land_rows(
    table: str,
    columns: list[tuple[str, str]],
    rows: list[dict],
    project_id: str,
    create_ddl: str,
    insert_sql: str,
) -> int:
    """Append *rows* to *table* under both db modes (AD-7: append-only, never UPDATE).

    *columns* is [(name, core.raw_landing type), ...] and IS the order of the positional
    INSERT. Values are read from each row by column name; an absent key lands
    NULL (AD-9 -- honest absence, never a fabricated 0).
    """
    db_mode = _get_db_mode()
    if db_mode not in ("duckdb", "bigquery"):
        raise ValueError(f"_land_rows: unsupported db_mode {db_mode!r}")

    if db_mode == "bigquery":
        from core.raw_landing import land_raw_rows  # noqa: PLC0415

        land_raw_rows(
            table,
            [{name: row.get(name) for name, _ in columns} for row in rows],
            columns=columns,
            project_id=project_id,
            backend="bigquery",
        )
        return len(rows)

    from core import warehouse_write  # noqa: PLC0415

    con = warehouse_write.open_raw_writer(_get_duckdb_path(), project_id=project_id)
    try:
        con.execute(create_ddl)
        values = [tuple(row.get(name) for name, _ in columns) for row in rows]
        if values:
            con.executemany(insert_sql, values)
    finally:
        con.close()
    return len(rows)


# ---------------------------------------------------------------------------
# HTTP error handling -- standard Google envelope, HTTP-status error_map.
# ---------------------------------------------------------------------------

# --- ACCESS PRECONDITIONS (Story 30.1, the doctrinal nuance of this connector).
#
# GBP answers 403 for three different situations and names none of them:
#   (a) the project has NOT been granted quota yet -- every GBP project starts at
#       0 QPM and stays there until Google approves an access request by hand;
#   (b) the legacy v4 host (reviews, local posts) is separately ALLOWLISTED and
#       this project is not on the allowlist;
#   (c) the caller is genuinely forbidden on that resource.
#
# All three classify as ``permission_denied`` -- that part is pure HTTP and core
# already decides it. What core cannot decide is the PRODUCT reading: (a) and (b)
# are EXPECTED PROVISIONING STATES of a connector that was just installed, not
# faults. Left unmarked they produce exactly what the research dossier says must
# not happen: a connection that looks broken, and an optional profile that fails
# the whole pull forever.
#
# So the module makes the judgment core cannot, in two halves:
#   - every 403 raised from a gated surface carries ``precondition`` and
#     ``precondition_message`` on the typed error, which is what says "Google
#     access pending" instead of "permission denied";
#   - a 403 on an OPTIONAL profile (reviews, local posts) never leaves the
#     connector at all: _prevented_envelope() turns it into a pull that reports
#     WHY it did not run (see pull_reviews / pull_social_post).
#
# AI-307: that envelope is now the SHARED contract of `core.pull_envelope`, and
# `queue._execute_job` reads it. Until 2026-08-21 it had no reader at all: this
# module built the three keys, the worker read `row_count` alone, and a refused
# `reviews` pull was recorded `done / 0 row` -- indistinguishable from a pull
# that ran and honestly found nothing. Building the envelope was never the gap;
# nothing consumed it.
#
# The core profile does NOT skip: a location_daily pull that lands nothing is a
# failure, and reporting it as a success would fabricate an empty day. It RAISES
# -- and that raise is the SECOND half of the same contract, not an exception to
# it. `core.pull_envelope.prevented_by_error` reads the two attributes off the
# raised error and the worker records the window `prevented`, with this module's
# own sentence, exactly as it does for a returned envelope.
#
# It had no reader either until 2026-08-25, and the cost was not cosmetic: the
# DEFAULT pull of every Business Profile project on its first day (0 QPM until
# Google approves by hand) was written `failed / permission_denied` with
# `user_action = "reconnect"` -- an instruction that releases nothing -- and
# `failed` is what `scheduler._reschedule_failed_pulls` re-arms every hour. The
# "no crash-loop either way" this comment used to claim was true of the ATTEMPT
# policy (retryable=False) and false of the SCHEDULER.

#: Surface id -> precondition reason. A 403 from a surface listed here is an
#: expected provisioning state; a 403 from anywhere else is just forbidden.
_GATED_SURFACES: dict[str, str] = {
    "performance": "google_access_pending",
    "businessinfo": "google_access_pending",
    "accounts": "google_access_pending",
    "reviews_v4": "reviews_access_pending",
    "local_posts_v4": "local_posts_access_pending",
}

#: The sentence shown to the person. ASCII-only (AI-03).
#:
#: EACH ONE NAMES THE GESTURE, never the status code (AI-307). "403 on the v4
#: host" is the cause and nobody can act on it; "request the quota for this
#: project, then re-ask these dates" is what releases the window. The sentence
#: also says what is NOT affected, because a person reading one refused profile
#: needs to know whether the rest of the collection is still standing.
_PRECONDITION_MESSAGES: dict[str, str] = {
    "google_access_pending": (
        "Google has not granted this project any Business Profile quota yet, so "
        "every call is refused. Request Business Profile API quota for this "
        "Google Cloud project, and re-ask these dates once Google approves it. "
        "Nothing is misconfigured on your side."
    ),
    "reviews_access_pending": (
        "Reviews are not collected yet: they are served by a separate Google "
        "host that is allowlisted on its own, apart from the quota grant. "
        "Request the reviews allowlist for this project, then re-ask these "
        "dates. Your daily Business Profile figures are unaffected."
    ),
    "local_posts_access_pending": (
        "Local posts are not collected yet: they are served by a separate "
        "Google host that is allowlisted on its own, apart from the quota grant. "
        "Request the local posts allowlist for this project, then re-ask these "
        "dates. Your daily Business Profile figures are unaffected."
    ),
}


def _precondition_for(status_code: int, surface: str) -> str | None:
    """Return the precondition reason a *status_code* on *surface* means, or None."""
    if status_code != 403:
        return None
    return _GATED_SURFACES.get(surface)


def precondition_of(exc: BaseException) -> str | None:
    """Read the precondition reason a typed error carries (None when it carries none).

    The one reader INSIDE this module, used by the optional profiles to decide
    whether to return a prevented envelope instead of letting the error out.

    Outside the module, the reader is `core.pull_envelope.prevented_by_error`,
    which owns the attribute names (`PRECONDITION_ATTR`) and the bounds on the
    sentence. This function does not duplicate that contract; it answers the one
    question the profiles below ask -- "is this an access gate or a real
    refusal?" -- and the spelling is taken from core so the two cannot drift.
    """
    from core.pull_envelope import PRECONDITION_ATTR  # noqa: PLC0415

    return getattr(exc, PRECONDITION_ATTR, None)


def _prevented_envelope(
    pull_id: str, date_from: str, date_to: str, reason: str
) -> dict:
    """The pull envelope of a profile the source did not ALLOW to run.

    Built by `core.pull_envelope`, which owns the shape for all 39 connectors and
    is read by `queue._execute_job` -- the window is then recorded `prevented`
    with a NULL row count, never `done / 0 row`. This module keeps exactly the
    half that is its own: which gates exist here, and what to say about each.

    It is a REFUSAL, said out loud -- never a success with no rows.
    """
    # Lazy, like every other `core.*` import in this module: a connector is
    # loaded by the module registry before core is fully wired.
    from core.pull_envelope import prevented_envelope  # noqa: PLC0415

    return prevented_envelope(
        pull_id=pull_id,
        date_from=date_from,
        date_to=date_to,
        reason=reason,
        message=_PRECONDITION_MESSAGES.get(reason, reason),
    )


def _raise_for_status(resp: httpx.Response, surface: str = "performance") -> None:
    """Raise the typed error for a GBP response, marking access preconditions.

    *surface* names which of the federated GBP APIs answered, because that is
    what decides whether a 403 is a provisioning gate or a refusal.
    """
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

    from core import pull_errors  # noqa: PLC0415

    try:
        body = resp.json()
    except Exception:
        body = resp.text
    override = pull_errors.error_for_class(
        _STATUS_OVERRIDES.get(resp.status_code), resp.status_code, body
    )
    error = override or pull_errors.classify_http_error(
        resp.status_code, body, _load_error_map()
    )
    reason = _precondition_for(resp.status_code, surface)
    if reason is not None:
        error.precondition = reason
        error.precondition_message = _PRECONDITION_MESSAGES[reason]
    raise error


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
            _raise_for_status(resp, surface="accounts")
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
                _raise_for_status(resp, surface="businessinfo")
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
# Story 30.1 -- `reviews` profile, behind the _reviews_v4 adapter.
#
# Reviews are the ONLY GBP surface still served by the deprecated v4 host. There
# is no v1 replacement at research time (2026-07-21), so the migration risk is
# real and it is contained HERE: `_reviews_v4` is the only object in this module
# that knows the v4 URL shape, the v4 page size and the v4 field names. Swapping
# to a future v1 endpoint means rewriting this class and nothing else.
#
# NON-ADDITIVE, and the whole point of the profile. `starRating` is a level on a
# 1..5 scale and `averageRating` is already an average -- SUM over either one
# produces a number with no meaning. They land as-is and the mart reads them by
# last-value / recompute (see dbt/marts/fact_gbp_review_rollup.sql). The only
# additive quantity in this profile is `new_reviews`, counted from createTime.
# ---------------------------------------------------------------------------

#: v4 starRating enum -> the 1..5 level. The UNSPECIFIED sentinel maps to None
#: (an absent rating, never 0 -- 0 would be a real value on a 1..5 scale).
_STAR_RATING_LEVELS = {
    "ONE": 1,
    "TWO": 2,
    "THREE": 3,
    "FOUR": 4,
    "FIVE": 5,
    "STAR_RATING_UNSPECIFIED": None,
}

_RAW_REVIEW_TABLE = "raw_gbp_review"

#: (column, core.raw_landing type) -- the order of the positional INSERT and
#: of stg_gbp_review.sql's SELECT. The platform vocabulary, not the DuckDB
#: dialect: `core.raw_landing` maps it to VARCHAR/BIGINT for DuckDB and to
#: STRING/INT64 for BigQuery, so one declaration serves both.
_RAW_REVIEW_COLUMNS: list[tuple[str, str]] = [
    ("review_id", "STRING"),
    ("location_id", "STRING"),
    ("account_id", "STRING"),
    ("review_star_rating", "INTEGER"),
    ("review_comment", "STRING"),
    ("review_create_time", "STRING"),
    ("review_update_time", "STRING"),
    ("reviewer_display_name", "STRING"),
    ("reviewer_is_anonymous", "BOOLEAN"),
    ("review_reply_comment", "STRING"),
    ("review_reply_update_time", "STRING"),
    ("average_rating", "FLOAT"),
    ("total_review_count", "INTEGER"),
    ("pull_id", "STRING"),
    ("loaded_at", "STRING"),
    ("project_id", "STRING"),
]

_RAW_REVIEW_CREATE_DDL = _create_ddl(_RAW_REVIEW_TABLE, _RAW_REVIEW_COLUMNS)
_RAW_REVIEW_INSERT_SQL = _insert_sql(_RAW_REVIEW_TABLE, _RAW_REVIEW_COLUMNS)


class _ReviewsV4Adapter:
    """The single seam that knows reviews live on the LEGACY v4 host.

    Deliberately a class and not three loose functions: the point is that a
    future v1 reviews endpoint replaces ONE object. Nothing outside it may
    mention GBP_V4_BASE for reviews, the 50-row v4 page cap, or a v4 field name.
    """

    base = GBP_V4_BASE
    surface = "reviews_v4"
    #: v4 caps reviews.list at 50 per page (research dossier, section 9).
    page_size = 50

    def list_reviews(self, parent: str, token: str) -> dict:
        """Walk accounts/{a}/locations/{l}/reviews and return the full listing.

        Returns {"reviews": [...], "averageRating": float|None,
        "totalReviewCount": int|None} -- the two list-level aggregates are read
        from the LAST page, which is the freshest statement of the level.
        Raises the typed error (marked with the reviews_access_pending
        precondition on 403) -- the caller decides whether that is fatal.
        """
        reviews: list[dict] = []
        average_rating: float | None = None
        total_review_count: int | None = None
        page_token: str | None = None
        while True:
            params: dict = {"pageSize": self.page_size}
            if page_token:
                params["pageToken"] = page_token
            resp = httpx.get(
                f"{self.base}/{parent}/reviews",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=60.0,
            )
            _raise_for_status(resp, surface=self.surface)
            body = resp.json()
            reviews.extend(body.get("reviews") or [])
            if body.get("averageRating") is not None:
                average_rating = _to_float(body.get("averageRating"))
            if body.get("totalReviewCount") is not None:
                total_review_count = _to_int(body.get("totalReviewCount"))
            page_token = body.get("nextPageToken")
            if not page_token:
                break
        return {
            "reviews": reviews,
            "averageRating": average_rating,
            "totalReviewCount": total_review_count,
        }


#: The adapter instance the profile talks to (the named seam of the story).
_reviews_v4 = _ReviewsV4Adapter()


def _to_float(value) -> float | None:
    """Coerce a rating (float or stringified) to float; None stays None."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _split_v4_parent(parent: str) -> tuple[str, str]:
    """Split ``accounts/<a>/locations/<l>`` into (``accounts/<a>``, ``locations/<l>``)."""
    parts = parent.split("/")
    account = "/".join(parts[0:2]) if len(parts) >= 2 else ""
    location = "/".join(parts[2:4]) if len(parts) >= 4 else parent
    return account, location


def transform_reviews(
    listing: dict, account_id: str = "", location_id: str = ""
) -> list[dict]:
    """Map a v4 reviews listing to canonical raw rows (pure function, no I/O).

    One row per review, each carrying the list-level `average_rating` and
    `total_review_count` -- both LEVELS of the location, repeated on every row so
    the rollup can read the last value without a second table.

    PII (AD-8 / research section 5.1): the reviewer photo URL is never persisted, and
    `isAnonymous` is honoured -- an anonymous reviewer lands with a NULL display
    name, not with whatever placeholder the API happened to send.

    A review with no `reviewId` is SKIPPED: the id is the upsert key, and a row
    without one could never be superseded.
    """
    average_rating = _to_float(listing.get("averageRating"))
    total_review_count = _to_int(listing.get("totalReviewCount"))

    rows: list[dict] = []
    for review in listing.get("reviews") or []:
        review_id = review.get("reviewId")
        if not review_id:
            logger.warning(
                "transform_reviews: skipping a review with no reviewId (location=%s)",
                location_id,
            )
            continue
        reviewer = review.get("reviewer") or {}
        is_anonymous = bool(reviewer.get("isAnonymous"))
        reply = review.get("reviewReply") or {}
        rows.append(
            {
                "review_id": review_id,
                "location_id": location_id,
                "account_id": account_id,
                # Non-additive: a 1..5 level, averaged downstream, NEVER summed.
                "review_star_rating": _STAR_RATING_LEVELS.get(review.get("starRating")),
                "review_comment": review.get("comment"),
                "review_create_time": review.get("createTime"),
                "review_update_time": review.get("updateTime"),
                "reviewer_display_name": None if is_anonymous else reviewer.get("displayName"),
                "reviewer_is_anonymous": is_anonymous,
                "review_reply_comment": reply.get("comment"),
                "review_reply_update_time": reply.get("updateTime"),
                "average_rating": average_rating,
                "total_review_count": total_review_count,
            }
        )
    return rows


def pull_reviews(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    location_id: str | None = None,
) -> dict:
    """Fetch a location's review stream and land it in raw_gbp_review.

    ALLOWLIST-GATED, and that is the behaviour this profile is built around: the
    v4 host answers 403 until Google grants the reviews allowlist, which is a
    SEPARATE grant from the 0-QPM quota approval. That 403 is an expected
    provisioning state of a freshly installed connector, so it is caught here and
    returned as an explicit SKIP -- the profile reports zero rows and says why,
    the daily profile keeps working, and nothing crash-loops.

    UPSERT ON reviewId, expressed the way AD-7 allows: raw is append-only, and
    stg_gbp_review supersedes on (project_id, review_id) ordered by
    review_update_time then pull_id -- so the current state of each review is
    exactly one row, and an edited review replaces its earlier text without any
    UPDATE against the raw zone.

    The date window is NOT sent to the provider: reviews.list has no date filter.
    date_from/date_to are echoed in the envelope and used by the rollup to count
    `new_reviews`; every reachable review is landed on every run.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2
    from core.pull_errors import ConnectorError  # noqa: PLC0415

    if not location_id:
        raise ValueError(
            "google-business-profile pull_reviews requires a selected location_id "
            "(topology selection_level 'location'); no env-var fallback (25.5+)."
        )

    token = nango_client.get_fresh_token(connection_id, provider="google-business-profile")
    account = location = ""

    try:
        # Topology resolution is INSIDE the guard: the account lookup goes through
        # the same 0-QPM gate, so a connector waiting on its grant must skip here
        # too rather than fail before it ever reaches the v4 host.
        parent = _resolve_location_parent(connection_id, location_id)
        account, location = _split_v4_parent(parent)
        listing = _reviews_v4.list_reviews(parent, token)
    except ConnectorError as exc:
        reason = precondition_of(exc)
        if reason is None:
            raise
        logger.info(
            "pull_reviews: prevented by an expected access precondition (%s): "
            "pull_id=%s location=%s",
            reason, pull_id, location,
        )
        return _prevented_envelope(pull_id, date_from, date_to, reason)

    rows = transform_reviews(listing, account_id=account, location_id=location)
    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    for row in rows:
        row["pull_id"] = pull_id
        row["loaded_at"] = loaded_at
        row["project_id"] = project_id

    row_count = _land_rows(
        _RAW_REVIEW_TABLE, _RAW_REVIEW_COLUMNS, rows, project_id,
        _RAW_REVIEW_CREATE_DDL, _RAW_REVIEW_INSERT_SQL,
    )
    logger.info(
        "pull_reviews: landed %d review(s): pull_id=%s location=%s", row_count, pull_id, location
    )
    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
    }


# ---------------------------------------------------------------------------
# Story 30.1 -- `search_keywords_monthly` profile.
#
# MONTHLY grain, and it is never reconciled with the daily fact: the API has no
# daily keyword breakdown, so a daily figure would have to be invented. Its own
# raw table, its own staging, its own mart.
#
# THE PRIVACY FLOOR IS THE POINT. Google returns `insightsValue.value` for a
# keyword above a privacy floor and `insightsValue.threshold` (a FLOOR, read as
# "fewer than N") for one below it -- the two are mutually exclusive. Coercing a
# threshold into a count would fabricate precision the source refuses to give, so
# both land in separate columns with `is_thresholded` saying which one is real.
# ---------------------------------------------------------------------------

_RAW_KEYWORD_TABLE = "raw_gbp_search_keyword_monthly"

_RAW_KEYWORD_COLUMNS: list[tuple[str, str]] = [
    ("month", "STRING"),
    ("location_id", "STRING"),
    ("search_keyword", "STRING"),
    ("search_keyword_impressions", "INTEGER"),
    ("search_keyword_impressions_threshold", "INTEGER"),
    ("is_thresholded", "BOOLEAN"),
    ("pull_id", "STRING"),
    ("loaded_at", "STRING"),
    ("project_id", "STRING"),
]

_RAW_KEYWORD_CREATE_DDL = _create_ddl(_RAW_KEYWORD_TABLE, _RAW_KEYWORD_COLUMNS)
_RAW_KEYWORD_INSERT_SQL = _insert_sql(_RAW_KEYWORD_TABLE, _RAW_KEYWORD_COLUMNS)


def _months_in_range(date_from: str, date_to: str) -> list[tuple[int, int]]:
    """Return every (year, month) touched by [date_from, date_to], inclusive.

    The keyword endpoint takes a monthly RANGE and answers one aggregate per
    keyword over the whole range -- the response carries no month. So the month
    is only knowable when it is the unit REQUESTED: the profile asks month by
    month and stamps each answer with the month it asked for. Slower, and the
    only way the grain is a fact rather than an assumption.
    """
    y, m = int(date_from[0:4]), int(date_from[5:7])
    end_y, end_m = int(date_to[0:4]), int(date_to[5:7])
    months: list[tuple[int, int]] = []
    while (y, m) <= (end_y, end_m):
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


def transform_search_keywords(
    counts: list[dict], month: str, location_id: str = ""
) -> list[dict]:
    """Map searchKeywordsCounts[] to canonical monthly rows (pure function).

    `insightsValue` carries EITHER `value` (an exact count) OR `threshold` (a
    floor for a low-volume keyword). Whichever came back lands in its own column
    and `is_thresholded` records which -- never a threshold written into the
    count column, never a threshold silently dropped.
    """
    rows: list[dict] = []
    for entry in counts or []:
        keyword = entry.get("searchKeyword")
        if not keyword:
            logger.warning(
                "transform_search_keywords: skipping an entry with no searchKeyword "
                "(month=%s location=%s)", month, location_id,
            )
            continue
        insights = entry.get("insightsValue") or {}
        value = _to_int(insights.get("value"))
        threshold = _to_int(insights.get("threshold"))
        rows.append(
            {
                "month": month,
                "location_id": location_id,
                "search_keyword": keyword,
                "search_keyword_impressions": value,
                "search_keyword_impressions_threshold": threshold,
                "is_thresholded": value is None and threshold is not None,
            }
        )
    return rows


def pull_search_keywords_monthly(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    location_id: str | None = None,
) -> dict:
    """Fetch monthly search-keyword impressions per location, month by month.

    GET {performance}/locations/{l}/searchkeywords/impressions/monthly with
    monthlyRange.{start,end}Month.{year,month}, paginated via pageToken. One
    request per month in [date_from, date_to] so every row carries a month that
    was asked for rather than inferred (see _months_in_range).

    A 403 here is the 0-QPM quota gate, the same gate the daily profile hits: it
    is surfaced as an access precondition and returned as an explicit SKIP rather
    than failing the connection.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2
    from core.pull_errors import ConnectorError  # noqa: PLC0415

    if not location_id:
        raise ValueError(
            "google-business-profile pull_search_keywords_monthly requires a selected "
            "location_id (topology selection_level 'location'); no env-var fallback (25.5+)."
        )

    loc = location_id if location_id.startswith("locations/") else f"locations/{location_id}"
    token = nango_client.get_fresh_token(connection_id, provider="google-business-profile")
    headers = {"Authorization": f"Bearer {token}"}

    rows: list[dict] = []
    try:
        for year, month in _months_in_range(date_from, date_to):
            month_label = f"{year:04d}-{month:02d}"
            page_token: str | None = None
            while True:
                params: dict = {
                    "monthlyRange.startMonth.year": year,
                    "monthlyRange.startMonth.month": month,
                    "monthlyRange.endMonth.year": year,
                    "monthlyRange.endMonth.month": month,
                    "pageSize": 100,
                }
                if page_token:
                    params["pageToken"] = page_token
                resp = httpx.get(
                    f"{GBP_PERFORMANCE_BASE}/{loc}/searchkeywords/impressions/monthly",
                    params=params,
                    headers=headers,
                    timeout=60.0,
                )
                _raise_for_status(resp, surface="performance")
                body = resp.json()
                rows.extend(
                    transform_search_keywords(
                        body.get("searchKeywordsCounts") or [],
                        month_label,
                        location_id=loc,
                    )
                )
                page_token = body.get("nextPageToken")
                if not page_token:
                    break
    except ConnectorError as exc:
        reason = precondition_of(exc)
        if reason is None:
            raise
        logger.info(
            "pull_search_keywords_monthly: prevented by an expected access "
            "precondition (%s): pull_id=%s location=%s",
            reason, pull_id, loc,
        )
        return _prevented_envelope(pull_id, date_from, date_to, reason)

    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    for row in rows:
        row["pull_id"] = pull_id
        row["loaded_at"] = loaded_at
        row["project_id"] = project_id

    row_count = _land_rows(
        _RAW_KEYWORD_TABLE, _RAW_KEYWORD_COLUMNS, rows, project_id,
        _RAW_KEYWORD_CREATE_DDL, _RAW_KEYWORD_INSERT_SQL,
    )
    logger.info(
        "pull_search_keywords_monthly: landed %d keyword-month row(s): pull_id=%s location=%s",
        row_count, pull_id, loc,
    )
    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_from,
        "date_to": date_to,
    }


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
    from core.pull_errors import ConnectorError  # noqa: PLC0415

    if not location_id:
        raise ValueError(
            "google-business-profile pull_social_post requires a selected location_id "
            "(topology selection_level 'location'); no env-var fallback (25.5+)."
        )

    token = nango_client.get_fresh_token(connection_id, provider="google-business-profile")

    raw_rows: list[dict] = []
    parent = ""
    try:
        parent = _resolve_location_parent(connection_id, location_id)
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
            _raise_for_status(resp, surface="local_posts_v4")
            body = resp.json()
            raw_rows.extend(body.get("localPosts") or [])
            page_token = body.get("nextPageToken")
            if not page_token:
                break
    except ConnectorError as exc:
        # Same allowlist gate as reviews: the v4 host answers 403 until granted.
        # An optional profile does not take the connection down with it.
        reason = precondition_of(exc)
        if reason is None:
            raise
        logger.info(
            "pull_social_post: prevented by an expected access precondition (%s): "
            "pull_id=%s location=%s",
            reason, pull_id, location_id,
        )
        prevented = _prevented_envelope(pull_id, date_from, date_to, reason)
        prevented["event_count"] = 0
        return prevented

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
        "row_count": event_count,
        "date_from": date_from,
        "date_to": date_to,
    }


# ---------------------------------------------------------------------------
# MCP tool -- reads from fact_gbp_location_daily mart (AD-12).
# ---------------------------------------------------------------------------


#: profile -> (mart, the column the date window filters on, the ORDER BY).
#: Three profiles, three GRAINS -- daily, per-review, monthly. They are read
#: through the same tool and never through the same query, because merging a
#: monthly aggregate or a rating level into the daily rows is the one thing this
#: connector's research dossier says must not happen.
_MART_BY_PROFILE: dict[str, tuple[str, str, str]] = {
    "location_daily": ("fact_gbp_location_daily", "date", "date, location_id"),
    "reviews": ("fact_gbp_review_rollup", "review_date", "review_date, location_id"),
    "search_keywords_monthly": (
        "fact_gbp_search_keyword_monthly",
        "month",
        "month, location_id, search_keyword",
    ),
}


def _get_mart_table(db_mode: str, project_id: str | None, mart: str) -> str:
    if db_mode == "duckdb":
        from core import warehouse_tenancy  # noqa: PLC0415

        return f"{warehouse_tenancy.mart_prefix(project_id)}{mart}"
    dataset = os.environ.get("BQ_MARTS_DATASET", "marts")
    gcp_project = os.environ.get("GCP_PROJECT", "")
    prefix = f"{gcp_project}.{dataset}" if gcp_project else dataset
    return f"{prefix}.{mart}"


def _query_mart(
    date_from: str, date_to: str, project_id: str, report_profile: str
) -> tuple[list[dict], str]:
    db_mode = _get_db_mode()
    if db_mode != "duckdb":
        raise ValueError(f"gbp mart query: unsupported db_mode {db_mode!r} at P-dev")
    if report_profile not in _MART_BY_PROFILE:
        raise ValueError(
            f"gbp mart query: unknown report_profile {report_profile!r} "
            f"(known: {', '.join(sorted(_MART_BY_PROFILE))})"
        )
    import duckdb  # noqa: PLC0415

    mart, date_column, order_by = _MART_BY_PROFILE[report_profile]
    table = _get_mart_table(db_mode, project_id, mart)
    # The monthly mart is keyed on YYYY-MM: the window bounds are truncated to
    # the same width so a day-precision request does not silently exclude the
    # month it falls in.
    lower, upper = (date_from, date_to)
    if date_column == "month":
        lower, upper = date_from[:7], date_to[:7]
    sql = (
        f"SELECT * FROM {table} WHERE project_id = ? "
        f"AND {date_column} BETWEEN ? AND ? ORDER BY {order_by}"
    )
    con = duckdb.connect(_get_duckdb_path(), read_only=True)
    try:
        rel = con.execute(sql, [project_id, lower, upper])
        cols = [d[0] for d in rel.description]
        return [dict(zip(cols, row)) for row in rel.fetchall()], mart
    finally:
        con.close()


@mcp_app.tool()
def get_google_business_profile_report(
    project_id: str = "default",  # AD-14: identity resolved from OAuth 2.1 + PKCE
    report_profile: str = "location_daily",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Google Business Profile local-listing performance -- reads the marts.

    Report profiles:
      - 'location_daily' (default): 4 impression surfaces (desktop/mobile x
        maps/search), conversations, direction requests, call/website clicks,
        bookings, food orders, food menu clicks -- all ADDITIVE daily counts.
      - 'reviews': per-day review rollup. avg_star_rating and average_rating are
        NON-ADDITIVE levels (never sum them); new_reviews is the additive count.
      - 'search_keywords_monthly': MONTHLY keyword impressions. A row with
        is_thresholded = true carries a FLOOR, not a count.

    Note: history is hard-capped at ~18 months and recent days lag ~3-7 days.

    Returns the canonical AD-1 envelope via structuredContent.

    Parameters:
        project_id: Project identifier (default: 'default', AD-14 placeholder).
        report_profile: 'location_daily' | 'reviews' | 'search_keywords_monthly'.
        date_from: Start date ISO-8601. Defaults to 90 days ago.
        date_to: End date ISO-8601. Defaults to yesterday.
    """
    from datetime import date, timedelta  # noqa: PLC0415

    if not date_to:
        date_to = (date.today() - timedelta(days=1)).isoformat()
    if not date_from:
        date_from = (date.today() - timedelta(days=90)).isoformat()

    try:
        rows, mart = _query_mart(date_from, date_to, project_id, report_profile)
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
                 "source_field": mart, "pull_id": None}, "alerts": []},
        "data": {"project_id": project_id, "report_profile": report_profile,
                 "date_from": date_from, "date_to": date_to, "rows": rows},
    }


# ---------------------------------------------------------------------------
# Register this module's raw table name with core.verification.
# ---------------------------------------------------------------------------
try:
    from core.verification import register_raw_table_name as _register_raw  # noqa: PLC0415

    _register_raw("raw_gbp_location_daily", provider="google-business-profile")
    _register_raw(_RAW_REVIEW_TABLE, provider="google-business-profile")
    _register_raw(_RAW_KEYWORD_TABLE, provider="google-business-profile")
except Exception:
    pass  # best-effort
