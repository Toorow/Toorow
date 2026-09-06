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
from dataclasses import dataclass
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


# ---------------------------------------------------------------------------
# Story 66.3 (AC 9) -- what a warehouse job COST, beside the rows it returned.
#
# The job count was already provable. The two other figures the acceptance names
# -- billed bytes and elapsed time -- were not, and for BigQuery they could not
# be: `client.query(...).result()` DROPPED the QueryJob, and `total_bytes_billed`
# lives on the job, never on the RowIterator.
#
# `WarehouseJobCost` is emitted for BOTH engines, and it never lets one engine's
# silence read as another's zero. `billed_bytes_state`:
#   `exact`          -- the engine billed, and this is the figure it reported;
#   `not_applicable` -- the engine does not bill bytes (DuckDB reads a local
#                       file: 0 would be a measurement, and there is none);
#   `unavailable`    -- the engine bills, and this job did not report it (a
#                       dry run, a cache hit on some paths, a script job).
# Elapsed is always `exact`: it is measured HERE, on the wall clock, and every
# engine has one.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WarehouseJobCost:
    """What one warehouse job cost. Never a bare number."""

    engine: str
    elapsed_ms: int
    billed_bytes: int | None = None
    billed_bytes_state: str = "not_applicable"
    cache_hit: bool | None = None

    def as_dict(self) -> dict:
        return {
            "engine": self.engine,
            "elapsed_ms": self.elapsed_ms,
            "billed_bytes": self.billed_bytes,
            "billed_bytes_state": self.billed_bytes_state,
            "cache_hit": self.cache_hit,
        }


@dataclass(frozen=True)
class WarehouseScanEstimate:
    """Ce qu'une requete VA scanner, demande avant de la lancer. Story 66.10, AR8.

    AR8 exige que les estimations de cout/scan soient calculees cote serveur
    AVANT l'execution. La moitie << limites de securite >> etait livree (le
    plafond de lignes, le budget en octets, le preflight de pivotabilite) ; la
    moitie ESTIMATION ne l'etait pas, et la story la nommait absente en disant
    << aucune API de cout n'est consultee dans cet epic >>. C'etait vrai a
    l'ecriture et ne l'est plus depuis 66.3 : `query_bigquery_measured` lit
    `total_bytes_billed`. Ce qui manquait vraiment est l'estimation AVANT, et
    BigQuery la donne gratuitement -- un `dry_run` ne lance rien et ne facture
    rien.

    JAMAIS UN NOMBRE NU, la meme regle que `WarehouseJobCost`. Un `0` de DuckDB
    serait une MESURE (<< cette requete ne scanne rien >>) alors que la verite
    est qu'une lecture de fichier local ne facture pas d'octets : `not_applicable`.
    Un estimateur injoignable rend `unavailable`, jamais 0 -- et n'empeche pas
    l'execution : ne pas savoir ce qu'une requete coutera n'est pas une raison de
    refuser de repondre.
    """

    engine: str
    scanned_bytes: int | None = None
    scanned_bytes_state: str = "not_applicable"
    unavailable_reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "engine": self.engine,
            "scanned_bytes": self.scanned_bytes,
            "scanned_bytes_state": self.scanned_bytes_state,
            "unavailable_reason": self.unavailable_reason,
        }


def estimate_scan_duckdb(sql: str, params: list) -> WarehouseScanEstimate:
    """DuckDB ne facture pas un scan : `not_applicable`, jamais 0."""
    del sql, params
    return WarehouseScanEstimate(engine="duckdb", scanned_bytes_state="not_applicable")


def estimate_scan_bigquery(sql: str, params: list) -> WarehouseScanEstimate:
    """Ce que BigQuery scannerait, par `dry_run` : rien n'est lance, rien n'est facture.

    `use_query_cache=False` est deliberatement pose : avec le cache, un dry run
    peut rendre 0 octet parce que la MEME question a deja ete posee -- ce qui
    dit ce que ce rejeu couterait, pas ce que la requete coute. L'estimation
    doit valoir pour la requete, pas pour sa chance.
    """
    from google.cloud import bigquery  # noqa: PLC0415

    client = bigquery.Client(project=_gcp_project() or None)
    job_config = bigquery.QueryJobConfig(
        dry_run=True,
        use_query_cache=False,
        query_parameters=[
            bigquery.ScalarQueryParameter(f"p{i}", "STRING", v) for i, v in enumerate(params)
        ],
    )
    job = client.query(sql, job_config=job_config)
    scanned = getattr(job, "total_bytes_processed", None)
    if scanned is None:
        return WarehouseScanEstimate(
            engine="bigquery",
            scanned_bytes_state="unavailable",
            unavailable_reason="the dry run returned no byte count",
        )
    return WarehouseScanEstimate(
        engine="bigquery",
        scanned_bytes=int(scanned),
        scanned_bytes_state="exact",
    )


