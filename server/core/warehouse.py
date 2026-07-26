"""toorow — warehouse read layer (Story 1.5, T2).

Reads from the ``fact_daily_kpi`` mart (BigQuery or local DuckDB).

# AD-12: MCP server reads marts only — never raw_* tables, CSV, or direct APIs.
# AD-2: This module is source-agnostic — no module-specific strings here.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time as _time_module
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


class WarehouseUnavailable(RuntimeError):
    """The warehouse could not be read (Story 22.3).

    Raised by reads that MUST NOT degrade to a silent [] -- e.g. the plan
    [Unmapped Actuals] perimeter, where an empty result would falsely claim
    "100 % du réel est mappé". Callers surface it as an honest 5xx/unavailable
    error, never a fabricated empty perimeter (story rule, AD-9).
    """


# ---------------------------------------------------------------------------
# Environment-driven connection config (same pattern as connector.py, T2.3)
# TOOROW_DB_MODE       = "duckdb" (default) | "bigquery"
# TOOROW_DUCKDB_PATH   = path to local .duckdb (duckdb mode)
# GCP_PROJECT             = GCP project ID (bigquery mode only)
# ---------------------------------------------------------------------------

_DB_MODE_ENV = "TOOROW_DB_MODE"
_DUCKDB_PATH_ENV = "TOOROW_DUCKDB_PATH"
_GCP_PROJECT_ENV = "GCP_PROJECT"


def _db_mode() -> str:
    return os.environ.get(_DB_MODE_ENV, "duckdb")


def _duckdb_path() -> str:
    return os.environ.get(_DUCKDB_PATH_ENV, "")


def _gcp_project() -> str:
    return os.environ.get(_GCP_PROJECT_ENV, "")


def _duckdb_mart_prefix(project_id: str | None = None) -> str:
    """DuckDB marts schema prefix -- delegated to the single naming point (24.1).

    Flag OFF (default): exact legacy ``main_marts.``. Flag ON: the org schema
    of *project_id* (``org_<wslug>_marts.``), legacy + WARNING when unresolvable.
    """
    from core import warehouse_tenancy  # noqa: PLC0415

    return warehouse_tenancy.mart_prefix(project_id)


# ---------------------------------------------------------------------------
# Composite sub-dimension splits (Story 8.11, R5) — source-agnostic helpers
# ---------------------------------------------------------------------------
#
# A composite breakdown encodes a dimension>sub-dimension split in the SAME
# long format using a '>' path separator, e.g. breakdown_dimension='country>device'
# with breakdown_value='FR>mobile'. No schema change: the existing query paths
# already filter on breakdown_dimension IN (...) / breakdown_value, so composite
# rows flow through query_daily_report and query_report unchanged — a caller
# selects them by passing the composite dimension name in `dimensions`.
#
# DOUBLE-COUNT SAFETY (see story DESIGN §2): a composite series independently
# totals the day exactly like each single-dimension series. compute_rollup /
# _rollup must therefore never receive a composite series mixed with its component
# single series for the same metric — callers keep dimensions distinct (reports
# name explicit dimension lists; the widget pins one total series). The dbt
# reconciliation test proves composite totals == single-dim totals.

COMPOSITE_SEPARATOR = ">"


def is_composite_dimension(breakdown_dimension: str | None) -> bool:
    """Return True when *breakdown_dimension* is a composite split (contains '>')."""
    return bool(breakdown_dimension) and COMPOSITE_SEPARATOR in breakdown_dimension


def split_composite_value(
    breakdown_dimension: str | None, breakdown_value: str | None
) -> list[tuple[str, str]]:
    """Decompose a composite (dimension, value) into ordered (part_dim, part_value) pairs.

    ``('country>device', 'FR>mobile')`` -> ``[('country', 'FR'), ('device', 'mobile')]``.
    Non-composite inputs return a single pair. Mismatched arities fall back to
    zipping the available parts (defensive; the mart never emits mismatches).
    """
    dim = breakdown_dimension or ""
    val = breakdown_value or ""
    dims = dim.split(COMPOSITE_SEPARATOR)
    vals = val.split(COMPOSITE_SEPARATOR)
    return list(zip(dims, vals))


# ---------------------------------------------------------------------------
# SQL builder — source-agnostic
# ---------------------------------------------------------------------------


def _build_query(
    schema_prefix: str,
    project_id: str,
    start_date: str,
    end_date: str,
    connectors: list[str] | None,
    *,
    placeholder: str = "?",
) -> tuple[str, list]:
    """Build a parameterized fact_daily_kpi query (review-1-5 F-01).

    User-supplied values NEVER enter the SQL string — they are bound as
    parameters. ``placeholder`` is "?" for DuckDB and "@pN" for BigQuery.

    # AD-12: reads marts only — never raw_* tables.
    """
    params: list = [project_id, start_date, end_date]

    def ph(i: int) -> str:
        return f"@p{i}" if placeholder != "?" else "?"

    base = f"""
    SELECT
        date,
        connector,
        metric,
        breakdown_dimension,
        breakdown_value,
        value,
        pull_id,
        loaded_at
    FROM {schema_prefix}fact_daily_kpi
    WHERE project_id = {ph(0)}
      AND date BETWEEN {ph(1)} AND {ph(2)}
    """
    if connectors:
        placeholders = ", ".join(ph(len(params) + i) for i in range(len(connectors)))
        base += f"  AND connector IN ({placeholders})\n"
        params.extend(connectors)
    base += "ORDER BY date, connector, metric, breakdown_dimension, breakdown_value"
    return base, params


# ---------------------------------------------------------------------------
# Backend implementations
# ---------------------------------------------------------------------------


def _duckdb_relation_exists(path: str, relation: str) -> bool:
    """Return True iff *relation* exists in the DuckDB file at *path* (any schema).

    Story 22.3 review F-4: used by query_campaign_spend to distinguish "marts not
    seeded" (relation absent -> WarehouseUnavailable, never a silent []) from a
    genuinely empty-but-present fact table. Any read error is treated as "absent"
    (fail-closed towards the honest error) rather than silently swallowed.
    """
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(path, read_only=True)
    try:
        found = con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = ? LIMIT 1",
            [relation],
        ).fetchone()
        return found is not None
    except Exception:  # noqa: BLE001 -- unreadable relation catalog => treat as absent
        return False
    finally:
        con.close()


def _query_duckdb(sql: str, params: list) -> list[dict]:
    import duckdb  # noqa: PLC0415

    path = _duckdb_path()
    if not path or not os.path.exists(path):
        logger.warning(
            json.dumps(
                {
                    "event": "warehouse_not_ready",
                    "message": "marts not populated — run Story 1.4 seed first",
                }
            )
        )
        return []
    con = duckdb.connect(path, read_only=True)
    try:
        rel = con.execute(sql, params)
        cols = [d[0] for d in rel.description]
        return [dict(zip(cols, row)) for row in rel.fetchall()]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Story 19.2 (CAP-22 / AD-22) -- read-through cache routing (interposed BELOW).
#
# The cache is a SEPARATE ephemeral DuckDB file (cache_warehouse.duckdb, built by
# Story 19.1's cache_warehouse.rebuild_cache) holding a window-scoped, project-
# scoped snapshot of the SAME marts. This section interposes the cache UNDER the
# public query_* functions WITHOUT changing a single signature (AD-2-style: the
# central entry point changes below, rollup/cards/reports/main.py do not move).
#
# The one guarantee of invariant (a) "memes chiffres": the cache is queried with
# the SAME _build_* builder as the origin (same placeholders, same filters, same
# ORDER BY) and the SAME per-function post-processing -- a single jeu de builders,
# two files. Only the ``schema_prefix`` differs (origin marts live under
# ``main_marts.``; the cache file materialises them at the root schema).
#
# A cache-hit requires ALL of:
#   (1) relation in the manifest allowlist,
#   (2) the ACTUALLY-queried window (already widened by _widen_to_prior) is a
#       subset of [min_date, max_date] in the manifest,
#   (3) the cache is fresh (cache_built_at >= last expected nightly, and within a
#       prudent configurable TTL) -- invariant (c), never frais mensonger,
#   (4) every requested project_id is covered by the manifest (AD-5).
# Otherwise: transparent fallthrough to the unchanged origin path.
#
# Any cache error (absent / corrupt / locked file) is caught -> fallthrough +
# structured warning; no read path ever raises because of the cache (invariant f).
# ---------------------------------------------------------------------------

_CACHE_ENABLED_ENV = "TOOROW_CACHE_ENABLED"
_CACHE_PATH_ENV = "TOOROW_CACHE_PATH"
_DEFAULT_CACHE_PATH = "cache_warehouse.duckdb"

# Freshness (invariant c). Primary rule: the cache must be at least as recent as
# the LAST EXPECTED NIGHTLY -- the most recent occurrence of the scheduler's fire
# time (SCHEDULER_NIGHTLY_HOUR:MINUTE in SCHEDULER_TIMEZONE) at-or-before "now".
# If a nightly has fired since the cache was built, the marts may have moved and a
# non-rebuilt cache would serve frais mensonger -> bypass.
#
# Belt-and-suspenders TTL (AD-9): even if the nightly anchor is somehow
# indeterminable (tz db missing, malformed env), a prudent configurable ceiling
# TOOROW_CACHE_TTL_SECONDS (default 26h > a 24h nightly cadence + build slack)
# bypasses a too-old cache. Both bounds are testable with an injected ``now``.
_CACHE_TTL_SECONDS_ENV = "TOOROW_CACHE_TTL_SECONDS"
_DEFAULT_CACHE_TTL_SECONDS = 26 * 3600  # 26h -- one nightly cadence + generous slack


def _cache_enabled() -> bool:
    return os.environ.get(_CACHE_ENABLED_ENV, "false").lower() == "true"


def _cache_path() -> str:
    return os.environ.get(_CACHE_PATH_ENV, _DEFAULT_CACHE_PATH)


def _cache_ttl_seconds() -> float:
    try:
        val = float(os.environ.get(_CACHE_TTL_SECONDS_ENV, str(_DEFAULT_CACHE_TTL_SECONDS)))
    except (TypeError, ValueError):
        return float(_DEFAULT_CACHE_TTL_SECONDS)
    return val if val > 0 else float(_DEFAULT_CACHE_TTL_SECONDS)


# ---------------------------------------------------------------------------
# In-memory hit/miss counters (AD-13 / NFR7). Simple, process-local, consultable
# by Story 19.3 via get_cache_stats(); reset via reset_cache_stats() in tests.
# Decisions: hit | miss-relation | miss-window | miss-project | bypass-stale |
# fallthrough-error | disabled. Each decision also emits a structured log line
# carrying the decision + latency_ms so the scan-economy can be measured.
# ---------------------------------------------------------------------------

_CACHE_STATS: dict[str, int] = {}
# F-3 (19.2 review): Cloud Run serves concurrent requests; the counter dict must be
# mutated atomically to avoid lost-update races. A module-level Lock is minimal and
# correct: _record_cache_decision is called only from _query_duckdb_routed (I/O
# bound, lock held for a dict lookup + int increment -- negligible contention).
_CACHE_STATS_LOCK = threading.Lock()


def get_cache_stats() -> dict[str, int]:
    """Return a copy of the in-memory hit/miss decision counters (AD-13, 19.3).

    Keys are the decision labels emitted by the router (``hit``, ``miss-relation``,
    ``miss-window``, ``miss-project``, ``bypass-stale``, ``fallthrough-error``,
    ``disabled``). Process-local and best-effort -- consulted by 19.3's health
    surface, not a durable metric store.
    """
    with _CACHE_STATS_LOCK:
        return dict(_CACHE_STATS)


def reset_cache_stats() -> None:
    """Clear the in-memory cache decision counters (test seam / 19.3 manual reset)."""
    with _CACHE_STATS_LOCK:
        _CACHE_STATS.clear()


def _record_cache_decision(decision: str, relation: str, latency_ms: float) -> None:
    """Bump the counter for *decision* and emit one structured trace line (AD-13/NFR7)."""
    with _CACHE_STATS_LOCK:
        _CACHE_STATS[decision] = _CACHE_STATS.get(decision, 0) + 1
    logger.info(
        json.dumps(
            {
                "event": "cache_route_decision",
                "decision": decision,
                "relation": relation,
                "latency_ms": round(latency_ms, 3),
            }
        )
    )


def _last_expected_nightly(now: datetime | None = None) -> datetime | None:
    """Return the most recent expected nightly fire instant at-or-before *now* (UTC).

    Anchored on the scheduler's own schedule (SCHEDULER_NIGHTLY_HOUR / _MINUTE in
    SCHEDULER_TIMEZONE, mirroring scheduler._scheduler_loop). This is the honest,
    testable definition of "le dernier nightly attendu" (invariant c): if the cache
    was built BEFORE this instant, a nightly has since run and the cache is stale.

    Returns None when the anchor cannot be computed (missing tz db / malformed env)
    so the caller falls back to the TTL bound alone (never frais mensonger).
    """
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        tz_name = os.environ.get("SCHEDULER_TIMEZONE", "Europe/Paris")
        tz = ZoneInfo(tz_name)
        hour = int(os.environ.get("SCHEDULER_NIGHTLY_HOUR", "2"))
        minute = int(os.environ.get("SCHEDULER_NIGHTLY_MINUTE", "0"))
        ref = (now or datetime.now(tz=timezone.utc)).astimezone(tz)
        candidate = ref.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate > ref:
            # Today's fire time has not arrived yet -> last nightly was yesterday's.
            candidate = candidate - timedelta(days=1)
        return candidate.astimezone(timezone.utc)
    except Exception as exc:  # noqa: BLE001 -- indeterminable anchor => TTL-only freshness
        logger.debug("warehouse: nightly_anchor_indeterminable: %s", exc)
        return None


def _parse_iso_dt(value: str | None) -> datetime | None:
    """Parse an ISO-8601 instant into an aware UTC datetime, or None on failure."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


