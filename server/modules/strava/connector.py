"""Strava connector -- Clubs domain (competitive intelligence on clubs).

Exposes a ``mcp_app: FastMCP`` instance the core loader mounts under the
``strava`` namespace (AD-2). Built to the epic-25 industrial standard:
generated api_catalog.json (swagger transcription), status-keyed error_map
(Strava publishes no stable sub-codes), declared single-level club topology.

THE central design fact: Strava's Clubs API has NO HISTORY. ``GET /clubs/{id}``
returns only the CURRENT value; there is no date parameter and no time-series
endpoint. The connector OWNS the series by snapshotting daily into a dated fact
(raw_strava_club_daily, one row per club per snapshot date). Pre-connection
history is unrecoverable. ``member_count`` / ``following_count`` are
NON-ADDITIVE point-in-time LEVELS (aggregation_rule=latest): read as last value
in a window, NEVER summed -- they land in the DEDICATED mart
fact_strava_club_snapshot, not the additive cross-source fact_daily_kpi (the GSC
average_position precedent).

Competitor boundary: ``GET /clubs/{id}`` does NOT require membership -> any
PUBLIC club is snapshotable by id. Private/absent clubs return 404 and are
SKIPPED with an alert (never a fabricated zero). There is no club search
endpoint, so competitor ids are per-project config; own club ids come from
``GET /athlete/clubs`` discovery. members/admins/activities are membership-gated
(own clubs only) and anonymized.

# AD-12: MCP server reads the fact_strava_club_snapshot mart only -- no raw_*.
# AD-3:  OAuth token from Nango immediately before use -- never stored/logged.
#        scope 'read' (covers club feeds). Access token 6h; refresh rotates.
# AD-7:  pull_id minted by the core scheduler and passed into pull().
# AD-2:  field renames driven by the manifest mappings -- no hardcoded field list.
# AD-4:  member_count/following_count non-additive (latest), landed raw per row.
# AI-03: ASCII-only stdout/log strings.
#
# API facts (VERIFIED 2026-07-21 against https://developers.strava.com/swagger/swagger.json):
#   - GET https://www.strava.com/api/v3/clubs/{id} -> DetailedClub (member_count,
#     following_count, name, sport_type, city/state/country, private, verified, url).
#     NO membership required for a PUBLIC club; private/absent -> 404.
#   - GET https://www.strava.com/api/v3/athlete/clubs -> SummaryClub[] (own clubs).
#   - Error body = Fault { message, errors[]:{resource, field, code} }; 'code' is a
#     free-form string, so error_map is keyed on HTTP status alone.
#   - Rate limit: 100 reads/15min, 1000/day; 429 (sometimes 403 code='exceeded') on breach.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Module-level FastMCP instance -- the public surface the loader mounts.
mcp_app = FastMCP("strava")

# Base URL for the Strava API v3.
STRAVA_API_BASE = "https://www.strava.com/api/v3"

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
    """Return the manifest's ``error_map`` (HTTP-status keyed; see _error_map_note)."""
    return _load_manifest().get("error_map") or {}


# ---------------------------------------------------------------------------
# transform() -- manifest-driven canonical field mapping (AD-2)
# ---------------------------------------------------------------------------

# DetailedClub fields cataloged as planned/media/flags that the v1 snapshot
# profiles do NOT store. Dropped defensively so a full DetailedClub payload
# reduces to the canonical snapshot row.
_DROP_FIELDS: set[str] = {
    "resource_state",
    "featured",
    "membership",
    "member",
    "admin",
    "owner",
    "club_type",
    "description",
    "activity_types",
    "cover_photo",
    "cover_photo_small",
    "profile",
    "profile_medium",
    "post_count",
    "owner_id",
}