def estimate_scan(sql: str, params: list) -> WarehouseScanEstimate:
    """L'estimation, sur le moteur configure, et qui NE LEVE JAMAIS.

    Un estimateur qui casserait ferait echouer une execution parfaitement
    valide pour n'avoir pas su dire ce qu'elle allait couter. Il rend
    `unavailable` en nommant la panne, et l'appelant continue.
    """
    engine = "bigquery" if _db_mode() == "bigquery" else "duckdb"
    try:
        if engine == "bigquery":
            return estimate_scan_bigquery(sql, params)
        return estimate_scan_duckdb(sql, params)
    except Exception as exc:  # noqa: BLE001 -- l'estimateur, jamais la requete
        logger.warning("warehouse: scan_estimate_unavailable: %s: %s", type(exc).__name__, exc)
        return WarehouseScanEstimate(
            engine=engine,
            scanned_bytes_state="unavailable",
            unavailable_reason=f"{type(exc).__name__}: {exc}",
        )


def query_duckdb_measured(sql: str, params: list) -> tuple[list[dict], WarehouseJobCost]:
    """`_query_duckdb`, plus what the job cost. One execution, not two."""
    started = _time_module.perf_counter()
    rows = _query_duckdb(sql, params)
    return rows, WarehouseJobCost(
        engine="duckdb",
        elapsed_ms=int((_time_module.perf_counter() - started) * 1000),
        billed_bytes=None,
        billed_bytes_state="not_applicable",
    )


def query_bigquery_measured(sql: str, params: list) -> tuple[list[dict], WarehouseJobCost]:
    """`_query_bigquery`, keeping the QueryJob instead of throwing it away."""
    from google.cloud import bigquery  # noqa: PLC0415

    started = _time_module.perf_counter()
    project = _gcp_project()
    client = bigquery.Client(project=project or None)
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter(f"p{i}", "STRING", v) for i, v in enumerate(params)
        ]
    )
    job = client.query(sql, job_config=job_config)
    result = job.result()
    cols = [f.name for f in result.schema]
    rows = [dict(zip(cols, row)) for row in result]

    billed = getattr(job, "total_bytes_billed", None)
    cached = bool(getattr(job, "cache_hit", False))
    # A CACHE HIT REPORTS, AND WHAT IT REPORTS IS `0`. The first version of this
    # function assumed the opposite -- "a cache hit bills nothing and REPORTS
    # nothing" -- and it was wrong on the second half: `QueryJob.total_bytes_billed`
    # is `int(statistics.query.totalBytesBilled)` and returns `None` only when the
    # field is ABSENT, which after `.result()` means a job that never completed.
    # BigQuery answers a cache hit with `cacheHit: true` AND `totalBytesBilled: "0"`.
    #
    # So the branch below used to put a free job and an unmeasured job on the same
    # row -- the exact confusion its own comment forbade. `cache_hit` was captured,
    # stored and read by nothing: the figure sat beside the decision it should have
    # made. It makes it now.
    #
    # `0` from a cache hit is `unavailable`, not `exact`: this query was answered
    # without touching a byte of the tables, so it says nothing about what the same
    # question costs. Re-profiling an edge inside the 120 s receipt window is
    # precisely the cache-hit path, so this is the second click, not an edge case.
    measured = billed is not None and not cached
    return rows, WarehouseJobCost(
        engine="bigquery",
        elapsed_ms=int((_time_module.perf_counter() - started) * 1000),
        billed_bytes=int(billed) if measured else None,
        billed_bytes_state="exact" if measured else "unavailable",
        cache_hit=getattr(job, "cache_hit", None),
    )


def _query_bigquery(sql: str, params: list) -> list[dict]:
    """The rows alone. Every existing caller wants exactly this."""
    rows, _cost = query_bigquery_measured(sql, params)
    return rows


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


#: The one written form of `loaded_at`, and the reason a string comparison is
#: sound at all. Every writer emits `YYYY-MM-DDTHH:MM:SS.ffffffZ` -- measured on
#: production 2026-08-23 (`raw_youtube_daily`: 27 characters, six fractional
#: digits, trailing `Z`) and produced by the loaders' own
#: `datetime.now(UTC).isoformat().replace("+00:00", "Z")`. FIXED WIDTH is what
#: makes lexicographic order equal chronological order; a bound value in any
#: other shape silently compares wrong at the boundary rather than failing.
_LOADED_AT_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"