_CACHE_BUILT_AT_FUTURE_TOLERANCE_S = 120  # spec F1: up to 120 s clock skew tolerated


def _cache_is_fresh(cache_built_at: str | None, *, now: datetime | None = None) -> bool:
    """Return True iff the cache is fresh enough to serve (invariant c, AD-9).

    Three independent bounds, ALL must hold:
      * future-guard (spec F1): cache_built_at must NOT be more than 120 s in the
        future (shifted clock / tampered manifest). Fail-closed -> bypass. Up to
        120 s of clock skew is tolerated; beyond that the manifest is rejected.
      * nightly anchor: cache_built_at >= last expected nightly (when determinable);
      * TTL ceiling:   now - cache_built_at <= TOOROW_CACHE_TTL_SECONDS.

    A None/unparseable cache_built_at is treated as NOT fresh (fail closed -> bypass).
    """
    built = _parse_iso_dt(cache_built_at)
    if built is None:
        return False
    ref_now = (now or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)

    # spec F1: future-timestamp guard. A cache_built_at more than 120 s in the
    # future passes both normal TTL and nightly-anchor checks (the nightly anchor
    # is in the past, the age is negative). Reject it explicitly -- fail closed.
    if (built - ref_now).total_seconds() > _CACHE_BUILT_AT_FUTURE_TOLERANCE_S:
        return False

    # TTL ceiling (belt-and-suspenders / indeterminable-anchor fallback).
    if (ref_now - built).total_seconds() > _cache_ttl_seconds():
        return False

    # Nightly anchor (primary honest rule).
    anchor = _last_expected_nightly(ref_now)
    if anchor is not None and built < anchor:
        return False
    return True


def _window_in_cache(start_date: str, end_date: str, manifest: dict) -> bool:
    """Return True iff [start_date, end_date] is a subset of the cache window.

    The *effective* (already _widen_to_prior-widened) window is passed by the
    caller so a delta query that reaches into the prior period is correctly judged
    a miss when that prior period falls outside the cache (never a truncated hit).
    """
    cmin = manifest.get("min_date")
    cmax = manifest.get("max_date")
    if not cmin or not cmax:
        return False
    # ISO date strings compare lexicographically == chronologically.
    return cmin <= start_date and end_date <= cmax


def _cache_decision(
    relation: str,
    project_ids: list[str],
    start_date: str,
    end_date: str,
    manifest: dict,
    *,
    now: datetime | None = None,
) -> str | None:
    """Return None on a cache-HIT, else a miss-reason label (19.2 decision core).

    Order of checks is deliberate so the emitted reason is the most specific:
    schema_version -> relation -> project -> window -> freshness.

    spec F2: schema_version is validated FIRST (before relation/window checks).
    An absent or mismatched schema_version means the manifest is not understood by
    this router version -> ``miss-schema`` -> fallthrough. Fail-closed: a manifest
    from a future/past builder never silently returns a hit.
    """
    from core import cache_warehouse  # noqa: PLC0415

    sv = manifest.get("schema_version")
    if sv is None or sv != cache_warehouse.CACHE_SCHEMA_VERSION:
        return "miss-schema"
    if relation not in (manifest.get("tables") or []):
        return "miss-relation"
    covered = set(manifest.get("project_ids") or [])
    if not covered or any(p not in covered for p in project_ids):
        return "miss-project"
    if not _window_in_cache(start_date, end_date, manifest):
        return "miss-window"
    if not _cache_is_fresh(manifest.get("cache_built_at"), now=now):
        return "bypass-stale"
    return None


# ---------------------------------------------------------------------------
# perf F-1/F-2 (19.4 review) — In-memory manifest cache.
#
# read_manifest opens a DuckDB connection on EVERY routed request (hit AND miss),
# adding 2 open/close round-trips per query on the hot path. This dict caches the
# last-read manifest per cache_path, keyed by the file's mtime_ns so a rebuild
# (which uses os.replace -> new inode / new mtime) is detected immediately.
#
# Layout: {cache_path: (mtime_ns: int, manifest: dict|None)}
# Protected by a module-level lock (same pattern as _CACHE_STATS_LOCK).
# A missing file returns None WITHOUT caching (the file may appear later).
# ---------------------------------------------------------------------------

_MANIFEST_MEM_CACHE: dict[str, tuple[int, dict | None]] = {}
_MANIFEST_MEM_CACHE_LOCK = threading.Lock()


