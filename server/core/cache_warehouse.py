"""toorow (toorow) -- read-through cache builder (Story 19.1, CAP-22 / AD-22).

Exports the allowlisted marts of the ORIGIN warehouse into a SEPARATE, ephemeral
DuckDB file (the *cache*) so that repeated in-window questions are served at ~50 ms
with **zero BigQuery scan** (19.2 does the read-through routing; THIS module only
BUILDS the snapshot + its manifest).

Naming discipline (epic-19, "DuckDB dev != cache DuckDB prod")
--------------------------------------------------------------
There are TWO DuckDB files and they must never be confused:

  * ``local.duckdb`` (env ``TOOROW_DUCKDB_PATH``, schema ``main_marts.*``) --
    the DEV warehouse, the source of truth in dev, the local equivalent of BigQuery.
  * ``cache_warehouse.duckdb`` (env ``TOOROW_CACHE_PATH``) -- the ephemeral
    read-through CACHE this module writes. A snapshot of the same marts, never a
    second compute source. Lost on Cloud Run restart, rebuilt lazily / at boot.

The ORIGIN is BigQuery in prod (``TOOROW_DB_MODE=bigquery``) and IS ALREADY the
dev DuckDB in dev (``TOOROW_DB_MODE=duckdb``). In dev we therefore test the
builder by simulation ``origin-duckdb -> cache-duckdb`` (two distinct files, two
connections). The real BigQuery extract path is **Phase B (AI-08), BLOCKED** --
its structure is prepared and annotated below, but not testable here.

Invariants held by this module (epic-19 (a)-(f))
------------------------------------------------
  * (b) PROVENANCE INTACTE (AD-9): ``pull_id`` / ``loaded_at`` are copied AS-IS,
    column-for-column -- never re-emitted, never recomputed.
  * (d) ISOLATION PROJET (AD-5): every export filters ``project_id`` -- the cache
    only ever holds rows for the covered projects, scoped exactly like every read.
  * (e) LE CACHE N'EST PAS UN WRITER (AD-8): the origin is opened READ-ONLY when the
    backend allows it and the export is a pure ``SELECT`` -- never an
    INSERT/ALTER/TRUNCATE against a mart. BigQuery/DuckDB origin stays the truth.
  * (f) EPHEMERE ASSUME: any export failure (origin unreachable, corrupt cache, ...)
    is logged + meta-alerted and NEVER raised to the caller. The service keeps
    reading the origin -- absence of cache is a latency degradation, not an error.

# AD-2: source-agnostic -- no module-specific strings here.
# AD-12: reads marts only (never raw_* tables, CSV, or direct APIs).
"""

from __future__ import annotations

import json
import logging
import os
import time as _time
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment-driven configuration
#
# TOOROW_CACHE_ENABLED    = "false" (default, prudent) | "true"
# TOOROW_CACHE_PATH       = cache file path (default "cache_warehouse.duckdb")
# TOOROW_CACHE_WINDOW_DAYS = rolling window length in days (default 30)
# TOOROW_CACHE_TABLES     = comma-separated allowlist (default: _DEFAULT_TABLES)
#
# ORIGIN config is the SAME as warehouse.py:
#   TOOROW_DB_MODE        = "duckdb" (default) | "bigquery"
#   TOOROW_DUCKDB_PATH    = path to local.duckdb (dev origin)
#   GCP_PROJECT              = GCP project ID (bigquery origin, Phase B)
# ---------------------------------------------------------------------------

_CACHE_ENABLED_ENV = "TOOROW_CACHE_ENABLED"
_CACHE_PATH_ENV = "TOOROW_CACHE_PATH"
_CACHE_WINDOW_DAYS_ENV = "TOOROW_CACHE_WINDOW_DAYS"
_CACHE_TABLES_ENV = "TOOROW_CACHE_TABLES"

_DEFAULT_CACHE_PATH = "cache_warehouse.duckdb"
_DEFAULT_WINDOW_DAYS = 30

# Manifest schema version -- bump when the manifest layout changes so 19.2 can
# reject a manifest it does not understand (fail closed -> fallthrough).
CACHE_SCHEMA_VERSION = 1

# Internal manifest table name inside the cache file (never a mart name).
_MANIFEST_TABLE = "_cache_manifest"

# ---------------------------------------------------------------------------
# Allowlist -- deduced from the REAL reads of warehouse.py (Story 19.1 duty).
#
# Every public warehouse read routes through a _build_* builder that filters
# BOTH ``project_id`` AND ``date BETWEEN ...`` on one of these relations:
#
#   fact_daily_kpi                  <- _build_query / _build_report_query
#                                      (query_daily_report, query_report additive)
#   semantic_ctr                    <- _build_semantic_view_query (AD-4 non-additive,
#                                      query_report with metric="ctr")
#   semantic_cpa                    <- _build_semantic_view_query (AD-4 non-additive,
#                                      query_report with metric="cpa")
#   semantic_roas                   <- _build_semantic_view_query (AD-4 non-additive,
#                                      query_report with metric="roas")
#   semantic_avg_position           <- _build_semantic_view_query (AD-4 non-additive,
#                                      query_report with metric="average_position").
#                                      spec F4 (19.4 debt RESOLVED): warehouse.py now
#                                      uses _SEMANTIC_VIEW_BY_METRIC which maps
#                                      "average_position" -> "semantic_avg_position"
#                                      (the real dbt view, matching business_alerts.py
#                                      G-03 fix). The previous "semantic_average_position"
#                                      (non-existent) bug is fixed; this view is now
#                                      legitimately cached and routed.
#   semantic_avg_position_composite <- _build_composite_position_query (Story 10.5)
#   dedup_estimate                  <- _build_dedup_estimate_query (Epic 17)
#   transaction_reconciliation_daily <- query_transaction_reconciliation_daily (17.4)
#
# EXCLUSIONS (intentional, documented):
#
#   semantic_average_position  DETTE SOLDEE (19.4): ce nom n'existe plus dans le
#     routeur. warehouse._SEMANTIC_VIEW_BY_METRIC redirige "average_position" vers
#     "semantic_avg_position" (voir ci-dessus). "semantic_average_position" ne sera
#     jamais construit ni routé.
#
#   fact_daily_kpi_all_pulls  EXCLUDED: chemin as-of (get_daily_report_asof),
#     full-history non-superseded pour comparaison de révisions -- la cacher
#     irait contre l'économie de fenêtre (directive Jean).
#
# HARD REQUIREMENT: every allowlisted relation MUST expose ``date`` and
# ``project_id`` columns so the window + AD-5 filters apply. A relation missing
# either is skipped with a structured warning (never crashes the build).
# ---------------------------------------------------------------------------