def transform(raw_rows: list[dict]) -> list[dict]:
    """Map raw DetailedClub fields to canonical names using the manifest mappings.

    AD-2: renames driven by canonical_metric_mapping + canonical_dimension_mapping
    (id -> club_id, name -> club_name, city -> club_city, state -> club_state,
    country -> club_country, private -> is_private, verified -> is_verified,
    url -> club_url). Fields absent from both mappings pass through unchanged
    (pull_id, connector, date, is_own_club, sport_type, member_count,
    following_count). Planned/media/flag fields (_DROP_FIELDS) are never stored.
    """
    manifest = _load_manifest()

    rename_map: dict[str, str] = {}
    for src, val in manifest.get("canonical_metric_mapping", {}).items():
        if isinstance(val, str):
            rename_map[src] = val
        elif isinstance(val, dict):
            rename_map[src] = val.get("canonical", src)
    rename_map.update(manifest.get("canonical_dimension_mapping", {}))

    result: list[dict] = []
    for row in raw_rows:
        canonical: dict = {}
        for key, value in row.items():
            if key in _DROP_FIELDS:
                continue
            canonical[rename_map.get(key, key)] = value
        result.append(canonical)
    return result


# ---------------------------------------------------------------------------
# Raw landing (wide, source-field column names -- dbt reads last-value).
# ---------------------------------------------------------------------------