def _read_manifest_cached(cache_path: str) -> dict | None:
    """Return the manifest for *cache_path*, served from memory when mtime is unchanged.

    perf F-1/F-2: avoids opening a DuckDB connection on every routed request.
    Invalidation: os.stat(cache_path).st_mtime_ns is compared on every call;
    a rebuild that uses os.replace() changes the inode/mtime, so the next call
    re-reads from disk and updates the in-memory entry.

    A missing file returns None without caching (the file may appear between calls).

    Note (AI-53 honesty): on a cache HIT the I/O cost is now 1 os.stat() call (to
    read mtime_ns) + 0 DuckDB connections (manifest from memory). The DuckDB
    connection for the actual data read (_read_from_cache) is still 1 per hit.
    So "scan BQ = 0 on hit" != "I/O = 0": 1 stat + 1 DuckDB data read per hit,
    but the manifest DuckDB open (the hot-path overhead) is eliminated.
    """
    from core import cache_warehouse  # noqa: PLC0415

    try:
        mtime_ns = os.stat(cache_path).st_mtime_ns
    except FileNotFoundError:
        # File absent -> None, not cached (may appear after a successful rebuild).
        return None
    except OSError:
        # Unreadable for another reason -> delegate to the real read_manifest.
        return cache_warehouse.read_manifest(cache_path)

    with _MANIFEST_MEM_CACHE_LOCK:
        cached = _MANIFEST_MEM_CACHE.get(cache_path)
        if cached is not None and cached[0] == mtime_ns:
            return cached[1]

    # Cache miss or mtime changed -> read from disk.
    manifest = cache_warehouse.read_manifest(cache_path)

    with _MANIFEST_MEM_CACHE_LOCK:
        _MANIFEST_MEM_CACHE[cache_path] = (mtime_ns, manifest)

    return manifest


def _query_duckdb_routed(
    build_fn,
    *,
    relation: str,
    project_ids: list[str],
    start_date: str,
    end_date: str,
) -> list[dict]:
    """Run a DuckDB read, serving it from the cache on a hit else the origin.

    ``build_fn(schema_prefix)`` returns ``(sql, params)`` for the SAME builder the
    origin uses -- so the cache runs identical SQL, only the schema prefix differs
    (origin ``main_marts.`` vs cache root schema). This single seam is what proves
    invariant (a) "memes chiffres": one builder, two files.

    *start_date* / *end_date* MUST be the EFFECTIVE window actually queried (post
    _widen_to_prior). *project_ids* is every project the read scopes to (AD-5).

    Fallthrough is transparent and total: cache disabled, any miss, or ANY cache
    error routes to ``_query_duckdb`` (the unchanged origin path). Never raises
    because of the cache (invariant f).
    """
    # 24.1: org naming resolves per project; a multi-org project list cannot be
    # served by ONE schema prefix -- per-org routing is story 24.4's concern.
    # Today every routed read scopes to a single project (AD-5).
    origin_project = project_ids[0] if project_ids else None

    if not _cache_enabled():
        origin_prefix = _duckdb_mart_prefix(origin_project)
        sql, params = build_fn(origin_prefix)
        return _query_duckdb(sql, params)

    t0 = _time_module.perf_counter()
    try:
        cache_path = _cache_path()
        manifest = _read_manifest_cached(cache_path)
        if manifest is None:
            # Absent / corrupt / unreadable cache -> transparent fallthrough.
            _record_cache_decision(
                "fallthrough-error", relation, (_time_module.perf_counter() - t0) * 1000
            )
            sql, params = build_fn(_duckdb_mart_prefix(origin_project))
            return _query_duckdb(sql, params)

        miss = _cache_decision(
            relation, project_ids, start_date, end_date, manifest
        )
        if miss is None:
            # HIT: same builder, cache file (root schema, no main_marts. prefix).
            rows = _read_from_cache(build_fn, cache_path)
            _record_cache_decision("hit", relation, (_time_module.perf_counter() - t0) * 1000)
            return rows

        # MISS: transparent fallthrough to the origin path (unchanged).
        _record_cache_decision(miss, relation, (_time_module.perf_counter() - t0) * 1000)
    except Exception as exc:  # noqa: BLE001 -- invariant f: cache never breaks a read
        logger.warning(
            json.dumps(
                {
                    "event": "cache_route_error",
                    "relation": relation,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        )
        _record_cache_decision(
            "fallthrough-error", relation, (_time_module.perf_counter() - t0) * 1000
        )

    sql, params = build_fn(_duckdb_mart_prefix(origin_project))
    return _query_duckdb(sql, params)


def _read_from_cache(build_fn, cache_path: str) -> list[dict]:
    """Execute the cache read against *cache_path* using the root-schema builder.

    The cache file materialises marts at the root schema (Story 19.1 CREATE TABLE
    "<relation>"), so the SAME builder is invoked with an empty ``schema_prefix``.
    Opens the cache READ_ONLY (the read layer never writes the cache; AD-8).
    """
    import duckdb  # noqa: PLC0415

    sql, params = build_fn("")  # cache: root schema, identical placeholders/filters
    con = duckdb.connect(cache_path, read_only=True)
    try:
        rel = con.execute(sql, params)
        cols = [d[0] for d in rel.description]
        return [dict(zip(cols, row)) for row in rel.fetchall()]
    finally:
        con.close()


def _query_bigquery(sql: str, params: list) -> list[dict]:
    from google.cloud import bigquery  # noqa: PLC0415

    project = _gcp_project()
    client = bigquery.Client(project=project or None)
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter(f"p{i}", "STRING", v) for i, v in enumerate(params)
        ]
    )
    result = client.query(sql, job_config=job_config).result()
    cols = [f.name for f in result.schema]
    return [dict(zip(cols, row)) for row in result]


def _check_bigquery_mart(project_id: str) -> bool:
    """Return True if marts are accessible in BigQuery.

    The dataset id is resolved by the single naming point (24.1): legacy
    ``marts_<project_id>`` by default, ``org_<wslug>_marts`` under
    TOOROW_ORG_SCHEMAS=1 (org = the physical isolation boundary, epic 24).
    """
    try:
        from google.cloud import bigquery  # noqa: PLC0415

        from core import warehouse_tenancy  # noqa: PLC0415

        project = _gcp_project()
        client = bigquery.Client(project=project or None)
        dataset_id = warehouse_tenancy.bigquery_marts_dataset(project_id)
        client.get_dataset(dataset_id)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _build_asof_query(
    schema_prefix: str,
    project_id: str,
    start_date: str,
    end_date: str,
    connectors: list[str] | None,
    as_of_ts: str,
    *,
    placeholder: str = "?",
) -> tuple[str, list]:
    """Build a parameterized as-of fact_daily_kpi_all_pulls query (Story 4.6, AC4).

    Implements "latest pull within loaded_at <= as_of_ts wins" using ROW_NUMBER()
    window function in SQL — never in Python (HG-1: SQL window functions, not Python
    sorted/max/filter, handle the full grain correctly for multi-breakdown rows).

    The as_of_ts value is ALWAYS a bound parameter — never string-interpolated
    (HG-3: SQL injection safety; parameterized binding means injection strings are
    passed as literal data, not executable SQL).

    # Revision comparison: call get_daily_report_asof twice with different as_of values;
    # compare pull_ids. If pull_id A != pull_id B, the value was revised.
    # Pull comparison endpoint deferred to Story 6.x.
    """
    params: list = []

    def ph(i: int) -> str:
        return f"@p{i}" if placeholder != "?" else "?"

    # as_of_ts first (index 0), then project_id (1), start_date (2), end_date (3)
    params = [as_of_ts, project_id, start_date, end_date]

    connector_clause = ""
    if connectors:
        placeholders = ", ".join(ph(len(params) + i) for i in range(len(connectors)))
        connector_clause = f"  AND connector IN ({placeholders})\n"
        params.extend(connectors)

    sql = f"""
    WITH kpi_snapshot AS (
        SELECT *,
               ROW_NUMBER() OVER (
                   PARTITION BY project_id, date, connector, metric,
                                breakdown_dimension, breakdown_value
                   ORDER BY loaded_at DESC
               ) AS _rn
        FROM {schema_prefix}fact_daily_kpi_all_pulls
        WHERE loaded_at <= CAST({ph(0)} AS TIMESTAMP)
          AND project_id = {ph(1)}
          AND date BETWEEN {ph(2)} AND {ph(3)}
{connector_clause}    )
    SELECT
        date,
        connector,
        metric,
        breakdown_dimension,
        breakdown_value,
        value,
        pull_id,
        loaded_at
    FROM kpi_snapshot
    WHERE _rn = 1
    ORDER BY date, connector, metric, breakdown_dimension, breakdown_value
    """
    return sql, params


def get_daily_report_asof(
    project_id: str,
    start_date: str,
    end_date: str,
    connectors: list[str] | None,
    as_of_ts: str,
) -> list[dict]:
    """Query fact_daily_kpi_all_pulls as-of as_of_ts (Story 4.6, AC4).

    Returns values as they were known at as_of_ts — loaded_at <= as_of_ts,
    latest pull within that window wins (ROW_NUMBER recomputed as-of).

    HG-1: supersede logic is in SQL (ROW_NUMBER window function), never Python.
    HG-3: as_of_ts is a bound SQL parameter — never string-interpolated.

    # Revision comparison: call get_daily_report_asof twice with different as_of values;
    # compare pull_ids. If pull_id A != pull_id B, the value was revised.
    # Pull comparison endpoint deferred to Story 6.x.
    """
    mode = _db_mode()

    if mode == "duckdb":
        schema_prefix = _duckdb_mart_prefix(project_id)
        sql, params = _build_asof_query(
            schema_prefix, project_id, start_date, end_date, connectors, as_of_ts
        )
        return _query_duckdb(sql, params)

    elif mode == "bigquery":
        if not _check_bigquery_mart(project_id):
            logger.warning(
                json.dumps(
                    {
                        "event": "warehouse_not_ready",
                        "message": "marts not populated — run Story 1.4 seed first",
                    }
                )
            )
            return []
        # BigQuery supports QUALIFY natively; use subquery + WHERE _rn = 1 for
        # portability (same pattern as DuckDB). BigQuery uses @pN named parameters.
        sql, params = _build_asof_query(
            "", project_id, start_date, end_date, connectors, as_of_ts, placeholder="@"
        )
        return _query_bigquery(sql, params)

    else:
        raise ValueError(f"Unknown TOOROW_DB_MODE: {mode!r}")