_DEFAULT_TABLES: tuple[str, ...] = (
    "fact_daily_kpi",
    "semantic_ctr",
    "semantic_cpa",
    "semantic_roas",
    # spec F4 (19.4 debt resolved): semantic_avg_position is NOW routed by
    # query_report(metrics=["average_position"]) via _SEMANTIC_VIEW_BY_METRIC in
    # warehouse.py (which maps average_position -> semantic_avg_position, aligning
    # with the real dbt view name and business_alerts.py G-03 fix). It belongs in
    # the default allowlist so cache hits are possible for this metric.
    "semantic_avg_position",
    "semantic_avg_position_composite",
    "dedup_estimate",
    "transaction_reconciliation_daily",
)


def _cache_enabled() -> bool:
    return os.environ.get(_CACHE_ENABLED_ENV, "false").lower() == "true"


def _cache_path() -> str:
    return os.environ.get(_CACHE_PATH_ENV, _DEFAULT_CACHE_PATH)


def _window_days() -> int:
    try:
        days = int(os.environ.get(_CACHE_WINDOW_DAYS_ENV, str(_DEFAULT_WINDOW_DAYS)))
    except (TypeError, ValueError):
        return _DEFAULT_WINDOW_DAYS
    return days if days > 0 else _DEFAULT_WINDOW_DAYS


def _allowlist() -> list[str]:
    """Return the declarative allowlist (env override or the deduced default).

    Adding a table is a config line (``TOOROW_CACHE_TABLES``), not code.
    """
    raw = os.environ.get(_CACHE_TABLES_ENV, "")
    if raw.strip():
        return [t.strip() for t in raw.split(",") if t.strip()]
    return list(_DEFAULT_TABLES)


def _origin_mode() -> str:
    return os.environ.get("TOOROW_DB_MODE", "duckdb")


def _origin_duckdb_path() -> str:
    return os.environ.get("TOOROW_DUCKDB_PATH", "")


def _duckdb_mart_prefix() -> str:
    """DuckDB origin schema prefix for the cache export.

    Delegated to the single naming point (Story 24.1). Deliberately UNSCOPED
    (no project_id): used ONLY on the legacy (flag OFF) path where one schema
    (``main_marts.``) holds every project's marts.
    """
    from core import warehouse_tenancy  # noqa: PLC0415

    return warehouse_tenancy.mart_prefix(None)


def _mart_schema_prefixes() -> list[str]:
    """Return the origin mart schema prefixes the cache must export from.

    Story 24.4 (AC5, CONDITION DE FLIP): the unscoped cache export is the second
    cross-project path that would degrade SILENTLY under the org topology. Its
    resolution now depends on the flag:

      * flag OFF (default) -> the EXACT legacy single prefix (``main_marts.``);
        no active-org listing, bit-identical to pre-24.4.
      * flag ON            -> ONE prefix per ACTIVE org (``org_<wslug>_marts.``),
        resolved via ``warehouse_tenancy.list_active_orgs`` (the single naming
        point). Each org's marts are exported into the ONE cache file (per-org
        cache FILES are story 24.6's concern).
    """
    from core import warehouse_tenancy  # noqa: PLC0415

    if not warehouse_tenancy.org_schemas_enabled():
        return [warehouse_tenancy.mart_prefix(None)]
    return [f"{schemas.marts}." for schemas in warehouse_tenancy.list_active_orgs()]


# ---------------------------------------------------------------------------
# Manifest dataclass-ish shape (plain dict for JSON portability)
# ---------------------------------------------------------------------------


def _now_utc_iso() -> str:
    """Return the current UTC instant as an ISO-8601 string (cache_built_at, AD-9)."""
    return datetime.now(tz=timezone.utc).isoformat()


def _window_bounds(window_days: int, *, today: date | None = None) -> tuple[str, str]:
    """Return the inclusive ``[min_date, max_date]`` ISO strings of the cache window.

    ``max_date`` is today; ``min_date`` is ``today - (window_days - 1)`` so the
    window spans exactly ``window_days`` calendar days. Passing *today* keeps the
    function pure/testable.
    """
    ref = today or date.today()
    max_date = ref
    min_date = ref - timedelta(days=window_days - 1)
    return min_date.isoformat(), max_date.isoformat()


# ---------------------------------------------------------------------------
# Origin readers (read-only) -- dev DuckDB now; BigQuery = Phase B BLOCKED.
# ---------------------------------------------------------------------------


def _origin_columns_duckdb(con, origin_alias: str, schema_prefix: str, table: str) -> list[str]:
    """Return the column names of the origin relation (read-only, via ATTACH alias).

    ``origin_alias`` is the ATTACH database alias; ``schema_prefix`` is the mart
    schema (``main_marts.``). Never string-interpolates user input -- the relation
    name is from the allowlisted config only.
    """
    rel = con.execute(
        f"SELECT * FROM {origin_alias}.{schema_prefix}{table} LIMIT 0"  # noqa: S608
    )
    return [d[0] for d in rel.description]