def canonical_as_of_bound(as_of_ts: str) -> str:
    """The as-of instant in the exact written form of `loaded_at`.

    WHY THIS IS NOT COSMETIC (AI-312, 2026-08-23). `loaded_at` is a STRING, so
    `loaded_at <= @as_of` is a lexicographic comparison, and two spellings of the
    same instant do not compare equal. `get_daily_report` normalises its `as_of`
    with `datetime.isoformat()`, which writes `+00:00`; `'…59Z' <= '…59+00:00'`
    is FALSE because `Z` sorts after `+`. Measured: the boundary day of every
    as-of question disappeared from the answer -- 30 days returned for a 31-day
    window, and the sum was short by exactly that day.

    A bare date means the END of that day, the same rule
    `query_execution.as_of_instant` already states: "reported as of the 5th"
    means everything that had landed by the close of the 5th.

    An unparseable value is returned untouched -- validating `as_of` belongs to
    the tool that accepted it, and swallowing the shape here would hide it.
    """
    text = str(as_of_ts)
    if len(text) == 10:
        return f"{text}T23:59:59.999999Z"
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return f"{moment.strftime(_LOADED_AT_FORMAT)}Z"


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

    NO CAST ON THE BOUND VALUE, and that is the fix of 2026-08-23 (AI-312).
    The predicate read ``loaded_at <= CAST(? AS TIMESTAMP)`` while ``loaded_at``
    is a STRING in both engines — VARCHAR in the DuckDB mart, and STRING in the
    production raw tables this mart derives from (measured on
    ``raw_youtube_daily``). DuckDB refuses the comparison outright (*Cannot
    compare values of type VARCHAR and type TIMESTAMP*), which took the WHOLE
    ``as_of`` surface of the eval corpus down — 7 questions of 50, every one of
    them a `warehouse_query_error`, and nothing had ever run this path far enough
    to see it.

    Comparing ISO-8601 UTC strings is the invariant the loaders already document
    (*"lexicographic sort == chronological"*, F-06) and the one the ``ORDER BY
    loaded_at DESC`` two lines below has always relied on. It is also what the
    sibling as-of builder in ``core.query_execution`` does — ``WHERE loaded_at
    <= ?``, no cast — so this call site was the outlier, not the rule. And a
    predicate with no function on the column keeps BigQuery's partition pruning,
    which a ``CAST(loaded_at AS TIMESTAMP)`` would have cost.

    # Revision comparison: call get_daily_report_asof twice with different as_of values;
    # compare pull_ids. If pull_id A != pull_id B, the value was revised.
    # Pull comparison endpoint deferred to Story 6.x.
    """
    params: list = []

    def ph(i: int) -> str:
        return f"@p{i}" if placeholder != "?" else "?"

    # as_of_ts first (index 0), then project_id (1), start_date (2), end_date (3)
    params = [canonical_as_of_bound(as_of_ts), project_id, start_date, end_date]

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
        WHERE loaded_at <= {ph(0)}
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


def _money_micros_sql(value_col: str, *, placeholder: str) -> str:
    """`value_col` as EXACT integer micros, per row, in the caller's dialect.

    The Python twin of `dbt/macros/fee_tax_to_micros.sql`, and deliberately its
    copy rather than a second idea. Both sides of the product must turn the same
    display decimal into the same integer, and the macro's own contract is the
    reason this exists at all: NORMALISE PER SOURCE ROW, THEN SUM EXACT BIGINTS.
    `ROUND(SUM(value) * 1e6)` puts the single rounding boundary AFTER the float
    drift instead of before it -- which is what `SUM(CAST(value AS DOUBLE))` did
    here, on `metric = 'cost'`, over every campaign-day of a pacing window.

    The dialect branch is the macro's, for the macro's reason and not a
    defensive one: DuckDB needs an explicitly parameterised `DECIMAL(p,s)` to
    stay in exact decimal arithmetic (an unqualified numeric expression silently
    promotes to DOUBLE and exactness is lost with no error), and BigQuery does
    not accept a parameterised DECIMAL inside CAST at all, offering bare NUMERIC
    fixed at (38,9).

    The dialect is read from the placeholder the caller already switches on --
    `?` is DuckDB, `@pN` is BigQuery -- so there is no second way to be wrong
    about which backend this statement is built for.
    """
    if placeholder != "?":
        return f"CAST(ROUND(CAST({value_col} AS NUMERIC) * 1000000) AS INT64)"
    return f"CAST(ROUND(CAST({value_col} AS DECIMAL(28,6)) * 1000000) AS BIGINT)"


def _with_display_spend(rows: list[dict]) -> list[dict]:
    """Add the display `spend` beside the exact `spend_micros` it is derived from.

    ONE division, at the very end, from an integer that is already exact. The
    callers of these two readers compare a window total against a media plan and
    have always read `spend`; they keep reading it, and the number they read no
    longer carries the drift of summing thousands of doubles. `spend_micros` is
    the authority -- anything that must be compared or reconciled reads that.
    """
    from core.money import MICROS_PER_UNIT  # noqa: PLC0415

    out: list[dict] = []
    for row in rows:
        micros = row.get("spend_micros")
        row = dict(row)
        row["spend"] = None if micros is None else int(micros) / MICROS_PER_UNIT
        out.append(row)
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


#: AI-260 -- the campaign-spend readers resolve the project's cleanup rules by
#: default. `None` disables (a raw read, e.g. an effect count measuring what a
#: rule WOULD remove); a resolved `CleanupRuleApplication` is used as-is, which
#: is how callers that surface the named states (plan-versus-actual matrix,
#: [Unmapped Actuals]) avoid a second resolution AND carry the states in their
#: own contract.
_CLEANUP_RESOLVE = object()

#: The exact fact partition both campaign-spend readers hard-code below. A
#: cleanup rule governs these reads if and only if it names this exact field --
#: string equality, the same exact-match rule the value-table bridge applies
#: (`plan_actual_alignment.ACTUAL_SOURCE_FIELD`, same value on purpose).
_CAMPAIGN_CLEANUP_FIELD = "campaign_id"


def _campaign_cleanup(project_id: str, cleanup) -> "object | None":
    """The `CleanupRuleApplication` this read applies, or None for a bare read.

    Fail-soft, NAMED, never a refusal: an unreadable governance store serves the
    read UNCLEANED and logs the outage -- refusing every spend read over a
    control-plane outage would take down the honest majority of reads no rule
    touches. Callers that must NAME the state in their contract resolve on their
    own connection and pass the application in.
    """
    if cleanup is None:
        return None
    if cleanup is not _CLEANUP_RESOLVE:
        return cleanup if getattr(cleanup, "applies", False) else None
    try:
        from core.cleanup_rule_application import resolve_cleanup_rules  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            application = resolve_cleanup_rules(
                conn, project_id=project_id, source_field=_CAMPAIGN_CLEANUP_FIELD
            )
    except Exception as exc:  # noqa: BLE001 -- the read proceeds uncleaned, named in the log
        logger.warning(
            "campaign spend cleanup rules unresolved (read served uncleaned): %s: %s",
            type(exc).__name__,
            exc,
        )
        return None
    if application.state == "unavailable":
        logger.warning(
            "campaign spend cleanup rules unavailable (read served uncleaned): %s",
            application.reason,
        )
    return application if application.applies else None


def _campaign_spend_sql(
    schema_prefix: str,
    placeholder: str,
    *,
    project_id: str,
    start_date: str,
    end_date: str,
    daily: bool,
    application,
) -> tuple[str, list]:
    """The ONE statement both campaign-spend readers run, cleanup rules woven in.

    AI-260: the applied cleanup rules are compiled INTO this statement by
    `core.cleanup_rule_application` in the dialect this statement is built for
    (the 60.3 obligation -- the fixture chain is DuckDB, production is BigQuery,
    and a rule compiled to only one of the two would work locally and fail in
    production or the reverse). Row rules guard the WHERE clause on the RAW
    collected value; strip rules rewrite the SERVED ``campaign_ref``, and the
    GROUP BY groups on the alias so stripped identities merge -- which is the
    point of a strip rule at read.

    Parameter order is appearance order, because DuckDB counts its placeholders:
    projection parameters (SELECT list) come FIRST, then the base three, then
    the WHERE-guard parameters. BigQuery numbers the same list continuously.
    """
    dialect = "bigquery" if placeholder != "?" else "duckdb"
    if application is not None:
        from core.cleanup_rule_application import (  # noqa: PLC0415
            compile_predicates,
            compile_projection,
        )

        projection, proj_params = compile_projection(
            application, dialect=dialect, first_param=0
        )
        predicate, pred_params = compile_predicates(
            application, dialect=dialect, first_param=len(proj_params) + 3
        )
    else:
        projection, proj_params, predicate, pred_params = None, [], None, []

    base = len(proj_params)

    def ph(i: int) -> str:
        return f"@p{base + i}" if placeholder != "?" else "?"

    campaign_expr = projection or "breakdown_value"
    day_select = "date AS day,\n            " if daily else ""
    day_group = ", day" if daily else ""
    guard = f"\n          AND {predicate}" if predicate else ""
    sql = f"""
        SELECT
            connector,
            {campaign_expr} AS campaign_ref,
            {day_select}SUM({_money_micros_sql("value", placeholder=placeholder)}) AS spend_micros
        FROM {schema_prefix}fact_daily_kpi
        WHERE project_id = {ph(0)}
          AND date BETWEEN {ph(1)} AND {ph(2)}
          AND metric = 'cost'
          AND breakdown_dimension = 'campaign_id'{guard}
        GROUP BY connector, campaign_ref{day_group}
        ORDER BY connector, campaign_ref{day_group}
        """  # noqa: S608 -- table name is a hard-coded constant; all values are bound params
    params: list = [*proj_params, project_id, start_date, end_date, *pred_params]
    return sql, params


def query_campaign_spend(
    project_id: str,
    start_date: str,
    end_date: str,
    cleanup=_CLEANUP_RESOLVE,
) -> list[dict]:
    """Query per-campaign total spend over a window (Story 22.3, FR38/CAP-26).

    Reads the REAL campaign-grain spend the fact carries: fact_daily_kpi rows with
    ``metric = 'cost'`` and ``breakdown_dimension = 'campaign_id'``. The campaign
    identity is ``breakdown_value`` (fact_daily_kpi has NO dedicated campaign
    column -- see dbt/models/marts/fact_daily_kpi.sql: meta-ads / tiktok-ads /
    linkedin-ads / klaviyo all emit breakdown_dimension='campaign_id'). Spend is
    summed over the FULL window per (connector, campaign_ref).

    AI-260: the project's ENABLED cleanup rules on the exact field
    ``campaign_id`` are applied IN the statement (see `_campaign_spend_sql`).
    ``cleanup`` defaults to resolving them from the control plane; pass a
    resolved `CleanupRuleApplication` to reuse one resolution and surface its
    named states, or ``None`` for a deliberately raw read. A Datastream-scoped
    rule applies through the `datastreams_dim` bridge only when every live
    Datastream of its connector carries it -- anything less is a NAMED gap,
    never a pick, and the gap travels on the resolving caller's contract.

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
    application = _campaign_cleanup(project_id, cleanup)

    def _sql(schema_prefix: str, placeholder: str = "?") -> tuple[str, list]:
        return _campaign_spend_sql(
            schema_prefix,
            placeholder,
            project_id=project_id,
            start_date=start_date,
            end_date=end_date,
            daily=False,
            application=application,
        )

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
                "Warehouse unavailable: the local analytics database is missing."
            )
        if not _duckdb_relation_exists(path, "fact_daily_kpi"):
            logger.warning(
                "query_campaign_spend: relation fact_daily_kpi absente de %r", path
            )
            raise WarehouseUnavailable(
                "Warehouse unavailable: the actual-spend table is absent."
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
                "Warehouse unavailable: cannot read the actual spend."
            ) from exc
        return _with_display_spend([_json_safe_row(r) for r in rows])

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
                "Warehouse unavailable: the marts are not populated yet."
            )
        sql_str, params = _sql("", placeholder="@")
        try:
            rows = _query_bigquery(sql_str, params)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "query_campaign_spend failed: %s: %s", type(exc).__name__, exc
            )
            raise WarehouseUnavailable(
                "Warehouse unavailable: cannot read the actual spend."
            ) from exc
        return _with_display_spend([_json_safe_row(r) for r in rows])

    else:
        raise ValueError(f"Unknown TOOROW_DB_MODE: {mode!r}")