# ---------------------------------------------------------------------------
# Story 6.1 — Report pack query path (AC3, AD-4 non-additive routing)
# ---------------------------------------------------------------------------


def _build_report_query(
    schema_prefix: str,
    project_id: str,
    connector: str,
    metrics: list[str],
    dimensions: list[str],
    start_date: str,
    end_date: str,
    *,
    placeholder: str = "?",
    connectors: list[str] | None = None,
) -> tuple[str, list]:
    """Build a parameterized fact_daily_kpi query for a report pack (Story 6.1).

    When ``connectors`` is provided (Story 6.3 cross-source), filters
    ``connector IN (connectors)`` instead of ``connector = <module>``.
    Filters ``metric IN report.metrics``,
    ``breakdown_dimension IN report.dimensions`` (excluding non-additive metrics —
    those are routed to semantic views by the caller), ``date BETWEEN ...`` and
    ``project_id = ...``. All user-influenced values are BOUND parameters
    (review-1-5 F-01 — never string-interpolated).
    """
    # Multi-connector path (Story 6.3): connectors list overrides single connector.
    effective_connectors: list[str] = connectors if connectors else [connector]

    # Build params and placeholders in SQL order (positional binding for DuckDB).
    # Order: project_id, then connectors (IN clause), then start_date, end_date,
    # then optional metrics, then optional dimensions.
    params: list = [project_id]

    def ph(i: int) -> str:
        return f"@p{i}" if placeholder != "?" else "?"

    conn_phs = ", ".join(ph(len(params) + i) for i in range(len(effective_connectors)))
    params.extend(effective_connectors)

    start_ph = ph(len(params))
    params.append(start_date)
    end_ph = ph(len(params))
    params.append(end_date)

    base = f"""
    SELECT
        date,
        connector,
        metric,
        breakdown_dimension,
        breakdown_value,
        value,
        pull_id,
        loaded_at
    FROM {schema_prefix}fact_daily_kpi
    WHERE project_id = {ph(0)}
      AND connector IN ({conn_phs})
      AND date BETWEEN {start_ph} AND {end_ph}
    """
    if metrics:
        m_ph = ", ".join(ph(len(params) + i) for i in range(len(metrics)))
        base += f"  AND metric IN ({m_ph})\n"
        params.extend(metrics)
    if dimensions:
        d_ph = ", ".join(ph(len(params) + i) for i in range(len(dimensions)))
        base += f"  AND breakdown_dimension IN ({d_ph})\n"
        params.extend(dimensions)
    base += "ORDER BY date, metric, breakdown_dimension, breakdown_value"
    return base, params


# ---------------------------------------------------------------------------
# spec F4 (19.4 review) — Metric → semantic view mapping (debt resolved)
#
# _build_semantic_view_query was building "semantic_" + metric.lower() for ALL
# metrics, including average_position -> "semantic_average_position" (INEXISTENT).
# The real view is "semantic_avg_position" (matches business_alerts.py:144).
#
# _SEMANTIC_VIEW_BY_METRIC is the single source of truth: default convention is
# "semantic_<metric>", with overrides for metrics whose dbt view name diverges.
# _SEMANTIC_COL_BY_METRIC maps metric -> column name inside the view (default:
# same as metric name).
#
# This constant mirrors business_alerts._SEMANTIC_VIEW_NAME / _SEMANTIC_COL_NAME
# (G-03 fix) and is used by BOTH _build_semantic_view_query AND the relation name
# passed to _query_duckdb_routed (query_report non-additive branch).
# ---------------------------------------------------------------------------

_SEMANTIC_VIEW_BY_METRIC: dict[str, str] = {
    # default: "semantic_" + metric  (no entry needed)
    # override: average_position lives in semantic_avg_position (not semantic_average_position)
    "average_position": "semantic_avg_position",
}

_SEMANTIC_COL_BY_METRIC: dict[str, str] = {
    # default: column name == metric name
    "average_position": "average_position",
}

# Evidence columns let the geographic semantic layer re-aggregate non-additive
# country rows after market grouping without averaging already-computed ratios.
_SEMANTIC_EVIDENCE_BY_METRIC: dict[str, tuple[str, ...]] = {
    "ctr": ("semantic_numerator", "semantic_denominator"),
    "roas": ("semantic_numerator", "semantic_denominator"),
    "cpa": ("semantic_numerator", "semantic_denominator"),
    "average_position": ("semantic_weight",),
}


def _semantic_view_name(metric: str) -> str:
    """Return the dbt view name (without schema prefix) for a non-additive metric."""
    metric_lc = metric.lower()
    return _SEMANTIC_VIEW_BY_METRIC.get(metric_lc, f"semantic_{metric_lc}")


def _semantic_col_name(metric: str) -> str:
    """Return the column name to SELECT from the semantic view for *metric*."""
    metric_lc = metric.lower()
    return _SEMANTIC_COL_BY_METRIC.get(metric_lc, metric_lc)


def _build_semantic_view_query(
    schema_prefix: str,
    project_id: str,
    connector: str,
    metric: str,
    dimensions: list[str],
    start_date: str,
    end_date: str,
    *,
    placeholder: str = "?",
) -> tuple[str, list]:
    """Build a parameterized semantic-view query for a non-additive metric (AD-4).

    View naming uses ``_SEMANTIC_VIEW_BY_METRIC`` (spec F4 debt resolution): the
    default convention is ``semantic_<metric>`` but ``average_position`` maps to
    ``semantic_avg_position`` (the real dbt view name, matching business_alerts.py
    G-03 fix). Column name is resolved via ``_SEMANTIC_COL_BY_METRIC``.

    ``metric`` is validated against the canonical dictionary before reaching here,
    so the view name is never attacker-controlled; date/project/connector are still
    bound as parameters (HG-3 injection safety).
    """
    view_suffix = _semantic_view_name(metric)
    view = f"{schema_prefix}{view_suffix}"
    col = _semantic_col_name(metric)
    evidence_sql = "".join(
        f"        {column},\n"
        for column in _SEMANTIC_EVIDENCE_BY_METRIC.get(metric.lower(), ())
    )

    def ph(i: int) -> str:
        return f"@p{i}" if placeholder != "?" else "?"

    params: list = [project_id, connector, start_date, end_date]
    sql = f"""
    SELECT
        date,
        connector,
        breakdown_dimension,
        breakdown_value,
        {col} AS value,
{evidence_sql}        pull_id
    FROM {view}
    WHERE project_id = {ph(0)}
      AND connector = {ph(1)}
      AND date BETWEEN {ph(2)} AND {ph(3)}
    """  # noqa: S608 — view from _SEMANTIC_VIEW_BY_METRIC (validated metric + hard-coded prefix)
    if dimensions:
        d_ph = ", ".join(ph(len(params) + i) for i in range(len(dimensions)))
        sql += f"  AND breakdown_dimension IN ({d_ph})\n"
        params.extend(dimensions)
    sql += "ORDER BY date, breakdown_dimension, breakdown_value"
    return sql, params