class _CacheSkipTable(Exception):
    """Raised internally when an allowlisted relation cannot be exported cleanly.

    Caught per-table by the builder so ONE bad/absent relation never aborts the
    whole cache build (allowlist unknown -> ignored properly, epic-19 acceptance).
    """


def _discover_project_ids_duckdb(con, origin_alias: str, schema_prefix: str) -> list[str]:
    """Return the distinct ``project_id`` values present in origin ``fact_daily_kpi``.

    Used when the caller does not pin an explicit project list: the cache covers
    every project the hot-path fact table knows about. Never raises -- returns []
    when the table is absent so the caller degrades to "empty cache".
    """
    try:
        rel = con.execute(
            f"SELECT DISTINCT project_id "  # noqa: S608 -- allowlisted relation only
            f"FROM {origin_alias}.{schema_prefix}fact_daily_kpi "
            f"ORDER BY project_id"
        )
        return [r[0] for r in rel.fetchall() if r[0] is not None]
    except Exception as exc:  # noqa: BLE001 -- fact table may be absent in a fresh dev DB
        logger.warning(
            "cache_warehouse: project_discovery_failed: %s: %s",
            type(exc).__name__,
            exc,
        )
        return []


# ---------------------------------------------------------------------------
# Cache writer -- writes the SEPARATE cache_warehouse.duckdb file.
#
# Copy strategy: the cache connection is PRIMARY and the origin is ATTACHed
# READ_ONLY (AD-8 / invariant e). ``CREATE TABLE <cache>.<t> AS SELECT * FROM
# <origin>.main_marts.<t> WHERE ...`` copies the window+project slice with
# DuckDB-native types PRESERVED end-to-end -- so pull_id / loaded_at land in the
# cache byte-identical to the origin (invariant b, AD-9). No Python round-trip,
# no type inference guesswork.
# ---------------------------------------------------------------------------


def _copy_table_via_attach(
    cache_con,
    origin_alias: str,
    schema_prefix: str,
    table: str,
    project_ids: list[str],
    min_date: str,
    max_date: str,
) -> int:
    """Copy the window+project slice of one allowlisted mart into the cache. Returns rows.

    Raises ``_CacheSkipTable`` when the relation lacks the ``date`` / ``project_id``
    columns needed to scope it (never a partial/unsafe copy). Idempotent: the cache
    table is dropped and recreated on every build so a rebuild leaves no stale rows.

    All user-influenced values (project ids, dates) are BOUND parameters -- never
    string-interpolated (HG-3). The relation name is from the allowlisted config.
    """
    cols = _origin_columns_duckdb(cache_con, origin_alias, schema_prefix, table)
    if "date" not in cols or "project_id" not in cols:
        raise _CacheSkipTable(
            f"relation {table!r} lacks date/project_id columns (cols={cols})"
        )

    proj_phs = ", ".join("?" for _ in project_ids)
    cache_con.execute(f'DROP TABLE IF EXISTS "{table}"')
    cache_con.execute(
        f'CREATE TABLE "{table}" AS '  # noqa: S608 -- allowlisted relation; values bound
        f"SELECT * FROM {origin_alias}.{schema_prefix}{table} "
        f"WHERE project_id IN ({proj_phs}) "
        f"AND date BETWEEN ? AND ? "
        f"ORDER BY project_id, date",
        [*project_ids, min_date, max_date],
    )
    count_rel = cache_con.execute(f'SELECT COUNT(*) FROM "{table}"')  # noqa: S608
    return int(count_rel.fetchone()[0])


def _append_table_via_attach(
    cache_con,
    origin_alias: str,
    schema_prefix: str,
    table: str,
    project_ids: list[str],
    min_date: str,
    max_date: str,
) -> int:
    """INSERT the window+project slice of *table* from a FURTHER org schema. Returns appended rows.

    Story 24.4 (AC5): under the org topology the cache is fed from N per-org marts
    schemas into ONE cache table -- the first schema CREATEs the table
    (``_copy_table_via_attach``), each subsequent schema APPENDs into it. Same
    scoping (project_id + window) and same bound-parameter discipline (HG-3).
    A schema missing the relation raises ``_CacheSkipTable`` (caller skips it).
    """
    cols = _origin_columns_duckdb(cache_con, origin_alias, schema_prefix, table)
    if "date" not in cols or "project_id" not in cols:
        raise _CacheSkipTable(
            f"relation {table!r} lacks date/project_id columns (cols={cols})"
        )

    proj_phs = ", ".join("?" for _ in project_ids)
    before_rel = cache_con.execute(f'SELECT COUNT(*) FROM "{table}"')  # noqa: S608
    before = int(before_rel.fetchone()[0])
    cache_con.execute(
        # BY NAME (review 24.4 F-3): defensive against column-order divergence
        # between per-org builds -- positional INSERT would silently mix columns.
        f'INSERT INTO "{table}" BY NAME '  # noqa: S608 -- allowlisted relation; values bound
        f"SELECT * FROM {origin_alias}.{schema_prefix}{table} "
        f"WHERE project_id IN ({proj_phs}) "
        f"AND date BETWEEN ? AND ?",
        [*project_ids, min_date, max_date],
    )
    after_rel = cache_con.execute(f'SELECT COUNT(*) FROM "{table}"')  # noqa: S608
    return int(after_rel.fetchone()[0]) - before