_RAW_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_strava_club_daily (
    snapshot_date    VARCHAR,
    club_id          VARCHAR,
    club_name        VARCHAR,
    sport_type       VARCHAR,
    club_city        VARCHAR,
    club_state       VARCHAR,
    club_country     VARCHAR,
    is_private       BOOLEAN,
    is_verified      BOOLEAN,
    club_url         VARCHAR,
    is_own_club      BOOLEAN,
    member_count     BIGINT,
    following_count  BIGINT,
    pull_id          VARCHAR,
    loaded_at        VARCHAR,
    project_id       VARCHAR
)
"""

_RAW_INSERT_SQL = """
INSERT INTO raw_strava_club_daily
    (snapshot_date, club_id, club_name, sport_type, club_city, club_state,
     club_country, is_private, is_verified, club_url, is_own_club,
     member_count, following_count, pull_id, loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _to_int(value) -> int | None:
    """Coerce a member_count/following_count value to int; None stays None (AD-9)."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _insert_raw_rows(
    rows: list[dict],
    pull_id: str,
    project_id: str,
) -> int:
    """Insert canonical (post-transform) snapshot rows into raw_strava_club_daily."""
    db_mode = _get_db_mode()
    if db_mode != "duckdb":
        raise ValueError(
            f"_insert_raw_rows: unsupported db_mode {db_mode!r} at P-dev "
            "(BigQuery path not yet implemented)"
        )
    import duckdb  # noqa: PLC0415

    duckdb_path = _get_duckdb_path()
    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")

    con = duckdb.connect(duckdb_path)
    con.execute(_RAW_CREATE_DDL)
    values = [
        (
            r.get("date", ""),
            str(r.get("club_id", "")),
            r.get("club_name", ""),
            r.get("sport_type", ""),
            r.get("club_city", ""),
            r.get("club_state", ""),
            r.get("club_country", ""),
            bool(r.get("is_private")) if r.get("is_private") is not None else None,
            bool(r.get("is_verified")) if r.get("is_verified") is not None else None,
            r.get("club_url", ""),
            bool(r.get("is_own_club")) if r.get("is_own_club") is not None else None,
            _to_int(r.get("member_count")),
            _to_int(r.get("following_count")),
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
# HTTP helpers -- typed error handling with the two Strava shims.
# ---------------------------------------------------------------------------


def _fault_code(body) -> str | None:
    """Extract the first Fault errors[].code, if the body is a Strava Fault."""
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            code = errors[0].get("code")
            return str(code) if code is not None else None
    return None


def _raise_for_status(resp: httpx.Response, *, context: str) -> None:
    """Route a non-2xx Strava response to the correct typed error.

    Shims (see manifest _error_map_note):
      (a) 429, or 403 with Fault code 'exceeded' / X-RateLimit-Usage over limit
          -> core.quota.RateLimitError (breaker path).
      (b) every other non-2xx -> classify_http_error with the manifest error_map
          (Fault body preserved as evidence).

    The 404-skip shim for competitor_snapshot is handled by the CALLER (a 404 is
    an expected, non-fatal 'unreachable' outcome there), not here.
    """
    if resp.status_code < 400:
        return

    try:
        body = resp.json()
    except Exception:
        body = resp.text

    rate_limited = resp.status_code == 429 or (
        resp.status_code == 403 and _fault_code(body) == "exceeded"
    )
    if rate_limited:
        from core.quota import RateLimitError  # noqa: PLC0415

        retry_after_raw = resp.headers.get("Retry-After", "0")
        try:
            retry_after = int(retry_after_raw) or None
        except (ValueError, TypeError):
            retry_after = None
        raise RateLimitError("strava", retry_after)

    from core.pull_errors import classify_http_error  # noqa: PLC0415

    raise classify_http_error(resp.status_code, body, _load_error_map())


def _fetch_club(client: httpx.Client, token: str, club_id: str) -> dict | None:
    """GET /clubs/{id}. Returns the DetailedClub dict, or None if unreachable (404).

    A 404 means a private or absent PUBLIC club for a non-member -- an EXPECTED,
    non-fatal outcome for competitor tracking (skip + alert), NEVER a crash and
    NEVER a fabricated zero row.
    """
    resp = client.get(
        f"{STRAVA_API_BASE}/clubs/{club_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    if resp.status_code == 404:
        return None
    _raise_for_status(resp, context=f"clubs/{club_id}")
    return resp.json()


def _club_to_raw_row(
    club: dict,
    *,
    snapshot_date: str,
    is_own_club: bool,
    pull_id: str,
) -> dict:
    """Build a raw snapshot row (source-field names) from a DetailedClub object."""
    row = dict(club)
    row["date"] = snapshot_date
    row["is_own_club"] = is_own_club
    row["pull_id"] = pull_id
    row["connector"] = "strava"
    return row


# ---------------------------------------------------------------------------
# pull() -- snapshot extraction (called by the queue worker only, AD-12)
# ---------------------------------------------------------------------------


def _snapshot_clubs(
    connection_id: str,
    project_id: str,
    pull_id: str,
    club_ids: list[str],
    own_club_ids: set[str],
    date_to: str,
) -> dict:
    """Snapshot each club id via GET /clubs/{id} and land the dated rows.

    # AD-3: token obtained immediately before use; falls out of scope after.
    Unreachable ids (404) are collected as alerts, never zero-filled.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    token = nango_client.get_fresh_token(connection_id, provider="strava")

    raw_rows: list[dict] = []
    unreachable: list[str] = []

    with httpx.Client() as client:
        for club_id in club_ids:
            cid = str(club_id)
            club = _fetch_club(client, token, cid)
            if club is None:
                unreachable.append(cid)
                logger.info(
                    "strava_club_unreachable: pull_id=%s club_id=%s (404 private/absent, skipped)",
                    pull_id, cid,
                )
                continue
            raw_rows.append(
                _club_to_raw_row(
                    club,
                    snapshot_date=date_to,
                    is_own_club=cid in own_club_ids,
                    pull_id=pull_id,
                )
            )

    canonical_rows = transform(raw_rows)
    row_count = _insert_raw_rows(canonical_rows, pull_id, project_id)

    logger.info(
        "strava_pull_completed: pull_id=%s row_count=%d unreachable=%d",
        pull_id, row_count, len(unreachable),
    )
    return {
        "pull_id": pull_id,
        "row_count": row_count,
        "date_from": date_to,
        "date_to": date_to,
        "unreachable_club_ids": unreachable,
    }


def pull_competitor_snapshot(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    club_ids: list[str] | None = None,
    own_club_ids: list[str] | None = None,
) -> dict:
    """AI-58 dispatch: snapshot competitor + own club ids (grain club_id x date).

    Snapshot-only source: ``date_to`` is the snapshot date (Strava returns no
    history, so date_from/date_to collapse to a single point). ``club_ids`` is
    the per-project list (competitor ids + own ids); ``own_club_ids`` marks which
    are the connected athlete's own clubs (is_own_club stamp). NO env-var
    fallback (25.5+ standard): without a configured list the pull lands zero rows.
    """
    ids = [str(c) for c in (club_ids or [])]
    own = {str(c) for c in (own_club_ids or [])}
    snapshot_date = date_to or date.today().isoformat()
    return _snapshot_clubs(
        connection_id, project_id, pull_id, ids, own, snapshot_date
    )


def pull_own_club_full(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    club_ids: list[str] | None = None,
) -> dict:
    """AI-58 dispatch: snapshot the connected athlete's OWN clubs.

    Own clubs default to discovery (GET /athlete/clubs) when no explicit list is
    given. The membership-gated feeds (members/admins/activities) are anonymized
    and are NOT persisted in v1 (privacy-lean; exposed as on-demand reads) -- this
    profile persists the same club snapshot as competitor_snapshot, with every
    club marked is_own_club=true.
    """
    if club_ids is None:
        discovered = discover_accounts(connection_id)
        ids = [str(acc["id"]) for acc in discovered]
    else:
        ids = [str(c) for c in club_ids]
    own = set(ids)
    snapshot_date = date_to or date.today().isoformat()
    return _snapshot_clubs(
        connection_id, project_id, pull_id, ids, own, snapshot_date
    )


def pull(
    connection_id: str,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    club_ids: list[str] | None = None,
    own_club_ids: list[str] | None = None,
) -> dict:
    """Default pull() = competitor_snapshot (AI-58 profile-less default dispatch)."""
    return pull_competitor_snapshot(
        connection_id, date_from, date_to, project_id, pull_id,
        club_ids=club_ids, own_club_ids=own_club_ids,
    )


# ---------------------------------------------------------------------------
# Account topology discovery (playbook section 5) -- own clubs only.
# ---------------------------------------------------------------------------


def discover_accounts(connection_id: str) -> list[dict]:
    """List the connected athlete's OWN clubs (GET /athlete/clubs).

    Single-level topology (selection_level 'club'). Returns the generic hierarchy
    core's topology flow consumes:

        [{"id": "<club_id>", "label": "<club name>"}, ...]

    COMPETITOR clubs are NOT discoverable (Strava exposes no club search/list
    endpoint) -- they are supplied as per-project config, not through this call.

    Raises:
        core.quota.RateLimitError on 429/403-exceeded (breaker path);
        a typed core.pull_errors.ConnectorError on any other non-2xx.
    """
    from core import nango_client  # noqa: PLC0415 -- AD-2: import at call time

    token = nango_client.get_fresh_token(connection_id, provider="strava")

    accounts: list[dict] = []
    with httpx.Client() as client:
        page = 1
        while True:
            resp = client.get(
                f"{STRAVA_API_BASE}/athlete/clubs",
                headers={"Authorization": f"Bearer {token}"},
                params={"page": page, "per_page": 200},
                timeout=30.0,
            )
            _raise_for_status(resp, context="athlete/clubs")
            batch = resp.json() or []
            for club in batch:
                club_id = club.get("id")
                if club_id is None:
                    continue
                accounts.append(
                    {"id": str(club_id), "label": club.get("name") or str(club_id)}
                )
            if len(batch) < 200:
                break
            page += 1
    return accounts


# ---------------------------------------------------------------------------
# MCP tool -- reads from fact_strava_club_snapshot mart (AD-12).
# ---------------------------------------------------------------------------


def _get_mart_table(db_mode: str) -> str:
    """Fully-qualified dedicated snapshot mart reference per engine."""
    if db_mode == "duckdb":
        from core import warehouse_tenancy  # noqa: PLC0415

        return f"{warehouse_tenancy.mart_prefix(None)}fact_strava_club_snapshot"
    dataset = os.environ.get("BQ_MARTS_DATASET", "marts")
    gcp_project = os.environ.get("GCP_PROJECT", "")
    prefix = f"{gcp_project}.{dataset}" if gcp_project else dataset
    return f"{prefix}.fact_strava_club_snapshot"


# SQL body -- user-supplied values NEVER enter the string (parameterized).
_MART_QUERY = """
    SELECT
        club_id,
        club_name,
        is_own_club,
        snapshot_date,
        member_count,
        following_count,
        pull_id,
        loaded_at AS freshness
    FROM {table}
    WHERE project_id = ?
      AND snapshot_date BETWEEN ? AND ?
    ORDER BY snapshot_date, club_id
"""


def _query_mart(date_from: str, date_to: str, project_id: str) -> list[dict]:
    """Query the dedicated fact_strava_club_snapshot mart (DuckDB, F-01)."""
    db_mode = _get_db_mode()
    if db_mode != "duckdb":
        raise ValueError(f"strava mart query: unsupported db_mode {db_mode!r} at P-dev")

    import duckdb  # noqa: PLC0415

    table = _get_mart_table(db_mode)
    sql = _MART_QUERY.format(table=table)
    con = duckdb.connect(_get_duckdb_path(), read_only=True)
    try:
        rel = con.execute(sql, [project_id, date_from, date_to])
        cols = [d[0] for d in rel.description]
        return [dict(zip(cols, row)) for row in rel.fetchall()]
    finally:
        con.close()


@mcp_app.tool()
def get_strava_report(
    project_id: str = "default",  # AD-14: identity resolved from OAuth 2.1 + PKCE
    report_profile: str = "competitor_snapshot",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Strava Club snapshot -- reads from fact_strava_club_snapshot mart.

    Report profile: competitor_snapshot (own + competitor public clubs).
    Metrics: member_count, following_count -- NON-ADDITIVE point-in-time levels
    (aggregation_rule=latest). The trend (member growth) exists only from the
    first connection date onward; Strava returns no history.

    Returns the canonical AD-1 envelope via structuredContent. Text channel is
    the lean LLM summary (<=30 lines).

    Parameters:
        project_id: Project identifier (default: 'default', AD-14 placeholder).
        report_profile: 'competitor_snapshot' or 'own_club_full'.
        date_from: Start date ISO-8601. Defaults to 90 days ago.
        date_to: End date ISO-8601. Defaults to today (latest snapshot).
    """
    from datetime import timedelta  # noqa: PLC0415

    if not date_to:
        date_to = date.today().isoformat()
    if not date_from:
        date_from = (date.today() - timedelta(days=90)).isoformat()

    try:
        rows = _query_mart(date_from, date_to, project_id)
    except Exception as exc:
        return {
            "schema_version": "1",
            "meta": {
                "freshness": None,
                "provenance": None,
                "alerts": [{"level": "error", "message": str(exc)}],
            },
            "data": {
                "project_id": project_id,
                "report_profile": report_profile,
                "date_from": date_from,
                "date_to": date_to,
                "clubs": [],
            },
        }

    pull_ids = {r["pull_id"] for r in rows if r.get("pull_id")}
    freshness_values = [r["freshness"] for r in rows if r.get("freshness")]
    latest_pull_id = max(pull_ids) if pull_ids else None
    latest_freshness = max(freshness_values) if freshness_values else None

    provenance = (
        {
            "source_system": "strava",
            "source_field": "fact_strava_club_snapshot",
            "pull_id": latest_pull_id,
        }
        if latest_pull_id is not None
        else None
    )

    return {
        "schema_version": "1",
        "meta": {
            "freshness": latest_freshness,
            "provenance": provenance,
            "alerts": [],
        },
        "data": {
            "project_id": project_id,
            "report_profile": report_profile,
            "date_from": date_from,
            "date_to": date_to,
            "clubs": rows,
        },
    }


# ---------------------------------------------------------------------------
# Register this module's raw table name with core.verification.
# module->core direction is allowed by AD-2 (only core->modules is forbidden).
# ---------------------------------------------------------------------------
try:
    from core.verification import register_raw_table_name as _register_raw  # noqa: PLC0415

    _register_raw("raw_strava_club_daily", provider="strava")
except Exception:
    pass  # best-effort; verification will log a warning if table name is missing