def query_report(
    project_id: str,
    connector: str,
    metrics: list[str],
    dimensions: list[str],
    start_date: str,
    end_date: str,
    *,
    connectors: list[str] | None = None,
    include_prior_period: bool = True,
) -> list[dict]:
    """Query the warehouse for a report pack, routing non-additive metrics (AD-4).

    Additive metrics are read from ``fact_daily_kpi``; non-additive metrics
    (those with a declared aggregation_rule in the canonical dictionary — e.g.
    ``average_position`` with ``impression_weighted``, or the ratio metrics) are
    routed to their ``semantic_<metric>`` view instead of being naively summed
    (AD-4). This is the ROUTING MECHANISM Story 6.2's GSC ``average_position``
    plugs into with zero tool changes.

    When ``connectors`` is provided (Story 6.3 cross-source reports), the additive
    query filters ``connector IN (connectors)`` and the non-additive semantic views
    are queried per-connector so each connector's weighted average is correct.
    When absent, behavior is identical to pre-6.3 (single connector = module_name).

    Returns a flat list of fact-shaped rows (date, connector, metric,
    breakdown_dimension, breakdown_value, value, pull_id, loaded_at). Rows sourced
    from semantic views carry the metric name and a null loaded_at.

    Never raises when the mart is not populated — returns [] with a structured
    warning (mirrors query_daily_report).
    """
    from core import report_dictionary  # noqa: PLC0415

    additive = [m for m in metrics if not report_dictionary.is_non_additive(m)]
    non_additive = [m for m in metrics if report_dictionary.is_non_additive(m)]

    # Effective connector list: multi-connector overrides single connector.
    effective_connectors: list[str] = connectors if connectors else None

    # G-06 fix: widen the fetch window to include the prior period so that
    # rollup._split_periods can populate delta/delta_pct. The caller is responsible
    # for filtering rows to the current window before putting them into
    # data.rows / summary tables (use rollup._split_periods at the call site).
    fetch_start = _widen_to_prior(start_date, end_date) if include_prior_period else start_date

    mode = _db_mode()
    if mode == "duckdb":
        schema_prefix = _duckdb_mart_prefix(project_id)
        placeholder = "?"
        runner = _query_duckdb
    elif mode == "bigquery":
        if not _check_bigquery_mart(project_id):
            logger.warning(
                json.dumps(
                    {
                        "event": "warehouse_not_ready",
                        "message": "marts not populated — run Story 1.4 seed first",
                    }
                )
            )
            return []
        schema_prefix = ""
        placeholder = "@"
        runner = _query_bigquery
    else:
        raise ValueError(f"Unknown TOOROW_DB_MODE: {mode!r}")

    rows: list[dict] = []
    # Story 19.2: in duckdb mode every read routes through the cache (same builder,
    # two files). The effective window is [fetch_start, end_date]. In bigquery mode
    # the cache is not involved (origin IS BigQuery) -- the original ``runner`` path
    # is preserved untouched.
    project_ids = [project_id]

    if additive:
        if mode == "duckdb":
            rows.extend(
                _query_duckdb_routed(
                    lambda prefix: _build_report_query(
                        prefix, project_id, connector, additive, dimensions,
                        fetch_start, end_date, connectors=effective_connectors,
                    ),
                    relation="fact_daily_kpi",
                    project_ids=project_ids,
                    start_date=fetch_start,
                    end_date=end_date,
                )
            )
        else:
            sql, params = _build_report_query(
                schema_prefix, project_id, connector, additive, dimensions,
                fetch_start, end_date, placeholder=placeholder,
                connectors=effective_connectors,
            )
            rows.extend(runner(sql, params))

    for metric in non_additive:
        # For non-additive metrics, query each connector's semantic view separately
        # so impression-weighted averages stay correct per connector (AD-4).
        conn_list = effective_connectors if effective_connectors else [connector]
        for conn in conn_list:
            if mode == "duckdb":
                # spec F4: relation name uses _semantic_view_name so average_position
                # routes to "semantic_avg_position" (the real view), not the
                # non-existent "semantic_average_position". Default-arg binding pins
                # the current (conn, metric) per iteration.
                _relation = _semantic_view_name(metric)

                def _semantic_build(prefix, _conn=conn, _metric=metric):
                    return _build_semantic_view_query(
                        prefix, project_id, _conn, _metric, dimensions,
                        fetch_start, end_date,
                    )

                semantic_rows = _query_duckdb_routed(
                    _semantic_build,
                    relation=_relation,
                    project_ids=project_ids,
                    start_date=fetch_start,
                    end_date=end_date,
                )
            else:
                sql, params = _build_semantic_view_query(
                    schema_prefix, project_id, conn, metric, dimensions,
                    fetch_start, end_date, placeholder=placeholder,
                )
                semantic_rows = runner(sql, params)
            for row in semantic_rows:
                row.setdefault("metric", metric)
                row.setdefault("loaded_at", None)
                rows.append(row)

    # Coerce date/datetime column values to ISO strings so the envelope is
    # JSON-serializable regardless of the mart's physical column types (DuckDB
    # returns native date/datetime objects; BigQuery returns strings).
    return [_json_safe_row(r) for r in rows]


def _build_composite_position_query(
    schema_prefix: str,
    project_id: str,
    composite_dim: str,
    start_date: str,
    end_date: str,
    connectors: list[str] | None,
    *,
    placeholder: str = "?",
) -> tuple[str, list]:
    """Build a parameterized query over ``semantic_avg_position_composite`` (Story 10.5 F-1).

    Reads the impression-weighted ``average_position`` at a COMPOSITE breakdown grain
    (e.g. ``'query>page'``) from the semantic composite VIEW. This is the ONLY warehouse
    read that surfaces composite ``average_position`` rows: ``fact_daily_kpi`` never
    carries ``average_position`` (AD-4 non-additive, ``test_composite_additive_only.sql``
    forbids it) and ``_build_semantic_view_query`` targets ``semantic_<metric>`` =
    ``semantic_average_position`` (marginal grains only), NOT the composite view.

    The view's weighted column is ``average_position`` -> aliased ``AS value`` and tagged
    ``metric = 'average_position'`` so callers can concatenate these rows straight into a
    fact-shaped row list. ``composite_dim`` is bound as a parameter (never interpolated);
    project/date/connectors are bound too (HG-3 injection safety). The view NAME is a
    hard-coded constant (never attacker-controlled).
    """
    view = f"{schema_prefix}semantic_avg_position_composite"

    def ph(i: int) -> str:
        return f"@p{i}" if placeholder != "?" else "?"

    # Order: project_id (0), composite_dim (1), start (2), end (3), then connectors.
    params: list = [project_id, composite_dim, start_date, end_date]
    sql = f"""
    SELECT
        date,
        connector,
        breakdown_dimension,
        breakdown_value,
        average_position AS value,
        pull_id,
        loaded_at
    FROM {view}
    WHERE project_id = {ph(0)}
      AND breakdown_dimension = {ph(1)}
      AND date BETWEEN {ph(2)} AND {ph(3)}
    """  # noqa: S608 — view name is a hard-coded constant; all values are bound params
    if connectors:
        c_ph = ", ".join(ph(len(params) + i) for i in range(len(connectors)))
        sql += f"  AND connector IN ({c_ph})\n"
        params.extend(connectors)
    sql += "ORDER BY date, breakdown_dimension, breakdown_value"
    return sql, params


def query_composite_positions(
    project_id: str,
    composite_dim: str,
    start_date: str,
    end_date: str,
    connectors: list[str] | None,
    *,
    include_prior_period: bool = True,
) -> list[dict]:
    """Query composite impression-weighted ``average_position`` rows (Story 10.5 F-1).

    Returns fact-shaped rows for the given composite grain (``composite_dim``, e.g.
    ``'query>page'``) sourced from ``semantic_avg_position_composite``:
    ``{date, connector, metric='average_position', breakdown_dimension, breakdown_value,
    value, pull_id, loaded_at}``. The value is the TRUE impression-weighted position per
    composite cell (computed in the view from staging), NEVER a naive mean/sum (AD-4).

    Callers concatenate these onto the ``fact_daily_kpi`` rows (which supply the matching
    composite ``impressions``) BEFORE ``rollup.split_periods`` so downstream detectors
    (e.g. cards._detect_cannibalisation) see the (query, page) position co-located with
    its impression weight.

    When ``include_prior_period=True`` (default) the fetch window is widened to the prior
    period of the same length — IDENTICAL to ``query_daily_report``/``query_report`` — so
    concatenated rows share the same window the caller then splits.

    Degrade-never-raise: returns ``[]`` with a structured warning when the view/mart is
    not populated (mirror of ``query_daily_report``). NEVER raises.
    """
    fetch_start = _widen_to_prior(start_date, end_date) if include_prior_period else start_date
    mode = _db_mode()

    if mode == "duckdb":
        # Story 19.2: route through the cache (same _build_composite_position_query).
        rows = _query_duckdb_routed(
            lambda prefix: _build_composite_position_query(
                prefix, project_id, composite_dim, fetch_start, end_date, connectors
            ),
            relation="semantic_avg_position_composite",
            project_ids=[project_id],
            start_date=fetch_start,
            end_date=end_date,
        )

    elif mode == "bigquery":
        if not _check_bigquery_mart(project_id):
            logger.warning(
                json.dumps(
                    {
                        "event": "warehouse_not_ready",
                        "message": "marts not populated — run Story 1.4 seed first",
                    }
                )
            )
            return []
        sql, params = _build_composite_position_query(
            "", project_id, composite_dim, fetch_start, end_date, connectors, placeholder="@"
        )
        rows = _query_bigquery(sql, params)

    else:
        raise ValueError(f"Unknown TOOROW_DB_MODE: {mode!r}")

    # Tag metric (the view carries no `metric` column) and coerce dates to ISO strings.
    out: list[dict] = []
    for row in rows:
        row.setdefault("metric", "average_position")
        row.setdefault("loaded_at", None)
        out.append(_json_safe_row(row))
    return out


def _json_safe_row(row: dict) -> dict:
    """Return *row* with date/datetime values coerced to ISO-8601 strings."""
    import datetime as _dt  # noqa: PLC0415

    out: dict = {}
    for key, val in row.items():
        if isinstance(val, (_dt.date, _dt.datetime)):
            out[key] = val.isoformat()
        else:
            out[key] = val
    return out


def _widen_to_prior(start_date: str, end_date: str) -> str:
    """Return the widened start date that covers one prior period of the same length.

    Prior period = [prior_start, start_date - 1 day] where the span equals
    (end_date - start_date + 1) days. Used by G-06 fix so _split_periods in
    rollup.py can bucket prior rows for delta computation.
    """
    from datetime import date as _date  # noqa: PLC0415
    from datetime import timedelta as _td

    try:
        start = _date.fromisoformat(start_date)
        end = _date.fromisoformat(end_date)
    except (ValueError, TypeError):
        return start_date
    span_days = (end - start).days + 1
    prior_start = start - _td(days=span_days)
    return prior_start.isoformat()