def query_campaign_spend_daily(
    project_id: str,
    start_date: str,
    end_date: str,
    cleanup=_CLEANUP_RESOLVE,
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
    Sums per (connector, breakdown_value, date) -- GROUP BY includes the day so a
    campaign that pulled multiple 'cost' rows on the same day is summed, not
    duplicated. AI-260: the same cleanup rules as the window-total reader, woven
    by the SAME builder (`_campaign_spend_sql`) -- a rule applied on one grain
    and not the other would make the two readers disagree about which campaigns
    exist, and the pacing screen shows them side by side.

    Returns a list of dicts with keys:
      connector, campaign_ref (= breakdown_value), day (ISO date), spend (float).

    Degrade contract (AD-9, story rule "JAMAIS filtré silencieusement"): on any real
    backend error the failure is LOGGED and re-raised as WarehouseUnavailable so the
    caller surfaces an honest error -- NEVER a silent [] (a fabricated empty daily
    perimeter would falsely claim "100 % du réel est mappé"). A truly empty mart
    returns [] with a structured warning (an honest "no spend").
    """
    mode = _db_mode()
    application = _campaign_cleanup(project_id, cleanup)

    def _sql(schema_prefix: str, placeholder: str = "?") -> tuple[str, list]:
        return _campaign_spend_sql(
            schema_prefix,
            placeholder,
            project_id=project_id,
            start_date=start_date,
            end_date=end_date,
            daily=True,
            application=application,
        )

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
                "Warehouse unavailable: the local analytics database is missing."
            )
        if not _duckdb_relation_exists(path, "fact_daily_kpi"):
            logger.warning(
                "query_campaign_spend_daily: relation fact_daily_kpi absente de %r", path
            )
            raise WarehouseUnavailable(
                "Warehouse unavailable: the actual-spend table is absent."
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
                "Warehouse unavailable: cannot read the actual spend."
            ) from exc
        return _with_display_spend([_json_safe_row(r) for r in rows])

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
                "Warehouse unavailable: the marts are not populated yet."
            )
        sql_str, params = _sql("", placeholder="@")
        try:
            rows = _query_bigquery(sql_str, params)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "query_campaign_spend_daily failed: %s: %s", type(exc).__name__, exc
            )
            raise WarehouseUnavailable(
                "Warehouse unavailable: cannot read the actual spend."
            ) from exc
        return _with_display_spend([_json_safe_row(r) for r in rows])

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
                "Warehouse unavailable: the local analytics database is missing."
            )
        for relation in _PLAN_PACING_RELATIONS:
            if not _duckdb_relation_exists(path, relation):
                logger.warning(
                    "query_plan_vs_actual: relation %s absente de %r", relation, path
                )
                raise WarehouseUnavailable(
                    "Warehouse unavailable: the pacing views are absent."
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
                "Warehouse unavailable: cannot read the plan pacing."
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
                "Warehouse unavailable: the marts are not populated yet."
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
                "Warehouse unavailable: cannot read the plan pacing."
            ) from exc
        return out

    else:
        raise ValueError(f"Unknown TOOROW_DB_MODE: {mode!r}")


# ---------------------------------------------------------------------------
# Story 41.6 (E41-FR06) -- the Tax & Fees composition read.
#
# Reads the four READ-ONLY Tax marts delivered by Stories 41.2-41.5 for ONE
# project and ONE window. Same discipline as the pacing read above: the relation
# names are hard-coded constants, every caller value is a bound parameter (HG-3),
# and the ORDER BY comes from a per-relation map -- never from caller input.
#
# Degrade contract (AD-9, E41-NFR02): a missing DuckDB file or a missing relation
# raises WarehouseUnavailable, which the API surfaces as an opaque 503. A
# fabricated empty ladder would read as "your invoice equals your net media",
# which is precisely the lie E41-NFR02 forbids. A relation that is PRESENT and
# EMPTY returns empty lists -- an honest empty, and a different answer.
#
# MEASURED, and true of every environment that exists today: fee_tax_ladder_daily
# has never executed (fact_daily_kpi predates fx_rate and 21 staging models lack
# raw seeds -- commit 1c78a532, independent of that diff), so these reads raise
# WarehouseUnavailable everywhere right now. That is the honest behaviour and the
# seam test asserts the 503 rather than a fabricated 200.
# ---------------------------------------------------------------------------

# The Tax & Fees relations this read touches (documented allowlist -- NEVER
# interpolated from caller input; these are hard-coded constants).
_FEE_TAX_BRIDGE_RELATIONS = (
    "fee_tax_ladder_rollup",
    "fee_tax_ladder_daily",
    "fee_tax_verification_allocation",
    "fee_tax_revenue_alignment_daily",
)

# Deterministic ordering per relation, hard-coded beside the allowlist so a caller
# can never choose a sort expression.
_FEE_TAX_ORDER_BY = {
    "fee_tax_ladder_rollup": "date, rollup_kind, rollup_key",
    "fee_tax_ladder_daily": "date, connector, breakdown_dimension, breakdown_value",
    "fee_tax_verification_allocation": "row_kind, date, connector, breakdown_value",
    "fee_tax_revenue_alignment_daily": "date, tax_basis",
}

# Relations whose grain carries rows with a NULL date ON PURPOSE.
# fee_tax_verification_allocation's `rule_without_base` reason rows have no date
# because "it priced nothing on any day, so it has no day" -- a plain BETWEEN
# would drop exactly the disclosure the overlay exists to make.
_FEE_TAX_NULL_DATE_RELATIONS = frozenset({"fee_tax_verification_allocation"})

# Only the rollup relation carries a (rollup_kind, rollup_key) selector. Applying
# it to any other relation would be a SQL error, i.e. a caller mistake surfaced as
# an opaque 503.
_FEE_TAX_ROLLUP_SELECTOR_RELATIONS = frozenset({"fee_tax_ladder_rollup"})


def _build_fee_tax_query(
    schema_prefix: str,
    relation: str,
    project_id: str,
    date_from: str,
    date_to: str,
    *,
    rollup_kind: str | None = None,
    rollup_key: str | None = None,
    placeholder: str = "?",
) -> tuple[str, list]:
    """Build a parameterized SELECT * over one Tax & Fees relation.

    ``relation`` MUST be one of :data:`_FEE_TAX_BRIDGE_RELATIONS` (hard-coded
    constants, never attacker-controlled). project_id, the window bounds and the
    rollup selector are BOUND parameters (HG-3 injection safety).
    """
    if relation not in _FEE_TAX_BRIDGE_RELATIONS:
        # Defensive: only the four known Tax relations may be queried.
        raise ValueError(f"Unknown fee/tax relation: {relation!r}")

    params: list = [project_id, date_from, date_to]

    def ph(i: int) -> str:
        return f"@p{i}" if placeholder != "?" else "?"

    if relation in _FEE_TAX_NULL_DATE_RELATIONS:
        date_clause = (
            f"AND (date IS NULL OR (date >= {ph(1)} AND date <= {ph(2)}))"
        )
    else:
        date_clause = f"AND date >= {ph(1)} AND date <= {ph(2)}"

    selector = ""
    if relation in _FEE_TAX_ROLLUP_SELECTOR_RELATIONS:
        if rollup_kind:
            params.append(rollup_kind)
            selector += f"\n      AND rollup_kind = {ph(len(params) - 1)}"
        if rollup_key:
            params.append(rollup_key)
            selector += f"\n      AND rollup_key = {ph(len(params) - 1)}"

    order_by = _FEE_TAX_ORDER_BY[relation]
    sql = f"""
    SELECT *
    FROM {schema_prefix}{relation}
    WHERE project_id = {ph(0)}
      {date_clause}{selector}
    ORDER BY {order_by}
    """  # noqa: S608 -- relation + ORDER BY are hard-coded constants; values are bound params
    return sql, params


def _query_fee_tax_relations(
    project_id: str,
    date_from: str,
    date_to: str,
    pairs: tuple[tuple[str, str], ...],
    *,
    rollup_kind: str | None = None,
    rollup_key: str | None = None,
    what: str = "the Tax & Fees composition",
) -> dict[str, list[dict]]:
    """Read one or more allowlisted Tax relations, both adapter arms.

    Returns ``{key: rows}`` for every ``(key, relation)`` in *pairs*. Raises
    :class:`WarehouseUnavailable` when the file, the dataset or ANY of the named
    relations is absent, and on any query failure -- never a silent ``[]``.
    """
    mode = _db_mode()
    out: dict[str, list[dict]] = {}

    if mode == "duckdb":
        path = _duckdb_path()
        if not path or not os.path.exists(path):
            logger.warning(
                "query_fee_tax: duckdb file absent (%r) -- Tax marts not seeded", path
            )
            raise WarehouseUnavailable(
                "Warehouse unavailable: the local analytics database is missing."
            )
        for _key, relation in pairs:
            if not _duckdb_relation_exists(path, relation):
                logger.warning(
                    "query_fee_tax: relation %s absent from %r", relation, path
                )
                raise WarehouseUnavailable(
                    "Warehouse unavailable: the Tax & Fees views are absent."
                )
        try:
            for key, relation in pairs:
                sql, params = _build_fee_tax_query(
                    _duckdb_mart_prefix(project_id),
                    relation,
                    project_id,
                    date_from,
                    date_to,
                    rollup_kind=rollup_kind,
                    rollup_key=rollup_key,
                )
                out[key] = [_json_safe_row(r) for r in _query_duckdb(sql, params)]
        except Exception as exc:  # noqa: BLE001
            # NEVER swallow silently -- a broken mart must be observable AND propagate.
            logger.warning(
                "query_fee_tax failed (%s): %s: %s", what, type(exc).__name__, exc
            )
            raise WarehouseUnavailable(
                "Warehouse unavailable: cannot read the Tax & Fees composition."
            ) from exc
        return out

    if mode == "bigquery":
        if not _check_bigquery_mart(project_id):
            logger.warning(
                json.dumps(
                    {
                        "event": "warehouse_not_ready",
                        "message": "marts not populated -- run the dbt build first",
                    }
                )
            )
            raise WarehouseUnavailable(
                "Warehouse unavailable: the marts are not populated yet."
            )
        try:
            for key, relation in pairs:
                sql, params = _build_fee_tax_query(
                    "",
                    relation,
                    project_id,
                    date_from,
                    date_to,
                    rollup_kind=rollup_kind,
                    rollup_key=rollup_key,
                    placeholder="@",
                )
                out[key] = [_json_safe_row(r) for r in _query_bigquery(sql, params)]
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "query_fee_tax failed (%s): %s: %s", what, type(exc).__name__, exc
            )
            raise WarehouseUnavailable(
                "Warehouse unavailable: cannot read the Tax & Fees composition."
            ) from exc
        return out

    raise ValueError(f"Unknown TOOROW_DB_MODE: {mode!r}")


def query_fee_tax_bridge(
    project_id: str,
    date_from: str,
    date_to: str,
    *,
    rollup_kind: str | None = None,
    rollup_key: str | None = None,
) -> dict[str, list[dict]]:
    """Read the composed ladder + the verification overlay (Story 41.6, E41-FR06).

    Returns ``{"ladder": [...], "verification": [...]}``.

    The two relations are read TOGETHER and returned SEPARATELY on purpose: the
    verification allocation carries ``keep_separate = TRUE`` on every row and must
    never be merged into a ladder total (E41-AD4, Epic 27 invariant 4).
    """
    return _query_fee_tax_relations(
        project_id,
        date_from,
        date_to,
        (
            ("ladder", "fee_tax_ladder_rollup"),
            ("verification", "fee_tax_verification_allocation"),
        ),
        rollup_kind=rollup_kind,
        rollup_key=rollup_key,
        what="bridge",
    )


#: The six components a deep-dive may be requested for. `verification` reads the
#: overlay relation; the other five read the per-row ladder.
FEE_TAX_COMPONENTS = (
    "platform_fee",
    "regulatory_tax",
    "wht_gross_up",
    "agency_fee",
    "sales_tax",
    "verification",
)


def query_fee_tax_components(
    project_id: str,
    date_from: str,
    date_to: str,
    *,
    component: str,
) -> list[dict]:
    """Read the contributing rows behind ONE component, at their own grain."""
    if component not in FEE_TAX_COMPONENTS:
        raise ValueError(f"Unknown fee/tax component: {component!r}")
    relation = (
        "fee_tax_verification_allocation"
        if component == "verification"
        else "fee_tax_ladder_daily"
    )
    result = _query_fee_tax_relations(
        project_id,
        date_from,
        date_to,
        (("rows", relation),),
        what=f"component {component}",
    )
    return result.get("rows", [])


def query_fee_tax_ladder_daily(
    project_id: str, date_from: str, date_to: str
) -> list[dict]:
    """Read the per-row composed ladder over a window (Story 58.6).

    THE DAILY RELATION, NOT THE ROLLUP. The Datastream `Cost` tab reads one
    connector's slice, and `fee_tax_ladder_rollup` is grained on
    (rollup_kind, rollup_key) with no connector to select on. Calling
    `query_fee_tax_components` for the same rows would name a component the
    caller is not deep-diving, so the reading would be labelled as something it
    is not.
    """
    result = _query_fee_tax_relations(
        project_id,
        date_from,
        date_to,
        (("rows", "fee_tax_ladder_daily"),),
        what="ladder daily",
    )
    return result.get("rows", [])


def query_fee_tax_alignment(
    project_id: str, date_from: str, date_to: str
) -> list[dict]:
    """Read the HT/TTC revenue alignment rows, unchanged (Story 41.5, E41-FR07)."""
    result = _query_fee_tax_relations(
        project_id,
        date_from,
        date_to,
        (("rows", "fee_tax_revenue_alignment_daily"),),
        what="alignment",
    )
    return result.get("rows", [])


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
    *,
    strict: bool = False,
) -> list[dict]:
    """Return the distinct values observed on one breakdown partition, with counts.

    Rows look like ``{"connector", "breakdown_value", "row_count"}``.

    TWO DEGRADE CONTRACTS, AND THE CALLER PICKS THE ONE ITS SCREEN CAN SURVIVE.
    By default this returns ``[]`` on an unreachable mart, with a structured
    warning (AD-12: marts only, never raw_*) -- the geography monitor of
    ``dq_monitors`` is written around that and treats "no observation" as a
    signal it may skip.

    ``strict=True`` raises ``WarehouseUnavailable`` instead, and story 61.1 is why
    it exists: on the ``Placements`` tab, ``[]`` is the exact shape of "this
    connector emitted no placement", so a swallowed failure would render an
    unreadable mart as a measured absence and offer an empty list of candidates
    to attach. "There is nothing" and "we could not look" are two reports, and a
    reader must not be handed one for the other.
    """
    mode = _db_mode()

    def _degrade(event: str, detail: str) -> list[dict]:
        logger.warning(json.dumps({"event": event, "error": detail}))
        if strict:
            raise WarehouseUnavailable(
                "Warehouse unavailable: cannot read the observed breakdown values."
            )
        return []

    if mode == "duckdb":
        sql, params = _build_breakdown_value_query(
            _duckdb_mart_prefix(project_id), project_id, dimension, start_date, end_date
        )
        try:
            return _query_duckdb(sql, params)
        except Exception as exc:  # noqa: BLE001
            return _degrade("warehouse_breakdown_read_failed", str(exc))
    if mode == "bigquery":
        if not _check_bigquery_mart(project_id):
            return _degrade(
                "warehouse_not_ready", "marts not populated — run Story 1.4 seed first"
            )
        sql, params = _build_breakdown_value_query(
            "", project_id, dimension, start_date, end_date, placeholder="@"
        )
        try:
            return _query_bigquery(sql, params)
        except Exception as exc:  # noqa: BLE001
            return _degrade("warehouse_breakdown_read_failed", str(exc))
    raise ValueError(f"Unknown TOOROW_DB_MODE: {mode!r}")