def _write_manifest(
    cache_con,
    *,
    tables: list[str],
    min_date: str,
    max_date: str,
    cache_built_at: str,
    row_counts: dict[str, int],
    project_ids: list[str],
) -> None:
    """Write the ``_cache_manifest`` row -- the hit/miss source of truth for 19.2.

    The manifest records: cached tables, window ``[min_date, max_date]``,
    ``cache_built_at`` (UTC ISO), per-table row counts, covered ``project_ids``,
    and the schema version. Stored as a single JSON-bearing row so 19.2 reads it
    with one SELECT and can evolve the payload behind ``schema_version``.
    """
    cache_con.execute(f'DROP TABLE IF EXISTS "{_MANIFEST_TABLE}"')
    cache_con.execute(
        f'CREATE TABLE "{_MANIFEST_TABLE}" ('
        "schema_version INTEGER, cache_built_at VARCHAR, "
        "min_date VARCHAR, max_date VARCHAR, tables_json VARCHAR, "
        "row_counts_json VARCHAR, project_ids_json VARCHAR)"
    )
    cache_con.execute(
        f'INSERT INTO "{_MANIFEST_TABLE}" VALUES (?, ?, ?, ?, ?, ?, ?)',
        [
            CACHE_SCHEMA_VERSION,
            cache_built_at,
            min_date,
            max_date,
            json.dumps(tables),
            json.dumps(row_counts),
            json.dumps(project_ids),
        ],
    )