def _build_dedup_estimate_query(
    schema_prefix: str,
    project_id: str,
    start_date: str,
    end_date: str,
    *,
    placeholder: str = "?",
) -> tuple[str, list]:
    """Build a parameterized query over ``marts.dedup_estimate`` (Story 17.2 / 17.3).

    Returns all rows for a project within the given date window. The mart is a VIEW
    (materialized='view' in dbt), so every call reads current data. Grain:
    (project_id, date, channel_connector). Columns returned:
      date, channel_connector, claimed_conversions, verified_total, claimed_total,
      duplication_rate, deduplicated_contribution, verification_source_type,
      verification_source_id, lead_event_name, estimate_label, pull_id.

    AD-9: the mart's own ``estimate_label = 'estimation'`` column is propagated so
    every caller can surface it. Columns may be NULL when the source of truth is not
    configured (opt-in per project) or is 'stripe' v1 (BLOCKED 15.7). Project_id and
    dates are bound parameters -- never string-interpolated (HG-3).
    """
    def ph(i: int) -> str:
        return f"@p{i}" if placeholder != "?" else "?"

    params: list = [project_id, start_date, end_date]
    sql = f"""
    SELECT
        date,
        channel_connector,
        claimed_conversions,
        verified_total,
        claimed_total,
        duplication_rate,
        deduplicated_contribution,
        verification_source_type,
        verification_source_id,
        lead_event_name,
        estimate_label,
        pull_id
    FROM {schema_prefix}dedup_estimate
    WHERE project_id = {ph(0)}
      AND date BETWEEN {ph(1)} AND {ph(2)}
    ORDER BY date, channel_connector
    """  # noqa: S608 — table name is a hard-coded constant; all values are bound params
    return sql, params


def query_dedup_estimate(
    project_id: str,
    start_date: str,
    end_date: str,
) -> list[dict]:
    """Query marts.dedup_estimate for a project within [start_date, end_date] (Story 17.3).

    Returns a list of dicts with keys:
      date, channel_connector, claimed_conversions, verified_total, claimed_total,
      duplication_rate, deduplicated_contribution, verification_source_type,
      verification_source_id, lead_event_name, estimate_label, pull_id.

    Returns [] (never raises) when:
      * the mart is not yet populated (17.2 not run);
      * the project has no verification_source_type (opt-in per project -- zero rows);
      * source_type = 'stripe' v1 (BLOCKED 15.7 -- no rows emitted by the mart).

    Degrade contract (AD-9 honesty):
      * duplication_rate=NULL means "indeterminate" (régies BLOCKED Phase B, or stripe v1),
        NEVER 0 % duplication. Callers must surface NULL as "source de vérification
        indisponible", not as 0.
      * deduplicated_contribution=NULL follows the same rule.
    """
    mode = _db_mode()

    if mode == "duckdb":
        # Story 19.2: route through the cache (same _build_dedup_estimate_query).
        # dedup_estimate is a single-project, non-widened window read (no prior period).
        try:
            rows = _query_duckdb_routed(
                lambda prefix: _build_dedup_estimate_query(
                    prefix, project_id, start_date, end_date
                ),
                relation="dedup_estimate",
                project_ids=[project_id],
                start_date=start_date,
                end_date=end_date,
            )
        except Exception as exc:  # noqa: BLE001 -- view may not exist yet (17.2 not delivered)
            # review-17-3 F-4: NEVER swallow silently -- a real SQL error (renamed
            # column, broken mart) must be observable, not disguised as opt-out.
            logger.warning(
                "query_dedup_estimate failed (served as empty): %s: %s",
                type(exc).__name__, exc,
            )
            return []
        return [_json_safe_row(r) for r in rows]

    elif mode == "bigquery":
        if not _check_bigquery_mart(project_id):
            logger.warning(
                json.dumps(
                    {
                        "event": "warehouse_not_ready",
                        "message": "marts not populated -- run 17.2 seed first",
                    }
                )
            )
            return []
        sql, params = _build_dedup_estimate_query(
            "", project_id, start_date, end_date, placeholder="@"
        )
        try:
            rows = _query_bigquery(sql, params)
        except Exception as exc:  # noqa: BLE001
            # review-17-3 F-4: observable, never silently disguised as opt-out.
            logger.warning(
                "query_dedup_estimate failed (served as empty): %s: %s",
                type(exc).__name__, exc,
            )
            return []
        return [_json_safe_row(r) for r in rows]

    else:
        raise ValueError(f"Unknown TOOROW_DB_MODE: {mode!r}")


def query_transaction_reconciliation_daily(
    project_id: str,
    start_date: str,
    end_date: str,
) -> list[dict]:
    """Query marts.transaction_reconciliation_daily for a project (Story 17.4).

    Pattern: identical to query_dedup_estimate (duckdb+bigquery, try/except LOGGED,
    never silent — review-17-5 F7 AD-9). Reads matched_count and with_txn (orders
    with a transaction_id eligible for verified-source matching) per (project_id, date).

    Returns a list of dicts with keys:
      date, shopify_orders_with_txn (= with_txn), matched_count, coverage_rate.
    (review-19-4 data F-1: the view exposes NO shopify_orders_total column --
    selecting it made every prod call fail into the logged-[] degraded path.)

    Returns [] (never raises) when:
      * the mart view does not exist yet (17.4 not run) -- LOGGED, not silent;
      * the project has no Shopify source or no reconciliable orders.

    Degraded contract (AD-9 honesty):
      * An empty result means the view is absent or has no data for this project.
        The caller surfaces this as "réconciliation indisponible" -- never a fake 0%.
    """
    mode = _db_mode()

    def _sql(schema_prefix: str, placeholder: str = "?") -> tuple[str, list]:
        def ph(i: int) -> str:
            return f"@p{i}" if placeholder != "?" else "?"
        params: list = [project_id, start_date, end_date]
        sql = f"""
        SELECT
            date,
            shopify_orders_with_txn,
            matched_count,
            coverage_rate
        FROM {schema_prefix}transaction_reconciliation_daily
        WHERE project_id = {ph(0)}
          AND date BETWEEN {ph(1)} AND {ph(2)}
        ORDER BY date
        """  # noqa: S608
        return sql, params

    if mode == "duckdb":
        # Story 19.2: route through the cache (same _sql builder, empty prefix on hit).
        try:
            rows = _query_duckdb_routed(
                lambda prefix: _sql(prefix),
                relation="transaction_reconciliation_daily",
                project_ids=[project_id],
                start_date=start_date,
                end_date=end_date,
            )
        except Exception as exc:  # noqa: BLE001
            # review-17-5 F7: NEVER swallow silently -- SQL error must be observable.
            logger.warning(
                "query_transaction_reconciliation_daily failed (served as empty): %s: %s",
                type(exc).__name__, exc,
            )
            return []
        return [_json_safe_row(r) for r in rows]

    elif mode == "bigquery":
        if not _check_bigquery_mart(project_id):
            logger.warning(
                json.dumps(
                    {
                        "event": "warehouse_not_ready",
                        "message": "transaction_reconciliation_daily not populated",
                    }
                )
            )
            return []
        sql_str, params = _sql("", placeholder="@")
        try:
            rows = _query_bigquery(sql_str, params)
        except Exception as exc:  # noqa: BLE001
            # review-17-5 F7: observable, never silently disguised as opt-out.
            logger.warning(
                "query_transaction_reconciliation_daily failed (served as empty): %s: %s",
                type(exc).__name__, exc,
            )
            return []
        return [_json_safe_row(r) for r in rows]

    else:
        raise ValueError(f"Unknown TOOROW_DB_MODE: {mode!r}")


def query_campaign_spend(
    project_id: str,
    start_date: str,
    end_date: str,
) -> list[dict]:
    """Query per-campaign total spend over a window (Story 22.3, FR38/CAP-26).

    Reads the REAL campaign-grain spend the fact carries: fact_daily_kpi rows with
    ``metric = 'cost'`` and ``breakdown_dimension = 'campaign_id'``. The campaign
    identity is ``breakdown_value`` (fact_daily_kpi has NO dedicated campaign
    column -- see dbt/models/marts/fact_daily_kpi.sql: meta-ads / tiktok-ads /
    linkedin-ads / klaviyo all emit breakdown_dimension='campaign_id'). Spend is
    summed over the FULL window per (connector, campaign_ref).

    Returns a list of dicts with keys:
      connector, campaign_ref (= breakdown_value), spend (float total over window).

    Used by mediaplan_mapping.list_unmapped_actuals to compute [Unmapped Actuals]:
    campaigns with real spend in the plan window that carry NO active mapping.

    Degrade contract (AD-9 honesty, story rule "JAMAIS filtré silencieusement"):
    on a real backend error the failure is LOGGED and re-raised as
    WarehouseUnavailable so the caller surfaces an honest error -- NEVER a silent
    []. (A truly empty mart returns [] with a structured warning, same as the
    other query_* functions, which is an honest "no spend", not a hidden failure.)
    """
    mode = _db_mode()

    def _sql(schema_prefix: str, placeholder: str = "?") -> tuple[str, list]:
        def ph(i: int) -> str:
            return f"@p{i}" if placeholder != "?" else "?"

        params: list = [project_id, start_date, end_date]
        sql = f"""
        SELECT
            connector,
            breakdown_value AS campaign_ref,
            SUM(CAST(value AS DOUBLE)) AS spend
        FROM {schema_prefix}fact_daily_kpi
        WHERE project_id = {ph(0)}
          AND date BETWEEN {ph(1)} AND {ph(2)}
          AND metric = 'cost'
          AND breakdown_dimension = 'campaign_id'
        GROUP BY connector, breakdown_value
        ORDER BY connector, breakdown_value
        """  # noqa: S608 -- table name is a hard-coded constant; all values are bound params
        return sql, params

    if mode == "duckdb":
        # Story 22.3 review F-4: the [Unmapped Actuals] perimeter MUST NOT degrade to
        # a silent []. _query_duckdb returns [] when the .duckdb file is absent (marts
        # not seeded), which would falsely claim "100 % du réel est mappé". A missing
        # file OR a missing fact_daily_kpi relation therefore raises WarehouseUnavailable
        # here (this guard is scoped to query_campaign_spend only -- the other query_*
        # functions keep their honest degrade-to-[] contract).
        path = _duckdb_path()
        if not path or not os.path.exists(path):
            logger.warning(
                "query_campaign_spend: duckdb file absent (%r) -- marts not seeded",
                path,
            )
            raise WarehouseUnavailable(
                "Entrepôt indisponible : la base analytique locale est introuvable."
            )
        if not _duckdb_relation_exists(path, "fact_daily_kpi"):
            logger.warning(
                "query_campaign_spend: relation fact_daily_kpi absente de %r", path
            )
            raise WarehouseUnavailable(
                "Entrepôt indisponible : la table des dépenses réelles est absente."
            )

        # Story 19.2: route through the read-through cache (same _sql builder).
        # Non-widened, single-project window read (no prior period).
        try:
            rows = _query_duckdb_routed(
                lambda prefix: _sql(prefix),
                relation="fact_daily_kpi",
                project_ids=[project_id],
                start_date=start_date,
                end_date=end_date,
            )
        except Exception as exc:  # noqa: BLE001
            # Story 22.3: NEVER swallow silently -- an unmapped-actuals read that
            # fails must be observable AND propagate (no fake empty perimeter).
            logger.warning(
                "query_campaign_spend failed: %s: %s", type(exc).__name__, exc
            )
            raise WarehouseUnavailable(
                "Entrepôt indisponible : impossible de lire les dépenses réelles."
            ) from exc
        return [_json_safe_row(r) for r in rows]

    elif mode == "bigquery":
        if not _check_bigquery_mart(project_id):
            logger.warning(
                json.dumps(
                    {
                        "event": "warehouse_not_ready",
                        "message": "marts not populated -- run Story 1.4 seed first",
                    }
                )
            )
            # Mart absent is an honest "not ready" -> propagate, never a fake [].
            raise WarehouseUnavailable(
                "Entrepôt indisponible : les marts ne sont pas encore peuplés."
            )
        sql_str, params = _sql("", placeholder="@")
        try:
            rows = _query_bigquery(sql_str, params)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "query_campaign_spend failed: %s: %s", type(exc).__name__, exc
            )
            raise WarehouseUnavailable(
                "Entrepôt indisponible : impossible de lire les dépenses réelles."
            ) from exc
        return [_json_safe_row(r) for r in rows]

    else:
        raise ValueError(f"Unknown TOOROW_DB_MODE: {mode!r}")


def query_campaign_spend_daily(
    project_id: str,
    start_date: str,
    end_date: str,
) -> list[dict]:
    """Query per-campaign, per-DAY spend over a window (Story 22.8 E1-F-1).

    Same source, gardes and degrade contract (WarehouseUnavailable, NEVER a silent
    []) as ``query_campaign_spend``, but at the DAILY grain: it does NOT collapse the
    window into a single per-campaign total, it keeps one row per (connector,
    campaign_ref, day). This is what list_unmapped_actuals needs to decide, day by
    day, whether a campaign's real spend falls INSIDE the window of a line that maps
    it (couvert) or OUTSIDE (hors_fenetre_lignes_mappees) -- the mart ventilates PER
    LINE WINDOW (day BETWEEN line.start AND line.end), so a per-window-total subtract
    would make spend falling outside a mapped line's window vanish from BOTH views.

    Reads the SAME fact_daily_kpi rows as query_campaign_spend (metric = 'cost',
    breakdown_dimension = 'campaign_id'); the campaign identity is breakdown_value.
    Sums per (connector, breakdown_value, date) -- GROUP BY includes f.date so a
    campaign that pulled multiple 'cost' rows on the same day is summed, not
    duplicated.

    Returns a list of dicts with keys:
      connector, campaign_ref (= breakdown_value), day (ISO date), spend (float).

    Degrade contract (AD-9, story rule "JAMAIS filtré silencieusement"): on any real
    backend error the failure is LOGGED and re-raised as WarehouseUnavailable so the
    caller surfaces an honest error -- NEVER a silent [] (a fabricated empty daily
    perimeter would falsely claim "100 % du réel est mappé"). A truly empty mart
    returns [] with a structured warning (an honest "no spend").
    """
    mode = _db_mode()

    def _sql(schema_prefix: str, placeholder: str = "?") -> tuple[str, list]:
        def ph(i: int) -> str:
            return f"@p{i}" if placeholder != "?" else "?"

        params: list = [project_id, start_date, end_date]
        sql = f"""
        SELECT
            connector,
            breakdown_value AS campaign_ref,
            date AS day,
            SUM(CAST(value AS DOUBLE)) AS spend
        FROM {schema_prefix}fact_daily_kpi
        WHERE project_id = {ph(0)}
          AND date BETWEEN {ph(1)} AND {ph(2)}
          AND metric = 'cost'
          AND breakdown_dimension = 'campaign_id'
        GROUP BY connector, breakdown_value, date
        ORDER BY connector, breakdown_value, date
        """  # noqa: S608 -- table name is a hard-coded constant; all values are bound params
        return sql, params

    if mode == "duckdb":
        # Same guard as query_campaign_spend: the [Unmapped Actuals] perimeter MUST
        # NOT degrade to a silent []. A missing .duckdb file OR a missing
        # fact_daily_kpi relation raises WarehouseUnavailable (never a fake "100 %
        # mappé").
        path = _duckdb_path()
        if not path or not os.path.exists(path):
            logger.warning(
                "query_campaign_spend_daily: duckdb file absent (%r) -- marts not seeded",
                path,
            )
            raise WarehouseUnavailable(
                "Entrepôt indisponible : la base analytique locale est introuvable."
            )
        if not _duckdb_relation_exists(path, "fact_daily_kpi"):
            logger.warning(
                "query_campaign_spend_daily: relation fact_daily_kpi absente de %r", path
            )
            raise WarehouseUnavailable(
                "Entrepôt indisponible : la table des dépenses réelles est absente."
            )

        try:
            rows = _query_duckdb_routed(
                lambda prefix: _sql(prefix),
                relation="fact_daily_kpi",
                project_ids=[project_id],
                start_date=start_date,
                end_date=end_date,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "query_campaign_spend_daily failed: %s: %s", type(exc).__name__, exc
            )
            raise WarehouseUnavailable(
                "Entrepôt indisponible : impossible de lire les dépenses réelles."
            ) from exc
        return [_json_safe_row(r) for r in rows]

    elif mode == "bigquery":
        if not _check_bigquery_mart(project_id):
            logger.warning(
                json.dumps(
                    {
                        "event": "warehouse_not_ready",
                        "message": "marts not populated -- run Story 1.4 seed first",
                    }
                )
            )
            raise WarehouseUnavailable(
                "Entrepôt indisponible : les marts ne sont pas encore peuplés."
            )
        sql_str, params = _sql("", placeholder="@")
        try:
            rows = _query_bigquery(sql_str, params)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "query_campaign_spend_daily failed: %s: %s", type(exc).__name__, exc
            )
            raise WarehouseUnavailable(
                "Entrepôt indisponible : impossible de lire les dépenses réelles."
            ) from exc
        return [_json_safe_row(r) for r in rows]

    else:
        raise ValueError(f"Unknown TOOROW_DB_MODE: {mode!r}")


# ---------------------------------------------------------------------------
# Story 22.4 (FR38 / CAP-26) -- plan-vs-actual & pacing read.
#
# Reads the three READ-ONLY pacing marts (plan_pacing_by_line / _by_channel /
# _by_plan) for ONE plan, scoped by (project_id, plan_id). These marts are NOT in
# the read-through cache allowlist (out of scope, Story 19.x) -- the read goes
# straight to the origin via _query_duckdb / _query_bigquery, never through
# _query_duckdb_routed. Fail-clean: like query_campaign_spend, a missing DuckDB
# file / a missing pacing relation raises WarehouseUnavailable rather than a silent
# [] (a fabricated empty pacing would falsely claim "aucune dépense", AD-9). A
# present-but-empty mart returns an empty rows list honestly (the plan simply has no
# active version / no lines).
# ---------------------------------------------------------------------------

# The pacing relations this read touches (documented allowlist -- NEVER interpolated
# from caller input; these are hard-coded constants).
_PLAN_PACING_RELATIONS = (
    "plan_pacing_by_line",
    "plan_pacing_by_channel",
    "plan_pacing_by_plan",
)