def read_manifest(cache_path: str | None = None) -> dict | None:
    """Read the cache manifest as a dict, or None when absent/unreadable.

    Consumed by 19.2 (hit/miss decision) and 19.3 (health surface). Never raises --
    a missing/corrupt cache returns None so callers fallthrough to the origin
    (invariant f). Returned dict keys: ``schema_version``, ``cache_built_at``,
    ``min_date``, ``max_date``, ``tables`` (list), ``row_counts`` (dict),
    ``project_ids`` (list).
    """
    path = cache_path or _cache_path()
    if not path or not os.path.exists(path):
        return None
    try:
        import duckdb  # noqa: PLC0415

        con = duckdb.connect(path, read_only=True)
        try:
            rel = con.execute(
                f'SELECT schema_version, cache_built_at, min_date, max_date, '  # noqa: S608
                f'tables_json, row_counts_json, project_ids_json '
                f'FROM "{_MANIFEST_TABLE}" LIMIT 1'
            )
            row = rel.fetchone()
        finally:
            con.close()
        if row is None:
            return None
        return {
            "schema_version": row[0],
            "cache_built_at": row[1],
            "min_date": row[2],
            "max_date": row[3],
            "tables": json.loads(row[4]) if row[4] else [],
            "row_counts": json.loads(row[5]) if row[5] else {},
            "project_ids": json.loads(row[6]) if row[6] else [],
        }
    except Exception as exc:  # noqa: BLE001 -- absent/corrupt cache => None, never raise
        logger.warning(
            "cache_warehouse: manifest_read_failed: %s: %s",
            type(exc).__name__,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Public builder API
# ---------------------------------------------------------------------------


def rebuild_cache(
    project_ids: list[str] | None = None,
    *,
    today: date | None = None,
) -> dict:
    """Rebuild the read-through cache from the origin warehouse (Story 19.1).

    Exports each allowlisted mart's window+project slice from the ORIGIN into the
    SEPARATE cache file (``TOOROW_CACHE_PATH``), then writes the manifest. This
    is the reusable on-demand entry point consumed later by the post-nightly hook
    and by 19.3's manual trigger.

    Args:
        project_ids: explicit projects to cache. When None, the builder discovers
            the projects present in the origin's ``fact_daily_kpi`` (AD-5: only
            those projects' rows enter the cache).
        today: reference date for the window (defaults to ``date.today()``);
            injectable for deterministic tests.

    Returns a result dict:
        ``{"status": "ok"|"disabled"|"skipped"|"failed", "tables": [...],
           "row_counts": {...}, "project_ids": [...], "min_date": str,
           "max_date": str, "cache_built_at": str, "cache_path": str,
           "reason": str|None}``.

    NEVER raises (invariant f): any failure is logged, meta-alerted, and returned
    as ``status="failed"`` so the caller (scheduler / endpoint) keeps the service
    healthy on the origin path.

    AD-8 (invariant e): the origin connection is opened READ-ONLY; the export is a
    pure SELECT. No mart is ever written.
    """
    if not _cache_enabled():
        logger.info("cache_warehouse: rebuild_skipped -- TOOROW_CACHE_ENABLED not true")
        return {"status": "disabled", "reason": "TOOROW_CACHE_ENABLED not true"}

    try:
        return _rebuild_cache_inner(project_ids=project_ids, today=today)
    except Exception as exc:  # noqa: BLE001 -- invariant f: never raise to caller
        logger.warning(
            "cache_warehouse: rebuild_failed: %s: %s",
            type(exc).__name__,
            exc,
        )
        _meta_alert("rebuild_cache", f"{type(exc).__name__}: {exc}")
        return {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}


def _rebuild_cache_inner(
    project_ids: list[str] | None,
    today: date | None,
) -> dict:
    """Do the actual origin->cache export. Raises on failure (wrapped by rebuild_cache)."""
    mode = _origin_mode()
    if mode == "bigquery":
        # ---------------------------------------------------------------------
        # Phase B (AI-08) -- BLOCKED. The real BigQuery extract path lives here:
        # run an EXPORT DATA / SELECT against fact_daily_kpi + semantic/dedup views
        # (window + project filtered, read-only), stream the result into the cache
        # DuckDB file. Billing note (AI-53, estimé sur doc publique Google Cloud,
        # à mesurer en Phase B): EXPORT DATA lui-même n'est pas facturé pour
        # l'opération d'export, mais le SELECT sous-jacent EST facturé au scan
        # (tarification on-demand, octets logiques scannés) -- ne pas amalgamer les
        # deux. Un slice 30 j × N projets de marts consolidés reste petit vs les
        # lectures in-window répétées économisées. À mesurer sur volumétrie réelle
        # en Phase B avant de tirer des conclusions de coût.
        # Not testable in dev (no live GCP) -- simulated via the duckdb origin below.
        # ---------------------------------------------------------------------
        raise _CacheOriginUnavailable(
            "bigquery origin extract is Phase B (AI-08) BLOCKED -- not implemented"
        )
    if mode != "duckdb":
        raise _CacheOriginUnavailable(f"unknown TOOROW_DB_MODE: {mode!r}")

    origin_path = _origin_duckdb_path()
    if not origin_path or not os.path.exists(origin_path):
        # Origin unreachable -> treated as a failure (meta-alerted by the wrapper),
        # never a raised exception past rebuild_cache (invariant f).
        raise _CacheOriginUnavailable(
            f"origin duckdb not found at TOOROW_DUCKDB_PATH={origin_path!r}"
        )

    import duckdb  # noqa: PLC0415

    # Story 24.4 (AC5): one prefix on the legacy (flag OFF) path, one per active org
    # under the org topology (flag ON). On the OFF path this is exactly
    # ``[main_marts.]`` -- discovery + copy stay bit-identical to pre-24.4.
    schema_prefixes = _mart_schema_prefixes()
    window_days = _window_days()
    min_date, max_date = _window_bounds(window_days, today=today)
    tables = _allowlist()
    cache_path = _cache_path()
    origin_alias = "origin"

    # F-3: AD-8 path-collision guard. If TOOROW_CACHE_PATH resolves to the same
    # real path as TOOROW_DUCKDB_PATH, the cache build would open the ORIGIN as
    # a primary read-write connection and corrupt it (violation AD-8). Raise so the
    # outer rebuild_cache wrapper logs + meta-alerts and returns status="failed"
    # (invariant f: the exception never propagates past rebuild_cache).
    if os.path.realpath(cache_path) == os.path.realpath(origin_path):
        raise _CacheOriginUnavailable(
            f"TOOROW_CACHE_PATH and TOOROW_DUCKDB_PATH resolve to the same "
            f"file ({os.path.realpath(cache_path)!r}). Opening the origin warehouse "
            f"as a writable primary connection violates AD-8. "
            f"Set TOOROW_CACHE_PATH to a DISTINCT file."
        )

    # F-6: Atomic build. Write into a .tmp file first; os.replace() it into
    # cache_path only after the manifest is committed. A crash mid-build leaves
    # the previous cache file intact (the .tmp is silently overwritten on the
    # next attempt). This prevents a partially-written cache from being served
    # with a stale manifest from the previous build.
    tmp_path = cache_path + ".tmp"
    if os.path.exists(tmp_path):
        try:
            os.remove(tmp_path)
        except OSError as exc:
            logger.debug("cache_warehouse: orphan_tmp_remove_failed: %s", exc)

    # The CACHE file is the PRIMARY connection; the ORIGIN is ATTACHed READ_ONLY
    # (AD-8 / invariant e: the build physically cannot write a mart -- DuckDB rejects
    # any write against a READ_ONLY attached database). Build into the SEPARATE cache
    # file (never TOOROW_DUCKDB_PATH -- distinct from local.duckdb).
    cache_con = duckdb.connect(tmp_path)
    try:
        # ATTACH is a catalog/DDL statement -- DuckDB does not reliably bind a
        # placeholder for the path, so it is embedded as a single-quoted literal.
        # The path comes from TOOROW_DUCKDB_PATH (operator env, not user input);
        # single quotes are escaped defensively. READ_ONLY enforces AD-8 (invariant e).
        origin_literal = origin_path.replace("'", "''")
        cache_con.execute(
            f"ATTACH '{origin_literal}' AS {origin_alias} (READ_ONLY)"  # noqa: S608
        )
        try:
            # Story 24.4 (AC5): discover projects across ALL mart schemas (one on
            # the flag-OFF path, one per active org on the ON path) -- each schema is
            # separate so a single SELECT DISTINCT on one schema is not enough.
            if project_ids:
                covered_projects = list(project_ids)
            else:
                discovered: list[str] = []
                seen: set[str] = set()
                for prefix in schema_prefixes:
                    for pid in _discover_project_ids_duckdb(
                        cache_con, origin_alias, prefix
                    ):
                        if pid not in seen:
                            seen.add(pid)
                            discovered.append(pid)
                covered_projects = discovered

            row_counts: dict[str, int] = {}
            exported_tables: list[str] = []

            if covered_projects:
                for table in tables:
                    # CREATE from the first schema that has the table, APPEND the
                    # rest -- so N per-org schemas feed the ONE cache table (AC5).
                    created = False
                    total = 0
                    exported = False
                    for prefix in schema_prefixes:
                        try:
                            if not created:
                                n = _copy_table_via_attach(
                                    cache_con, origin_alias, prefix, table,
                                    covered_projects, min_date, max_date,
                                )
                                created = True
                            else:
                                n = _append_table_via_attach(
                                    cache_con, origin_alias, prefix, table,
                                    covered_projects, min_date, max_date,
                                )
                        except _CacheSkipTable as skip:
                            logger.warning(
                                "cache_warehouse: table_skipped table=%s prefix=%s reason=%s",
                                table, prefix, skip,
                            )
                            continue
                        except Exception as exc:  # noqa: BLE001 -- one bad relation != abort
                            logger.warning(
                                "cache_warehouse: table_export_failed table=%s prefix=%s %s: %s",
                                table, prefix, type(exc).__name__, exc,
                            )
                            continue
                        total += n
                        exported = True
                    if exported:
                        row_counts[table] = total
                        exported_tables.append(table)

            cache_built_at = _now_utc_iso()
            _write_manifest(
                cache_con,
                tables=exported_tables,
                min_date=min_date,
                max_date=max_date,
                cache_built_at=cache_built_at,
                row_counts=row_counts,
                project_ids=covered_projects,
            )
        finally:
            try:
                cache_con.execute(f"DETACH {origin_alias}")
            except Exception as exc:  # noqa: BLE001 -- best-effort; never mask the real error
                logger.debug("cache_warehouse: detach_failed: %s", exc)
    finally:
        cache_con.close()

    # F-6: Atomic promotion. os.replace() is atomic on POSIX and best-effort on
    # Windows (same-volume moves). The manifest is already committed inside tmp_path
    # at this point -- only now does the final cache_path become visible to readers.
    #
    # perf F-3 (Windows): on Windows, os.replace() can raise PermissionError when a
    # concurrent reader holds an open handle on cache_path (DuckDB opens files with
    # shared-read semantics that may conflict with file-rename on NTFS). Retry up to
    # 3 times with 50 ms intervals before giving up. After exhaustion, the exception
    # propagates to _rebuild_cache_inner -> rebuild_cache which logs + meta-alerts
    # and returns status="failed" (invariant f: never raises to the caller).
    #
    # perf F-4 (invariant): there is at most ONE .tmp file at any point. The orphan
    # check at the top of _rebuild_cache_inner removes a leftover .tmp from a
    # previous crashed build before writing a new one. A crash after os.replace()
    # leaves no .tmp; a crash before it leaves a .tmp that will be overwritten on
    # the next attempt (the cleanup at the top removes it first, then a fresh
    # cache_con writes a new one).
    _REPLACE_RETRIES = 3
    _REPLACE_RETRY_SLEEP_S = 0.05
    for _attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(tmp_path, cache_path)
            break
        except PermissionError:
            if _attempt < _REPLACE_RETRIES - 1:
                _time.sleep(_REPLACE_RETRY_SLEEP_S)
            else:
                raise  # exhausted -- propagates to rebuild_cache -> status="failed"

    total_rows = sum(row_counts.values())
    logger.info(
        "cache_warehouse: rebuild_ok tables=%d projects=%d rows=%d window=[%s,%s] "
        "cache_path=%s built_at=%s",
        len(exported_tables),
        len(covered_projects),
        total_rows,
        min_date,
        max_date,
        cache_path,
        cache_built_at,
    )
    return {
        "status": "ok",
        "tables": exported_tables,
        "row_counts": row_counts,
        "project_ids": covered_projects,
        "min_date": min_date,
        "max_date": max_date,
        "cache_built_at": cache_built_at,
        "cache_path": cache_path,
        "reason": None,
    }


class _CacheOriginUnavailable(Exception):
    """Origin warehouse cannot be read (missing file, BLOCKED backend, unknown mode).

    Raised by the inner builder and converted into ``status="failed"`` +
    meta-alert by ``rebuild_cache`` (invariant f: never propagates to the caller).
    """


def _meta_alert(step: str, reason: str) -> None:
    """Best-effort meta-alert on rebuild failure (mirrors scheduler AI-32 pattern).

    Delegates to the scheduler's ``_insert_meta_alert`` so a failed cache build
    surfaces on the normal alert-delivery path. Never raises -- if the scheduler /
    DB is unavailable the failure is already logged by the caller.
    """
    try:
        from core.scheduler import _insert_meta_alert  # noqa: PLC0415

        _insert_meta_alert(step, reason)
    except Exception as exc:  # noqa: BLE001
        logger.debug("cache_warehouse: meta_alert_failed step=%s: %s", step, exc)


# ===========================================================================
# Story 12.19 -- deterministic daily sample-preview reader (server-side masking).
#
# GOAL: for each day in [date_from, date_to], return the FIRST ``limit`` eligible
# rows of the datastream's warehouse materialisation for a requested pipeline stage
# (collected | mapped | processed | published), with a DETERMINISTIC, reproducible
# ordering so "first N" is stable evidence -- not a random draw.
#
# STAGE MATERIALISATION -- HONEST APPROXIMATION (AD-9, AD-12, "do not invent data"):
#   The readable warehouse cache holds ONLY the consolidated dbt marts
#   (``fact_daily_kpi`` et al -- AD-12: marts only, never raw_* tables). There is NO
#   physically distinct per-stage relation in the cache/origin for a raw *collected*
#   extract, an intermediate *mapped* projection, or a *processed* view: mapping is
#   Postgres metadata (app.datastream_mappings / mapping_versions) and *published* is
#   a Postgres POINTER (app.datastreams.current_published_execution_id) over the SAME
#   mart rows -- publication PROMOTES a candidate, it never re-materialises the data
#   (see datastream_publication.py: "NO BigQuery write"). Therefore every stage reads
#   the SAME mart slice and each response HONESTLY declares which materialisation was
#   actually served (``served_stage``) plus WHY when it differs from the request
#   (``stage_note``). This module NEVER fabricates a distinct-per-stage row set.
#
# The stage-specific VERSION provenance IS honest and distinct: the caller passes the
# mapping_version_id / current_published_execution_id it resolved from Postgres, and
# this reader echoes exactly the one that applies to the requested stage.
#
# SECURITY (mandatory): raw sensitive values NEVER leave the server. Columns whose
# name matches the PII heuristic (reused from schema_context_gen.PII_PATTERN) are
# masked to a fixed sentinel BEFORE the row dict is built -- the raw value is read
# from the warehouse into a local and discarded; it is never placed in the response.
# ===========================================================================

# Hard caps (evidence, not bulk export). Per-day rows <= _SAMPLE_MAX_LIMIT and a
# total cell budget guard so a very wide relation cannot blow the response size.
_SAMPLE_DEFAULT_LIMIT = 5
_SAMPLE_MAX_LIMIT = 20
_SAMPLE_MAX_DAYS = 92  # a bounded interval; a wider range is a caller error (400)
_SAMPLE_CELL_BUDGET = 20_000  # total (rows x columns) cells across all days
_SAMPLE_MASK_SENTINEL = "[MASKED]"
# Deterministic ordering is applied over the mart's stable grain columns when they
# exist; any missing column is skipped so the ORDER BY never references an absent
# name. ``pull_id`` (ULID, lexicographically monotonic) is the final tie-breaker so
# two runs over an unchanged relation emit byte-identical "first N" rows.
_SAMPLE_ORDER_COLUMNS = (
    "date",
    "connector",
    "metric",
    "breakdown_dimension",
    "breakdown_value",
    "pull_id",
)
# The stage a request maps to when the requested stage has no distinct
# materialisation. All four collapse onto the mart in v1 (documented above).
_SAMPLE_SERVED_STAGE = "processed"


class SampleReadError(Exception):
    """The warehouse sample could not be read (bad input, backend down, no marts).

    Carries a ``code`` so the HTTP layer maps it to the right status:
      * ``invalid_range`` / ``invalid_stage`` -> 400
      * ``warehouse_unavailable``             -> 502
    Never leaks a raw provider/sensitive value in its message.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _daterange_days(date_from: str, date_to: str) -> list[str]:
    """Return the inclusive list of ISO day strings in ``[date_from, date_to]``.

    Raises ``SampleReadError('invalid_range', ...)`` on a malformed date, an
    inverted range, or a range wider than ``_SAMPLE_MAX_DAYS`` (bounded evidence).
    """
    try:
        d0 = date.fromisoformat(date_from)
        d1 = date.fromisoformat(date_to)
    except (TypeError, ValueError) as exc:
        raise SampleReadError("invalid_range", f"invalid date: {exc}") from exc
    if d1 < d0:
        raise SampleReadError("invalid_range", "date_to is before date_from")
    span = (d1 - d0).days + 1
    if span > _SAMPLE_MAX_DAYS:
        raise SampleReadError(
            "invalid_range",
            f"range spans {span} days; max is {_SAMPLE_MAX_DAYS}",
        )
    return [(d0 + timedelta(days=n)).isoformat() for n in range(span)]


def _pii_columns(col_names: list[str]) -> list[str]:
    """Return the subset of ``col_names`` that match the shared PII heuristic.

    Reuses ``schema_context_gen.PII_PATTERN`` (email/token/password/secret/phone/
    ssn/credit_card/card_number/api_key/private_key/dob/birth) so masking policy is
    defined ONCE across the codebase. Conservative default: name-based, applied to
    EVERY value of a matched column regardless of content.
    """
    from core.schema_context_gen import is_pii_column  # noqa: PLC0415

    return [c for c in col_names if is_pii_column(c)]


def _mask_cell(value: object, column: str, masked_set: frozenset[str]) -> object:
    """Return the response-safe value for one cell: sentinel for a masked column.

    The RAW value is only read here; when the column is masked the raw is discarded
    and never returned. Non-JSON-native values are stringified so the response is
    always JSON-serialisable (dates/decimals from the warehouse driver).
    """
    if column in masked_set:
        return _SAMPLE_MASK_SENTINEL
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def read_datastream_sample(
    *,
    project_id: str,
    connector: str,
    stage: str,
    date_from: str,
    date_to: str,
    limit: int = _SAMPLE_DEFAULT_LIMIT,
) -> dict:
    """Read a deterministic daily sample of a datastream's warehouse rows (12.19).

    For every day in ``[date_from, date_to]`` this returns the first ``limit``
    eligible mart rows for ``connector`` (the datastream's ``module_name``), scoped
    to ``project_id`` (AD-5), deterministically ordered, with PII columns masked
    server-side BEFORE the response is built.

    Args:
        project_id: owning project -- ALWAYS filtered (AD-5 isolation).
        connector:  the datastream's ``module_name`` == mart ``connector`` value.
        stage:      requested pipeline stage (collected|mapped|processed|published).
        date_from / date_to: inclusive ISO day bounds (bounded to _SAMPLE_MAX_DAYS).
        limit:      per-day row cap (default 5, hard-capped at _SAMPLE_MAX_LIMIT).

    Returns a dict:
        ``{"served_stage": str, "stage_note": str|None, "masked_fields": [...],
           "days": [{"date", "row_count", "rejection_count", "field_count",
                     "rows": [{col: value, ...}]}]}``

    Raises ``SampleReadError`` (never a raw exception) on bad input / backend down.
    The HTTP layer owns auth + project-scope enforcement; this reader assumes the
    ``project_id`` it is handed has already passed the AD-5 access check.
    """
    valid_stages = {"collected", "mapped", "processed", "published"}
    if stage not in valid_stages:
        raise SampleReadError(
            "invalid_stage",
            f"unknown stage {stage!r}; expected one of {sorted(valid_stages)}",
        )
    per_day_limit = max(1, min(int(limit or _SAMPLE_DEFAULT_LIMIT), _SAMPLE_MAX_LIMIT))
    days = _daterange_days(date_from, date_to)

    from core import warehouse  # noqa: PLC0415

    relation = "fact_daily_kpi"  # AD-12: marts only.
    mode = warehouse._db_mode()

    # Column discovery (once) so we know which columns to mask and can build the
    # deterministic ORDER BY over only-present grain columns.
    try:
        col_names = _sample_relation_columns(warehouse, mode, project_id, relation)
    except SampleReadError:
        raise
    except Exception as exc:  # noqa: BLE001 -- any discovery failure => honest 502
        raise SampleReadError(
            "warehouse_unavailable", f"{type(exc).__name__}: {exc}"
        ) from exc
    if not col_names:
        # Relation absent / marts not seeded -> honest empty evidence per day,
        # never a fabricated row (AD-9). served_stage still declared.
        note = _sample_stage_note(stage)
        return {
            "served_stage": _SAMPLE_SERVED_STAGE,
            "stage_note": note,
            "masked_fields": [],
            "days": [
                {"date": d, "row_count": 0, "rejection_count": 0,
                 "field_count": 0, "rows": []}
                for d in days
            ],
        }

    masked_set = frozenset(_pii_columns(col_names))
    field_count = len(col_names)
    order_cols = [c for c in _SAMPLE_ORDER_COLUMNS if c in col_names]

    # Cell-budget guard: shrink the per-day limit if a very wide relation x day span
    # would exceed the total cell budget (rows x columns). Evidence stays bounded.
    if field_count > 0:
        max_total_rows = max(1, _SAMPLE_CELL_BUDGET // field_count)
        max_rows_per_day = max(1, max_total_rows // max(1, len(days)))
        per_day_limit = min(per_day_limit, max_rows_per_day, _SAMPLE_MAX_LIMIT)

    day_payloads: list[dict] = []
    for day in days:
        try:
            rows = _sample_day_rows(
                warehouse, mode, project_id, connector, relation,
                day, order_cols, col_names, per_day_limit,
            )
        except Exception as exc:  # noqa: BLE001 -- one backend hiccup => honest 502
            raise SampleReadError(
                "warehouse_unavailable", f"{type(exc).__name__}: {exc}"
            ) from exc

        masked_rows: list[dict] = []
        for raw_row in rows:
            masked_rows.append(
                {col: _mask_cell(raw_row.get(col), col, masked_set) for col in col_names}
            )
        day_payloads.append(
            {
                "date": day,
                "row_count": len(masked_rows),
                # Rejection materialisation (managed-feed rejected rows) is a
                # SEPARATE Postgres surface; the warehouse mart carries no rejected
                # rows, so mart-stage evidence honestly reports 0 here. The HTTP
                # layer overlays the real per-day rejection_count for inbound feeds.
                "rejection_count": 0,
                "field_count": field_count,
                "rows": masked_rows,
            }
        )

    return {
        "served_stage": _SAMPLE_SERVED_STAGE,
        "stage_note": _sample_stage_note(stage),
        "masked_fields": sorted(masked_set),
        "days": day_payloads,
    }


def _sample_stage_note(requested_stage: str) -> str | None:
    """Return the honesty note when the requested stage has no distinct relation.

    All four stages collapse onto the consolidated mart (``processed``) in v1; when
    the caller asks for a different stage we say so explicitly instead of implying a
    distinct materialisation exists.
    """
    if requested_stage == _SAMPLE_SERVED_STAGE:
        return None
    return (
        f"Requested stage '{requested_stage}' has no distinct warehouse "
        f"materialisation; served the closest available stage "
        f"'{_SAMPLE_SERVED_STAGE}' (consolidated mart). Stage version provenance "
        f"is still reported from the datastream's Postgres pointers."
    )


def _sample_relation_columns(warehouse, mode: str, project_id: str, relation: str) -> list[str]:
    """Return the column names of the mart relation, or [] when it is absent.

    DuckDB reads a ``LIMIT 0`` from the project's mart schema; BigQuery reads the
    dataset INFORMATION_SCHEMA. Any "relation absent" signal returns [] (honest
    empty), distinct from a genuine backend failure which propagates.
    """
    from core.schema_context_gen import _quote_ident  # noqa: PLC0415

    if mode == "duckdb":
        import duckdb  # noqa: PLC0415

        path = warehouse._duckdb_path()
        if not path or not os.path.exists(path):
            return []
        prefix = warehouse._duckdb_mart_prefix(project_id)
        con = duckdb.connect(path, read_only=True)
        try:
            # prefix is a trusted naming-point value (e.g. "main_marts."); relation is
            # a module constant. Validate the bare relation ident defensively.
            _quote_ident(relation)
            try:
                rel = con.execute(f"SELECT * FROM {prefix}{relation} LIMIT 0")  # noqa: S608
            except Exception:  # noqa: BLE001 -- relation absent => honest empty
                return []
            return [d[0] for d in rel.description]
        finally:
            con.close()
    if mode == "bigquery":
        if not warehouse._check_bigquery_mart(project_id):
            return []
        from core import warehouse_tenancy  # noqa: PLC0415

        dataset = warehouse_tenancy.bigquery_marts_dataset(project_id)
        sql = (
            "SELECT column_name FROM "
            f"`{dataset}`.INFORMATION_SCHEMA.COLUMNS "
            "WHERE table_name = @p0 ORDER BY ordinal_position"
        )
        out = warehouse._query_bigquery(sql, [relation])
        return [r["column_name"] for r in out]
    raise SampleReadError("warehouse_unavailable", f"unknown TOOROW_DB_MODE: {mode!r}")


def _sample_day_rows(
    warehouse,
    mode: str,
    project_id: str,
    connector: str,
    relation: str,
    day: str,
    order_cols: list[str],
    col_names: list[str],
    limit: int,
) -> list[dict]:
    """Return up to ``limit`` deterministically-ordered rows for one day.

    project_id + connector + date are BOUND parameters (never interpolated). The
    ORDER BY uses only present grain columns (validated + quoted). ``limit`` is a
    server-validated int in [1, _SAMPLE_MAX_LIMIT] -- interpolated safely.
    """
    from core.schema_context_gen import _quote_ident  # noqa: PLC0415

    safe_limit = max(1, min(int(limit), _SAMPLE_MAX_LIMIT))
    order_by = ", ".join(_quote_ident(c) for c in order_cols) if order_cols else "1"

    if mode == "duckdb":
        prefix = warehouse._duckdb_mart_prefix(project_id)
        _quote_ident(relation)
        sql = (
            f"SELECT * FROM {prefix}{relation} "  # noqa: S608 -- relation is a constant
            "WHERE project_id = ? AND connector = ? AND date = ? "
            f"ORDER BY {order_by} LIMIT {safe_limit}"
        )
        return warehouse._query_duckdb(sql, [project_id, connector, day])
    if mode == "bigquery":
        from core import warehouse_tenancy  # noqa: PLC0415

        dataset = warehouse_tenancy.bigquery_marts_dataset(project_id)
        sql = (
            f"SELECT * FROM `{dataset}`.{relation} "  # noqa: S608 -- relation is a constant
            "WHERE project_id = @p0 AND connector = @p1 AND date = @p2 "
            f"ORDER BY {order_by} LIMIT {safe_limit}"
        )
        return warehouse._query_bigquery(sql, [project_id, connector, day])
    raise SampleReadError("warehouse_unavailable", f"unknown TOOROW_DB_MODE: {mode!r}")