def _build_plan_pacing_query(
    schema_prefix: str,
    relation: str,
    project_id: str,
    plan_id: str,
    *,
    placeholder: str = "?",
) -> tuple[str, list]:
    """Build a parameterized SELECT * over one plan pacing relation for a plan.

    ``relation`` is one of _PLAN_PACING_RELATIONS (a hard-coded constant, never
    attacker-controlled). project_id and plan_id are BOUND parameters (HG-3
    injection safety). Ordered deterministically so the response is stable.
    """
    if relation not in _PLAN_PACING_RELATIONS:
        # Defensive: only the three known relations may be queried.
        raise ValueError(f"Unknown plan pacing relation: {relation!r}")

    def ph(i: int) -> str:
        return f"@p{i}" if placeholder != "?" else "?"

    order_by = "sort_order, line_key" if relation == "plan_pacing_by_line" else (
        "channel" if relation == "plan_pacing_by_channel" else "plan_id"
    )
    params: list = [project_id, plan_id]
    sql = f"""
    SELECT *
    FROM {schema_prefix}{relation}
    WHERE project_id = {ph(0)}
      AND plan_id = {ph(1)}
    ORDER BY {order_by}
    """  # noqa: S608 -- relation + ORDER BY are hard-coded constants; values are bound params
    return sql, params


def query_plan_vs_actual(
    project_id: str,
    plan_id: str,
) -> dict[str, list[dict]]:
    """Read the plan-vs-actual pacing marts for ONE plan (Story 22.4, FR38/CAP-26).

    Returns a dict with three lists (each a list of fact-shaped dicts):
      {
        "lines":    [...],   # plan_pacing_by_line  (one row per line)
        "channels": [...],   # plan_pacing_by_channel (one row per channel)
        "plan":     [...],   # plan_pacing_by_plan   (0 or 1 row)
      }
    Every row carries its provenance (plan_version_id + actual_pull_id_min/max/count).
    NULL is honest throughout (pace NULL when allocated_to_date=0, actual NULL for
    plan-only lines) -- the mart never fabricates a 0.

    Degrade contract (AD-9, story rule): a missing DuckDB file or a missing pacing
    relation raises WarehouseUnavailable so the caller surfaces an honest error and a
    503 -- NEVER a silent empty pacing (which would read as "aucune dépense"). A
    genuinely empty-but-present mart returns empty lists (the plan has no active
    version / no lines yet) -- an honest empty, distinct from a hidden failure.
    """
    mode = _db_mode()

    if mode == "duckdb":
        path = _duckdb_path()
        if not path or not os.path.exists(path):
            logger.warning(
                "query_plan_vs_actual: duckdb file absent (%r) -- marts not seeded", path
            )
            raise WarehouseUnavailable(
                "Entrepôt indisponible : la base analytique locale est introuvable."
            )
        for relation in _PLAN_PACING_RELATIONS:
            if not _duckdb_relation_exists(path, relation):
                logger.warning(
                    "query_plan_vs_actual: relation %s absente de %r", relation, path
                )
                raise WarehouseUnavailable(
                    "Entrepôt indisponible : les vues de pacing sont absentes."
                )

        out: dict[str, list[dict]] = {}
        try:
            for key, relation in (
                ("lines", "plan_pacing_by_line"),
                ("channels", "plan_pacing_by_channel"),
                ("plan", "plan_pacing_by_plan"),
            ):
                sql, params = _build_plan_pacing_query(
                    _duckdb_mart_prefix(project_id), relation, project_id, plan_id
                )
                out[key] = [_json_safe_row(r) for r in _query_duckdb(sql, params)]
        except Exception as exc:  # noqa: BLE001
            # NEVER swallow silently -- a broken mart must be observable AND propagate.
            logger.warning(
                "query_plan_vs_actual failed: %s: %s", type(exc).__name__, exc
            )
            raise WarehouseUnavailable(
                "Entrepôt indisponible : impossible de lire le pacing du plan."
            ) from exc
        return out

    elif mode == "bigquery":
        if not _check_bigquery_mart(project_id):
            logger.warning(
                json.dumps(
                    {
                        "event": "warehouse_not_ready",
                        "message": "marts not populated -- run Story 1.4 seed first",
                    }
                )
            )
            raise WarehouseUnavailable(
                "Entrepôt indisponible : les marts ne sont pas encore peuplés."
            )
        out = {}
        try:
            for key, relation in (
                ("lines", "plan_pacing_by_line"),
                ("channels", "plan_pacing_by_channel"),
                ("plan", "plan_pacing_by_plan"),
            ):
                sql, params = _build_plan_pacing_query(
                    "", relation, project_id, plan_id, placeholder="@"
                )
                out[key] = [_json_safe_row(r) for r in _query_bigquery(sql, params)]
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "query_plan_vs_actual failed: %s: %s", type(exc).__name__, exc
            )
            raise WarehouseUnavailable(
                "Entrepôt indisponible : impossible de lire le pacing du plan."
            ) from exc
        return out

    else:
        raise ValueError(f"Unknown TOOROW_DB_MODE: {mode!r}")


def query_daily_report(
    project_id: str,
    start_date: str,
    end_date: str,
    connectors: list[str] | None,
    *,
    include_prior_period: bool = True,
) -> list[dict]:
    """Query fact_daily_kpi mart and return raw rows as list[dict].

    Each row contains: date, connector, metric, breakdown_dimension,
    breakdown_value, value, pull_id, loaded_at.

    When ``include_prior_period=True`` (default, G-06 fix), the SQL date filter
    is widened to also fetch the preceding window of the same length so that
    ``rollup._split_periods`` can compute delta/delta_pct. Call sites that need
    current-window-only rows (envelope data.rows, summary tables) should filter
    via ``rollup._split_periods`` after calling this function.

    Returns an empty list (never raises) when the mart is not yet populated,
    logging a structured warning so the caller can surface the empty-state
    response without crashing (T2.4).
    """
    fetch_start = _widen_to_prior(start_date, end_date) if include_prior_period else start_date
    mode = _db_mode()

    if mode == "duckdb":
        # Story 19.2: route through the read-through cache (same builder, two files).
        # Effective window = [fetch_start, end_date] (already _widen_to_prior-widened)
        # so a prior-period delta reaching outside the cache is judged a miss.
        return _query_duckdb_routed(
            lambda prefix: _build_query(prefix, project_id, fetch_start, end_date, connectors),
            relation="fact_daily_kpi",
            project_ids=[project_id],
            start_date=fetch_start,
            end_date=end_date,
        )

    elif mode == "bigquery":
        if not _check_bigquery_mart(project_id):
            logger.warning(
                json.dumps(
                    {
                        "event": "warehouse_not_ready",
                        "message": "marts not populated — run Story 1.4 seed first",
                    }
                )
            )
            return []
        sql, params = _build_query(
            "", project_id, fetch_start, end_date, connectors, placeholder="@"
        )
        return _query_bigquery(sql, params)

    else:
        raise ValueError(f"Unknown TOOROW_DB_MODE: {mode!r}")


# ---------------------------------------------------------------------------
# Story 37.9 -- country partition inventory for the geography DQ monitor.
# ---------------------------------------------------------------------------


def _build_breakdown_value_query(
    schema_prefix: str,
    project_id: str,
    dimension: str,
    start_date: str,
    end_date: str,
    *,
    placeholder: str = "?",
) -> tuple[str, list]:
    """Build the distinct (connector, breakdown_value, rows) query for one partition.

    The dimension is BOUND as a parameter, never interpolated: it is caller data
    (AD-2 -- the core holds no dimension vocabulary of its own here).
    """

    def ph(i: int) -> str:
        return f"@p{i}" if placeholder != "?" else "?"

    sql = f"""
    SELECT
        connector,
        breakdown_value,
        COUNT(*) AS row_count
    FROM {schema_prefix}fact_daily_kpi
    WHERE project_id = {ph(0)}
      AND breakdown_dimension = {ph(1)}
      AND date BETWEEN {ph(2)} AND {ph(3)}
    GROUP BY connector, breakdown_value
    ORDER BY connector, breakdown_value
    """
    return sql, [project_id, dimension, start_date, end_date]


def query_breakdown_values(
    project_id: str,
    dimension: str,
    start_date: str,
    end_date: str,
) -> list[dict]:
    """Return the distinct values observed on one breakdown partition, with counts.

    Rows look like ``{"connector", "breakdown_value", "row_count"}``. Never raises
    when the mart is absent -- returns ``[]`` with the same structured warning as
    the other read paths (AD-12: marts only, never raw_*).
    """
    mode = _db_mode()
    if mode == "duckdb":
        sql, params = _build_breakdown_value_query(
            _duckdb_mart_prefix(project_id), project_id, dimension, start_date, end_date
        )
        try:
            return _query_duckdb(sql, params)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                json.dumps({"event": "warehouse_breakdown_read_failed", "error": str(exc)})
            )
            return []
    if mode == "bigquery":
        if not _check_bigquery_mart(project_id):
            logger.warning(
                json.dumps(
                    {
                        "event": "warehouse_not_ready",
                        "message": "marts not populated — run Story 1.4 seed first",
                    }
                )
            )
            return []
        sql, params = _build_breakdown_value_query(
            "", project_id, dimension, start_date, end_date, placeholder="@"
        )
        try:
            return _query_bigquery(sql, params)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                json.dumps({"event": "warehouse_breakdown_read_failed", "error": str(exc)})
            )
            return []
    raise ValueError(f"Unknown TOOROW_DB_MODE: {mode!r}")
